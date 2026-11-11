#!/usr/bin/env python3
"""The Tares side of the pace, for the `pace_daemon` scenario of parity.py: pace_test.sh feeds a real Tares
fake /usage readings and asks it what the pace is. The crew engines do not take part (the harness runs this on
both sides and compares the two printouts, which must be the same); the daemon is the real one, run in this
process on a throwaway database.

    pace_daemon.py <bash|python>

Prints one JSON object per line, in the order of the bash suite: first {"settings": ..., "pace": ...} for the
crew settings the way they start, then {"set": ...} once the pace is switched on, then one {"plan": [session,
weekly, seconds until the window resets], "pace": <GET /api/crew/pace, without the note and the reset time>} for each reading, and a last one for a
reading with no limit in it. Standard library, httpx and the package itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="tares-pace-daemon-")
os.environ["TARES_DB"] = os.path.join(TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-pace"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

# the readings of pace_test.sh, in its order: session %, weekly %, seconds until the window resets (negative: it has)
READINGS = [(84, 89, 10800), (85, 89, 10800), (10, 10, 10800), (10, 89, 10800), (10, 90, 10800), (94, 90, 10800),
            (95, 90, 10800), (100, 10, 10800), (100, 10, -60)]


def iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_line(session: int, weekly: int, reset_in: int, nonce: int) -> dict:
    """What `plan` of pace_test.sh posts: the two limits /usage reads, as a claude_plan line."""
    return {"type": "claude_plan", "timestamp": iso(0), "nonce": nonce, "usage_report": {"rate_limits": {"limits": [
        {"kind": "session", "percent": session, "resets_at": iso(reset_in), "scope": None, "is_active": True},
        {"kind": "weekly_all", "percent": weekly, "resets_at": iso(259200), "scope": None, "is_active": False}]}}}


async def main() -> int:
    import httpx
    sys.path.insert(0, ROOT)
    from tares.daemon import make_app
    app = make_app()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    mk = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)

    def show(**doc) -> None:
        if "pace" in doc:  # what the reading says; the note and the reset time hold the clock, which differs between two runs
            p = doc["pace"]
            doc["pace"] = {k: p.get(k) for k in ("on", "known", "level", "at_limit", "state", "session_pct", "weekly_pct")}
            doc["pace"]["until"] = bool(p.get("until"))
        print(json.dumps(doc, sort_keys=True))

    async def push(line: dict) -> None:
        async with mk() as cx:
            r = await cx.post("/ingest/claude_code", content=json.dumps(line) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})
            assert r.status_code < 300, r.text

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            assert r.status_code < 300, r.text
            show(settings=(await cx.get("/api/crew/settings")).json().get("pace", "missing"),
                 pace=(await cx.get("/api/crew/pace")).json())
            await cx.put("/api/crew/settings", json={"pace": True})
            show(set=(await cx.get("/api/crew/settings")).json().get("pace", "missing"))
        for n, (s, w, reset_in) in enumerate(READINGS):
            await push(plan_line(s, w, reset_in, n))
            async with mk() as cx:
                show(plan=[s, w, reset_in], pace=(await cx.get("/api/crew/pace")).json())
        # a reading with no limit in it: the newest reading wins, and it knows nothing
        await push({"type": "claude_plan", "timestamp": iso(0), "nonce": 99, "usage_report": {"rate_limits": {"limits": []}}})
        async with mk() as cx:
            show(plan=None, pace=(await cx.get("/api/crew/pace")).json())
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
