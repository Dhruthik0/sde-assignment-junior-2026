"""Run the dispatcher:  python -m dispatcher [--provider URL] [--port 8200] [--db dispatcher.db]"""
import argparse
import asyncio
import json
import logging
import os

import uvicorn

from .service import Dispatcher, create_app

DEFAULT_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "assignment-b-workflow", "fixtures", "campaigns.json")


def main():
    ap = argparse.ArgumentParser(description="Durable campaign dispatcher")
    ap.add_argument("--fixtures", default=DEFAULT_FIXTURES)
    ap.add_argument("--provider", default="http://127.0.0.1:8099")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--db", default="dispatcher.db")
    ap.add_argument("--out", default="final_state.csv")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

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


if __name__ == "__main__":
    main()
