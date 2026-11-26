"""Spec -> dbt staging models + tests, then prove them on sample events.

For every event in an accepted spec this writes
  models/staging/stg_<event>.sql   typed columns pulled out of the JSON payload
  models/staging/_<event>.yml      not_null for required properties, accepted_values for enums

Then it generates sample payloads from the JSON Schema: valid ones, and broken
ones (a required property missing, a value outside the allowed list). The check
passes only if every valid sample survives the model's tests and every broken
sample is caught, both by the JSON Schema and by the dbt-style tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import yaml
from jsonschema import Draft7Validator

CAST = {"string": "VARCHAR", "number": "DOUBLE", "integer": "BIGINT", "boolean": "BOOLEAN", "array": "JSON"}


def model_sql(event: str, schema: dict) -> str:
    cols = []
    for name, p in schema["properties"].items():
        if p["type"] == "array":
            cols.append(f"    json_extract(properties, '$.{name}') AS {name}")
        else:
            cols.append(f"    TRY_CAST(json_extract_string(properties, '$.{name}') AS {CAST[p['type']]}) AS {name}")
    body = ",\n".join(["    event_id", "    occurred_at"] + cols)
    return (f"-- Generated from the {event} spec. Do not edit by hand; change the spec.\n"
            f"SELECT\n{body}\nFROM {{{{ source('tracking', 'raw_events') }}}}\nWHERE event_name = '{event}'\n")


def model_yml(event: str, schema: dict) -> dict:
    columns = [{"name": "event_id", "data_tests": ["not_null", "unique"]}]
    for name, p in schema["properties"].items():
        tests = []
        if name in schema.get("required", []):
            tests.append("not_null")
        if "enum" in p:
            tests.append({"accepted_values": {"values": p["enum"],
                                              **({"quote": False} if p["type"] == "integer" else {})}})
        col = {"name": name, "description": p.get("description", "")}
        if tests:
            col["data_tests"] = tests
        columns.append(col)
    return {"version": 2, "models": [{"name": f"stg_{event}", "description": schema.get("title", event),
                                      "columns": columns}]}


def write_dbt(schemas: dict[str, dict], out: Path) -> list[Path]:
    staging = out / "models" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    written = []
    for event, schema in schemas.items():
        sql, yml = staging / f"stg_{event}.sql", staging / f"_{event}.yml"
        sql.write_text(model_sql(event, schema))
        yml.write_text(yaml.safe_dump(model_yml(event, schema), sort_keys=False))
        written += [sql, yml]
    return written


# --- sample events ---------------------------------------------------------

def _value(name: str, p: dict, i: int):
    if p["type"] == "array" and "enum" in p.get("items", {}):
        vals = p["items"]["enum"]
        return [vals[i % len(vals)], vals[(i + 1) % len(vals)]]
    if "enum" in p:
        return p["enum"][i % len(p["enum"])]
    return {"string": f"{name}_{i}", "number": 10.5 + i, "integer": i + 1, "boolean": i % 2 == 0,
            "array": [f"{name}_{i}a", f"{name}_{i}b"]}[p["type"]]


def samples(schema: dict, n: int = 6) -> tuple[list[dict], list[tuple[str, dict]]]:
    props, req = schema["properties"], schema.get("required", [])
    valid = []
    for i in range(n):
        keep = [k for k in props if k in req or i % 2 == 0]   # optional properties on half the rows
        valid.append({k: _value(k, props[k], i) for k in keep})
    broken = []
    for k in req:
        row = {x: _value(x, props[x], 0) for x in props}
        row.pop(k)
        broken.append((f"missing required {k}", row))
    for k, p in props.items():
        if p["type"] == "array" and "enum" in p.get("items", {}):
            row = {x: _value(x, props[x], 0) for x in props}
            row[k] = ["not_an_allowed_value"]
            broken.append((f"{k} element outside allowed values", row))
        elif "enum" in p:
            row = {x: _value(x, props[x], 0) for x in props}
            row[k] = 999999 if p["type"] == "integer" else "not_an_allowed_value"
            broken.append((f"{k} outside allowed values", row))
    return valid, broken


def _failures(con, event: str, schema: dict) -> int:
    """Run the generated tests as SQL; return how many rows fail any of them."""
    conds = []
    for name, p in schema["properties"].items():
        if name in schema.get("required", []):
            conds.append(f"{name} IS NULL")
        if p["type"] == "array" and "enum" in p.get("items", {}):
            vals = ", ".join("'" + str(v).replace("'", "''") + "'" for v in p["items"]["enum"])
            conds.append(f"({name} IS NOT NULL AND EXISTS (SELECT 1 FROM unnest(CAST({name} AS VARCHAR[])) t(v)"
                         f" WHERE v NOT IN ({vals})))")
        elif "enum" in p:
            vals = ", ".join(str(v) if p["type"] == "integer" else "'" + str(v).replace("'", "''") + "'"
                             for v in p["enum"])
            conds.append(f"({name} IS NOT NULL AND {name} NOT IN ({vals}))")
    if not conds:
        return 0
    return con.execute(f"SELECT COUNT(*) FROM stg_{event} WHERE {' OR '.join(conds)}").fetchone()[0]


def prove(schemas: dict[str, dict]) -> dict:
    """Valid samples pass, broken samples are caught. Returns counts per event."""
    report = {}
    for event, schema in schemas.items():
        validator = Draft7Validator(schema)
        valid, broken = samples(schema)
        schema_ok = all(validator.is_valid(v) for v in valid)
        schema_catches = sum(not validator.is_valid(b) for _, b in broken)

        con = duckdb.connect(":memory:")
        con.execute("CREATE TABLE raw_events (event_id VARCHAR, event_name VARCHAR, occurred_at TIMESTAMP, properties VARCHAR)")

        def load(rows):
            con.execute("DELETE FROM raw_events")
            con.executemany("INSERT INTO raw_events VALUES (?, ?, TIMESTAMP '2026-12-01 10:00:00', ?)",
                            [(f"e{i}", event, json.dumps(r)) for i, r in enumerate(rows)])
            sql = model_sql(event, schema).replace("{{ source('tracking', 'raw_events') }}", "raw_events")
            con.execute(f"CREATE OR REPLACE VIEW stg_{event} AS {sql}")

        load(valid)
        tests_pass_valid = _failures(con, event, schema) == 0
        tests_catch = 0
        for _, b in broken:
            load([b])
            tests_catch += _failures(con, event, schema) > 0
        con.close()
        report[event] = {"valid_samples": len(valid), "broken_samples": len(broken),
                         "schema_accepts_valid": schema_ok, "schema_catches_broken": schema_catches,
                         "tests_pass_valid": tests_pass_valid, "tests_catch_broken": tests_catch,
                         "ok": schema_ok and tests_pass_valid and schema_catches == len(broken)
                         and tests_catch == len(broken)}
    return report
