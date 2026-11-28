# Tares enterprise licence, see ee/LICENSE
"""The kept Crew page on a cell with two people (TR-1630): who may see what is never decided by a
body kept from before.

Each person reads their own page. Putting someone on a private project, taking them off, opening or
closing the project, and adding, changing or removing a person all show on the very next read.

Run: .venv/bin/python ee/tests/test_factory_crew_cache_team.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-factory-crew-cache-team-")
os.environ["TARES_HOME"] = os.path.join(TMP, "t")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-cache-team"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import factory_home as fh

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(seconds_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def main():
    from tares.daemon import make_app
    app = make_app()
    keys, pids = {"root": {"Authorization": f"Bearer {TOKEN}"}}, {}

    async def call(who, method, path, body=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=keys[who]) as cx:
            return await cx.request(method, path, json=body)

    async def send(who, method, path, body=None):
        r = await call(who, method, path, body)
        assert r.status_code < 300, f"{who} {method} {path}: {r.status_code} {r.text}"
        return r.json()

    async def page(who):
        return await send(who, "GET", "/api/factory/crew")

    async with app.router.lifespan_context(app):
        for who in ("alex", "kim"):
            r = await send("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"] if who == "alex" else ["read"],
                "person": {"email": f"{who}-cc@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r['secret']}"}
            pids[who] = r["person"]["id"]
        store = app.state.store
        store.create_project("prj-q", "Project Quiet", goal="Build it")
        store.set_project_kind("prj-q", "software_factory")
        store.set_project_lead("prj-q", pids["alex"])
        store.set_project_scope("prj-q", "private")
        lines = [{"type": "session_station", "sessionId": "q1", "cwd": "/w/crew", "timestamp": ts(60),
                  "station": "quiet-build", "role": "builder", "parent": "", "factory_station": "quiet-build",
                  "tares_project": "prj-q"},
                 {"type": "session_state", "sessionId": "q1", "cwd": "/w/crew", "timestamp": ts(5),
                  "state": "working", "reason": ""}]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=keys["alex"]) as cx:
            r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})
        assert r.status_code < 300, r.text

        def sees(body):
            return "Project Quiet" in json.dumps(body)

        print("== each person their own page ==")
        fh.clear_cache()
        a, k = await page("alex"), await page("kim")
        ck("the lead sees the private project", sees(a), a["builders"])
        ck("someone not on it, reading right after, does not", not sees(k), k["builders"])
        ck("and both read their own again", sees(await page("alex")) and not sees(await page("kim")))

        print("== a change of who sees it shows on the next read ==")

        async def after(label, who, write, was, is_now):
            fh.clear_cache()
            before = sees(await page(who))
            await write()
            now = sees(await page(who))
            ck(label, (before, now) == (was, is_now), (before, now))

        await after("put on the project", "kim", lambda: send(
            "alex", "POST", "/api/projects/prj-q/members", {"person_id": pids["kim"]}), False, True)
        await after("taken off it", "kim", lambda: send(
            "alex", "DELETE", f"/api/projects/prj-q/members/{pids['kim']}"), True, False)
        await after("the project opened to the team", "kim", lambda: send(
            "alex", "PATCH", "/api/projects/prj-q", {"scope": "team"}), False, True)
        await after("and made private again", "kim", lambda: send(
            "alex", "PATCH", "/api/projects/prj-q", {"scope": "private"}), True, False)

        print("== a change to the people drops the kept pages ==")

        async def drops(label, write):
            fh.clear_cache()
            await page("alex")
            held = bool(fh._cache)
            out = await write()
            ck(label, held and not fh._cache, (held, list(fh._cache)))
            return out

        await drops("someone added from the People page", lambda: send(
            "root", "POST", "/api/factory/people", {"email": "sam-cc@example.com"}))
        lee = await drops("someone added through the people route", lambda: send(
            "root", "POST", "/api/people", {"name": "Lee", "email": "lee-cc@example.com"}))
        await drops("a person changed", lambda: send(
            "root", "PATCH", f"/api/people/{lee['id']}", {"is_admin": True}))
        await drops("a person removed", lambda: send("root", "DELETE", f"/api/people/{lee['id']}"))


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
