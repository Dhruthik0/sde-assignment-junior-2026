"""Randomised property tests (fixed seeds, so failures reproduce).

- `next_open` / `is_open` agree with a brute-force minute-by-minute scan.
- `pick` never breaks R2/R3/R8 and never skips a higher-priority lead it could have dialled.
- The store reaches the same final state whatever order (and how many times) facts arrive.
"""
import random
from datetime import datetime, timedelta

import pytest

from dispatcher.rules import DAYS, IST, Candidate, Window, is_open, next_open, pick, priority
from dispatcher.store import Store

REG = {"start": "09:00", "end": "21:00"}


def random_window(r: random.Random) -> Window:
    s = r.randrange(6 * 60, 20 * 60, 30)
    e = r.randrange(s + 60, 23 * 60 + 1, 30)
    days = r.sample(DAYS, r.randint(1, 7))
    return Window.build({"start": f"{s // 60:02d}:{s % 60:02d}", "end": f"{e // 60:02d}:{e % 60:02d}",
                         "days": days}, REG)


def brute_open(w: Window, t: datetime) -> bool:
    lt = t.astimezone(IST)
    if DAYS[lt.weekday()] not in w.days:
        return False
    start = lt.replace(hour=w.start.hour, minute=w.start.minute, second=0, microsecond=0)
    end = lt.replace(hour=w.end.hour, minute=w.end.minute, second=0, microsecond=0) - timedelta(seconds=20)
    return start <= lt < end


@pytest.mark.parametrize("seed", range(300))
def test_next_open_matches_brute_force(seed):
    r = random.Random(seed)
    w = random_window(r)
    t = datetime(2026, 11, 9, tzinfo=IST) + timedelta(seconds=r.randrange(7 * 86400))
    got = next_open(w, t)
    if got is None:  # claims it never opens: check a full week, every 5 min (windows are >= 30 min)
        assert not any(brute_open(w, t + timedelta(minutes=5 * i)) for i in range(7 * 24 * 12))
        assert not is_open(w, t)
        return
    assert got >= t and brute_open(w, got)
    assert is_open(w, t) == brute_open(w, t)
    # nothing open strictly between t and got (scan every 10 s; windows are on 30-min marks)
    probe = t
    while probe < got:
        assert not brute_open(w, probe), (w, t, got, probe)
        probe += timedelta(seconds=10)


@pytest.mark.parametrize("seed", range(300))
def test_pick_invariants(seed):
    r = random.Random(seed)
    now = datetime(2026, 11, 13, 12, 0, tzinfo=IST)  # Friday noon
    windows = {"c1": Window.build({"start": "10:00", "end": "19:00", "days": ["fri"]}, REG),
               "c2": Window.build({"start": "09:30", "end": "11:00", "days": ["fri"]}, REG),  # closed
               "c3": Window.build({"start": "11:00", "end": "20:00", "days": DAYS}, REG)}
    tenants = {"c1": "t1", "c2": "t1", "c3": "t2"}
    limits = {"t1": r.randint(0, 4), "t2": r.randint(0, 3)}
    phones = [f"+9190000000{i:02d}" for i in range(r.randint(3, 15))]
    pending = []
    for i in range(r.randint(0, 25)):
        cid = r.choice(list(windows))
        kind = r.choice(["fresh", "retry", "callback"])
        ready = None if kind == "fresh" else now + timedelta(minutes=r.randint(-120, 60))
        pending.append(Candidate(cid, f"l{i}", tenants[cid], r.choice(phones), kind, ready))
    busy = set(r.sample(phones, r.randint(0, 2)))
    dnd = set(r.sample(phones, r.randint(0, 2)))
    active = {t: r.randint(0, limits[t]) for t in limits}

    out = pick(now, pending, windows, busy, dnd, active, limits)

    out_phones = [c.phone for c in out]
    assert len(out_phones) == len(set(out_phones))                         # R3 within the pick
    assert not set(out_phones) & (busy | dnd)                              # R3 vs active, R8
    for t in limits:                                                       # R2
        assert active[t] + sum(c.tenant_id == t for c in out) <= limits[t]
    for c in out:                                                          # due + window (R1, R5, R6)
        assert c.ready_at is None or c.ready_at <= now
        assert is_open(windows[c.campaign_id], now)
    assert out == sorted(out, key=priority)
    # Greedy optimality: any eligible lead left out was blocked by something already chosen.
    chosen = {(c.campaign_id, c.lead_id) for c in out}
    for c in pending:
        if (c.campaign_id, c.lead_id) in chosen:
            continue
        eligible = ((c.ready_at is None or c.ready_at <= now) and is_open(windows[c.campaign_id], now)
                    and c.phone not in busy | dnd)
        if not eligible:
            continue
        ahead = [o for o in out if priority(o) < priority(c)]
        assert (c.phone in {o.phone for o in ahead}
                or active[c.tenant_id] + sum(o.tenant_id == c.tenant_id for o in ahead) >= limits[c.tenant_id])


# ------------------------------------------------------------------ store: order independence
CFG = {
    "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
    "campaigns": [{"campaign_id": "ca", "tenant_id": "t", "leads_file": "a.csv", "max_attempts": 3},
                  {"campaign_id": "cb", "tenant_id": "t", "leads_file": "b.csv", "max_attempts": 3}],
}
OUTCOMES = [("no_answer", None), ("busy", None), ("failed", None), ("completed", "interested"),
            ("completed", "not_interested"), ("completed", "callback_requested"),
            ("completed", "wrong_number"), ("completed", "dnd")]
T0 = datetime(2026, 11, 12, 10, 0, tzinfo=IST)


def history(r: random.Random):
    """A plausible sequence of facts for leads ca/a1, ca/a2, cb/b1 (b1 shares a1's phone)."""
    facts = []
    t = T0
    for cid, lid, phone in [("ca", "a1", "+919000000001"), ("ca", "a2", "+919000000002"),
                            ("cb", "b1", "+919000000001")]:
        for n in range(1, r.randint(1, 3) + 1):
            t += timedelta(minutes=r.randint(1, 90))
            sid, rid = f"CA_{lid}_{n}", f"{cid}:{lid}:{n}"
            status, disp = r.choice(OUTCOMES)
            accepted = {"call_sid": sid, "request_id": rid, "campaign_id": cid, "lead_id": lid,
                        "state": "active", "created_at": t.isoformat()}
            ended = {"event": "call.completed", "call_sid": sid, "request_id": rid, "campaign_id": cid,
                     "lead_id": lid, "phone": phone, "status": status, "disposition": disp,
                     "callback_at": (t + timedelta(hours=2)).isoformat() if disp == "callback_requested" else None,
                     "started_at": t.isoformat(), "ended_at": (t + timedelta(minutes=3)).isoformat()}
            facts += [accepted, ended]
            if disp in ("interested", "not_interested", "wrong_number", "dnd"):
                break
    return facts


def final_after(tmp_path, name, facts):
    d = tmp_path / name
    d.mkdir()
    (d / "a.csv").write_text("lead_id,phone,customer_name\na1,+919000000001,X\na2,+919000000002,Y\n")
    (d / "b.csv").write_text("lead_id,phone,customer_name\nb1,+919000000001,X\n")
    s = Store(str(d / "db"), CFG, str(d), world_id="w")
    for f in facts:
        s.record_call(f)
    rows = s.final_rows()
    states = s.db.execute("SELECT campaign_id, lead_id, state, kind, ready_at FROM leads "
                          "ORDER BY 1, 2").fetchall()
    s.db.close()
    return rows, [tuple(x) for x in states]


@pytest.mark.parametrize("seed", range(60))
def test_store_is_order_and_duplicate_independent(tmp_path, seed):
    r = random.Random(seed)
    facts = history(r)
    expected = final_after(tmp_path, "inorder", facts)
    scrambled = facts + r.sample(facts, r.randint(0, len(facts)))  # duplicates
    r.shuffle(scrambled)                                            # any order
    assert final_after(tmp_path, "scrambled", scrambled) == expected
