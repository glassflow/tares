"""Factory Settings, the Crew page and the home card read the setting the save writes (TR-893).

A signed-in owner (an admin key that names a person) saves the crew's settings as their own, not
as the cell's default. Every screen that shows those settings must read the same scope, or the
control snaps back after a reload. The owner changes how far the crew may go, the number of
builders, the challenger and pacing; each of the three screens shows the new value on the next
read. The cell's own credential still sets the default, which a second person who set nothing sees.

Run: .venv/bin/python tests/test_factory_crew_scope.py
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _env import scrub_os_environ
scrub_os_environ()
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="tares-factory-crew-scope-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-scope"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

P = F = 0
MERGES = "The crew merges"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    keys = {}

    async def call(who, method, path, body=None):
        hdr = {"Authorization": f"Bearer {TOKEN}"} if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body)

    async with app.router.lifespan_context(app):
        for who in ("alex", "kim"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-cs@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
        store = app.state.store
        store.create_project("prj-x", "software_factory", "Project X", {}, goal="Build X")

        async def seen(who):
            """What the three screens show, for this person, right now."""
            s = (await call(who, "GET", "/api/factory/settings")).json()["crew"]
            c = (await call(who, "GET", "/api/factory/crew")).json()["how_far"]
            hm = (await call(who, "GET", "/api/home")).json()["crew"]["how_far"]
            rv = (await call(who, "GET", "/api/factory/reviews")).json()["autonomy_text"]
            return {"reviews": rv, "settings": (s["how_far"]["value"], s["builders_max"], s["challenger"], s["pace"]),
                    "crew": c, "home": hm}

        print("== before: the cell's default ==")
        before = await seen("alex")
        ck("the owner starts on the cell's default", before["settings"][0] != "L4-ship", str(before))

        print("== the owner saves their own ==")
        r = await call("alex", "PUT", "/api/crew/settings", {"autonomy": "L4-ship", "builders_max": 5,
                                                              "challenger": True, "pace": True})
        ck("the save answers", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        ck("and stores a personal setting, not the cell's default",
           (await call("root", "GET", "/api/crew/settings")).json()["autonomy"] != "L4-ship")
        now = await seen("alex")
        ck("Settings shows the saved value after a reload", now["settings"] == ("L4-ship", 5, True, True),
           str(now["settings"]))
        ck("the Crew page shows it", MERGES in now["crew"]["text"] and "5 builders" in now["crew"]["detail"],
           str(now["crew"]))
        ck("the home card shows it", now["home"]["autonomy"] == "L4-ship", str(now["home"]))
        ck("the Reviews page says who merges", MERGES in now["reviews"], now["reviews"])

        print("== another person and the cell are unchanged ==")
        other = await seen("kim")
        ck("a second person sees the cell's default", other["settings"][0] != "L4-ship", str(other))
        ck("on every screen", other["home"]["autonomy"] != "L4-ship"
           and MERGES not in other["crew"]["text"] and MERGES not in other["reviews"], str(other))

        print("== the cell's own credential sets the default ==")
        r = await call("root", "PUT", "/api/crew/settings", {"autonomy": "L3-review"})
        ck("the default saves", r.status_code == 200, r.text[:200])
        root = await seen("root")
        ck("and shows on every screen for it", root["settings"][0] == "L3-review"
           and root["home"]["autonomy"] == "L3-review" and MERGES not in root["crew"]["text"], str(root))

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
