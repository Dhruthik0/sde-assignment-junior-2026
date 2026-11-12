"""Lead state machine: idempotency, crash recovery, DND and final-state precedence."""
import pytest

from dispatcher.store import Store

CFG = {
    "regulatory_window": {"start": "09:00", "end": "21:00"},
    "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
    "tenants": [{"tenant_id": "t1", "max_concurrent_calls": 2}],
    "campaigns": [
        {"campaign_id": "ca", "tenant_id": "t1", "leads_file": "a.csv", "max_attempts": 3,
         "window": {"start": "10:00", "end": "19:00", "days": ["thu"]}},
        {"campaign_id": "cb", "tenant_id": "t1", "leads_file": "b.csv", "max_attempts": 3,
         "window": {"start": "10:00", "end": "19:00", "days": ["thu"]}},
    ],
}
SHARED = "+919000000001"


@pytest.fixture
def store(tmp_path):
    (tmp_path / "a.csv").write_text(f"lead_id,phone,customer_name\na1,{SHARED},X\na2,+919000000002,Y\n")
    (tmp_path / "b.csv").write_text(f"lead_id,phone,customer_name\nb1,{SHARED},X\n")
    return Store(str(tmp_path / "d.db"), CFG, str(tmp_path), world_id="w1")


def state(s, cid, lid):
    return s.db.execute("SELECT state, kind, ready_at, request_id FROM leads WHERE campaign_id=? "
                        "AND lead_id=?", (cid, lid)).fetchone()


_n = 0


def accept(s, cid, lid, rid):
    global _n
    _n += 1
    sid = f"CA{_n}"
    s.record_call({"call_sid": sid, "request_id": rid, "campaign_id": cid, "lead_id": lid,
                   "state": "active", "created_at": "2026-11-12T10:00:00+05:30"})
    return sid


def webhook(sid, rid, cid, lid, status, disposition=None, ended="2026-11-12T10:05:00+05:30",
            callback_at=None, phone="+919000000002"):
    return {"event": "call.completed", "call_sid": sid, "request_id": rid, "campaign_id": cid,
            "lead_id": lid, "phone": phone, "status": status, "disposition": disposition,
            "callback_at": callback_at, "started_at": "2026-11-12T10:00:00+05:30", "ended_at": ended}


def dial(s, cid, lid, status, disposition=None, **kw):
    rid = s.begin_dial(cid, lid)
    sid = accept(s, cid, lid, rid)
    s.record_call(webhook(sid, rid, cid, lid, status, disposition, **kw))
    return rid, sid


def test_request_id_is_deterministic_per_attempt(store):
    rid = store.begin_dial("ca", "a2")
    assert rid == "ca:a2:1"
    assert state(store, "ca", "a2")["state"] == "dialing"
    store.abort_dial(rid)                     # 503: not placed
    assert state(store, "ca", "a2")["state"] == "pending"
    assert store.begin_dial("ca", "a2") == "ca:a2:1"   # same key reused, so a retry can't double-dial


def test_begin_dial_only_from_pending(store):
    assert store.begin_dial("ca", "a2")
    assert store.begin_dial("ca", "a2") is None


def test_duplicate_webhook_is_harmless(store):
    rid, sid = dial(store, "ca", "a2", "no_answer")
    before = state(store, "ca", "a2")
    store.record_call(webhook(sid, rid, "ca", "a2", "no_answer"))
    assert state(store, "ca", "a2") == before
    assert before["kind"] == "retry" and before["ready_at"] == "2026-11-12T11:05:00+05:30"
    assert store.final_rows()[1]["attempts"] == 1


def test_webhook_before_accept_response(store):
    rid = store.begin_dial("ca", "a2")
    store.record_call(webhook("CA_x", rid, "ca", "a2", "completed", "interested"))
    assert state(store, "ca", "a2")["state"] == "completed"
    store.record_call({"call_sid": "CA_x", "request_id": rid, "campaign_id": "ca", "lead_id": "a2",
                       "state": "active", "created_at": "2026-11-12T10:00:00+05:30"})  # late 201
    assert state(store, "ca", "a2")["state"] == "completed"


def test_callback_becomes_priority_kind(store):
    dial(store, "ca", "a2", "completed", "callback_requested", callback_at="2026-11-12T12:00:00+05:30")
    row = state(store, "ca", "a2")
    assert (row["state"], row["kind"], row["ready_at"]) == ("pending", "callback", "2026-11-12T12:00:00+05:30")


def test_exhausted_after_max_attempts(store):
    for i in range(3):
        dial(store, "ca", "a2", "busy", ended=f"2026-11-12T1{i}:05:00+05:30")
    assert state(store, "ca", "a2")["state"] == "exhausted"
    assert store.final_rows()[1] == {"campaign_id": "ca", "lead_id": "a2", "final_state": "exhausted",
                                     "attempts": 3, "last_status": "busy", "last_disposition": ""}


def test_dnd_blocks_same_phone_in_other_campaign(store):
    dial(store, "ca", "a1", "completed", "dnd", phone=SHARED)
    assert state(store, "ca", "a1")["state"] == "completed"
    assert state(store, "cb", "b1")["state"] == "dnd_blocked"
    assert SHARED in store.dnd_phones()


def test_exhausted_beats_dnd_blocked(store):
    for i in range(3):
        dial(store, "cb", "b1", "no_answer", ended=f"2026-11-12T1{i}:05:00+05:30", phone=SHARED)
    dial(store, "ca", "a1", "completed", "dnd", phone=SHARED)
    assert state(store, "cb", "b1")["state"] == "exhausted"


def test_dnd_waits_for_active_call_on_other_lead(store):
    # Defensive: R3 means the dispatcher never has two leads on one phone active at once, but the
    # derivation must still give the right answer if it ever happened.
    rid = store.begin_dial("cb", "b1")
    sid = accept(store, "cb", "b1", rid)
    dial(store, "ca", "a1", "completed", "dnd", phone=SHARED)
    assert state(store, "cb", "b1")["state"] == "active"     # still on the phone; can't decide yet
    store.record_call(webhook(sid, rid, "cb", "b1", "completed", "interested", phone=SHARED))
    assert state(store, "cb", "b1")["state"] == "completed"  # completed beats dnd_blocked


def test_state_survives_reopen_and_new_world_resets(store, tmp_path):
    rid = store.begin_dial("ca", "a2")
    store.db.close()
    again = Store(str(tmp_path / "d.db"), CFG, str(tmp_path), world_id="w1")
    assert again.resumed and again.dialing_requests() == [rid]
    fresh = Store(str(tmp_path / "d.db"), CFG, str(tmp_path), world_id="w2")
    assert not fresh.resumed and fresh.dialing_requests() == []


def test_busy_counts_dialing_and_active(store):
    store.begin_dial("ca", "a1")
    rid = store.begin_dial("ca", "a2")
    accept(store, "ca", "a2", rid)
    phones, active = store.busy()
    assert phones == {SHARED, "+919000000002"} and active == {"t1": 2}


def test_final_state_csv_contract(store, tmp_path):
    # a2: two attempts; last_status/disposition must come from the most recently *ended* call.
    dial(store, "ca", "a2", "no_answer", ended="2026-11-12T10:05:00+05:30")
    dial(store, "ca", "a2", "completed", "callback_requested", ended="2026-11-12T11:10:00+05:30",
         callback_at="2026-11-12T13:00:00+05:30")
    out = tmp_path / "final_state.csv"
    store.write_final_state(str(out))
    assert out.read_text().splitlines() == [
        "campaign_id,lead_id,final_state,attempts,last_status,last_disposition",
        "ca,a1,pending,0,,",            # no ended call: both columns empty
        "ca,a2,pending,2,completed,callback_requested",
        "cb,b1,pending,0,,",            # sorted by campaign_id, then lead_id
    ]
