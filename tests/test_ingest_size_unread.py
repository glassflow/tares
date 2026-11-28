"""When the store's size cannot be read (Postgres restarting, TR-1815 review), ingest does not
answer 500: the size check lets the batch through to the store, whose own refusal is a 503 the
sender retries on, and /health says the size is unavailable instead of failing.

In process, on the daemon's store: `deps.storage` is made to raise, a batch of session lines is
posted and stored (202), and /health answers 200 with status degraded. With the size readable
again, /health is ok.

Run: .venv/bin/python tests/test_ingest_size_unread.py (TARES_TEST_PG and PYTHONPATH=tests/pg_harness
for Postgres)
"""
import asyncio
import json
import logging
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

os.environ["TARES_HOME"] = tempfile.mkdtemp(prefix="tares-size-unread-")
os.environ.pop("TARES_AUTH_TOKEN", None)
os.environ.pop("TARES_MAX_DB_SIZE", None)

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {str(detail)[:300]}"))


def lines(session, n):
    return "\n".join(json.dumps({
        "sessionId": session, "type": "assistant", "cwd": "/w", "uuid": f"{session}-{i}",
        "timestamp": "2026-11-27T10:00:00Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": f"m{i}"}]}})
        for i in range(n)) + "\n"


async def main():
    from tares.daemon import make_app
    app = make_app()
    deps = app.state.deps
    readable = deps.storage

    def unreadable():
        raise RuntimeError("the database is restarting")

    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
        deps.storage = unreadable
        r = await cx.post("/ingest/claude_code", content=lines("s1", 3),
                          headers={"content-type": "application/x-ndjson"})
        ck("the size unreadable: the batch is still stored (202, not 500)", r.status_code == 202, r.text)
        n = app.state.store.usage().get("events")
        ck("its three lines are in the store", n == 3, n)
        h = await cx.get("/health")
        ck("/health answers 200, degraded, and says why", h.status_code == 200
           and h.json()["status"] == "degraded" and "storage size unavailable" in (h.json().get("detail") or ""),
           h.text)
        deps.storage = readable
        h = await cx.get("/health")
        ck("the size readable again: /health is ok", h.json()["status"] == "ok", h.text)

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
