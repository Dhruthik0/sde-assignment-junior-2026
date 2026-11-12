#!/usr/bin/env bash
# Full run with crashes injected at exact commit/HTTP boundaries, restarting until every lead is
# final, then compare final_state.csv with the provider's /admin/leads.
#   usage: solution/crash_inject.sh [PROB=0.03] [SEED=8] [FAILURE_SCALE=1]
set -uo pipefail
PROB=${1:-0.03}; SEED=${2:-8}; SCALE=${3:-1}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
RUN=${RUN_DIR:-$ROOT/solution/run_crash}
rm -rf "$RUN"; mkdir -p "$RUN"

(cd "$ROOT/assignment-b-workflow" && exec python -m mock_telephony --seed "$SEED" --failure-scale "$SCALE") \
  > "$RUN/mock.log" 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null || true' EXIT
until curl -sf localhost:8099/v1/clock >/dev/null; do sleep 0.2; done

starts=0
until [ -f "$RUN/final_state.csv" ] || [ $starts -ge 500 ]; do
  starts=$((starts + 1))
  python "$ROOT/solution/crash_inject.py" "$RUN/dispatcher.db" "$RUN/final_state.csv" "$PROB" >> "$RUN/dispatcher.log" 2>&1
done
echo "starts=$starts crashes=$(wc -l < "$RUN/crashes.log" 2>/dev/null || echo 0)"
sort "$RUN/crashes.log" 2>/dev/null | uniq -c
curl -s localhost:8099/admin/report | python -c '
import json, sys; r = json.load(sys.stdin)
print("violation_count", r["violation_count"], r["violations_by_type"], "max_concurrency", r["max_concurrency_seen"])'
curl -s localhost:8099/admin/leads | python "$ROOT/solution/compare.py" "$RUN/final_state.csv"
