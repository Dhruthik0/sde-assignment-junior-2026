#!/usr/bin/env bash
# Full run against a fresh mock, kill -9'ing the dispatcher KILLS times at random points,
# then compare final_state.csv with the provider's /admin/leads.
#   usage: solution/chaos.sh [KILLS=3] [FAILURE_SCALE=1] [SEED=7]
set -euo pipefail
KILLS=${1:-3}; SCALE=${2:-1}; SEED=${3:-7}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
RUN=${RUN_DIR:-$ROOT/solution/run}
mkdir -p "$RUN"; rm -f "$RUN"/dispatcher.db* "$RUN"/final_state.csv "$RUN"/*.log

(cd "$ROOT/assignment-b-workflow" && exec python -m mock_telephony --failure-scale "$SCALE" --seed "$SEED") \
  > "$RUN/mock.log" 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null || true' EXIT
until curl -sf localhost:8099/v1/clock >/dev/null; do sleep 0.2; done

start() { (cd "$ROOT/solution" && exec python -m dispatcher --db "$RUN/dispatcher.db" --out "$RUN/final_state.csv") \
            >> "$RUN/dispatcher.log" 2>&1 & DISP=$!; }
start
for i in $(seq "$KILLS"); do
  sleep "$(python -c 'import random; print(round(random.uniform(5, 40), 1))')"
  if kill -0 $DISP 2>/dev/null; then kill -9 $DISP; echo "kill -9 #$i"; sleep 1; start; fi
done
wait $DISP

curl -s localhost:8099/admin/report | python -c '
import json, sys; r = json.load(sys.stdin); r.pop("violations"); print(json.dumps(r, indent=1))'
curl -s localhost:8099/admin/leads | python "$ROOT/solution/compare.py" "$RUN/final_state.csv"
