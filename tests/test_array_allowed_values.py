"""Allowed values on an array property apply to each element (REQ-006 crashed the proof before)."""
from spec_agent.checks import _prop_schema
from spec_agent.pipeline import prove


def test_array_allowed_values_are_checked_per_element():
    prop = _prop_schema({"name": "filters_active", "type": "array", "description": "d",
                         "allowed_values": ["category", "price_range"]})
    assert prop["items"]["enum"] == ["category", "price_range"] and "enum" not in prop
    schema = {"title": "e", "type": "object", "additionalProperties": False, "required": ["id"],
              "properties": {"id": {"type": "string"}, "filters_active": prop}}
    r = prove({"e": schema})["e"]
    assert r["ok"] and r["tests_catch_broken"] == r["broken_samples"] == 2
