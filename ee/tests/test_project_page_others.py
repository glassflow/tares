# Tares enterprise licence, see ee/LICENSE
"""On a team cell the project page keeps the viewer's waiting items apart from other people's.

A question the crew put on the desk for Adam showed on Ashish's view of the project as "Question for
you", with an Answer button the cell would refuse him. Now:

  - for Ashish it is under `waiting_others`, labelled "Question for Adam", with nothing to press, and
    not in his `waiting`;
  - for Adam it is in `waiting`, as his;
  - a permission request for Adam is "Permission for Adam" for Ashish;
  - an item addressed to Ashish stays his;
  - on a cell of one nothing moves.

Run: .venv/bin/python ee/tests/test_project_page_others.py
"""
import asyncio
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-project-page-others-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-project-page-others"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

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
    mk = lambda h=None: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                          headers=h or root)
    async with app.router.lifespan_context(app):
        keys, pid = {}, {}
        async with mk() as cx:
            for who in ("ashish", "adam"):
                r = await cx.post("/api/keys", json={"name": f"{who}-laptop", "scopes": ["admin"],
                                                     "person": {"email": f"{who}@example.com", "name": who.capitalize()}})
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
                pid[who] = r.json()["person"]["id"]
        async with mk(keys["ashish"]) as cx:
            uid = (await cx.post("/api/projects", json={"template": "custom", "name": "brain", "objects": []})).json()["id"]
        store.set_project_kind(uid, "software_factory")
        store.add_project_member(uid, pid["adam"], added_by="ashish")
        async with mk() as cx:
            for body in ({"question": "Give the crew Docker and Rust?", "recommendation": "Restart the crew",
                          "why": "the lore tickets need them", "project": uid, "to": "adam@example.com"},
                         {"question": "builder was refused a command. Allow it?", "recommendation": "Allow it",
                          "why": "it needs it", "project": uid, "station": "brain-build-m1",
                          "command": "cargo test", "to": "adam@example.com"},
                         {"question": "Which colour for the links?", "recommendation": "blue", "why": "it reads best",
                          "project": uid, "to": "ashish@example.com"}):
                r = await cx.post("/api/desk", json=body)
                ck(f"the desk takes: {body['question'][:30]}", r.status_code in (200, 201), r.text)

        async def page(who):
            async with mk(keys[who]) as cx:
                r = await cx.get(f"/api/factory/projects/{uid}")
                assert r.status_code == 200, r.text
                return r.json()

        a = await page("ashish")
        mine = [w["detail"] for w in a["waiting"]]
        others = {w["label"]: w for w in a["waiting_others"]}
        ck("Ashish's own question stays his", any("colour" in d for d in mine), mine)
        ck("Adam's question is not in Ashish's waiting", not any("Docker" in d for d in mine), mine)
        ck("it is under waiting_others, named for Adam, with nothing to press",
           "Question for Adam" in others and "Docker" in others["Question for Adam"]["detail"]
           and others["Question for Adam"]["actions"] == [], list(others))
        ck("a permission request for Adam reads Permission for Adam",
           "Permission for Adam" in others and others["Permission for Adam"]["actions"] == [], list(others))
        ck("each names the person it is for", others["Question for Adam"]["for_person"]["id"] == pid["adam"])

        d = await page("adam")
        ck("for Adam the same question is his own", any("Docker" in w["detail"] for w in d["waiting"])
           and not any("Docker" in w["detail"] for w in d["waiting_others"]), [w["detail"] for w in d["waiting"]])
        ck("and Ashish's question is Ashish's", any(w["label"] == "Question for Ashish" for w in d["waiting_others"]),
           [w["label"] for w in d["waiting_others"]])

    print(f"{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
