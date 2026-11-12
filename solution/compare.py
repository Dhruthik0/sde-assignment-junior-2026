"""Diff our final_state.csv against the provider's /admin/leads (JSON on stdin)."""
import csv
import json
import sys

expected = {(r["campaign_id"], r["lead_id"]): r for r in json.load(sys.stdin)}
with open(sys.argv[1]) as f:
    ours = {(r["campaign_id"], r["lead_id"]): r for r in csv.DictReader(f)}

diffs = []
for key, e in sorted(expected.items()):
    o = ours.get(key)
    want = (e["expected_state"], str(e["attempts"]), e["last_status"], e["last_disposition"])
    got = (o["final_state"], o["attempts"], o["last_status"], o["last_disposition"]) if o else None
    if want != got:
        diffs.append(f"{key}: provider={want} ours={got}")
print(f"{len(expected)} leads, {len(diffs)} mismatches")
print("\n".join(diffs[:20]))
sys.exit(1 if diffs else 0)
