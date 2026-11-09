# Tares enterprise licence, see ee/LICENSE
"""The factory project page's lead and member controls need a licence (TR-1079).

`GET /api/factory/projects/{uid}` says in `people` whether the caller may change the lead
(`can_set_lead`: an admin, or the lead) and add a member (`can_add`: the lead, or the crew's own
key). Both routes behind those flags answer 402 without an enterprise licence, so the console must
not offer them: without a licence, or with one that has no enterprise feature, both flags are false
for everyone, the lead and an admin too. With a licence they are as they were: true for an admin
or the lead, false for a person who is neither, and the list of people to pick from stays empty
for someone who cannot change anything.

In process, no ports: the licence is stored through PUT /api/licence.

Run: .venv/bin/python ee/tests/test_factory_project_controls.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

_TMP = tempfile.mkdtemp(prefix="tares-factory-project-controls-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-project-controls"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    keys = {}

    async def call(who, method, path, body=None):
        hdr = root if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body)

    async with app.router.lifespan_context(app):
        # adding a person needs a licence, so set the cell up licensed and take the licence away after
        r = await call("root", "PUT", "/api/licence", {"key": licensed.test_licence()})
        assert r.status_code == 200, r.text
        for who, scopes in (("lead", ["admin"]), ("admin", ["admin"]), ("reader", ["read"])):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": scopes,
                "person": {"email": f"{who}@example.com", "name": who.capitalize()}})
            assert r.status_code < 300, r.text
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await call("lead", "POST", "/api/projects", {"template": "custom", "name": "Atlas",
                                                         "objects": [], "kind": "software_factory"})
        assert r.status_code < 300, r.text
        uid = r.json()["id"]

        async def people(who):
            r = await call(who, "GET", f"/api/factory/projects/{uid}")
            assert r.status_code == 200, (who, r.status_code, r.text[:200])
            return r.json()["people"]

        def flags(p):
            return p["can_set_lead"], p["can_add"], len(p["people"])

        ck("the project has a lead, the creator", (await people("root"))["lead"] is not None)
        r = await call("root", "DELETE", "/api/licence")
        assert r.status_code in (200, 204), r.text

        for why in ("no licence", "a licence without the enterprise feature"):
            if why != "no licence":
                r = await call("root", "PUT", "/api/licence",
                               {"key": licensed.test_licence(features=("other",))})
                ck("the licence is stored", r.status_code == 200, r.text[:200])
            print(f"== {why} ==")
            for who in ("lead", "admin", "root", "reader"):
                p = await people(who)
                ck(f"{who}: no change-the-lead control, no add-a-member control",
                   p["can_set_lead"] is False and p["can_add"] is False, str(flags(p)))
                ck(f"{who}: nobody to pick from either (TR-930 F2)", len(p["people"]) == 0, str(flags(p)))
            ck("the lead is still named, the page still reads", (await people("lead"))["lead"]["name"] == "Lead")

        print("== licensed ==")
        r = await call("root", "PUT", "/api/licence", {"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        p = await people("lead")
        ck("the lead may change the lead and add a member", p["can_set_lead"] is True and p["can_add"] is True,
           str(flags(p)))
        ck("and has people to pick from", len(p["people"]) >= 3, str(flags(p)))
        p = await people("admin")
        ck("an admin who is not the lead may change the lead, not add a member",
           p["can_set_lead"] is True and p["can_add"] is False, str(flags(p)))
        p = await people("root")
        ck("the crew's own key may change the lead and add a member", p["can_set_lead"] is True
           and p["can_add"] is True, str(flags(p)))
        p = await people("reader")
        ck("a read key may do neither and sees nobody to pick", flags(p) == (False, False, 0), str(flags(p)))

        print("== a valid key on a cell without the ee package (TR-930 F1) ==")
        from tares import team as team_mod
        real_ee = team_mod._ee
        team_mod._ee = lambda: None
        try:
            for who in ("lead", "admin", "root"):
                p = await people(who)
                ck(f"{who}: the key alone does not offer the controls",
                   p["can_set_lead"] is False and p["can_add"] is False and not p["people"], str(flags(p)))
        finally:
            team_mod._ee = real_ee
        p = await people("lead")
        ck("with the package back the controls return", p["can_set_lead"] is True and p["can_add"] is True,
           str(flags(p)))

        print("== the licence goes ==")
        r = await call("root", "DELETE", "/api/licence")
        ck("the licence is removed", r.status_code in (200, 204), r.text[:200])
        p = await people("lead")
        ck("the lead's controls are gone with it", p["can_set_lead"] is False and p["can_add"] is False, str(flags(p)))


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
