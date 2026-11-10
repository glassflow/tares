"""The integration slots over HTTP, and the get_integrations MCP tool (TR-842).

One in-process cell with auth on and fake integrations installed (tickets: faketix with a url, a
secret token and a required project; review: fakerev; an ee one; a slow one). Covered:
 - GET /api/factory/integrations lists the 7 slots, each with what is installed (name, title, ee,
   allowed, settings_schema), the stored choice and what applies; a station key gets 403;
 - PUT /api/factory/integrations/{slot}: admin only (a read key and a person's ingest key get 403),
   unknown slot 404, an integration that is not installed, an unknown field, a missing required field,
   a wrong type and settings that are not an object all give 422 naming the problem; a blank secret keeps
   the stored one, null clears it; an ee integration without a licence gives 402;
 - POST .../test: admin only; a changed address with a blank secret is 422 "re-enter the secret when changing
   the address"; an internal address is refused unless TARES_WEBHOOK_ALLOW_PRIVATE; a slow test gives
   {ok: false}; a message that echoes a secret has it masked;
 - /api/projects/{uid}/integrations/{slot}: the admin and the project's lead may write, another person's
   key, a read key, a project key and a station may not; memory (and every slot but tickets, review, ci)
   is 422; DELETE goes back to the factory's choice; a project's test and write never see the
   factory's secret;
 - an integration that is not installed hides every stored value; a stored default name does not read "not
   installed"; no response in the whole run holds a secret value;
 - GET /api/factory/integrations/effective (what the get_integrations tool reads) is open to a station and
   holds no integration settings.

Run: python tests/test_integrations_api.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-integrations-api-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-integrations"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ.pop("TARES_WEBHOOK_ALLOW_PRIVATE", None)

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import integrations_api, licence
from tares.integrations import registry
from tares.integrations.tickets import TicketBackend
from tares.integrations.review import ReviewGate
from tares.integrations.types import Field

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


SEEN = []


class FakeTickets(TicketBackend):
    name = "faketix"
    title = "Fake tickets"
    settings_schema = [Field("url", "Address", "url"), Field("token", "Token", "secret"),
                       Field("project", "Project key", "text", required=True),
                       Field("sync", "Sync", "bool"),
                       Field("flow", "Statuses", "mapping", options=["todo", "done"])]

    async def check_connection(self, settings):
        SEEN.append(dict(settings))
        return True, f"reached {settings.get('url')} as {settings.get('token')}"


class SlowTickets(TicketBackend):
    name = "slowtix"
    title = "Slow tickets"
    settings_schema = [Field("url", "Address", "url")]

    async def check_connection(self, settings):
        await asyncio.sleep(5)
        return True, "late"


class EeTickets(TicketBackend):
    name = "eetix"
    title = "Enterprise tickets"
    ee = True
    settings_schema = [Field("token", "Token", "secret")]

    async def check_connection(self, settings):
        return True, "ok"


class FakeReview(ReviewGate):
    name = "fakerev"
    title = "Fake review"
    settings_schema = [Field("token", "Token", "secret")]

    async def check_connection(self, settings):
        return True, "ok"


registry.load()
for cls in (FakeTickets, SlowTickets, EeTickets):
    registry._loaded[("tickets", cls.name)] = cls
registry._loaded[("review", "fakerev")] = FakeReview

licensed = [False]
licence.ee_allowed = lambda feature="enterprise": licensed[0]

ALL = []          # every response text of the run, searched for secret values at the end
SECRETS = ["FSECRET-factory-1", "PSECRET-project-1", "LEADSECRET-1", "GONESECRET-1", "REVSECRET-1", "PROJOWN-secret-9"]


def ru(r):
    ALL.append(r.text)
    return r


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    def mk(token=TOKEN, station=None, role="builder"):
        hdr = {"Authorization": f"Bearer {token}"}
        if station:
            hdr.update({"X-Tares-Station": station, "X-Tares-Role": role})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)
    m.TARESD = mf.TARESD = "http://t"
    m._cx = mf._cx = lambda timeout=10: mk()

    async with app.router.lifespan_context(app):
        ann = store.create_person("Ann", email="ann@example.com")
        ben = store.create_person("Ben", email="ben@example.com")
        store.create_project("p1", "custom", "alpha", {}, lead_person_id=ann["id"])
        store.create_project("p2", "custom", "beta", {}, lead_person_id=ben["id"])

        async with mk() as root:
            async def key(name, scopes, person=None, project=None):
                body = {"name": name, "scopes": scopes}
                if person:
                    body["person_id"] = person["id"]
                if project:
                    body["project"] = project
                return (await root.post("/api/keys", json=body)).json()["secret"]

            reader = await key("reader", ["read"])
            ann_k = await key("ann", ["read", "ingest"], ann)
            ben_k = await key("ben", ["read", "ingest"], ben)
            adm_k = await key("admin2", ["admin"])
            proj_k = await key("pk", [], project="p1")

            print("== the list ==")
            r = ru(await root.get("/api/factory/integrations"))
            ck("an admin lists", r.status_code == 200, r.text[:300])
            slots = r.json()["slots"]
            ck("seven slots in order", [s["slot"] for s in slots] == [
                "tickets", "review", "ci", "memory", "monitoring", "guardrails", "runtime"],
               [s["slot"] for s in slots])
            ck("a fresh cell has no choice and the defaults apply",
               all(s["choice"] is None and s["effective"]["source"] == "default"
                   and s["effective"]["reason"] == "" for s in slots)
               and {s["slot"]: s["effective"]["integration"] for s in slots}["ci"] == "local", slots)
            tix = slots[0]
            names = {a["name"]: a for a in tix["available"]}
            ck("tickets lists what is installed, with its schema",
               set(names) == {"tares", "linear", "faketix", "slowtix", "eetix"}
               and [f["name"] for f in names["linear"]["settings_schema"]] == ["api_key"]
               and names["faketix"]["title"] == "Fake tickets" and names["faketix"]["ee"] is False
               and names["faketix"]["allowed"] is True
               and [f["name"] for f in names["faketix"]["settings_schema"]] == ["url", "token", "project", "sync", "flow"]
               and names["faketix"]["settings_schema"][2]["required"] is True
               and names["faketix"]["settings_schema"][4]["options"] == ["todo", "done"], names)
            ck("an ee integration is not allowed without a licence",
               names["eetix"]["ee"] is True and names["eetix"]["allowed"] is False, names["eetix"])
            for who, k in (("a read key", reader), ("a person's key", ann_k)):
                async with mk(k) as cx:
                    ck(f"{who} may list", (await cx.get("/api/factory/integrations")).status_code == 200)
            async with mk(proj_k) as cx:
                ck("a project key may not list", (await cx.get("/api/factory/integrations")).status_code == 403)
            async with mk(station="shop-build-x", role="builder") as cx:
                r = ru(await cx.get("/api/factory/integrations"))
                ck("a station may not list", r.status_code == 403, r.status_code)

            print("== choosing for the factory ==")
            body = {"name": "faketix", "settings": {"url": "http://93.184.216.34/api", "token": SECRETS[0],
                                                    "project": "FAC"}}
            for who, k in (("a read key", reader), ("a person's ingest key", ann_k)):
                async with mk(k) as cx:
                    r = ru(await cx.put("/api/factory/integrations/tickets", json=body))
                    ck(f"{who} may not choose", r.status_code == 403, r.status_code)
            async with mk(station="shop-build-x", role="builder") as cx:
                ck("a station may not choose", (await cx.put("/api/factory/integrations/tickets",
                                                             json=body)).status_code == 403)
            ck("nothing was stored by the refused calls", store.get_choice("tickets") is None)
            r = ru(await root.put("/api/factory/integrations/tickets", json=body))
            ck("an admin chooses", r.status_code == 200 and r.json()["choice"]["name"] == "faketix", r.text)
            e = r.json()
            ck("the answer hides the secret and says it is set",
               e["choice"]["settings"]["token"] == "" and e["choice"]["secrets"]["token"] == {"has_value": True}
               and e["choice"]["settings"]["url"] == "http://93.184.216.34/api"
               and SECRETS[0] not in r.text, e["choice"])
            ck("what applies is now the factory's choice",
               e["effective"]["integration"] == "faketix" and e["effective"]["source"] == "factory"
               and e["effective"]["title"] == "Fake tickets" and e["effective"]["settings"]["token"] == "", e["effective"])
            async with mk(adm_k) as cx:
                ck("an admin key chooses too", (await cx.put("/api/factory/integrations/review",
                                                             json={"name": "fakerev", "settings": {"token": SECRETS[4]}})
                                                ).status_code == 200)
            r = ru(await root.put("/api/factory/integrations/tickets", json={
                "name": "faketix", "settings": {"project": "FAC2", "token": ""}}))
            ck("a blank secret keeps the stored one",
               r.status_code == 200 and store.stored_settings("tickets", "faketix")["token"] == SECRETS[0]
               and store.stored_settings("tickets", "faketix")["project"] == "FAC2", r.text)
            r = ru(await root.put("/api/factory/integrations/tickets", json={
                "name": "faketix", "settings": {"project": "FAC2", "token": None}}))
            ck("null clears it", "token" not in store.stored_settings("tickets", "faketix"), r.text)
            await root.put("/api/factory/integrations/tickets", json={
                "name": "faketix", "settings": {"project": "FAC", "token": SECRETS[0],
                                                "url": "http://93.184.216.34/api"}})

            def bad(label, slot, body, code, needle):
                async def go():
                    r = ru(await root.put(f"/api/factory/integrations/{slot}", json=body))
                    ck(label, r.status_code == code and needle in r.text, (r.status_code, r.text[:200]))
                return go
            before = store.get_choice("tickets")
            await bad("an unknown slot is 404", "nope", {"name": "x"}, 404, "no slot")()
            await bad("a missing name is 422", "tickets", {"settings": {}}, 422, "name is required")()
            await bad("an integration not installed is 422", "tickets", {"name": "ghost"}, 422, "not installed")()
            await bad("an unknown field names it", "tickets", {"name": "faketix", "settings": {"bogus": "1"}},
                      422, "bogus")()
            r = ru(await root.put("/api/factory/integrations/tickets", json={"name": "faketix", "settings": {"project": ""}}))
            ck("a missing required field names it", r.status_code == 422 and "project" in r.json()["detail"], r.text)
            await bad("a wrong type names the field", "tickets",
                      {"name": "faketix", "settings": {"project": "A", "sync": "yes"}}, 422, "sync")()
            await bad("a mapping key outside the options is refused", "tickets",
                      {"name": "faketix", "settings": {"project": "A", "flow": {"nope": "x"}}}, 422, "flow")()
            for notobj in ([], "text", 5):
                await bad(f"settings {notobj!r} is not an object", "tickets",
                          {"name": "faketix", "settings": notobj}, 422, "settings must be an object")()
            ck("the refused writes changed nothing", store.get_choice("tickets")["settings"] == before["settings"]
               and store.stored_settings("tickets", "faketix")["token"] == SECRETS[0])
            r = ru(await root.put("/api/factory/integrations/memory", json={"name": "anything", "settings": {}}))
            ck("a slot with nothing installed refuses any name", r.status_code == 422, r.text)

            print("== a licence ==")
            r = ru(await root.put("/api/factory/integrations/tickets", json={"name": "eetix", "settings": {}}))
            ck("an ee integration without a licence is 402", r.status_code == 402, (r.status_code, r.text))
            licensed[0] = True
            r = ru(await root.get("/api/factory/integrations"))
            ck("with a licence it is allowed",
               {a["name"]: a for a in r.json()["slots"][0]["available"]}["eetix"]["allowed"] is True)
            r = ru(await root.put("/api/factory/integrations/tickets", json={"name": "eetix", "settings": {"token": "x-ee-token"}}))
            ck("and can be chosen", r.status_code == 200 and r.json()["effective"]["integration"] == "eetix", r.text)
            licensed[0] = False
            r = ru(await root.get("/api/factory/integrations"))
            e = r.json()["slots"][0]["effective"]
            ck("a licence that goes away falls back to the default, with the reason",
               e["integration"] == "tares" and e["source"] == "default" and e["reason"] == "needs an enterprise licence", e)
            await root.put("/api/factory/integrations/tickets", json={
                "name": "faketix", "settings": {"project": "FAC", "url": "http://93.184.216.34/api", "token": SECRETS[0]}})

            print("== not installed, and the default's own name ==")
            store.set_choice("memory", "gone", {"url": "http://x", "token": SECRETS[3]}, "t")
            r = ru(await root.get("/api/factory/integrations"))
            mem = r.json()["slots"][3]
            ck("a choice that is not installed hides every stored value",
               mem["choice"]["settings"] == {"url": "", "token": ""} and SECRETS[3] not in r.text
               and mem["choice"]["secrets"]["url"] == {"has_value": True}, mem["choice"])
            ck("and what applies is the default, because it is not installed",
               mem["effective"]["integration"] == "tares" and mem["effective"]["reason"] == "not installed", mem["effective"])
            store.set_choice("memory", "tares", {}, "t")
            mem = ru(await root.get("/api/factory/integrations")).json()["slots"][3]
            ck("the default's own stored name is not reported as missing",
               mem["effective"]["integration"] == "tares" and mem["effective"]["reason"] == "", mem["effective"])

            print("== testing a connection ==")
            tb = {"name": "faketix", "settings": {"url": "http://93.184.216.34/api", "project": "FAC", "token": ""}}
            for who, k in (("a read key", reader), ("a person's key", ann_k)):
                async with mk(k) as cx:
                    ck(f"{who} may not test", (await cx.post("/api/factory/integrations/tickets/test", json=tb)).status_code == 403)
            async with mk(station="shop-build-x", role="builder") as cx:
                ck("a station may not test", (await cx.post("/api/factory/integrations/tickets/test", json=tb)).status_code == 403)
            SEEN.clear()
            r = ru(await root.post("/api/factory/integrations/tickets/test", json=tb))
            ck("a test uses the stored values", r.status_code == 200 and r.json()["ok"] is True
               and SEEN[-1]["token"] == SECRETS[0] and SEEN[-1]["url"] == "http://93.184.216.34/api", (r.text, SEEN))
            ck("the message never holds the secret it was given",
               SECRETS[0] not in r.text and "as ***" in r.json()["message"], r.text)
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"url": "http://93.184.216.35/api"}}))
            ck("a changed address with a blank secret is 422",
               r.status_code == 422 and "re-enter the secret when changing the address" in r.json()["detail"], r.text)
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"url": "http://93.184.216.35/api", "token": ""}}))
            ck("the same with an explicit blank secret", r.status_code == 422, r.text)
            SEEN.clear()
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"url": "http://93.184.216.35/api", "token": "typed-new-token"}}))
            ck("a changed address with a new secret is tested with both",
               r.status_code == 200 and SEEN[-1]["token"] == "typed-new-token" and SEEN[-1]["url"].endswith(".35/api"), (r.text, SEEN))
            ck("and nothing was saved by a test", store.stored_settings("tickets", "faketix")["token"] == SECRETS[0]
               and store.stored_settings("tickets", "faketix")["url"].endswith(".34/api"))
            SEEN.clear()
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"url": "http://93.184.216.34/api", "token": ""}}))
            ck("the same address with a blank secret is fine", r.status_code == 200 and SEEN[-1]["token"] == SECRETS[0], r.text)
            SEEN.clear()
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={"name": "faketix", "settings": {}}))
            ck("a test with no settings tries what is saved, not only the secret",
               r.status_code == 200 and SEEN[-1] == {"url": "http://93.184.216.34/api", "project": "FAC", "token": SECRETS[0]},
               (r.text, SEEN))
            SEEN.clear()
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"token": "only-a-new-token"}}))
            ck("a test that posts only a token reaches the stored address and project",
               r.status_code == 200 and SEEN[-1] == {"url": "http://93.184.216.34/api", "project": "FAC", "token": "only-a-new-token"},
               (r.text, SEEN))
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"project": ""}}))
            ck("a test is refused when a required field is left empty",
               r.status_code == 422 and "project" in r.json()["detail"], r.text)
            store.set_choice("tickets", "faketix", {"url": "http://169.254.169.254/latest", "project": "FAC",
                                                    "token": SECRETS[0]}, "t")
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={"name": "faketix", "settings": {}}))
            ck("a stored address that is not posted gets the outbound check too",
               r.status_code == 422 and "internal address" in r.json()["detail"], r.text)
            store.set_choice("tickets", "faketix", {"url": "http://93.184.216.34/api", "project": "FAC",
                                                    "token": SECRETS[0]}, "t")
            SEEN.clear()
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"url": "http://93.184.216.35/api", "token": None}}))
            ck("a changed address with the secret cleared carries nothing, so it is not refused",
               r.status_code == 200 and "token" not in SEEN[-1] and SEEN[-1]["project"] == "FAC", (r.text, SEEN))
            for label, st in (("blank", {"url": "http://93.184.216.35/api", "project": "FAC", "token": ""}),
                              ("left out", {"url": "http://93.184.216.35/api", "project": "FAC"})):
                r = ru(await root.put("/api/factory/integrations/tickets", json={"name": "faketix", "settings": st}))
                ck(f"a save with a changed address and the secret {label} is 422",
                   r.status_code == 422 and "re-enter the secret when changing the address" in r.json()["detail"], r.text)
            ck("and the refused save changed nothing", store.stored_settings("tickets", "faketix") ==
               {"url": "http://93.184.216.34/api", "project": "FAC", "token": SECRETS[0]}, store.stored_settings("tickets", "faketix"))
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"url": "http://127.0.0.1:9/x", "token": "t-new-secret"}}))
            ck("an internal address is refused", r.status_code == 422 and "internal address" in r.json()["detail"], r.text)
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"url": "ftp://93.184.216.34/x", "token": "t-new-secret"}}))
            ck("a url that is not http is refused", r.status_code == 422 and "http" in r.json()["detail"], r.text)
            os.environ["TARES_WEBHOOK_ALLOW_PRIVATE"] = "1"
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "faketix", "settings": {"url": "http://127.0.0.1:9/x", "token": "t-new-secret"}}))
            ck("TARES_WEBHOOK_ALLOW_PRIVATE allows it", r.status_code == 200, r.text)
            os.environ.pop("TARES_WEBHOOK_ALLOW_PRIVATE")
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={"name": "ghost", "settings": {}}))
            ck("testing what is not installed is 422", r.status_code == 422, r.text)
            integrations_api.TEST_SECONDS = 0.2
            r = ru(await root.post("/api/factory/integrations/tickets/test", json={
                "name": "slowtix", "settings": {"url": "http://93.184.216.34/"}}))
            ck("a slow test gives ok false, not an error",
               r.status_code == 200 and r.json()["ok"] is False and "within" in r.json()["message"], r.text)
            integrations_api.TEST_SECONDS = 10

            print("== a project's own choice ==")
            pb = {"name": "faketix", "settings": {"url": "http://93.184.216.40/api", "project": "PRJ", "token": SECRETS[2]}}
            async with mk(ann_k) as cx:
                r = ru(await cx.put("/api/projects/p1/integrations/tickets", json=pb))
                ck("the project's lead may choose", r.status_code == 200 and r.json()["effective"]["source"] == "project"
                   and r.json()["choice"]["settings"]["token"] == "" and SECRETS[2] not in r.text, r.text)
                ck("and read it back", (await cx.get("/api/projects/p1/integrations/tickets")).json()["choice"]["name"] == "faketix")
                r = ru(await cx.put("/api/projects/p2/integrations/tickets", json=pb))
                ck("not on another project", r.status_code == 403, r.status_code)
                r = ru(await cx.put("/api/projects/p1/integrations/memory", json={"name": "x", "settings": {}}))
                ck("memory on a project is 422", r.status_code == 422 and "tickets, review, ci" in r.text, r.text)
                for slot in ("monitoring", "guardrails", "runtime", "bogus"):
                    ck(f"{slot} on a project is 422", (await cx.put(f"/api/projects/p1/integrations/{slot}",
                                                                   json={"name": "x"})).status_code == 422)
                ck("a project read of memory is 422 too", (await cx.get("/api/projects/p1/integrations/memory")).status_code == 422)
            for who, k in (("a read key", reader), ("another person's key", ben_k), ("a project key", proj_k)):
                async with mk(k) as cx:
                    r = ru(await cx.put("/api/projects/p1/integrations/tickets", json=pb))
                    ck(f"{who} may not choose for p1", r.status_code == 403, r.status_code)
                    r = ru(await cx.post("/api/projects/p1/integrations/tickets/test", json=pb))
                    ck(f"{who} may not test for p1", r.status_code == 403, r.status_code)
                    r = ru(await cx.delete("/api/projects/p1/integrations/tickets"))
                    ck(f"{who} may not clear p1's choice", r.status_code == 403, r.status_code)
                    r = ru(await cx.get("/api/projects/p1/integrations/tickets"))
                    ck(f"{who} may not read p1's choice", r.status_code == 403 and "http://93" not in r.text, r.status_code)
            async with mk(station="shop-build-x", role="builder") as cx:
                ck("a station may not read a project's choice", (await cx.get("/api/projects/p1/integrations/tickets")).status_code == 403)
            async with mk(adm_k) as cx:
                ck("an admin may choose for any project", (await cx.put("/api/projects/p2/integrations/review",
                                                                        json={"name": "fakerev", "settings": {}})).status_code == 200)
            async with mk(adm_k) as cx:
                ck("an admin may read a project's choice", (await cx.get("/api/projects/p1/integrations/tickets")).status_code == 200)
                await cx.put("/api/projects/p1/integrations/tickets", json=pb)
            async with mk(ann_k) as cx:
                for label, st in (("blank", {"url": "http://93.184.216.50/api", "project": "PRJ", "token": ""}),
                                  ("left out", {"url": "http://93.184.216.50/api", "project": "PRJ"})):
                    r = ru(await cx.put("/api/projects/p1/integrations/tickets", json={"name": "faketix", "settings": st}))
                    ck(f"a lead moving the address of a stored secret ({label}) is 422",
                       r.status_code == 422 and "re-enter the secret" in r.json()["detail"], r.text)
                ck("and the project row is unchanged", store.stored_settings("tickets", "faketix", "p1") ==
                   {"url": "http://93.184.216.40/api", "project": "PRJ", "token": SECRETS[2]},
                   store.stored_settings("tickets", "faketix", "p1"))
                SEEN.clear()
                r = ru(await cx.post("/api/projects/p1/integrations/tickets/test", json={"name": "faketix", "settings": {}}))
                ck("a project test with no settings tries the project's own saved values",
                   r.status_code == 200 and SEEN[-1] == {"url": "http://93.184.216.40/api", "project": "PRJ", "token": SECRETS[2]},
                   (r.text, SEEN))
            ck("the project row is its own, the factory's is untouched",
               store.stored_settings("tickets", "faketix", "p1")["token"] == SECRETS[2]
               and store.stored_settings("tickets", "faketix")["token"] == SECRETS[0])
            r = ru(await root.get("/api/projects/nope/integrations/tickets"))
            ck("an unknown project is 404", r.status_code == 404, r.status_code)
            for slot in ("memory", "bogus"):
                r = ru(await root.get(f"/api/projects/nope/integrations/{slot}"))
                ck(f"an unknown project is 404 before the slot {slot} is judged", r.status_code == 404, r.status_code)
                r = ru(await root.put(f"/api/projects/nope/integrations/{slot}", json={"name": "x"}))
                ck(f"... on a write too ({slot})", r.status_code == 404, r.status_code)

            print("== a project never uses the factory's secret ==")
            async with mk(ann_k) as cx:
                await cx.put("/api/projects/p1/integrations/review", json={"name": "fakerev", "settings": {}})
                r = ru(await cx.put("/api/projects/p1/integrations/tickets", json={
                    "name": "faketix", "settings": {"project": "PRJ", "url": "http://93.184.216.40/api", "token": ""}}))
                ck("a blank secret on a project write keeps its own, not the factory's",
                   store.stored_settings("tickets", "faketix", "p1")["token"] == SECRETS[2], r.text)
                await cx.delete("/api/projects/p1/integrations/tickets")
                r = ru(await cx.put("/api/projects/p1/integrations/tickets", json={
                    "name": "faketix", "settings": {"project": "PRJ", "url": "http://93.184.216.40/api", "token": ""}}))
                ck("a project with no secret of its own gets none from the factory",
                   "token" not in store.stored_settings("tickets", "faketix", "p1"), store.stored_settings("tickets", "faketix", "p1"))
                SEEN.clear()
                r = ru(await cx.post("/api/projects/p1/integrations/tickets/test", json={
                    "name": "faketix", "settings": {"project": "PRJ", "url": "http://93.184.216.40/api"}}))
                ck("a project's test does not see the factory's secret",
                   r.status_code == 200 and not SEEN[-1].get("token") and SECRETS[0] not in json.dumps(SEEN)
                   and SECRETS[0] not in r.text, (r.text, SEEN))
                SEEN.clear()
                r = ru(await cx.post("/api/projects/p1/integrations/tickets/test", json={
                    "name": "faketix", "settings": {"url": "http://93.184.216.41/api", "token": "typed-by-lead"}}))
                ck("a project's test takes what the lead typed", r.status_code == 200 and SEEN[-1]["token"] == "typed-by-lead", r.text)
                await cx.put("/api/projects/p1/integrations/tickets", json={
                    "name": "faketix", "settings": {"project": "PRJ", "url": "http://93.184.216.41/api", "token": SECRETS[5]}})
                SEEN.clear()
                r = ru(await cx.post("/api/projects/p1/integrations/tickets/test", json={
                    "name": "faketix", "settings": {"project": "PRJ", "url": "http://93.184.216.41/api", "token": ""}}))
                ck("a project's test can use its own stored secret",
                   r.status_code == 200 and SEEN[-1]["token"] == SECRETS[5] and SECRETS[5] not in r.text, (r.text, SEEN))
                r = ru(await cx.delete("/api/projects/p1/integrations/tickets"))
                ck("clearing goes back to the factory's choice",
                   r.status_code == 200 and r.json()["cleared"] is True and r.json()["effective"]["source"] == "factory"
                   and r.json()["choice"] is None, r.text)
                r = ru(await cx.delete("/api/projects/p1/integrations/tickets"))
                ck("clearing again says nothing was there", r.status_code == 200 and r.json()["cleared"] is False)

            print("== what applies, for a station ==")
            async with mk(station="shop-build-x", role="builder") as cx:
                r = ru(await cx.get("/api/factory/integrations/effective"))
                eff = r.json()
                ck("a station reads what applies", r.status_code == 200 and len(eff["slots"]) == 7
                   and eff["slots"][0] == {"slot": "tickets", "integration": "faketix", "title": "Fake tickets",
                                           "source": "factory", "reason": ""}, r.text)
                ck("it holds no integration settings", '"settings"' not in r.text and "93.184" not in r.text, r.text)
                r = ru(await cx.get("/api/factory/integrations/effective", params={"project": "alpha"}))
                ck("for a project, by name", r.status_code == 200 and r.json()["project"] == "p1", r.text)
            async with mk(ann_k) as cx:
                await cx.put("/api/projects/p1/integrations/tickets", json={
                    "name": "slowtix", "settings": {"url": "http://93.184.216.40/"}})
            async with mk(station="shop-build-x", role="builder") as cx:
                r = ru(await cx.get("/api/factory/integrations/effective", params={"project": "p1"}))
                ck("the project's own choice shows, and the factory's does for another project",
                   r.json()["slots"][0]["integration"] == "slowtix" and r.json()["slots"][0]["source"] == "project", r.text)
                r = ru(await cx.get("/api/factory/integrations/effective", params={"project": "p2"}))
                ck("another project gets the factory's", r.json()["slots"][0]["source"] == "factory", r.text)
                ck("an unknown project is 404", (await cx.get("/api/factory/integrations/effective",
                                                              params={"project": "nope"})).status_code == 404)
            async with mk(proj_k) as cx:
                ck("a project key may not read it", (await cx.get("/api/factory/integrations/effective")).status_code == 403)

            print("== the get_integrations tool ==")
            out = json.loads(await mf.get_integrations())
            ck("it lists the seven slots for the factory", out["project"] is None and [s["slot"] for s in out["slots"]][0] == "tickets"
               and len(out["slots"]) == 7 and out["slots"][0]["integration"] == "faketix", out)
            out2 = json.loads(await mf.get_integrations(project="p1"))
            ck("and for a project", out2["project"] == "p1" and out2["slots"][0]["integration"] == "slowtix", out2)
            raw = await mf.get_integrations(project="alpha")
            ck("it never returns integration settings", '"settings"' not in raw and "93.184" not in raw and "token" not in raw, raw)
            bad_out = await mf.get_integrations(project="nope")
            ck("an unknown project says so", "nope" in bad_out and "slots" not in bad_out, bad_out)

    print("== no secret in any response ==")
    blob = "\n".join(ALL)
    for s in SECRETS + ["typed-new-token", "typed-by-lead", "t-new-secret", "x-ee-token"]:
        ck(f"{s} is in no response", s not in blob)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
