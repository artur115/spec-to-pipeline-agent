"""Deterministic checks on a proposed spec. These run before a human ever sees it.

Errors block the spec; warnings go on the ticket for the PM and engineer.
The KPI check reads the KPI configs directly: if the spec touches an event that a
KPI is built on, that KPI is flagged even if the agent forgot it.
"""
from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass, field

from jsonschema import Draft7Validator

NAME = re.compile(r"^[a-z]+(_[a-z0-9]+)+$")
PII = re.compile(r"(^|_)(email|phone|first_name|last_name|full_name|address|street)(_|$)")
JSON_TYPES = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean", "array": "array"}


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    kpis_from_config: dict[str, str] = field(default_factory=dict)   # kpi -> why
    schemas: dict[str, dict] = field(default_factory=dict)            # event -> merged JSON Schema

    @property
    def ok(self) -> bool:
        return not self.errors


def _prop_schema(p: dict) -> dict:
    s = {"type": JSON_TYPES[p["type"]], "description": p["description"]}
    if p["type"] == "array":
        s["items"] = {"type": "string"}
    if p["allowed_values"] and p["type"] == "array":
        s["items"]["enum"] = p["allowed_values"]   # allowed values apply to each element
    elif p["allowed_values"]:
        vals = p["allowed_values"]
        if p["type"] == "integer" and all(v.lstrip("-").isdigit() for v in vals):
            s["enum"] = [int(v) for v in vals]
        else:
            s["enum"] = vals
    return s


def check(spec: dict, catalog: dict, kpis: dict) -> Findings:
    f = Findings()
    names = list(catalog)
    if not spec["events"] and not spec["clarifying_questions"]:
        f.errors.append("no events and no questions: nothing to build or ask")

    for ev in spec["events"]:
        n, action = ev["name"], ev["action"]
        if action == "new":
            if n in catalog:
                f.errors.append(f"{n}: already in the catalog; reuse or extend it instead of re-creating it")
                continue
            if not NAME.match(n):
                f.errors.append(f"{n}: name must be snake_case object_action")
            close = difflib.get_close_matches(n, names, n=1, cutoff=0.8)
            if close:
                f.warnings.append(f"{n}: looks like existing event {close[0]}; confirm it is different")
            schema = {"$schema": "http://json-schema.org/draft-07/schema#", "title": n, "type": "object",
                      "properties": {}, "required": [], "additionalProperties": False}
        else:
            if n not in catalog:
                f.errors.append(f"{n}: marked '{action}' but there is no such catalog event")
                continue
            schema = copy.deepcopy(catalog[n])
            if action == "reuse":
                changed = []
                for p in ev["properties"]:
                    old = schema["properties"].get(p["name"])
                    known = {str(v) for v in (old or {}).get("enum", [])}
                    if old is None or (p["allowed_values"] and not set(p["allowed_values"]) <= known):
                        changed.append(p["name"])
                if changed:
                    f.warnings.append(f"{n}: marked reuse but changes {', '.join(changed)}; treated as extend")

        for p in ev["properties"]:
            pn = p["name"]
            if PII.search(pn):
                f.errors.append(f"{n}.{pn}: looks like personal data; don't capture it in analytics events")
            if not re.match(r"^[a-z][a-z0-9_]*$", pn):
                f.errors.append(f"{n}.{pn}: property names must be snake_case")
            new_prop = _prop_schema(p)
            old = schema["properties"].get(pn)
            if old:
                if old["type"] != new_prop["type"]:
                    f.errors.append(f"{n}.{pn}: changes type {old['type']} -> {new_prop['type']} (breaking)")
                    continue
                if "enum" in old and "enum" in new_prop:
                    removed = [v for v in old["enum"] if v not in new_prop["enum"]]
                    if removed:
                        f.warnings.append(f"{n}.{pn}: drops allowed values {removed}; existing data still has them")
                    new_prop["enum"] = list(dict.fromkeys(old["enum"] + new_prop["enum"]))
                schema["properties"][pn] = {**old, **new_prop}
            else:
                schema["properties"][pn] = new_prop
                if p["required"]:
                    if action == "new":
                        schema["required"].append(pn)
                    else:
                        f.warnings.append(f"{n}.{pn}: new required property on an existing event; "
                                          "older clients won't send it, so it is added as optional")
        try:
            Draft7Validator.check_schema(schema)
        except Exception as e:  # pragma: no cover - our own builder should never fail this
            f.errors.append(f"{n}: invalid JSON Schema: {e}")
        f.schemas[n] = schema

        if action in ("extend", "reuse"):
            for kpi, cfg in kpis.items():
                if n in cfg["events"]:
                    f.kpis_from_config.setdefault(kpi, f"built on {n}, which this spec {action}s")

    known_kpis = set(kpis)
    for link in spec["kpi_links"]:
        if link["kpi"] not in known_kpis:
            f.warnings.append(f"kpi_links: {link['kpi']} is not a configured KPI")
    return f
