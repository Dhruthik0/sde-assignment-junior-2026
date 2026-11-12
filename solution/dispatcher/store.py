"""SQLite-backed lead state machine.

The `calls` table holds facts the provider told us (a call was accepted, a call ended). Callers
only record facts or start/abort a dial; after each, `_refresh` recomputes the lead's state from
those facts plus the lead's in-flight `request_id`. Facts only ever get added, so applying the same fact twice (duplicate
webhook, webhook + poll, replay after a crash) produces the same state.
"""
from __future__ import annotations

import csv
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .rules import FINAL_STATES, TERMINAL, Candidate, next_attempt

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leads (
    campaign_id TEXT NOT NULL,
    lead_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    phone       TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN
                  ('pending','dialing','active','completed','exhausted','dnd_blocked')),
    kind        TEXT CHECK (kind IN ('fresh','retry','callback')),  -- set while pending
    ready_at    TEXT,            -- earliest start of next attempt (ISO), NULL = now
    request_id  TEXT UNIQUE,     -- dial we committed to but haven't seen accepted/rejected yet
    PRIMARY KEY (campaign_id, lead_id)
);
CREATE INDEX IF NOT EXISTS leads_phone ON leads (phone);
CREATE INDEX IF NOT EXISTS leads_state ON leads (state);
CREATE TABLE IF NOT EXISTS calls (            -- one row per call the provider accepted
    call_sid    TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL UNIQUE,
    campaign_id TEXT NOT NULL,
    lead_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    ended_at    TEXT,            -- NULL while the call is active
    status      TEXT,
    disposition TEXT,
    callback_at TEXT,
    FOREIGN KEY (campaign_id, lead_id) REFERENCES leads
);
CREATE INDEX IF NOT EXISTS calls_lead ON calls (campaign_id, lead_id);
CREATE TABLE IF NOT EXISTS dnd_phones (phone TEXT PRIMARY KEY);
"""


def dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Store:
    def __init__(self, path: str, cfg: dict, fixtures_dir: str, world_id: str):
        """Open (or create) the DB. `world_id` identifies the provider run; if it changed, the
        provider was restarted with a fresh world and our old state means nothing, so we reset."""
        self.cfg = cfg
        self.campaigns = {c["campaign_id"]: c for c in cfg["campaigns"]}
        self.db = sqlite3.connect(path, isolation_level=None)  # we manage transactions ourselves
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL survives kill -9; FULL only adds power-loss safety
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='world'").fetchone()
        self.resumed = row is not None and row["value"] == world_id
        if not self.resumed:
            with self.tx():
                for t in ("calls", "dnd_phones", "leads", "meta"):
                    self.db.execute(f"DELETE FROM {t}")
                self.db.execute("INSERT INTO meta VALUES ('world', ?)", (world_id,))
                for c in cfg["campaigns"]:
                    with open(os.path.join(fixtures_dir, c["leads_file"]), newline="") as f:
                        for r in csv.DictReader(f):
                            self.db.execute(
                                "INSERT INTO leads (campaign_id, lead_id, tenant_id, phone, state, kind)"
                                " VALUES (?, ?, ?, ?, 'pending', 'fresh')",
                                (c["campaign_id"], r["lead_id"], c["tenant_id"], r["phone"]))

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ transitions
    def begin_dial(self, campaign_id: str, lead_id: str) -> str | None:
        """Commit to dialling before we send anything. The request_id is deterministic per
        (lead, attempt number), so re-sending it after a crash or 504 can never place a 2nd call."""
        with self.tx():
            n = self.db.execute("SELECT COUNT(*) FROM calls WHERE campaign_id=? AND lead_id=?",
                                (campaign_id, lead_id)).fetchone()[0]
            rid = f"{campaign_id}:{lead_id}:{n + 1}"
            cur = self.db.execute(
                "UPDATE leads SET state='dialing', request_id=? "
                "WHERE campaign_id=? AND lead_id=? AND state='pending'", (rid, campaign_id, lead_id))
        return rid if cur.rowcount else None

    def abort_dial(self, request_id: str) -> None:
        """The provider confirmed this request did not place a call (503, or 404 on lookup)."""
        with self.tx():
            row = self.db.execute("SELECT campaign_id, lead_id FROM leads WHERE request_id=?",
                                  (request_id,)).fetchone()
            if row:
                self.db.execute("UPDATE leads SET request_id=NULL WHERE request_id=?", (request_id,))
                self._refresh(row["campaign_id"], row["lead_id"])

    def record_call(self, call: dict) -> None:
        """Apply a provider call object (POST/GET body or webhook body). Idempotent."""
        result = call.get("result") or (call if call.get("event") == "call.completed" else None)
        cid, lid = call["campaign_id"], call["lead_id"]
        with self.tx():
            if not self.db.execute("SELECT 1 FROM leads WHERE campaign_id=? AND lead_id=?",
                                   (cid, lid)).fetchone():
                raise KeyError(f"unknown lead {cid}/{lid}")
            self.db.execute(
                "INSERT OR IGNORE INTO calls (call_sid, request_id, campaign_id, lead_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (call["call_sid"], call["request_id"], cid, lid,
                 call.get("created_at") or call.get("started_at")))
            if result:
                self.db.execute(
                    "UPDATE calls SET ended_at=?, status=?, disposition=?, callback_at=? "
                    "WHERE call_sid=? AND ended_at IS NULL",
                    (result["ended_at"], result["status"], result["disposition"],
                     result.get("callback_at"), call["call_sid"]))
                if result["disposition"] == "dnd":
                    self.db.execute("INSERT OR IGNORE INTO dnd_phones VALUES (?)", (result["phone"],))
                    # R8: every other lead on this phone may now be blocked.
                    for r in self.db.execute("SELECT campaign_id, lead_id FROM leads WHERE phone=?",
                                             (result["phone"],)).fetchall():
                        self._refresh(r["campaign_id"], r["lead_id"])
            self._refresh(cid, lid)

    def _refresh(self, cid: str, lid: str) -> None:
        """Derive the lead's state from facts. Order encodes R9 precedence."""
        lead = self.db.execute("SELECT phone, request_id FROM leads WHERE campaign_id=? AND lead_id=?",
                               (cid, lid)).fetchone()
        calls = self.db.execute("SELECT * FROM calls WHERE campaign_id=? AND lead_id=?",
                                (cid, lid)).fetchall()
        rid = lead["request_id"]
        if rid and any(c["request_id"] == rid for c in calls):
            rid = None  # the dial we were waiting on is now a known call
        ended = sorted((c for c in calls if c["ended_at"]), key=lambda c: dt(c["ended_at"]))
        kind = ready = None
        if rid:
            state = "dialing"
        elif len(ended) < len(calls):
            state = "active"
        elif any(c["disposition"] in TERMINAL for c in calls):
            state = "completed"
        elif len(calls) >= self.campaigns[cid]["max_attempts"]:
            state = "exhausted"
        elif self.db.execute("SELECT 1 FROM dnd_phones WHERE phone=?", (lead["phone"],)).fetchone():
            state = "dnd_blocked"
        else:
            state = "pending"
            last = None
            if ended:
                c = ended[-1]
                last = {"status": c["status"], "disposition": c["disposition"],
                        "ended_at": dt(c["ended_at"]), "callback_at": dt(c["callback_at"])}
            kind, at = next_attempt(last, self.cfg["retry_policy"])
            ready = at.isoformat() if at else None
        self.db.execute("UPDATE leads SET state=?, kind=?, ready_at=?, request_id=? "
                        "WHERE campaign_id=? AND lead_id=?", (state, kind, ready, rid, cid, lid))

    # ------------------------------------------------------------------ queries
    def pending(self) -> list[Candidate]:
        return [Candidate(r["campaign_id"], r["lead_id"], r["tenant_id"], r["phone"], r["kind"],
                          dt(r["ready_at"]))
                for r in self.db.execute("SELECT * FROM leads WHERE state='pending'")]

    def busy(self) -> tuple[set[str], dict[str, int]]:
        """Phones and per-tenant counts of calls being dialled or active."""
        rows = self.db.execute("SELECT tenant_id, phone FROM leads WHERE state IN ('dialing','active')")
        phones, active = set(), {}
        for r in rows:
            phones.add(r["phone"])
            active[r["tenant_id"]] = active.get(r["tenant_id"], 0) + 1
        return phones, active

    def dnd_phones(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT phone FROM dnd_phones")}

    def dialing_requests(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT request_id FROM leads WHERE state='dialing'")]

    def active_calls(self) -> list[tuple[str, datetime]]:
        return [(r["call_sid"], dt(r["created_at"]))
                for r in self.db.execute("SELECT call_sid, created_at FROM calls WHERE ended_at IS NULL")]

    def all_final(self) -> bool:
        q = "SELECT COUNT(*) FROM leads WHERE state NOT IN ({})".format(",".join("?" * len(FINAL_STATES)))
        return self.db.execute(q, tuple(FINAL_STATES)).fetchone()[0] == 0

    def status(self) -> dict:
        out: dict = {}
        for r in self.db.execute("SELECT campaign_id, state, COUNT(*) n FROM leads GROUP BY 1, 2"):
            out.setdefault(r["campaign_id"], {})[r["state"]] = r["n"]
        return out

    def final_rows(self) -> list[dict]:
        rows = []
        for lead in self.db.execute("SELECT * FROM leads ORDER BY campaign_id, lead_id").fetchall():
            calls = self.db.execute("SELECT * FROM calls WHERE campaign_id=? AND lead_id=?",
                                    (lead["campaign_id"], lead["lead_id"])).fetchall()
            ended = sorted((c for c in calls if c["ended_at"]), key=lambda c: dt(c["ended_at"]))
            last = ended[-1] if ended else None
            rows.append({"campaign_id": lead["campaign_id"], "lead_id": lead["lead_id"],
                         "final_state": lead["state"], "attempts": len(calls),
                         "last_status": last["status"] if last else "",
                         "last_disposition": (last["disposition"] or "") if last else ""})
        return rows

    def write_final_state(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["campaign_id", "lead_id", "final_state", "attempts",
                                              "last_status", "last_disposition"], lineterminator="\n")
            w.writeheader()
            w.writerows(self.final_rows())
        os.replace(tmp, path)
