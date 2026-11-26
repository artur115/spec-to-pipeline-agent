import copy
import json
from pathlib import Path

import pytest
import yaml

from spec_agent.agent import SCHEMA, load_catalog, load_kpis, load_requests, propose
from spec_agent.checks import check
from spec_agent.evaluate import score, summarise, ticket
from spec_agent.llm import LLM, MissingRecording
from spec_agent.pipeline import model_sql, prove, samples

ROOT = Path(__file__).resolve().parent.parent
CATALOG, KPIS = load_catalog(), load_kpis()
LABELS = yaml.safe_load((ROOT / "evals" / "labels.yml").read_text())
REFERENCE = json.loads((ROOT / "examples" / "REQ-020.reference.json").read_text())


def prop(name, type_="string", required=False, values=()):
    return {"name": name, "type": type_, "required": required, "description": "", "allowed_values": list(values)}


def spec(*events, kpis=(), questions=()):
    return {"summary": "", "clarifying_questions": list(questions), "events": list(events),
            "kpi_links": [{"kpi": k, "reason": ""} for k in kpis], "privacy_notes": ""}


def event(name, action="new", *props):
    return {"name": name, "action": action, "trigger": "", "description": "", "properties": list(props)}


# --- customer assets ----------------------------------------------------------

def test_every_request_has_a_label_and_kpis_reference_real_events():
    assert set(load_requests()) == set(LABELS)
    for cfg in KPIS.values():
        assert set(cfg["events"]) <= set(CATALOG)
    for lab in LABELS.values():
        assert set(lab["required_kpis"]) | set(lab.get("allowed_kpis", [])) <= set(KPIS)


# --- deterministic checks ---------------------------------------------------------

def test_recreating_a_catalog_event_is_an_error():
    f = check(spec(event("order_completed", "new", prop("order_id"))), CATALOG, KPIS)
    assert any("already in the catalog" in e for e in f.errors)


def test_personal_data_is_blocked():
    f = check(spec(event("back_in_stock_subscribed", "new", prop("email"))), CATALOG, KPIS)
    assert any("personal data" in e for e in f.errors)


def test_changing_a_property_type_is_breaking():
    f = check(spec(event("order_completed", "extend", prop("revenue", "string"))), CATALOG, KPIS)
    assert any("breaking" in e for e in f.errors)


def test_new_required_property_on_existing_event_becomes_optional():
    f = check(spec(event("return_requested", "extend", prop("reason", required=True, values=["damaged"]))),
              CATALOG, KPIS)
    assert f.ok and "reason" not in f.schemas["return_requested"]["required"]
    assert any("added as optional" in w for w in f.warnings)


def test_extending_an_enum_keeps_old_values():
    f = check(spec(event("order_completed", "extend", prop("fulfillment_method", values=["curbside"]))),
              CATALOG, KPIS)
    assert f.schemas["order_completed"]["properties"]["fulfillment_method"]["enum"] == ["ship", "pickup", "curbside"]


def test_config_check_flags_kpis_the_agent_missed():
    f = check(spec(event("order_completed", "extend", prop("fulfillment_method", values=["curbside"]))),
              CATALOG, KPIS)
    assert {"checkout_conversion", "return_rate", "store_pickup_adoption"} <= set(f.kpis_from_config)


def test_bad_event_name_is_an_error():
    f = check(spec(event("WishlistAdd", "new", prop("product_id"))), CATALOG, KPIS)
    assert any("snake_case" in e for e in f.errors)


def test_empty_spec_without_questions_is_an_error():
    assert not check(spec(), CATALOG, KPIS).ok


# --- pipeline -------------------------------------------------------------------

def test_reference_spec_passes_checks_and_pipeline_proof():
    f = check(REFERENCE, CATALOG, KPIS)
    assert f.ok and set(f.schemas) == {"checkout_step_viewed", "checkout_started"}
    assert all(v["ok"] for v in prove(f.schemas).values())


def test_broken_samples_cover_required_and_enums():
    schema = check(REFERENCE, CATALOG, KPIS).schemas["checkout_step_viewed"]
    _, broken = samples(schema)
    reasons = {r for r, _ in broken}
    assert "missing required step" in reasons and "step outside allowed values" in reasons


def test_generated_sql_uses_a_dbt_source():
    sql = model_sql("guide_viewed", CATALOG["guide_viewed"])
    assert "{{ source('tracking', 'raw_events') }}" in sql and "WHERE event_name = 'guide_viewed'" in sql


# --- scoring ----------------------------------------------------------------------

def test_vague_request_scores_only_on_asking():
    asked = spec(questions=["What should 'like' mean: rating, time on page, or shares?"])
    s = score("REQ-005", asked, check(asked, CATALOG, KPIS), {}, LABELS["REQ-005"])
    assert s.asked and s.ready
    built = spec(event("guide_liked", "new", prop("guide_id", required=True)))
    s2 = score("REQ-005", built, check(built, CATALOG, KPIS), {}, LABELS["REQ-005"])
    assert not s2.ready


def test_checker_rescues_a_missed_kpi_link():
    sp = spec(event("order_completed", "extend", prop("fulfillment_method", values=["curbside"])))
    f = check(sp, CATALOG, KPIS)
    s = score("REQ-004", sp, f, prove(f.schemas), LABELS["REQ-004"])
    assert s.kpi_recall_agent == 0.0 and s.kpi_recall_with_checker == 1.0 and s.ready


def test_reference_spec_scores_ready_and_ticket_lists_kpis():
    f = check(REFERENCE, CATALOG, KPIS)
    s = score("REQ-020", REFERENCE, f, prove(f.schemas), LABELS["REQ-020"])
    assert s.ready
    md = ticket("REQ-020", load_requests()["REQ-020"], REFERENCE, f, prove(f.schemas), KPIS)
    assert "- [ ] **checkout_conversion**" in md and "stg_checkout_step_viewed" in md
    assert summarise([s])["ready_for_engineering"] == 1


# --- agent call ---------------------------------------------------------------------

def test_agent_replays_a_recorded_spec(tmp_path, monkeypatch):
    out = copy.deepcopy(REFERENCE)
    out.pop("_note")
    monkeypatch.setattr(LLM, "_call", lambda self, s, m, sc: (out, {"input_tokens": 3, "output_tokens": 2}))
    text = load_requests()["REQ-020"]
    assert propose(LLM("record", tmp_path), "REQ-020", text, CATALOG, KPIS) == out
    monkeypatch.setattr(LLM, "_call", lambda *a: pytest.fail("replay must not call the API"))
    assert propose(LLM("replay", tmp_path), "REQ-020", text, CATALOG, KPIS) == out
    with pytest.raises(MissingRecording):
        propose(LLM("replay", tmp_path), "REQ-019", load_requests()["REQ-019"], CATALOG, KPIS)


def test_output_schema_is_strict_everywhere():
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    walk(SCHEMA)
