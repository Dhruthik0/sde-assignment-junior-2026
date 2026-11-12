"""Run the dispatcher:  python -m dispatcher [--provider URL] [--port 8200] [--db dispatcher.db]"""
import argparse
import asyncio
import fcntl
import json
import logging
import os
import sys

import uvicorn

from .service import Dispatcher, create_app

# Defaults are anchored to solution/, not the working directory, so a restart from a different
# directory resumes the same DB instead of silently starting a fresh one.
SOLUTION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FIXTURES = os.path.join(SOLUTION_DIR, "..", "assignment-b-workflow", "fixtures", "campaigns.json")


def main():
    ap = argparse.ArgumentParser(description="Durable campaign dispatcher")
    ap.add_argument("--fixtures", default=DEFAULT_FIXTURES)
    ap.add_argument("--provider", default="http://127.0.0.1:8099")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--db", default=os.path.join(SOLUTION_DIR, "dispatcher.db"))
    ap.add_argument("--out", default=os.path.join(SOLUTION_DIR, "final_state.csv"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # One dispatcher per DB: two would each fill the same free tenant slots (R2). The kernel drops
    # the lock when the process dies, even on kill -9, so a restart never finds a stale lock.
    lock = open(a.db + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.error("another dispatcher is already running on %s; exiting", a.db)
        sys.exit(2)

    with open(a.fixtures) as f:
        cfg = json.load(f)
    d = Dispatcher(cfg, os.path.dirname(os.path.abspath(a.fixtures)), a.db, a.provider,
                   f"http://{a.host}:{a.port}/webhooks/telephony", a.out)
    server = uvicorn.Server(uvicorn.Config(create_app(d), host=a.host, port=a.port, log_level="warning"))

    async def serve():
        stopper = asyncio.create_task(stop_when_done())
        await server.serve()
        stopper.cancel()

    async def stop_when_done():
        await d.done.wait()
        server.should_exit = True

    asyncio.run(serve())
    sys.exit(1 if d.failed else 0)


if __name__ == "__main__":
    main()
