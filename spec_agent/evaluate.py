"""Run the 20 PM requests through the agent, check each spec, score it against the labels.

    python -m spec_agent.evaluate --mode replay     # CI, no key
    python -m spec_agent.evaluate --mode record     # real run, needs ANTHROPIC_API_KEY

For every request it writes report/specs/<id>.json (the spec, the checks, the proof)
and report/specs/<id>.md, the ticket a PM confirms before engineering starts.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .agent import ROOT, load_catalog, load_kpis, load_requests, propose
from .checks import Findings, check
from .llm import LLM, MissingRecording
from .pipeline import prove

REPORT = ROOT / "report"


@dataclass
class Score:
    request_id: str
    must_ask: bool
    asked: bool
    ask_ok: bool
    kpi_required: list[str] = field(default_factory=list)
    kpi_agent: list[str] = field(default_factory=list)
    kpi_checker_added: list[str] = field(default_factory=list)
    kpi_recall_agent: float | None = None
    kpi_recall_with_checker: float | None = None
    kpi_precision: float | None = None
    touch_ok: bool = True
    no_duplicate_ok: bool = True
    properties_ok: bool = True
    privacy_ok: bool = True
    checks_ok: bool = True
    pipeline_ok: bool = True
    ready: bool = False
    notes: list[str] = field(default_factory=list)


def score(rid: str, spec: dict, f: Findings, proof: dict, label: dict) -> Score:
    asked = bool(spec["clarifying_questions"]) and not spec["events"]
    must_ask = label["must_ask"]
    s = Score(rid, must_ask, asked, asked == must_ask or (asked and label.get("allow_ask", False)))
    if must_ask:
        s.ready = s.ask_ok
        if not s.ask_ok:
            s.notes.append("built a spec for a request that needed questions first")
        return s

    req = set(label["required_kpis"])
    allowed = req | set(label.get("allowed_kpis", []))
    agent = {l["kpi"] for l in spec["kpi_links"]}
    checker = set(f.kpis_from_config) - agent
    s.kpi_required, s.kpi_agent, s.kpi_checker_added = sorted(req), sorted(agent), sorted(checker)
    if req:
        s.kpi_recall_agent = len(req & agent) / len(req)
        s.kpi_recall_with_checker = len(req & (agent | checker)) / len(req)
    if agent:
        s.kpi_precision = len(agent & allowed) / len(agent)

    by_name = {e["name"]: e for e in spec["events"]}
    for ev in label.get("must_touch", []):
        if by_name.get(ev, {}).get("action") not in ("extend", "reuse"):
            s.touch_ok = False
            s.notes.append(f"should reuse or extend {ev}")
    if label.get("no_new_events") and any(e["action"] == "new" for e in spec["events"]):
        s.no_duplicate_ok = False
        s.notes.append("already tracked; no new event needed")
    if any("already in the catalog" in e for e in f.errors):
        s.no_duplicate_ok = False

    names = {p["name"] for e in spec["events"] for p in e["properties"]}
    names |= {p for e in spec["events"] if e["action"] != "new" for p in f.schemas.get(e["name"], {}).get("properties", {})}
    for options in label.get("required_properties", []):
        if not names & set(options):
            s.properties_ok = False
            s.notes.append(f"missing a property like {options[0]}")
    if names & set(label.get("forbidden_properties", [])):
        s.privacy_ok = False
        s.notes.append("captures personal data")
    s.checks_ok = f.ok
    s.pipeline_ok = all(v["ok"] for v in proof.values()) if proof else not spec["events"]
    s.ready = all([s.ask_ok, s.touch_ok, s.no_duplicate_ok, s.properties_ok, s.privacy_ok, s.checks_ok,
                   s.pipeline_ok, (s.kpi_recall_with_checker in (None, 1.0))])
    return s


def ticket(rid: str, request: str, spec: dict, f: Findings, proof: dict, kpis: dict) -> str:
    out = [f"# {rid}: event spec for review", "", "> " + request.split("\n\n", 2)[-1].strip().replace("\n", "\n> "), "",
           f"**Summary.** {spec['summary']}", ""]
    if spec["clarifying_questions"]:
        out += ["## Questions for the PM", *[f"- {q}" for q in spec["clarifying_questions"]], ""]
    if spec["events"]:
        out += ["## Events", "", "| Event | Change | Fires when |", "|---|---|---|"]
        out += [f"| `{e['name']}` | {e['action']} | {e['trigger']} |" for e in spec["events"]]
        for e in spec["events"]:
            if e["properties"]:
                out += ["", f"**{e['name']}**", "", "| Property | Type | Required | Allowed values |", "|---|---|---|---|"]
                out += [f"| `{p['name']}` | {p['type']} | {'yes' if p['required'] else 'no'} | "
                        f"{', '.join(p['allowed_values']) or 'any'} |" for p in e["properties"]]
        out.append("")
    links = {l["kpi"]: l["reason"] for l in spec["kpi_links"]}
    links.update({k: f"(added by the config check) {v}" for k, v in f.kpis_from_config.items() if k not in links})
    if links:
        out += ["## KPIs to confirm before engineering starts", ""]
        out += [f"- [ ] **{k}** (owner: {kpis.get(k, {}).get('owner', '?')}): {v}" for k, v in links.items()]
        out.append("")
    if f.errors or f.warnings:
        out += ["## Checks", *[f"- ❌ {e}" for e in f.errors], *[f"- ⚠️ {w}" for w in f.warnings], ""]
    if proof:
        out += ["## Pipeline proof", ""]
        out += [f"- `stg_{ev}`: {p['valid_samples']} valid samples pass; {p['tests_catch_broken']}/{p['broken_samples']} "
                f"broken samples caught{'' if p['ok'] else ' ❌'}" for ev, p in proof.items()]
    if spec["privacy_notes"]:
        out += ["", f"**Privacy.** {spec['privacy_notes']}"]
    return "\n".join(out) + "\n"


def summarise(scores: list[Score]) -> dict:
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None
    specs = [s for s in scores if not s.must_ask]
    return {
        "requests": len(scores),
        "ready_for_engineering": sum(s.ready for s in scores),
        "ask_correct": sum(s.ask_ok for s in scores),
        "vague_requests": sum(s.must_ask for s in scores),
        "kpi_recall_agent": mean(s.kpi_recall_agent for s in specs),
        "kpi_recall_with_checker": mean(s.kpi_recall_with_checker for s in specs),
        "kpi_precision": mean(s.kpi_precision for s in specs),
        "reused_catalog_events": sum(s.touch_ok and s.no_duplicate_ok for s in specs),
        "properties_ok": sum(s.properties_ok for s in specs),
        "privacy_ok": sum(s.privacy_ok for s in specs),
        "checks_ok": sum(s.checks_ok for s in specs),
        "pipeline_ok": sum(s.pipeline_ok for s in specs),
        "specs_scored": len(specs),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["replay", "record", "live"], default="replay")
    a = ap.parse_args()
    catalog, kpis, requests = load_catalog(), load_kpis(), load_requests()
    labels = yaml.safe_load((ROOT / "evals" / "labels.yml").read_text())
    llm = LLM(a.mode, ROOT / "recordings")
    (REPORT / "specs").mkdir(parents=True, exist_ok=True)
    scores = []
    for rid, text in requests.items():
        try:
            spec = propose(llm, rid, text, catalog, kpis)
        except MissingRecording:
            print(f"{rid}: no recording, skipped")
            continue
        f = check(spec, catalog, kpis)
        proof = prove(f.schemas) if f.ok and spec["events"] else {}
        s = score(rid, spec, f, proof, labels[rid])
        scores.append(s)
        (REPORT / "specs" / f"{rid}.json").write_text(json.dumps(
            {"spec": spec, "errors": f.errors, "warnings": f.warnings, "kpis_from_config": f.kpis_from_config,
             "proof": proof, "score": asdict(s)}, indent=2) + "\n")
        (REPORT / "specs" / f"{rid}.md").write_text(ticket(rid, text, spec, f, proof, kpis))
        print(f"{rid} ready={s.ready} {'; '.join(s.notes)}")
    if not scores:
        print("No recordings yet: run with --mode record (needs ANTHROPIC_API_KEY).")
        return
    summary = summarise(scores)
    summary["usage"] = {"model": llm.model, "effort": llm.effort, "mode": a.mode,
                        **asdict(llm.usage), "cost_usd": llm.usage.cost_usd(llm.model)}
    (REPORT / "scorecard.json").write_text(json.dumps({"summary": summary, "requests": [asdict(s) for s in scores]},
                                                      indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
