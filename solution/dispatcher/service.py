"""Dispatcher process: webhook receiver + scheduling loop + provider reconciliation.

One asyncio process. Every DB write is a committed SQLite transaction made *before* the external
action it guards, so `kill -9` at any line leaves state we can resume from (see SUBMISSION.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .rules import IST, Window, end_margin, is_open, next_open, pick
from .store import Store

log = logging.getLogger("dispatcher")

TICK_S = 0.02                          # real seconds between scheduling passes
# A call that has been active this long has certainly ended at the provider (the mock's longest
# call is ~6.5 sim min); if no webhook came, ask the provider directly.
POLL_AFTER = timedelta(minutes=8)
POLL_EVERY = timedelta(minutes=2)
# After the provider rejects a dial (409/422/429: our view of the world was wrong), leave that lead
# alone for a while instead of re-sending every tick. Each rejection is a recorded violation.
REJECT_BACKOFF = timedelta(minutes=5)


class SimClock:
    def __init__(self, info: dict):
        self.sim_epoch = datetime.fromisoformat(info["sim_epoch"])
        self.real_epoch = float(info["real_epoch"])
        self.speed = float(info["speed"])

    def now(self) -> datetime:
        return self.sim_epoch + timedelta(seconds=(time.time() - self.real_epoch) * self.speed)


class Dispatcher:
    def __init__(self, cfg: dict, fixtures_dir: str, db_path: str, provider: str,
                 callback_url: str, out_path: str):
        self.cfg, self.fixtures_dir, self.db_path = cfg, fixtures_dir, db_path
        self.callback_url, self.out_path = callback_url, out_path
        self.http = httpx.AsyncClient(base_url=provider, timeout=10.0)
        self.windows = {c["campaign_id"]: Window.build(c["window"], cfg["regulatory_window"])
                        for c in cfg["campaigns"]}
        for cid, w in self.windows.items():
            if next_open(w, datetime.now(IST)) is None:
                log.warning("campaign %s: calling window never opens; its leads will never be dialled", cid)
        self.limits = {t["tenant_id"]: t["max_concurrent_calls"] for t in cfg["tenants"]}
        self.secret = cfg["webhook_secret"].encode()
        self.inflight: set[str] = set()        # request_ids with a live task in this process
        self.last_poll: dict[str, datetime] = {}
        self.tasks: set[asyncio.Task] = set()
        self.store: Store | None = None
        self.clock: SimClock | None = None
        self.hold: dict[tuple[str, str], datetime] = {}  # lead -> don't dial before (after a reject)
        self.done = asyncio.Event()
        self.failed = False

    # ------------------------------------------------------------------ startup
    async def connect(self) -> None:
        while True:
            try:
                info = (await self.http.get("/v1/clock")).raise_for_status().json()
                break
            except httpx.HTTPError as e:
                log.warning("provider not reachable (%s), retrying", e)
                await asyncio.sleep(1)
        self.clock = SimClock(info)
        margin = end_margin(self.clock.speed)
        self.windows = {c["campaign_id"]: Window.build(c["window"], self.cfg["regulatory_window"], margin)
                        for c in self.cfg["campaigns"]}
        self.store = Store(self.db_path, self.cfg, self.fixtures_dir, world_id=str(info["real_epoch"]))
        log.info("%s; sim_now=%s", "resumed from DB" if self.store.resumed else "fresh start",
                 self.clock.now().isoformat())

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        """Scheduling loop. If it dies, take the whole process down (exit 1) so a supervisor
        restarts it, rather than leaving a webhook server up with nothing dispatching."""
        try:
            await self._run()
        except Exception:
            log.exception("scheduler crashed; exiting")
            self.failed = True
            self.done.set()

    async def _run(self) -> None:
        await self.connect()
        while True:
            now = self.clock.now()
            self.reconcile(now)
            if self.store.all_final() and not self.inflight:
                self.store.write_final_state(self.out_path)
                log.info("all leads final; wrote %s; %s", self.out_path, self.store.status())
                self.done.set()
                return
            phones, active = self.store.busy()
            pending = [c for c in self.store.pending()
                       if (c.campaign_id, c.lead_id) not in self.hold
                       or self.hold[(c.campaign_id, c.lead_id)] <= now]
            for c in pick(now, pending, self.windows, phones, self.store.dnd_phones(),
                          active, self.limits):
                rid = self.store.begin_dial(c.campaign_id, c.lead_id)
                if rid:
                    self.spawn(rid, self.dial(c, rid))
            await asyncio.sleep(TICK_S)

    def spawn(self, rid: str, coro) -> None:
        self.inflight.add(rid)
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def finished(t: asyncio.Task):
            self.tasks.discard(t)
            self.inflight.discard(rid)
            if not t.cancelled() and t.exception():
                log.error("task %s failed: %r", rid, t.exception())
        task.add_done_callback(finished)

    def reconcile(self, now: datetime) -> None:
        # Dials whose outcome we don't know (504, timeout, or we crashed mid-request).
        for rid in self.store.dialing_requests():
            if rid not in self.inflight:
                self.spawn(rid, self.resolve_request(rid))
        # Active calls whose webhook never came.
        for sid, created in self.store.active_calls():
            key, last = "poll:" + sid, self.last_poll.get(sid)
            if now - created < POLL_AFTER or (last and now - last < POLL_EVERY) or key in self.inflight:
                continue
            self.last_poll[sid] = now
            self.spawn(key, self.poll_call(sid))

    # ------------------------------------------------------------------ provider calls
    async def dial(self, c, rid: str) -> None:
        # Re-check the window right before sending. The loop decided a moment ago, and at high
        # --speed a few ms of event-loop delay is many simulated seconds.
        if not is_open(self.windows[c.campaign_id], self.clock.now()):
            self.store.abort_dial(rid)
            return
        body = {"request_id": rid, "campaign_id": c.campaign_id, "lead_id": c.lead_id,
                "phone": c.phone, "callback_url": self.callback_url}
        try:
            r = await self.http.post("/v1/calls", json=body)
        except httpx.HTTPError as e:  # we may or may not have reached the provider
            log.warning("dial %s: %r, outcome unknown", rid, e)
            return await self.resolve_request(rid)
        if r.status_code in (200, 201):
            self.store.record_call(r.json())
            log.info("dial %s accepted (%s) kind=%s", rid, r.status_code, c.kind)
        elif r.status_code == 503:
            self.store.abort_dial(rid)  # not placed; lead goes back to pending, same request_id
        elif r.status_code == 504:
            log.warning("dial %s: 504, outcome unknown", rid)
            await self.resolve_request(rid)
        else:  # 409/422/429: a rule broke. Should never happen; shout about it.
            log.error("dial %s rejected %s: %s", rid, r.status_code, r.text)
            self.hold[(c.campaign_id, c.lead_id)] = self.clock.now() + REJECT_BACKOFF
            self.store.abort_dial(rid)

    async def resolve_request(self, rid: str) -> None:
        """Ask the provider whether this request_id placed a call."""
        try:
            r = await self.http.get("/v1/calls", params={"request_id": rid})
        except httpx.HTTPError as e:
            log.warning("lookup %s failed: %r; will retry", rid, e)
            await asyncio.sleep(0.2)
            return
        if r.status_code == 200:
            self.store.record_call(r.json())
        elif r.status_code == 404:
            self.store.abort_dial(rid)
        else:
            log.warning("lookup %s: %s; will retry", rid, r.status_code)
            await asyncio.sleep(0.2)

    async def poll_call(self, sid: str) -> None:
        try:
            r = await self.http.get(f"/v1/calls/{sid}")
        except httpx.HTTPError as e:
            log.warning("poll %s failed: %r", sid, e)
            return
        if r.status_code == 200 and r.json().get("state") == "ended":
            log.info("poll %s: ended without a webhook, recovered", sid)
            self.store.record_call(r.json())

    # ------------------------------------------------------------------ webhooks
    def verify(self, raw: bytes, signature: str | None) -> bool:
        expected = hmac.new(self.secret, raw, hashlib.sha256).hexdigest()
        return signature is not None and hmac.compare_digest(expected, signature)


def create_app(d: Dispatcher) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(d.run())
        yield
        task.cancel()
        await d.http.aclose()

    app = FastAPI(title="Campaign dispatcher", lifespan=lifespan)

    @app.post("/webhooks/telephony")
    async def webhook(request: Request):
        raw = await request.body()
        if not d.verify(raw, request.headers.get("X-AU-Signature")):
            log.warning("webhook rejected: bad signature")
            return JSONResponse({"error": "bad_signature"}, status_code=401)
        if d.store is None:  # still starting up; provider will redeliver
            return JSONResponse({"error": "starting"}, status_code=503)
        try:
            d.store.record_call(json.loads(raw))
        except (ValueError, KeyError, TypeError, AttributeError) as e:  # bad JSON, not an object, missing field, unknown lead
            log.error("webhook rejected: %r", e)  # don't log the body: it contains the phone (PII)
            return JSONResponse({"error": "bad_request"}, status_code=400)
        return {"ok": True}

    @app.get("/status")
    async def status():
        if d.store is None:
            return {"starting": True}
        return {"sim_now": d.clock.now().isoformat(), "inflight": len(d.inflight),
                "campaigns": d.store.status()}

    return app
