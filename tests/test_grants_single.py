"""Core with no licence: grants work for one person, and the waiting is ee (TR-922).

Team, project and personal grants are core. The owner is admin and lead, so each one is in effect
at once and the grants docs hold it. What needs a team is a grant that waits for someone else to
agree: `areas` answers 402 `licence_required`, the consent and decline routes answer 402 (they are
FALLBACKS rows), `waiting_for_me` is empty, and the MCP tool `consent_grant` is not listed.

A grant that was already waiting (a team cell left it so) is a permission someone has not agreed to.
Without ee it stays waiting, it is never copied into the grants docs, and the only thing that works
on it is a revoke.

The licensed behaviour of the same routes and tool is ee/tests/test_grants_teams.py and
ee/tests/test_grant_decline_sticks.py.

Run: .venv/bin/python tests/test_grants_single.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-grants-single-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = AUTH = "root-token-grants-single"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import team as team_mod

team_mod._set_ee_for_tests(None)   # no ee team, whatever is installed on this machine

from tares import mcp_server as ms
from tares.daemon import make_app

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def exact_402(r, text):
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return (r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"
            and text in b["message"] and "enterprise licence" in b["message"] and "—" not in b["message"])


CONSENT = "Agreeing to someone else's grant needs an enterprise licence."
AREAS = "Grants that wait for an area owner need an enterprise licence."


async def routes():
    print("== grants of one person take effect at once ==")
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(AUTH)) as cx:
        store = app.state.store
        # the owner: an admin key with no person acts as `local`, the one person on the cell. The
        # system credential has no person, so a personal grant needs this key.
        r = await cx.post("/api/keys", json={"name": "owner", "scopes": ["admin"]})
        cx.headers.update(H(r.json().get("secret", "")))
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Shop", "objects": []})
        uid = r.json().get("id")
        ck("a project is made", bool(uid), f"{r.status_code} {r.text[:100]}")

        def row(r, what):
            g = (r.json() or {}).get("grant") or {}
            ck(f"{what}: 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
            return g

        g = row(await cx.post("/api/grants", json={"kind": "team", "words": "run the tests"}), "a team grant")
        ck("the team grant is in effect at once", g.get("state") == "active" and g.get("in_effect") is True
           and g.get("waiting_for") == [], str(g))
        g = row(await cx.post("/api/grants", json={"kind": "project", "project": uid, "words": "deploy to staging"}),
                "a project grant")
        ck("the project grant is in effect at once", g.get("state") == "active" and g.get("in_effect") is True
           and g.get("waiting_for") == [], str(g))
        g = row(await cx.post("/api/grants", json={"kind": "personal", "words": "use my laptop"}), "a personal grant")
        ck("the personal grant is in effect at once", g.get("state") == "active" and g.get("in_effect") is True
           and g.get("waiting_for") == [], str(g))

        d = (await cx.get("/api/grants", params={"project": uid})).json()
        ck("the team grant is in the shared grants doc", "run the tests" in d["all"], d["all"])
        ck("the project grant is in the project's grants doc", "deploy to staging" in d["project"], d["project"])
        ck("the personal grant is in neither doc", "use my laptop" not in d["all"] + d["project"])
        ck("all three are listed, in effect",
           sorted(x["words"] for x in d["grants"] if x["in_effect"]) == ["deploy to staging", "run the tests", "use my laptop"],
           str([(x["words"], x["state"]) for x in d["grants"]]))
        ck("nothing waits for the caller", d["waiting_for_me"] == [], str(d["waiting_for_me"]))

        print("== naming areas needs a licence ==")
        r = await cx.post("/api/grants", json={"kind": "project", "project": uid, "words": "touch acme/web",
                                               "areas": ["acme/web"]})
        ck("areas: 402 licence_required with {code, message, url}", exact_402(r, AREAS), f"{r.status_code} {r.text[:160]}")
        ck("... and nothing was written", not any(g["words"] == "touch acme/web" for g in store.all_grant_rows()))
        r = await cx.post("/api/grants", json={"kind": "project", "project": uid, "words": "touch nothing", "areas": []})
        ck("an empty areas list is just a grant (201)", r.status_code == 201, f"{r.status_code} {r.text[:100]}")

        print("== a grant that was left waiting stays waiting ==")
        left = store.add_grant_row("project", "ship acme/web fixes", project=uid, given_by="local",
                                   areas=["acme/web"], state="waiting", needs_lead=True)
        gid = left["id"]
        d = (await cx.get("/api/grants", params={"project": uid})).json()
        again = next((x for x in d["grants"] if x["id"] == gid), None)
        ck("GET /api/grants lists it, still waiting and not in effect",
           again is not None and again["state"] == "waiting" and again["in_effect"] is False, str(again))
        ck("it is waiting for no one the console could name", again is not None and again["waiting_for"] == [], str(again))
        ck("the store row is still waiting", store.get_grant_row(gid)["state"] == "waiting")
        ck("it is not in the project's grants doc", "ship acme/web fixes" not in d["project"], d["project"])
        ck("it is not in the shared grants doc", "ship acme/web fixes" not in d["all"])
        ck("it is not waiting for the caller either", d["waiting_for_me"] == [], str(d["waiting_for_me"]))
        d2 = (await cx.get("/api/grants")).json()
        ck("a read with no project leaves it alone too", store.get_grant_row(gid)["state"] == "waiting"
           and "ship acme/web fixes" not in d2["all"])
        r = await cx.post("/api/grants", json={"kind": "team", "words": "run the tests"})
        ck("adding other words changes nothing about it", store.get_grant_row(gid)["state"] == "waiting")
        r = await cx.post("/api/grants", json={"kind": "project", "project": uid, "words": "ship acme/web fixes"})
        ck("adding the same words again leaves it waiting (201, the same grant)",
           r.status_code == 201 and r.json()["grant"]["id"] == gid and r.json()["grant"]["state"] == "waiting",
           f"{r.status_code} {r.text[:200]}")
        d = (await cx.get("/api/grants", params={"project": uid})).json()
        ck("... it did not take effect and is in neither doc",
           store.get_grant_row(gid)["state"] == "waiting" and "ship acme/web fixes" not in d["project"] + d["all"],
           d["project"])
        ck("... and no second grant of those words was made",
           sum(1 for g in store.all_grant_rows() if g["words"] == "ship acme/web fixes") == 1)

        for what, path, body in (("consent", f"/api/grants/{gid}/consent", None),
                                 ("decline", f"/api/grants/{gid}/decline", {"reason": "no"})):
            r = await cx.post(path, json=body)
            ck(f"POST .../{what}: 402 licence_required with {{code, message, url}}", exact_402(r, CONSENT),
               f"{r.status_code} {r.text[:160]}")
        ck("so it is still waiting, with no consent and no decline recorded",
           store.get_grant_row(gid)["state"] == "waiting" and store.get_grant_row(gid)["consents"] == [])
        r = await cx.post("/api/keys", json={"name": "ing", "scopes": ["ingest"]})
        ing = r.json().get("secret", "")
        r = await cx.post(f"/api/grants/{gid}/consent", headers=H(ing))
        ck("a key that may only ingest reaches the answer: 402, not 403", exact_402(r, CONSENT),
           f"{r.status_code} {r.text[:100]}")

        r = await cx.post(f"/api/grants/{gid}/revoke")
        ck("it can be revoked", r.status_code == 200 and r.json()["grant"]["state"] == "revoked",
           f"{r.status_code} {r.text[:160]}")
        ck("and it is revoked in the store", store.get_grant_row(gid)["state"] == "revoked")
        d = (await cx.get("/api/grants", params={"project": uid})).json()
        ck("a revoked grant is left out of the list", not any(x["id"] == gid for x in d["grants"]))
        ck("... and was never in the docs", "ship acme/web fixes" not in d["project"] + d["all"])

        print("== revoking one in effect still takes its line out ==")
        pid = next(x["id"] for x in store.all_grant_rows() if x["words"] == "deploy to staging")
        r = await cx.post(f"/api/grants/{pid}/revoke")
        d = (await cx.get("/api/grants", params={"project": uid})).json()
        ck("revoke works as before", r.status_code == 200 and "deploy to staging" not in d["project"],
           f"{r.status_code} {d['project']}")


def tools():
    print("== MCP tools without ee ==")
    names = [t.name for t in asyncio.run(ms.mcp.list_tools())]
    ck("consent_grant is not a tool", "consent_grant" not in names, str(names))
    ck("add_grant and read_grants (core tools) are still there", "add_grant" in names and "read_grants" in names,
       str(names))
    ck("no ee tool is registered", ms.EE_TOOLS == set(), str(ms.EE_TOOLS))
    from tares import mcp_factory as mf
    line = mf._grant_line({"id": "G1", "kind": "project", "words": "ship it", "state": "waiting",
                           "waiting_for": [], "given_at": "2026-11-07T00:00:00"})
    ck("a waiting grant that waits for no one is not shown as 'waiting for '",
       "waiting for" not in line and "not in effect" in line, line)


asyncio.run(routes())
tools()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
