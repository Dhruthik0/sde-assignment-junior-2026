"""Scheduling rules with a fake clock: every test passes `now` explicitly."""
from datetime import datetime, timedelta

from dispatcher.rules import IST, Candidate, Window, is_open, next_attempt, next_open, pick

REG = {"start": "09:00", "end": "21:00"}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
ZEN = Window.build({"start": "09:30", "end": "18:00", "days": WEEKDAYS}, REG)
ALL_DAY = Window.build({"start": "08:00", "end": "22:00", "days": WEEKDAYS + ["sat", "sun"]}, REG)
RETRY = {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15}


def at(day: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    """Nov 2026 in IST. The 12th is a Thursday."""
    return datetime(2026, 11, day, hh, mm, ss, tzinfo=IST)


# ------------------------------------------------------------------ window maths (R1)
def test_window_is_intersected_with_regulatory_window():
    assert (ALL_DAY.start.hour, ALL_DAY.end.hour) == (9, 21)
    assert not is_open(ALL_DAY, at(14, 8, 59))
    assert is_open(ALL_DAY, at(14, 9, 0))


def test_window_start_inclusive_end_exclusive_with_margin():
    assert not is_open(ZEN, at(13, 9, 29, 59))
    assert is_open(ZEN, at(13, 9, 30))
    assert is_open(ZEN, at(13, 17, 59, 39))
    assert not is_open(ZEN, at(13, 17, 59, 40))   # last 20 s held back as safety margin
    assert not is_open(ZEN, at(13, 18, 0))


def test_end_margin_scales_with_speed():
    from dispatcher.rules import end_margin
    assert end_margin(300) == timedelta(seconds=20)
    assert end_margin(600) == timedelta(seconds=30)
    assert end_margin(12000) == timedelta(minutes=10)
    fast = Window.build({"start": "09:30", "end": "18:00", "days": WEEKDAYS}, REG, end_margin(12000))
    assert is_open(fast, at(13, 17, 49, 59)) and not is_open(fast, at(13, 17, 50))


def test_window_respects_days():
    assert not is_open(ZEN, at(14, 12))       # Saturday
    assert is_open(ALL_DAY, at(14, 12))


def test_window_handles_utc_input():
    assert is_open(ZEN, datetime.fromisoformat("2026-11-13T04:00:00+00:00"))  # 09:30 IST


def test_next_open_same_day_and_weekend_rollover():
    assert next_open(ZEN, at(13, 7)) == at(13, 9, 30)
    assert next_open(ZEN, at(13, 12)) == at(13, 12)
    assert next_open(ZEN, at(13, 18, 30)) == at(16, 9, 30)  # Fri evening -> Mon morning


def test_window_outside_regulatory_hours_never_opens():
    early = Window.build({"start": "06:00", "end": "08:30", "days": WEEKDAYS}, REG)
    assert next_open(early, at(13, 7)) is None
    assert not is_open(early, at(13, 9, 0))
    assert next_open(Window.build({"start": "10:00", "end": "18:00", "days": []}, REG), at(13, 7)) is None


# ------------------------------------------------------------------ next eligible time (R5, R6)
def test_fresh_lead_is_ready_now():
    assert next_attempt(None, RETRY) == ("fresh", None)


def test_retry_delays_from_ended_at():
    end = at(13, 10)
    for status, mins in [("no_answer", 60), ("busy", 60), ("failed", 15)]:
        last = {"status": status, "disposition": None, "ended_at": end, "callback_at": None}
        assert next_attempt(last, RETRY) == ("retry", end + timedelta(minutes=mins))


def test_callback_waits_for_callback_at_then_next_window_opening():
    cb = at(13, 19, 15)  # after the zen window closes on Friday
    last = {"status": "completed", "disposition": "callback_requested",
            "ended_at": at(13, 17), "callback_at": cb}
    kind, ready = next_attempt(last, RETRY)
    assert (kind, ready) == ("callback", cb)
    assert next_open(ZEN, ready) == at(16, 9, 30)


# ------------------------------------------------------------------ priority & constraints
def cand(lid, kind="fresh", ready=None, phone=None, tenant="t1", campaign="c1"):
    return Candidate(campaign, lid, tenant, phone or f"+91{lid}", kind, ready)


def run_pick(pending, now=None, busy=(), dnd=(), active=None, limit=10):
    now = now or at(13, 12)
    return [c.lead_id for c in pick(now, pending, {"c1": ALL_DAY, "c2": ZEN}, set(busy), set(dnd),
                                    active or {}, {"t1": limit})]


def test_priority_callbacks_then_retries_then_fresh():
    pending = [cand("f1"), cand("r1", "retry", at(13, 11)), cand("cb2", "callback", at(13, 11, 30)),
               cand("cb1", "callback", at(13, 11)), cand("r0", "retry", at(13, 10))]
    assert run_pick(pending) == ["cb1", "cb2", "r0", "r1", "f1"]


def test_not_dialled_before_ready_at():
    assert run_pick([cand("r1", "retry", at(13, 12, 1)), cand("f1")]) == ["f1"]


def test_tenant_concurrency_counts_existing_active_calls():
    pending = [cand(f"f{i}") for i in range(5)]
    assert run_pick(pending, active={"t1": 2}, limit=4) == ["f0", "f1"]


def test_limit_shared_across_campaigns_of_tenant():
    pending = [cand("a", campaign="c1"), cand("b", campaign="c2"), cand("c", campaign="c2")]
    assert run_pick(pending, limit=2) == ["a", "b"]


def test_one_call_per_phone():
    pending = [cand("a", phone="+91999"), cand("b", phone="+91999", campaign="c2"), cand("c")]
    assert run_pick(pending) == ["a", "c"]
    assert run_pick(pending, busy={"+91999"}) == ["c"]


def test_dnd_phone_never_picked():
    assert run_pick([cand("a", phone="+91999"), cand("b")], dnd={"+91999"}) == ["b"]


def test_closed_window_skips_only_that_campaign():
    pending = [cand("zen", campaign="c2"), cand("all", campaign="c1")]
    assert run_pick(pending, now=at(14, 12)) == ["all"]   # Saturday: zen window closed
