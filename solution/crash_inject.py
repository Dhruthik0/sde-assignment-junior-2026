"""Run the dispatcher with crashes injected at exact points (os._exit has kill -9 semantics).

Each wrapped step crashes the process with probability PROB, right after it completes:
committing a dial, committing a call/result (before the webhook gets its 2xx), aborting a dial,
and receiving a provider answer before it is recorded. crash_inject.sh restarts until done.

usage: python solution/crash_inject.py DB OUT PROB
"""
import os
import random
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dispatcher import __main__ as entry, store  # noqa: E402

DB, OUT, PROB = sys.argv[1], sys.argv[2], float(sys.argv[3])
CRASH_LOG = os.path.join(os.path.dirname(os.path.abspath(DB)), "crashes.log")


def maybe_die(where: str) -> None:
    if random.random() < PROB:
        with open(CRASH_LOG, "a") as f:
            f.write(where + "\n")
        os._exit(9)


def crash_after(cls, name: str, where: str) -> None:
    orig = getattr(cls, name)

    def wrapped(*a, **kw):
        result = orig(*a, **kw)
        maybe_die(where)
        return result
    setattr(cls, name, wrapped)


crash_after(store.Store, "begin_dial", "after begin_dial commit, before POST")
crash_after(store.Store, "record_call", "after record_call commit (before webhook 2xx / after 201)")
crash_after(store.Store, "abort_dial", "after abort_dial commit")

_post, _get = httpx.AsyncClient.post, httpx.AsyncClient.get


async def post(self, *a, **kw):
    r = await _post(self, *a, **kw)
    maybe_die(f"after provider answered POST {r.status_code}, before recording")
    return r


async def get(self, *a, **kw):
    r = await _get(self, *a, **kw)
    if str(a[0]).startswith("/v1/calls"):
        maybe_die("after lookup/poll answered, before recording")
    return r

httpx.AsyncClient.post, httpx.AsyncClient.get = post, get
sys.argv = ["dispatcher", "--db", DB, "--out", OUT]
entry.main()
