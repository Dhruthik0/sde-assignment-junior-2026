# Assignment B: Durable Campaign Dispatcher

See [`../SUBMISSION.md`](../SUBMISSION.md) for design, decisions and results.

```
solution/
├── dispatcher/
│   ├── rules.py      # pure scheduling rules (windows, next attempt, priority, pick). No I/O.
│   ├── store.py      # SQLite lead state machine; state derived from provider facts
│   ├── service.py    # webhook receiver + scheduling loop + 504/lost-webhook reconciliation
│   └── __main__.py   # CLI entry point
├── tests/            # pytest: rules, store, service (fake provider), randomised properties
├── chaos.sh          # full run against the mock with random kill -9, then diff vs /admin/leads
├── crash_inject.sh   # full run crashing at exact commit/HTTP boundaries (crash_inject.py)
└── compare.py        # final_state.csv vs /admin/leads
```

Run (from repo root, venv active, mock running on :8099):

```bash
cd solution && python -m dispatcher            # writes solution/final_state.csv when every lead is final
curl -s localhost:8200/status                  # per-campaign progress while it runs
```
