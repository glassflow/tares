"""A project kept for later: off the home, under Later in All projects, and back on a start.

A daemon with auth on, a cell of one person. A specced project is Ready to start with a Later
action; POST /api/projects/{id}/later takes it off the home (the card, the project rows), counts it
in `later_count`, lists it as `later` on the projects page and on its own page; later=false or a
hand-over starts it again. A project the crew has, or a finished one, cannot be kept for later.
The set_later tool does the same.

Run: .venv/bin/python tests/test_project_later.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-later-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-later"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory_home as fh

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=root)
    m._cx = mf._cx = mk

    async def call(method, path, body=None, **params):
        async with mk() as cx:
            return await cx.request(method, path, json=body, params=params or None)

    async def home():
        fh._cache.clear()
        r = await call("GET", "/api/home")
        assert r.status_code == 200, r.text
        return r.json()

    async with app.router.lifespan_context(app):
        r = await call("POST", "/api/projects", {"template": "custom", "name": "backlog-idea",
                                                 "objects": [], "kind": "software_factory"})
        uid = r.json()["id"]
        r = await call("POST", f"/api/projects/{uid}/tickets", {"title": "First part", "repo": "acme/web"})
        ck("a ticket", r.status_code == 201, r.text)
        r = await call("POST", f"/api/projects/{uid}/docs", {"kind": "spec", "title": "Spec", "body": "the idea"})
        ck("a spec", r.status_code < 300, r.text)

        print("== a specced project is Ready to start, with Later on its card ==")
        h = await home()
        card = next((i for i in h["waiting_for_you"] if i["id"] == f"ready_to_start:{uid}"), {})
        ck("Ready to start", card.get("kind") == "ready_to_start", h["waiting_for_you"])
        ck("its Later action names the project", {"label": "Later", "primary": False, "do": "later",
                                                  "project": uid} in card.get("actions", []), card)
        ck("later is a verb the home knows", "later" in fh.ACTIONS)
        ck("and it is a project row", uid in [p["id"] for p in h["projects"]])

        print("== Later takes it off the home ==")
        r = await call("POST", f"/api/projects/{uid}/later", {"later": True})
        ck("kept for later", r.status_code == 200 and r.json() == {"later": True}, r.text)
        ck("its status is later", store.get_project(uid)["status"] == "later")
        h = await home()
        ck("no Ready to start card", f"ready_to_start:{uid}" not in {i["id"] for i in h["waiting_for_you"]})
        ck("no project row", uid not in [p["id"] for p in h["projects"]])
        ck("counted apart, not as finished", h["later_count"] == 1 and h["finished_count"] == 0,
           (h["later_count"], h["finished_count"]))
        r = await call("POST", f"/api/projects/{uid}/later", {"later": True})
        ck("asking again changes nothing", r.status_code == 200 and store.get_project(uid)["status"] == "later")

        print("== it is under Later in All projects, and on its page ==")
        r = await call("GET", "/api/factory/projects")
        row = next(p for p in r.json()["projects"] if p["id"] == uid)
        ck("state later, labelled Later, kept for later", row["state"] == "later" and row["state_label"] == "Later"
           and row["meta"].startswith("Kept for later"), row)
        ck("counted under later", r.json()["counts"]["later"] == 1, r.json()["counts"])
        r = await call("GET", f"/api/factory/projects/{uid}")
        d = r.json()
        ck("the page says Later, with Start now and no Later", d["state"] == "later" and d["state_label"] == "Later"
           and d["later"] is True and d["can_later"] is False, {k: d.get(k) for k in ("state", "later", "can_later")})

        print("== Start now brings it back ==")
        r = await call("POST", f"/api/projects/{uid}/later", {"later": False})
        ck("started", r.status_code == 200 and store.get_project(uid)["status"] == "active", r.text)
        h = await home()
        ck("Ready to start again, and a row", f"ready_to_start:{uid}" in {i["id"] for i in h["waiting_for_you"]}
           and uid in [p["id"] for p in h["projects"]] and h["later_count"] == 0)
        d = (await call("GET", f"/api/factory/projects/{uid}")).json()
        ck("its page offers Later again", d["later"] is False and d["can_later"] is True)

        print("== the set_later tool, then the hand-over starts it ==")
        out = await mf.set_later(project="backlog-idea")
        ck("set_later keeps it for later", out.startswith("Kept for later")
           and store.get_project(uid)["status"] == "later", out)
        r = await call("POST", f"/api/projects/{uid}/handover",
                       {"repos": [{"path": os.path.join(_TMP, "r"), "name": "acme/web"}]})
        ck("handed over", r.status_code == 200, r.text)
        ck("the hand-over starts it", store.get_project(uid)["status"] == "active")

        print("== what cannot be kept for later ==")
        r = await call("POST", f"/api/projects/{uid}/later", {"later": True})
        ck("a project the crew has: 409, with the way out", r.status_code == 409 and "take it back" in r.text, r.text)
        d = (await call("GET", f"/api/factory/projects/{uid}")).json()
        ck("and its page offers no Later", d["can_later"] is False)
        store.update_project(uid, status="done")
        r = await call("POST", f"/api/projects/{uid}/later", {"later": True})
        ck("a finished project: 409", r.status_code == 409 and "finished" in r.text, r.text)
        r = await call("POST", "/api/projects/uc_nope/later", {"later": True})
        ck("an unknown project: 404", r.status_code == 404, r.text)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
