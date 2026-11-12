"""Pure scheduling rules: calling windows, next eligible time, priority.

No I/O and no clock. Every function takes `now` as an argument, so tests drive it with a fake clock.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TERMINAL = frozenset({"interested", "not_interested", "wrong_number", "dnd"})
FINAL_STATES = frozenset({"completed", "exhausted", "dnd_blocked"})
KIND_RANK = {"callback": 0, "retry": 1, "fresh": 2}  # R6: callbacks > retries > fresh

# Don't start a call in the last 20 s of a window: our request reaches the provider a little after
# we decide (at 600x, 1 ms of real latency is 0.6 simulated seconds). Kept small so a callback due
# in the window's last minute is still honoured that day.
END_MARGIN = timedelta(seconds=20)


@dataclass(frozen=True)
class Window:
    start: time
    end: time
    days: frozenset[str]

    @classmethod
    def build(cls, campaign: dict, regulatory: dict) -> "Window":
        """Campaign window intersected with the regulatory window (R1)."""
        return cls(start=max(_hm(campaign["start"]), _hm(regulatory["start"])),
                   end=min(_hm(campaign["end"]), _hm(regulatory["end"])),
                   days=frozenset(campaign["days"]))


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def next_open(w: Window, t: datetime, margin: timedelta = END_MARGIN) -> datetime:
    """Earliest instant >= t at which a call may be started in window `w`."""
    lt = t.astimezone(IST)
    for d in range(8):
        day = (lt + timedelta(days=d)).date()
        if DAYS[day.weekday()] not in w.days:
            continue
        start = datetime.combine(day, w.start, IST)
        end = datetime.combine(day, w.end, IST) - margin
        if lt < end:
            return max(lt, start)
    raise ValueError(f"window {w} never opens")


def is_open(w: Window, now: datetime) -> bool:
    return next_open(w, now) <= now


def next_attempt(last: dict | None, retry_policy: dict) -> tuple[str, datetime | None]:
    """(kind, earliest start) of a lead's next attempt, from its most recently ended call (R5, R6).

    `last` has `status`, `disposition`, `ended_at`, `callback_at` (datetimes parsed).
    """
    if last is None:
        return "fresh", None
    if last["disposition"] == "callback_requested":
        return "callback", last["callback_at"]
    minutes = retry_policy.get(f"{last['status']}_minutes", 0)
    return "retry", last["ended_at"] + timedelta(minutes=minutes)


@dataclass(frozen=True)
class Candidate:
    campaign_id: str
    lead_id: str
    tenant_id: str
    phone: str
    kind: str
    ready_at: datetime | None  # None = ready immediately


def priority(c: Candidate) -> tuple:
    # Earliest-due first within a class; ids break ties so ordering is deterministic.
    due = c.ready_at or datetime.min.replace(tzinfo=timezone.utc)
    return KIND_RANK[c.kind], due, c.campaign_id, c.lead_id


def pick(now: datetime, pending: list[Candidate], windows: dict[str, Window],
         busy_phones: set[str], dnd_phones: set[str],
         active: dict[str, int], limits: dict[str, int]) -> list[Candidate]:
    """Leads to dial right now, in priority order, obeying R1, R2, R3, R5, R6 and R8.

    `busy_phones` = phones with a call being dialled or active. `active` = per-tenant count of
    calls being dialled or active (dialling counts, so a slot is reserved before the request).
    """
    busy, active, out = set(busy_phones), Counter(active), []
    for c in sorted(pending, key=priority):
        if c.ready_at is not None and c.ready_at > now:
            continue
        if not is_open(windows[c.campaign_id], now):
            continue
        if c.phone in busy or c.phone in dnd_phones:
            continue
        if active[c.tenant_id] >= limits[c.tenant_id]:
            continue
        busy.add(c.phone)
        active[c.tenant_id] += 1
        out.append(c)
    return out
