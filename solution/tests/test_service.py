"""Dispatcher <-> provider handling against a scripted fake provider (httpx.MockTransport)."""
import asyncio
import hashlib
import hmac
import json
from datetime import datetime

import httpx
import pytest

from dispatcher.rules import IST, Candidate
from dispatcher.service import Dispatcher, create_app
from dispatcher.store import Store

SECRET = "whsec_test"
CFG = {
    "regulatory_window": {"start": "09:00", "end": "21:00"},
    "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
    "webhook_secret": SECRET,
    "tenants": [{"tenant_id": "t1", "max_concurrent_calls": 2}],
    "campaigns": [{"campaign_id": "ca", "tenant_id": "t1", "leads_file": "a.csv", "max_attempts": 3,
                   "window": {"start": "10:00", "end": "19:00", "days": ["thu"]}}],
}
PHONE = "+919000000002"
LEAD = Candidate("ca", "a1", "t1", PHONE, "fresh", None)
NOON = datetime(2026, 11, 12, 12, 0, tzinfo=IST)  # Thursday


class FakeClock:
    def __init__(self, now):
        self.t = now

    def now(self):
        return self.t


def call_body(rid, state="active", **result):
    body = {"call_sid": "CA1", "request_id": rid, "campaign_id": "ca", "lead_id": "a1",
            "state": state, "created_at": NOON.isoformat()}
    if result:
        body["result"] = {"event": "call.completed", "call_sid": "CA1", "request_id": rid,
                          "campaign_id": "ca", "lead_id": "a1", "phone": PHONE,
                          "started_at": NOON.isoformat(), "callback_at": None, **result}
    return body


@pytest.fixture
def make(tmp_path):
    (tmp_path / "a.csv").write_text(f"lead_id,phone,customer_name\na1,{PHONE},X\n")

    def build(handler, now=NOON):
        d = Dispatcher(CFG, str(tmp_path), str(tmp_path / "d.db"), "http://p", "http://cb",
                       str(tmp_path / "out.csv"))
        d.http = httpx.AsyncClient(base_url="http://p", transport=httpx.MockTransport(handler))
        d.store = Store(str(tmp_path / "d.db"), CFG, str(tmp_path), world_id="w")
        d.clock = FakeClock(now)
        return d
    return build


def lead_state(d):
    return d.store.db.execute("SELECT state, request_id FROM leads").fetchone()


def dial(d):
    rid = d.store.begin_dial("ca", "a1")
    asyncio.run(d.dial(LEAD, rid))
    return rid


def test_201_marks_active(make):
    d = make(lambda req: httpx.Response(201, json=call_body(json.loads(req.content)["request_id"])))
    dial(d)
    assert lead_state(d)["state"] == "active"


def test_503_releases_slot_and_keeps_request_id(make):
    d = make(lambda req: httpx.Response(503, json={"error": "service_unavailable"}))
    assert dial(d) == "ca:a1:1"
    assert tuple(lead_state(d)) == ("pending", None)
    assert d.store.begin_dial("ca", "a1") == "ca:a1:1"


@pytest.mark.parametrize("placed, expected", [(True, "active"), (False, "pending")])
def test_504_is_resolved_by_request_id_lookup(make, placed, expected):
    seen = []

    def provider(req):
        seen.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(504, json={"error": "upstream_timeout"})
        rid = req.url.params["request_id"]
        return httpx.Response(200, json=call_body(rid)) if placed else httpx.Response(404)
    d = make(provider)
    dial(d)
    assert seen == [("POST", "/v1/calls"), ("GET", "/v1/calls")]
    assert lead_state(d)["state"] == expected


def test_timeout_is_treated_as_unknown_outcome(make):
    def provider(req):
        if req.method == "POST":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json=call_body(req.url.params["request_id"], "ended",
                                                  status="completed", disposition="interested",
                                                  ended_at=NOON.isoformat()))
    d = make(provider)
    dial(d)
    assert lead_state(d)["state"] == "completed"


def test_rejection_backs_off_instead_of_hammering(make):
    d = make(lambda req: httpx.Response(409, json={"error": "concurrent_dial_same_phone"}))
    dial(d)
    assert lead_state(d)["state"] == "pending"
    assert d.hold[("ca", "a1")] > NOON


def test_window_rechecked_just_before_sending(make):
    def provider(req):
        raise AssertionError("must not call the provider after the window closed")
    late = datetime(2026, 11, 12, 18, 59, 50, tzinfo=IST)  # inside the 20 s end margin
    d = make(provider, now=late)
    dial(d)
    assert lead_state(d)["state"] == "pending"


def signed(body: dict):
    raw = json.dumps(body).encode()
    return raw, hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def test_webhook_signature_and_idempotency(make):
    d = make(lambda req: httpx.Response(201, json=call_body(json.loads(req.content)["request_id"])))
    rid = dial(d)
    wh = call_body(rid, "ended", status="no_answer", disposition=None, ended_at=NOON.isoformat())["result"]
    raw, sig = signed(wh)
    bad, bad_sig = signed({"event": "call.completed"})  # signed but missing fields

    async def scenario():
        # ASGITransport doesn't run the lifespan, so the scheduler loop stays off.
        app = httpx.ASGITransport(app=create_app(d))
        async with httpx.AsyncClient(transport=app, base_url="http://cb") as client:
            post = lambda body, s: client.post("/webhooks/telephony", content=body,
                                               headers={"X-AU-Signature": s})
            assert (await post(raw, "bad")).status_code == 401
            assert lead_state(d)["state"] == "active"
            for _ in range(2):  # duplicate delivery
                assert (await post(raw, sig)).status_code == 200
            assert (await post(bad, bad_sig)).status_code == 400
    asyncio.run(scenario())
    assert lead_state(d)["state"] == "pending"
    assert d.store.final_rows()[0]["attempts"] == 1
