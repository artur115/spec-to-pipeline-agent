"""PM request -> proposed event spec. One structured call per request."""
from __future__ import annotations

from pathlib import Path

import yaml

from .llm import LLM

ROOT = Path(__file__).resolve().parent.parent
CUSTOMER = ROOT / "customer"


def load_catalog() -> dict:
    return yaml.safe_load((CUSTOMER / "event_catalog.yml").read_text())["events"]


def load_kpis() -> dict:
    return {p.stem: yaml.safe_load(p.read_text()) for p in sorted((CUSTOMER / "kpi_config").glob("*.yml"))}


def load_requests() -> dict:
    return {p.stem: p.read_text() for p in sorted((CUSTOMER / "requests").glob("*.md"))}


SYSTEM = """You turn product managers' tracking requests into event specs for Harbor & Pine's data team.

You get the request, the current event catalog (JSON Schema per event) and the KPI configs.
Return a spec that an engineer could build from, or questions for the PM if you can't.

Rules
- Reuse before you add. If a catalog event already covers the action, use it:
  action "reuse" when no change is needed, "extend" when it needs a new property or a new
  allowed value. Use "new" only for an action the catalog doesn't have.
- New event names are snake_case object_action in the past tense (wishlist_item_added).
- For "extend", list only the properties you add or change. For "reuse", list nothing new.
- Never change the type of an existing property. A property you add to an existing event
  should be optional, because older app versions won't send it.
- Do not capture personal data (email, phone, name, address). Say how to avoid it instead.
- kpi_links: every existing KPI whose numbers could change because of this spec, with why.
  The PM confirms these before engineering starts.
- If the request is too vague to spec (no clear user action or question), return no events
  and ask 1-3 specific questions instead.
"""

PROPERTY = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string", "enum": ["string", "number", "integer", "boolean", "array"]},
        "required": {"type": "boolean"},
        "description": {"type": "string"},
        "allowed_values": {"type": "array", "items": {"type": "string"},
                           "description": "Empty when any value is allowed."},
    },
    "required": ["name", "type", "required", "description", "allowed_values"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "clarifying_questions": {"type": "array", "items": {"type": "string"}},
        "events": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "action": {"type": "string", "enum": ["new", "extend", "reuse"]},
                "trigger": {"type": "string", "description": "Exactly when it fires."},
                "description": {"type": "string"},
                "properties": {"type": "array", "items": PROPERTY},
            },
            "required": ["name", "action", "trigger", "description", "properties"],
            "additionalProperties": False,
        }},
        "kpi_links": {"type": "array", "items": {
            "type": "object",
            "properties": {"kpi": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["kpi", "reason"], "additionalProperties": False,
        }},
        "privacy_notes": {"type": "string"},
    },
    "required": ["summary", "clarifying_questions", "events", "kpi_links", "privacy_notes"],
    "additionalProperties": False,
}


def prompt(request: str, catalog: dict, kpis: dict) -> str:
    return ("## Request\n" + request +
            "\n## Event catalog (JSON Schema draft-07)\n```yaml\n" + yaml.safe_dump(catalog, sort_keys=False) +
            "```\n## KPI configs\n```yaml\n" + yaml.safe_dump(kpis, sort_keys=False) + "```\n")


def propose(llm: LLM, request_id: str, request: str, catalog: dict, kpis: dict) -> dict:
    return llm.complete_json(SYSTEM, [{"role": "user", "content": prompt(request, catalog, kpis)}],
                             SCHEMA, tag=request_id)
