# Tares enterprise licence, see ee/LICENSE
"""Refused team actions on a Tares Cloud cell, with the ee team installed (TR-1088).

The control plane sets TARES_WORKSPACE_API_URL on a cloud cell. Whatever the reason a team action
is refused (no key, a key without the enterprise feature, a key that ended, every seat used), the
402 carries the same plain words: no licence, key or enterprise, and no docs link. The check
does not change: each refusal still happens and writes nothing. A self-hosted cell keeps each
reason's own words and the link.

In process, no ports: the key is stored through PUT /api/licence.

Run: .venv/bin/python ee/tests/test_licence_cloud.py
"""
import asyncio
import os
import re
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-licence-cloud-ee-")
ROOT = "root-token-licence-cloud-ee"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.licence as L
from tares import team as team_mod
from tares_ee import areas as areas_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


H = {"Authorization": f"Bearer {ROOT}"}
WORDS = re.compile(r"licen[cs]e|enterprise|\bkey\b", re.I)
DOCS = "https://docs.glassflow.ai/tares/enterprise"
CLOUD_URL = "https://workspace-api.example.test"


def cloud(on):
    if on:
        os.environ["TARES_WORKSPACE_API_URL"] = CLOUD_URL
    else:
        os.environ.pop("TARES_WORKSPACE_API_URL", None)


def plain_402(r):
    """The cloud refusal: 402, the three keys, the plain words, no link, no licence wording."""
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return (r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"
            and b["message"] == L.CLOUD_WORDS and b["url"] == "" and not WORDS.search(b["message"])
            and DOCS not in r.text)


def own_402(r, words):
    """The self-hosted refusal: the reason's own words and the enterprise page."""
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return r.status_code == 402 and b.get("message") == words and b.get("url") == DOCS


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H)
        store = app.state.store
        uid = (await cx.post("/api/projects", json={"template": "custom", "name": "Shop", "objects": []})).json()["id"]
        pid = store.create_person("Pia", "pia@x.dev")["id"]

        async def licence(text):
            r = await cx.put("/api/licence", json={"key": text})
            ck("the key is stored", r.status_code == 200, r.text[:200])

        n = {"i": 0}

        async def add_person():
            n["i"] += 1
            return await cx.post("/api/people", json={"name": f"New {n['i']}", "email": f"new{n['i']}@x.dev"})

        async def refused_everywhere(why, add_words, route_words):
            """Each refused team action, on a cloud cell and then on a self-hosted one."""
            people = len(store.list_people())
            for on in (True, False):
                cloud(on)
                kind = "cloud" if on else "self-hosted"
                r = await add_person()
                ck(f"{why}, {kind}: adding a person is refused with "
                   + ("the plain words" if on else "its own words and the link"),
                   plain_402(r) if on else own_402(r, add_words), f"{r.status_code} {r.text[:160]}")
                ck(f"{why}, {kind}: nobody was added", len(store.list_people()) == people)
                if route_words:
                    r = await cx.put("/api/areas/acme/web", json={"person_id": "local"})
                    ck(f"{why}, {kind}: setting an area owner is refused with "
                       + ("the plain words" if on else "its own words and the link"),
                       plain_402(r) if on else own_402(r, route_words), f"{r.status_code} {r.text[:160]}")
                    r = await cx.patch(f"/api/projects/{uid}", json={"lead_person_id": pid})
                    ck(f"{why}, {kind}: handing a project to a lead is refused the same way",
                       plain_402(r) if on else r.status_code == 402 and r.json().get("url") == DOCS,
                       f"{r.status_code} {r.text[:160]}")
                    ck(f"{why}, {kind}: nothing was saved", areas_mod.load(store) == [])
            cloud(True)

        print("== a cloud cell with no key ==")
        cloud(True)
        ck("no key: the team is still the single-person one", type(team_mod.get()).__name__ == "SingleTeam")
        await refused_everywhere("no key", team_mod.ADD_PERSON_REASON, "Repo owners need an enterprise licence.")

        print("== a key without the enterprise feature ==")
        await licence(licensed.test_licence(features=("other",)))
        await refused_everywhere("no enterprise feature", team_mod.ADD_PERSON_REASON,
                                 "Repo owners need an enterprise licence.")

        print("== a key that ended ==")
        gone = (date.today() - timedelta(days=3)).isoformat()
        await licence(licensed.test_licence(expires=gone))
        st = (await cx.get("/api/licence")).json()
        ck("ended but still valid for ee", st["valid"] and st["expired"] and st["ee_allowed"], str(st))
        from tares_ee.team import EXPIRED_REASON
        await refused_everywhere("ended key", EXPIRED_REASON, None)

        print("== every seat used ==")
        await licence(licensed.test_licence(seats=1))
        st = (await cx.get("/api/licence")).json()
        ck("one seat, more people on the cell", st["ee_allowed"] and st["used"] >= 1, str(st))
        people = store.list_people()
        seat_words = (f"Every seat of the enterprise licence is in use ({st['used']} of 1). "
                      "Ask for more seats to add people.")
        await refused_everywhere("seats used", seat_words, None)

        print("== a key with a free seat still adds a person ==")
        await licence(licensed.test_licence(seats=50))
        for on in (True, False):
            cloud(on)
            r = await add_person()
            ck(f"licensed, {'cloud' if on else 'self-hosted'}: adding a person works (201)", r.status_code == 201,
               f"{r.status_code} {r.text[:160]}")

        await cx.aclose()


asyncio.run(main())
cloud(False)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
