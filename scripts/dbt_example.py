"""Build the reference spec into a real dbt project and run `dbt build` on sample events.

    python scripts/dbt_example.py examples/REQ-020.reference.json
"""
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spec_agent.agent import load_catalog, load_kpis  # noqa: E402
from spec_agent.checks import check  # noqa: E402
from spec_agent.pipeline import samples, write_dbt  # noqa: E402

spec = json.loads(Path(sys.argv[1]).read_text())
f = check(spec, load_catalog(), load_kpis())
assert f.ok, f.errors
out = ROOT / "build" / "example_dbt"
shutil.rmtree(out, ignore_errors=True)
write_dbt(f.schemas, out)
(out / "dbt_project.yml").write_text(
    "name: tracking_example\nversion: '1.0.0'\nconfig-version: 2\nprofile: tracking_example\n"
    "seeds:\n  tracking_example:\n    +schema: raw\n    raw_events:\n      +column_types: {properties: varchar}\n")
(out / "profiles.yml").write_text(
    "tracking_example:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: example.duckdb\n")
(out / "models" / "_sources.yml").write_text(
    "version: 2\nsources:\n  - name: tracking\n    schema: main_raw\n    tables:\n      - name: raw_events\n")
(out / "seeds").mkdir()
with open(out / "seeds" / "raw_events.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["event_id", "event_name", "occurred_at", "properties"])
    n = 0
    for event, schema in f.schemas.items():
        for row in samples(schema)[0]:
            n += 1
            w.writerow([f"e{n}", event, "2026-12-01 10:00:00", json.dumps(row)])
# The models read the seed through source(), so load it first.
subprocess.run(["dbt", "seed", "--profiles-dir", "."], cwd=out, check=True, capture_output=True)
r = subprocess.run(["dbt", "build", "--profiles-dir", "."], cwd=out, capture_output=True, text=True)
print("\n".join(l for l in r.stdout.splitlines() if "PASS" in l or "ERROR" in l or "FAIL" in l or "Done" in l))
sys.exit(r.returncode)
