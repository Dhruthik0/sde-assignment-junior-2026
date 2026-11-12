"""Mutation testing: break one rule at a time in a temp copy of solution/ and check that pytest
catches it. A surviving mutant means a rule the tests don't pin down.
    usage (repo root): python solution/mutants.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="mutants-"))
PY = sys.executable

MUTANTS = [
    ("R1 regulatory window ignored", "rules.py",
     'start=max(_hm(campaign["start"]), _hm(regulatory["start"]))', 'start=_hm(campaign["start"])'),
    ("R1 end becomes inclusive", "rules.py",
     "if candidate < datetime.combine(day, w.end, IST) - w.margin", "if candidate <= datetime.combine(day, w.end, IST) - w.margin"),
    ("R1 end margin dropped", "rules.py",
     "if candidate < datetime.combine(day, w.end, IST) - w.margin", "if candidate < datetime.combine(day, w.end, IST)"),
    ("R1 days of week ignored", "rules.py",
     "if DAYS[day.weekday()] not in w.days:", "if False:"),
    ("R1 is_open ignores time", "rules.py", "return nxt is not None and nxt <= now", "return nxt is not None"),
    ("R1 margin not scaled by speed", "rules.py",
     "return max(END_MARGIN, timedelta(seconds=REAL_LATENCY_BUDGET_S * speed))", "return END_MARGIN"),
    ("R2 limit off by one", "rules.py", "if active[c.tenant_id] >= limits[c.tenant_id]:", "if active[c.tenant_id] > limits[c.tenant_id]:"),
    ("R2 picked calls not counted", "rules.py", "        active[c.tenant_id] += 1\n", ""),
    ("R3 busy phone ignored", "rules.py", "if c.phone in busy or c.phone in dnd_phones:", "if c.phone in dnd_phones:"),
    ("R3 same phone twice in one pick", "rules.py", "        busy.add(c.phone)\n", ""),
    ("R8 DND phone ignored in pick", "rules.py", "if c.phone in busy or c.phone in dnd_phones:", "if c.phone in busy:"),
    ("R5 retry delay ignored", "rules.py",
     'minutes = retry_policy.get(f"{last[\'status\']}_minutes", 0)', "minutes = 0"),
    ("R6 callback treated as retry", "rules.py",
     'if last["disposition"] == "callback_requested":', "if False:"),
    ("R6 retries before callbacks", "rules.py",
     'KIND_RANK = {"callback": 0, "retry": 1, "fresh": 2}', 'KIND_RANK = {"callback": 1, "retry": 0, "fresh": 2}'),
    ("R6 fresh before retries", "rules.py",
     'KIND_RANK = {"callback": 0, "retry": 1, "fresh": 2}', 'KIND_RANK = {"callback": 0, "retry": 2, "fresh": 1}'),
    ("R6 not-yet-due lead dialled", "rules.py",
     "if c.ready_at is not None and c.ready_at > now:", "if False:"),
    ("R9 exhausted/dnd precedence swapped", "store.py",
     '''        elif len(calls) >= self.campaigns[cid]["max_attempts"]:
            state = "exhausted"
        elif self.db.execute("SELECT 1 FROM dnd_phones WHERE phone=?", (lead["phone"],)).fetchone():
            state = "dnd_blocked"''',
     '''        elif self.db.execute("SELECT 1 FROM dnd_phones WHERE phone=?", (lead["phone"],)).fetchone():
            state = "dnd_blocked"
        elif len(calls) >= self.campaigns[cid]["max_attempts"]:
            state = "exhausted"'''),
    ("R7 terminal disposition ignored", "store.py",
     'elif any(c["disposition"] in TERMINAL for c in calls):', "elif False:"),
    ("R8 other leads on phone not refreshed", "store.py",
     '''                    for r in self.db.execute("SELECT campaign_id, lead_id FROM leads WHERE phone=?",''',
     '''                    for r in self.db.execute("SELECT campaign_id, lead_id FROM leads WHERE 0 AND phone=?",'''),
    ("R4 max_attempts off by one", "store.py",
     'elif len(calls) >= self.campaigns[cid]["max_attempts"]:', 'elif len(calls) > self.campaigns[cid]["max_attempts"]:'),
    ("idempotency key not deterministic", "store.py",
     'rid = f"{campaign_id}:{lead_id}:{n + 1}"', 'import uuid; rid = f"{campaign_id}:{lead_id}:{uuid.uuid4().hex}"'),
    ("dialing not counted as busy", "store.py",
     "WHERE state IN ('dialing','active')", "WHERE state IN ('active')"),
    # Expected survivor: the provider's duplicate deliveries are byte-identical, so overwriting an
    # ended call with the same result changes nothing observable.
    ("duplicate webhook overwrites result", "store.py",
     "WHERE call_sid=? AND ended_at IS NULL", "WHERE call_sid=?"),
    ("last_status from first call", "store.py", "last = ended[-1] if ended else None", "last = ended[0] if ended else None"),
    ("CSV not sorted", "store.py", "SELECT * FROM leads ORDER BY campaign_id, lead_id", "SELECT * FROM leads ORDER BY lead_id DESC"),
    ("504 treated as not placed", "service.py",
     '''            log.warning("dial %s: 504, outcome unknown", rid)
            await self.resolve_request(rid)''',
     '''            self.store.abort_dial(rid)'''),
    ("503 treated as placed/unknown kept slot forever", "service.py",
     "            self.store.abort_dial(rid)  # not placed", "            pass  # not placed"),
    ("signature not checked", "service.py",
     "return signature is not None and hmac.compare_digest(expected, signature)", "return True"),
    ("timeout treated as not placed", "service.py",
     "            return await self.resolve_request(rid)", "            return self.store.abort_dial(rid)"),
    ("no window re-check before send", "service.py",
     "if not is_open(self.windows[c.campaign_id], self.clock.now()):", "if False:"),
    ("reject re-sent every tick", "service.py",
     "            self.hold[(c.campaign_id, c.lead_id)] = self.clock.now() + REJECT_BACKOFF\n", ""),
    ("lookup 404 keeps lead dialing", "service.py",
     "        elif r.status_code == 404:\n            self.store.abort_dial(rid)", "        elif r.status_code == 404:\n            pass"),
]

killed, survived = 0, []
for name, fname, old, new in MUTANTS:
    if WORK.exists():
        shutil.rmtree(WORK)
    shutil.copytree(SRC / "solution", WORK / "solution", ignore=shutil.ignore_patterns("run*", "__pycache__", "*.db*"))
    shutil.copy(SRC / "pytest.ini", WORK / "pytest.ini")
    f = WORK / "solution/dispatcher" / fname
    text = f.read_text()
    assert text.count(old) == 1, f"mutant '{name}': pattern found {text.count(old)} times"
    f.write_text(text.replace(old, new))
    r = subprocess.run([PY, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"], cwd=WORK,
                       capture_output=True, text=True)
    if r.returncode == 0:
        survived.append(name)
        print(f"SURVIVED  {name}")
    else:
        killed += 1
        fail = next((l for l in r.stdout.splitlines() if l.startswith("FAILED")), r.stdout.splitlines()[-1])
        print(f"killed    {name:45s} {fail[:110]}")
shutil.rmtree(WORK)
print(f"\n{killed}/{len(MUTANTS)} mutants killed; survived: {survived}")
