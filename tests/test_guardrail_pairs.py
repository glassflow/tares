"""Guardrail pairs: a hook point paired with a guardrail integration, block or warn (TR-863).

One in-process cell with auth on and fake guardrail integrations whose answer a test sets (deny,
allow, warn-only, raise, never answer) and that record what they were asked and the integration
settings they ran with. Covered:
 - the store: the table, add, list, get, delete; ids GP1, GP2... that grow and are not handed out
   again below the highest; on_fail is block by default; an exact twin is not added twice;
 - null and empty inputs: no pairs at all (run gives the slot's answer only, gate is open, GET gives
   {"pairs": []}); empty settings, missing settings, null settings; a blank secret on update keeps the
   old value, null clears it, a non-empty one replaces it; an empty or unknown hook, an empty or
   not installed integration, a bad on_fail, an unknown key, a value of the wrong type, a missing
   required field, an empty body and a settings that is not an object are all 422; an enterprise
   integration without a licence is 402;
 - secrets stay redacted on every read (factory GET, project GET, the add and update answers) and the
   real value is in no response body; a project pair keeps its own secret and never takes a factory
   one (not on add, not on update with its id, and a factory update never changes it);
 - who may: an admin on the factory routes; the project's lead or an admin on a project's routes;
   another person, a read key and a crew station get 403; no credential is 401; an unknown project is
   404; a project request cannot remove a factory pair (403, it stays), names another project's pair
   as 404, and an admin removes a project pair from the factory routes;
 - the project view lists the factory's pairs read-only, with empty settings;
 - run(): the slot's guardrail first and its error still raises; factory pairs before the project's, each
   in the order added; a project pair the same as a factory pair runs once (not when its settings
   differ); a pair for another hook point does not run; a pair is given its own settings; block keeps
   a denial blocking and warn makes it a warning; a guardrail that raises, never answers, is no longer
   installed or needs a licence the cell lacks follows on_fail with "guardrail did not answer:
   <integration>" and never shows the cause; one failing pair does not stop the next;
 - warnings on a ticket: merge_ready writes a guardrail_warn row, once per warning, a different warning
   writes another, a block writes none, no ticket writes nothing, and a recording that fails does not
   break the answer;
 - the answers: merge_ready, release_ready (route and tool) and the hand-over route list `guardrails`; a
   block at pre_build is the 409 with the reason and records nothing, a warning lets it through and
   is returned and shown by the tool;
 - deleting a project deletes its pairs; /health lists guardrail_pairs.

Run: python tests/test_guardrail_pairs.py
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

_TMP = tempfile.mkdtemp(prefix="tares-guardrail-pairs-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-pairs"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import factory_github, licence, merge_ready as mr
from tares.integrations import hooks, registry
from tares.integrations.ci import CISource
from tares.integrations.guardrails import Guardrail
from tares.integrations.review import ReviewGate
from tares.integrations.types import CIState, Decision, Field, Verdict

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


HEAD = "bbbbbbb2222222222222222222222222222222bb"
PAIRS = "/api/factory/guardrails/pairs"

# what each fake guardrail answers (a Decision, None, an Exception or "sleep") and what it was asked
ANS: dict = {}
LOG: list = []
licensed = [False]
licence.ee_allowed = lambda feature="enterprise": licensed[0]

ALL: list = []          # every response text of the run, searched for secret values at the end
SECRETS = ["FSECRET-factory-1", "FSECRET-factory-2", "PSECRET-project-1", "PSECRET-project-2",
           "LEADSECRET-1", "KEEPME-secret-7", "BOOM-detail-TOKEN9"]

SCHEMA = [Field("url", "Address", "text"), Field("token", "Token", "secret")]


def ru(r):
    ALL.append(r.text)
    return r


def make_rail(name, schema, ee=False):
    class Rail(Guardrail):
        async def check(self, stage, payload):
            LOG.append({"name": self.name, "stage": stage, "settings": dict(self.settings),
                        "ticket": payload.get("ticket")})
            a = ANS.get(self.name)
            if a == "sleep":
                await asyncio.sleep(60)
                return Decision(allow=True, reason="answered late", integration=self.name)
            if isinstance(a, Exception):
                raise a
            return a
    Rail.name = name
    Rail.title = f"Fake {name}"
    Rail.settings_schema = schema
    Rail.ee = ee
    Rail.__init__ = lambda self, store=None: setattr(self, "store", store)
    return Rail


class FakeReview(ReviewGate):
    name = "fakerev"
    title = "Fake review"

    def __init__(self, store=None):
        self.store = store

    async def verdict(self, pr):
        return Verdict(state="approved", head=HEAD, by="Fake")


class FakeCI(CISource):
    name = "fakeci"
    title = "Fake CI"

    def __init__(self, store=None):
        self.store = store

    async def state(self, pr, required):
        return CIState(state="success", head=HEAD)


async def fake_read_pr(store, repo, number, prev=None):
    return {**(prev or {}), "repo": repo, "number": number, "head": HEAD, "state": "open",
            "merged": False, "draft": False, "base": "factory-modular/m4",
            "url": f"https://github.com/{repo}/pull/{number}"}


async def fake_pr_files(store, repo, number):
    return [{"filename": "tares/a.py", "additions": 3, "deletions": 1}]


factory_github.read_pr = fake_read_pr
factory_github.pr_files = fake_pr_files


def deny(reason="Too big"):
    return Decision(allow=False, reason=reason)


def say(**answers):
    ANS.clear()
    ANS.update(answers)
    LOG.clear()


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def mk(token=TOKEN, station=None, role="builder"):
        hdr = {"Authorization": f"Bearer {token}"} if token else {}
        if station:
            hdr.update({"X-Tares-Station": station, "X-Tares-Role": role})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)

    m.TARESD = mf.TARESD = "http://t"
    m._cx = mf._cx = lambda timeout=10: mk()

    registry.load()
    for name in ("gr_a", "gr_b", "gr_c", "gr_gone", "gr_slot"):
        registry._loaded[("guardrails", name)] = make_rail(name, SCHEMA)
    registry._loaded[("guardrails", "gr_plain")] = make_rail("gr_plain", [])
    registry._loaded[("guardrails", "gr_req")] = make_rail(
        "gr_req", [Field("channel", "Channel", "text", required=True), Field("token", "Token", "secret")])
    registry._loaded[("guardrails", "gr_ee")] = make_rail("gr_ee", SCHEMA, ee=True)
    registry._loaded[("guardrails", "gr_tok")] = make_rail(
        "gr_tok", [Field("token", "Token", "secret", required=True)])
    registry._loaded[("guardrails", "gr_url")] = make_rail(
        "gr_url", [Field("url", "Address", "url"), Field("token", "Token", "secret")])
    registry._loaded[("review", "fakerev")] = FakeReview
    registry._loaded[("ci", "fakeci")] = FakeCI

    def clear():
        for p in store.list_guardrail_pairs():
            store.delete_guardrail_pair(p["id"])

    def rows(tid):
        return [r for r in store.ticket_history(tid) if r["field"] == "guardrail_warn"]

    async with app.router.lifespan_context(app):
        ann = store.create_person("Ann", email="ann@example.com")
        ben = store.create_person("Ben", email="ben@example.com")
        store.create_project("p1", "custom", "alpha", {}, lead_person_id=ann["id"])
        store.create_project("p2", "custom", "beta", {}, lead_person_id=ben["id"])
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        blog = json.loads(await m.create_project("Blog", "Write things"))["id"]
        await mf.write_milestone("Store", "", [], project="Shop")
        await m.write_ticket("Catalog", milestone="Store", project="Shop")
        cat = store.list_tickets(shop)[0]
        tid = cat["id"]
        store.update_ticket(tid, pr={"repo": "acme/shop", "number": 7,
                                     "url": "https://github.com/acme/shop/pull/7"})
        store.set_project_choice(shop, "review", "fakerev", {}, by="test")
        store.set_project_choice(shop, "ci", "fakeci", {}, by="test")

        print("== no pairs at all ==")
        ck("the store has none", store.list_guardrail_pairs() == [])
        ck("run gives the slot's answer only: nothing from the built-in guardrail",
           await hooks.run("pre_merge", shop, {}) == [])
        g = await hooks.gate("pre_build", shop, {})
        ck("gate is open", g == {"ok": True, "reasons": [], "warnings": [], "decisions": []}, g)
        async with mk() as root:
            r = ru(await root.get(PAIRS))
            ck("GET factory gives an empty list", r.status_code == 200 and r.json() == {"pairs": []}, r.text)
            r = ru(await root.get("/api/projects/p1/guardrails/pairs"))
            ck("GET project gives an empty list", r.status_code == 200 and r.json() == {"pairs": []}, r.text)

        print("== the store ==")
        a, how = store.save_guardrail_pair("factory", None, "pre_merge", "gr_a",
                                           {"url": "u1", "token": SECRETS[0]}, "block", "tester")
        ck("a pair is added as GP1 with block", how == "added" and a["id"] == "GP1"
           and a["on_fail"] == "block" and a["scope"] == "factory" and a["project_uid"] is None
           and a["created_by"] == "tester" and a["created_at"] is not None, a)
        ck("its secret is redacted in the view",
           a["settings"] == {"url": "u1", "token": ""} and a["secrets"] == {"token": {"has_value": True}}, a)
        ck("the title is the integration's", a["title"] == "Fake gr_a", a)
        b, how = store.save_guardrail_pair("factory", None, "pre_merge", "gr_a",
                                           {"url": "u1", "token": SECRETS[0]}, "block")
        ck("the exact twin adds nothing", how == "twin" and b["id"] == "GP1"
           and len(store.list_guardrail_pairs()) == 1, (how, b))
        b, how = store.save_guardrail_pair("factory", None, "pre_merge", "gr_a",
                                           {"url": "u1", "token": SECRETS[0]}, "warn")
        ck("the same with another on_fail is a different pair", how == "added" and b["id"] == "GP2", (how, b))
        c, how = store.save_guardrail_pair("project", "p1", "pre_merge", "gr_a",
                                           {"url": "u1", "token": SECRETS[0]}, "block")
        ck("the same in a project is a different pair", how == "added" and c["id"] == "GP3", (how, c))
        ck("get gives one", store.get_guardrail_pair("gp2")["id"] == "GP2" and store.get_guardrail_pair("GP9") is None
           and store.get_guardrail_pair("") is None)
        ck("list by scope", [p["id"] for p in store.list_guardrail_pairs("factory")] == ["GP1", "GP2"]
           and [p["id"] for p in store.list_guardrail_pairs("project", "p1")] == ["GP3"]
           and store.list_guardrail_pairs("project", "p2") == []
           and len(store.list_guardrail_pairs()) == 3)
        ck("delete removes one, twice says so", store.delete_guardrail_pair("GP1") is True
           and store.delete_guardrail_pair("GP1") is False and store.delete_guardrail_pair("") is False)
        d, how = store.save_guardrail_pair("factory", None, "pre_build", "gr_b", None, "block")
        ck("an id below the highest is not handed out again", d["id"] == "GP4", d)
        ck("settings None is stored as empty", d["settings"] == {} and d["secrets"] == {"token": {"has_value": False}}, d)
        ck("a stored pair with the real secret is readable for the runner only",
           store.guardrail_pair_stored("GP2")["settings"]["token"] == SECRETS[0])
        try:
            store.save_guardrail_pair("factory", None, "pre_build", "gr_a", {}, "block", pair_id="GP3")
            ck("an id of another scope is refused", False, "no error")
        except LookupError:
            ck("an id of another scope is refused", True)
        clear()
        ck("cleared", store.list_guardrail_pairs() == [])

        async with mk() as root:
            async def key(name, scopes, person=None):
                body = {"name": name, "scopes": scopes}
                if person:
                    body["person_id"] = person["id"]
                return (await root.post("/api/keys", json=body)).json()["secret"]

            reader = await key("reader", ["read"])
            ann_k = await key("ann", ["read", "ingest"], ann)
            ben_k = await key("ben", ["read", "ingest"], ben)

            print("== adding and the defaults ==")
            r = ru(await root.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_a",
                                                "settings": {"url": "https://factory.example/hook",
                                                             "token": SECRETS[0]}}))
            ck("an admin adds a factory pair, 201", r.status_code == 201 and r.json()["added"] is True, r.text)
            fp = r.json()["pair"]
            ck("on_fail defaults to block and the view is redacted",
               fp["on_fail"] == "block" and fp["scope"] == "factory" and fp["hook"] == "pre_merge"
               and fp["integration"] == "gr_a" and fp["settings"] == {"url": "https://factory.example/hook", "token": ""}
               and fp["secrets"] == {"token": {"has_value": True}} and fp["project_uid"] is None
               and fp["created_by"] and fp["created_at"], fp)
            r = ru(await root.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_a",
                                                "settings": {"url": "https://factory.example/hook",
                                                             "token": SECRETS[0]}}))
            ck("the exact twin adds nothing: 200, added false",
               r.status_code == 200 and r.json()["added"] is False and r.json()["pair"]["id"] == fp["id"], r.text)
            r = ru(await root.post(PAIRS, json={"hook": "pre_release", "integration": "gr_plain"}))
            ck("settings missing is fine", r.status_code == 201 and r.json()["pair"]["settings"] == {}, r.text)
            plain = r.json()["pair"]
            r = ru(await root.post(PAIRS, json={"hook": "pre_build", "integration": "gr_plain", "settings": None,
                                                "on_fail": "warn"}))
            ck("settings null is fine, on_fail warn is kept", r.status_code == 201
               and r.json()["pair"]["settings"] == {} and r.json()["pair"]["on_fail"] == "warn", r.text)
            r = ru(await root.post(PAIRS, json={"hook": "pre_build", "integration": "gr_a", "settings": {}}))
            ck("settings empty is fine", r.status_code == 201 and r.json()["pair"]["secrets"] == {"token": {"has_value": False}}, r.text)
            r = ru(await root.get(PAIRS))
            ids = [p["id"] for p in r.json()["pairs"]]
            ck("GET lists them oldest first with growing ids", ids == ["GP1", "GP2", "GP3", "GP4"], ids)
            ck("and shows the secret as empty with has_value", r.json()["pairs"][0]["settings"]["token"] == ""
               and r.json()["pairs"][0]["secrets"]["token"]["has_value"] is True, r.text)

            print("== bad input is 422 ==")

            async def bad(label, body, code=422, needle=""):
                r = ru(await root.post(PAIRS, json=body))
                ck(label, r.status_code == code and needle in r.text, (r.status_code, r.text[:200]))
                return r
            before = len(store.list_guardrail_pairs())
            await bad("an empty body", {}, 422)
            r = ru(await root.post(PAIRS))
            ck("no body at all", r.status_code == 422, r.status_code)
            await bad("an empty hook", {"hook": "", "integration": "gr_a"}, 422, "hook")
            await bad("no hook", {"integration": "gr_a"}, 422, "hook")
            await bad("an unknown hook point", {"hook": "pre_deploy", "integration": "gr_a"}, 422, "pre_merge")
            await bad("an empty integration", {"hook": "pre_merge", "integration": ""}, 422, "integration")
            await bad("no integration", {"hook": "pre_merge"}, 422, "integration")
            await bad("an integration that is not installed", {"hook": "pre_merge", "integration": "nope"}, 422, "not installed")
            await bad("an integration of another slot", {"hook": "pre_merge", "integration": "fakerev"}, 422, "not installed")
            await bad("a bad on_fail", {"hook": "pre_merge", "integration": "gr_a", "on_fail": "maybe"}, 422, "on_fail")
            await bad("an empty on_fail", {"hook": "pre_merge", "integration": "gr_a", "on_fail": ""}, 422, "on_fail")
            await bad("settings that is not an object", {"hook": "pre_merge", "integration": "gr_a", "settings": "x"}, 422, "object")
            await bad("settings that is a list", {"hook": "pre_merge", "integration": "gr_a", "settings": []}, 422, "object")
            await bad("an unknown key", {"hook": "pre_merge", "integration": "gr_a", "settings": {"nope": "1"}}, 422, "nope")
            await bad("a value of the wrong type", {"hook": "pre_merge", "integration": "gr_a", "settings": {"url": 5}}, 422, "url")
            await bad("a required field missing", {"hook": "pre_merge", "integration": "gr_req", "settings": {}}, 422, "channel")
            await bad("a required field blank", {"hook": "pre_merge", "integration": "gr_req", "settings": {"channel": ""}}, 422, "channel")
            r = ru(await root.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_req", "settings": {"channel": "ops"}}))
            ck("a required field given is added", r.status_code == 201, r.text)
            await bad("an unknown id", {"id": "GP99", "hook": "pre_merge", "integration": "gr_a"}, 404, "GP99")
            ck("nothing was added by a refusal", len(store.list_guardrail_pairs()) == before + 1)
            r = ru(await root.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_ee"}))
            ck("an enterprise integration without a licence is 402", r.status_code == 402, (r.status_code, r.text))
            licensed[0] = True
            r = ru(await root.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_ee", "settings": {"token": SECRETS[1]}}))
            ck("and with one it is added", r.status_code == 201, (r.status_code, r.text))
            ee_id = r.json()["pair"]["id"]
            licensed[0] = False

            print("== secrets on update ==")
            fid = fp["id"]

            async def stored_token(pid):
                return (store.guardrail_pair_stored(pid)["settings"] or {}).get("token")
            body = {"id": fid, "hook": "pre_merge", "integration": "gr_a", "settings": {"url": "https://factory.example/hook"}}
            r = ru(await root.post(PAIRS, json=body))
            ck("a missing secret keeps the stored value", r.status_code == 200 and r.json()["updated"] is True
               and r.json()["pair"]["secrets"]["token"]["has_value"] is True
               and await stored_token(fid) == SECRETS[0], r.text)
            r = ru(await root.post(PAIRS, json={**body, "settings": {"url": "https://factory.example/hook", "token": ""}}))
            ck("a blank secret keeps it", await stored_token(fid) == SECRETS[0] and r.json()["pair"]["settings"]["token"] == "", r.text)
            r = ru(await root.post(PAIRS, json={**body, "settings": {"url": "https://factory.example/hook", "token": SECRETS[1]}}))
            ck("a non-empty secret replaces it", await stored_token(fid) == SECRETS[1], r.text)
            r = ru(await root.post(PAIRS, json={**body, "settings": {"url": "https://factory.example/hook", "token": None}}))
            ck("null clears it", await stored_token(fid) is None
               and r.json()["pair"]["secrets"]["token"]["has_value"] is False, r.text)
            ru(await root.post(PAIRS, json={**body, "settings": {"url": "https://factory.example/hook", "token": SECRETS[0]}}))
            r = ru(await root.post(PAIRS, json={"id": fid, "settings": {"url": "https://factory.example/hook"}}))
            ck("an update with only the id keeps hook, integration and on_fail",
               r.status_code == 200 and r.json()["pair"]["hook"] == "pre_merge"
               and r.json()["pair"]["integration"] == "gr_a" and r.json()["pair"]["on_fail"] == "block"
               and await stored_token(fid) == SECRETS[0], r.text)
            r = ru(await root.post(PAIRS, json={"id": fid, "on_fail": "warn", "settings": {"url": "https://factory.example/hook"}}))
            ck("an update changes on_fail and keeps the id", r.json()["pair"]["on_fail"] == "warn"
               and r.json()["pair"]["id"] == fid and r.json()["pair"]["scope"] == "factory", r.text)
            r = ru(await root.post(PAIRS, json={"id": fid, "hook": "pre_merge", "integration": "gr_b",
                                                "settings": {"url": "https://factory.example/hook"}}))
            ck("choosing another integration starts from nothing: no secret travels",
               r.status_code == 200 and await stored_token(fid) is None, r.text)
            ru(await root.post(PAIRS, json={"id": fid, "hook": "pre_merge", "integration": "gr_a",
                                            "on_fail": "block", "settings": {"url": "https://factory.example/hook", "token": SECRETS[0]}}))
            r = ru(await root.post(PAIRS, json={"id": fid, "hook": "pre_merge", "integration": "gr_tok", "settings": {}}))
            ck("a secret the new integration needs is not taken from the old one", r.status_code == 422
               and "token is required" in r.text and await stored_token(fid) == SECRETS[0], r.text)

            print("== a changed address needs the secret again ==")
            r = ru(await root.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_url",
                                                "settings": {"url": "https://old.example/hook", "token": SECRETS[5]}}))
            uid_ = r.json()["pair"]["id"]
            ub = {"id": uid_, "hook": "pre_merge", "integration": "gr_url"}
            r = ru(await root.post(PAIRS, json={**ub, "settings": {"url": "https://new.example/hook"}}))
            ck("a new address with the secret left out is refused", r.status_code == 422
               and "re-enter the secret" in r.text, r.text)
            r = ru(await root.post(PAIRS, json={**ub, "settings": {"url": "https://new.example/hook", "token": ""}}))
            ck("a new address with a blank secret is refused", r.status_code == 422, r.text)
            ck("and nothing changed", store.guardrail_pair_stored(uid_)["settings"]
               == {"url": "https://old.example/hook", "token": SECRETS[5]}, store.guardrail_pair_stored(uid_))
            r = ru(await root.post(PAIRS, json={**ub, "settings": {"url": "https://old.example/hook"}}))
            ck("the same address keeps the secret without it", r.status_code == 200
               and store.guardrail_pair_stored(uid_)["settings"]["token"] == SECRETS[5], r.text)
            r = ru(await root.post(PAIRS, json={**ub, "settings": {"url": "https://new.example/hook", "token": SECRETS[6]}}))
            ck("a new address with a new secret is saved", r.status_code == 200
               and store.guardrail_pair_stored(uid_)["settings"] == {"url": "https://new.example/hook", "token": SECRETS[6]}, r.text)
            r = ru(await root.post(PAIRS, json={**ub, "settings": {"url": "https://third.example/hook", "token": None}}))
            ck("a new address with the secret cleared is saved", r.status_code == 200
               and store.guardrail_pair_stored(uid_)["settings"].get("token") is None, r.text)
            store.delete_guardrail_pair(uid_)
            r = ru(await root.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_url",
                                                "settings": {"url": "https://first.example/hook"}}))
            ck("a pair with no secret may change its address", r.status_code == 201 and (await root.post(
                PAIRS, json={"id": r.json()["pair"]["id"], "settings": {"url": "https://second.example/hook"}})).status_code == 200, r.text)
            store.delete_guardrail_pair(r.json()["pair"]["id"])

            print("== a project pair keeps its own secret ==")
            L = "/api/projects/p1/guardrails/pairs"
            async with mk(ann_k) as lead:
                r = ru(await lead.post(L, json={"hook": "pre_merge", "integration": "gr_a",
                                                "settings": {"url": "https://project.example/x"}}))
                ck("the lead adds a project pair, 201", r.status_code == 201 and r.json()["pair"]["scope"] == "project"
                   and r.json()["pair"]["project_uid"] == "p1", r.text)
                pp = r.json()["pair"]
                ck("with the secret omitted it has none: the factory's is not copied",
                   pp["secrets"]["token"]["has_value"] is False
                   and "token" not in store.guardrail_pair_stored(pp["id"])["settings"], pp)
                r = ru(await lead.post(L, json={"id": pp["id"], "hook": "pre_merge", "integration": "gr_a",
                                                "settings": {"url": "https://project.example/x", "token": ""}}))
                ck("a blank secret on update of a project pair never picks up the factory value",
                   "token" not in store.guardrail_pair_stored(pp["id"])["settings"], r.text)
                r = ru(await lead.post(L, json={"id": pp["id"], "hook": "pre_merge", "integration": "gr_a",
                                                "settings": {"url": "https://project.example/x", "token": SECRETS[2]}}))
                ck("its own secret is stored", store.guardrail_pair_stored(pp["id"])["settings"]["token"] == SECRETS[2]
                   and r.json()["pair"]["settings"]["token"] == "", r.text)
                ru(await root.post(PAIRS, json={"id": fid, "hook": "pre_merge", "integration": "gr_a",
                                                "settings": {"url": "https://factory.example/hook", "token": SECRETS[3]}}))
                ck("a factory update never changes a project pair",
                   store.guardrail_pair_stored(pp["id"])["settings"]["token"] == SECRETS[2]
                   and store.guardrail_pair_stored(fid)["settings"]["token"] == SECRETS[3])
                ru(await root.post(PAIRS, json={"id": fid, "hook": "pre_merge", "integration": "gr_a",
                                                "settings": {"url": "https://factory.example/hook", "token": SECRETS[0]}}))
                r = ru(await lead.post(L, json={"id": fid, "hook": "pre_merge", "integration": "gr_a"}))
                ck("a project request naming a factory pair id is 404 and changes nothing",
                   r.status_code == 404 and store.guardrail_pair_stored(fid)["settings"]["token"] == SECRETS[0], r.text)
                r = ru(await lead.post(L, json={"hook": "pre_merge", "integration": "gr_a",
                                                "settings": {"url": "https://project.example/x", "token": SECRETS[2]}}))
                ck("the project's exact twin adds nothing", r.status_code == 200 and r.json()["added"] is False, r.text)

                print("== the project view ==")
                r = ru(await lead.get(L))
                pairs = r.json()["pairs"]
                fac = [p for p in pairs if p["scope"] == "factory"]
                own = [p for p in pairs if p["scope"] == "project"]
                ck("the lead sees the factory pairs read-only", r.status_code == 200 and len(fac) >= 4
                   and all(p["settings"] == {} and p["secrets"] == {} for p in fac), r.text)
                ck("with hook, integration and on_fail", all(p["hook"] and p["integration"] and p["on_fail"] for p in fac))
                ck("the factory's settings are not in the answer", "factory.example" not in r.text, r.text)
                ck("the project's own pair is whole, secret redacted", len(own) == 1 and own[0]["id"] == pp["id"]
                   and own[0]["settings"]["url"] == "https://project.example/x" and own[0]["settings"]["token"] == ""
                   and own[0]["secrets"]["token"]["has_value"] is True, own)
                ck("factory pairs come before the project's", [p["scope"] for p in pairs] == ["factory"] * len(fac) + ["project"])

            print("== who may ==")
            ck("the factory GET lists no project pair", all(p["scope"] == "factory" for p in (await root.get(PAIRS)).json()["pairs"]))
            for who, k in (("a read key", reader), ("another person", ben_k)):
                async with mk(k) as cx:
                    ck(f"{who} may not read the project's pairs", (await cx.get(L)).status_code == 403)
                    r = await cx.post(L, json={"hook": "pre_merge", "integration": "gr_b"})
                    ck(f"{who} may not add one", r.status_code == 403, r.status_code)
                    ck(f"{who} may not delete one", (await cx.delete(f"{L}/{pp['id']}")).status_code == 403)
                    ck(f"{who} may not touch the factory's", (await cx.get(PAIRS)).status_code == 403
                       and (await cx.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_b"})).status_code == 403
                       and (await cx.delete(f"{PAIRS}/{fid}")).status_code == 403)
            async with mk(station="shop-build-x") as cx:
                ck("a station may not read, add or delete on the factory routes",
                   (await cx.get(PAIRS)).status_code == 403
                   and (await cx.post(PAIRS, json={"hook": "pre_merge", "integration": "gr_b"})).status_code == 403
                   and (await cx.delete(f"{PAIRS}/{fid}")).status_code == 403)
                ck("nor on a project's routes", (await cx.get(L)).status_code == 403
                   and (await cx.post(L, json={"hook": "pre_merge", "integration": "gr_b"})).status_code == 403
                   and (await cx.delete(f"{L}/{pp['id']}")).status_code == 403)
            async with mk(None) as cx:
                ck("no credential is 401", (await cx.get(PAIRS)).status_code == 401
                   and (await cx.get(L)).status_code == 401)
            ck("the factory pair is still there", store.get_guardrail_pair(fid) is not None)
            for method, url in (("GET", "/api/projects/nope/guardrails/pairs"),
                                ("POST", "/api/projects/nope/guardrails/pairs"),
                                ("DELETE", "/api/projects/nope/guardrails/pairs/GP1")):
                r = await root.request(method, url, json={"hook": "pre_merge", "integration": "gr_a"} if method == "POST" else None)
                ck(f"{method} for an unknown project is 404", r.status_code == 404, r.status_code)
            async with mk(ann_k) as lead:
                r = ru(await lead.delete(f"{L}/{fid}"))
                ck("a lead cannot delete a factory pair: 403 with a plain message",
                   r.status_code == 403 and "factory" in r.text and store.get_guardrail_pair(fid) is not None, r.text)
            r = ru(await root.delete(f"{L}/{fid}"))
            ck("not even an admin, on a project's route", r.status_code == 403 and store.get_guardrail_pair(fid) is not None, r.text)
            async with mk(ben_k) as ben_lead:
                r = ru(await ben_lead.delete(f"/api/projects/p2/guardrails/pairs/{pp['id']}"))
                ck("another project's pair is 404 on this project's route",
                   r.status_code == 404 and store.get_guardrail_pair(pp["id"]) is not None, r.text)
            r = ru(await root.delete(f"/api/projects/p2/guardrails/pairs/{pp['id']}"))
            ck("an admin gets the same 404", r.status_code == 404, r.text)
            r = ru(await root.delete(f"{L}/GP999"))
            ck("an unknown pair is 404", r.status_code == 404, r.text)
            async with mk(ann_k) as lead:
                r = ru(await lead.post(L, json={"hook": "pre_release", "integration": "gr_b", "on_fail": "warn"}))
                other = r.json()["pair"]["id"]
                r = ru(await lead.delete(f"{L}/{other}"))
                ck("the lead deletes their own pair", r.status_code == 200 and r.json() == {"deleted": True, "id": other}, r.text)
                ck("and it is gone", store.get_guardrail_pair(other) is None)
            r = ru(await root.delete(f"{PAIRS}/{pp['id']}"))
            ck("an admin deletes a project pair from the factory route", r.status_code == 200 and store.get_guardrail_pair(pp["id"]) is None, r.text)
            r = ru(await root.delete(f"{PAIRS}/{plain['id']}"))
            ck("an admin deletes a factory pair", r.status_code == 200 and store.get_guardrail_pair(plain["id"]) is None, r.text)
            r = ru(await root.delete(f"{PAIRS}/{plain['id']}"))
            ck("deleting it again is 404", r.status_code == 404, r.text)
            store.delete_guardrail_pair(ee_id)

            print("== run: order and dedupe ==")
            clear()
            hook = "pre_merge"
            store.save_guardrail_pair("project", shop, hook, "gr_c", {}, "block")
            store.save_guardrail_pair("factory", None, hook, "gr_a", {"url": "u"}, "block")
            store.save_guardrail_pair("factory", None, hook, "gr_b", {}, "block")
            store.save_guardrail_pair("factory", None, "pre_release", "gr_plain", {}, "block")
            say(gr_a=Decision(allow=True, reason="a ok"), gr_b=Decision(allow=True, reason="b ok"),
                gr_c=Decision(allow=True, reason="c ok"))
            out = await hooks.run(hook, shop, {})
            ck("factory pairs run before the project's, each in the order added",
               [x["name"] for x in LOG] == ["gr_a", "gr_b", "gr_c"], LOG)
            ck("and answer in that order with their names", [d.integration for d in out] == ["gr_a", "gr_b", "gr_c"], out)
            ck("a pair for another hook point did not run", all(x["name"] != "gr_plain" for x in LOG))
            say()
            await hooks.run(hook, blog, {})
            ck("another project runs the factory's pairs only", [x["name"] for x in LOG] == ["gr_a", "gr_b"], LOG)
            say()
            await hooks.run(hook, None, {})
            ck("no project runs the factory's pairs only", [x["name"] for x in LOG] == ["gr_a", "gr_b"], LOG)
            clear()
            store.save_guardrail_pair("factory", None, hook, "gr_a", {"url": "u", "token": SECRETS[0]}, "block")
            store.save_guardrail_pair("project", shop, hook, "gr_a", {"url": "u", "token": SECRETS[0]}, "block")
            say()
            await hooks.run(hook, shop, {})
            ck("a project pair identical to a factory pair, secret included, runs once",
               len(LOG) == 1 and LOG[0]["settings"] == {"url": "u", "token": SECRETS[0]}, LOG)
            clear()
            store.save_guardrail_pair("factory", None, hook, "gr_a", {"url": "u", "token": SECRETS[0]}, "warn")
            store.save_guardrail_pair("project", shop, hook, "gr_a", {"url": "u", "token": SECRETS[0]}, "block")
            say()
            await hooks.run(hook, shop, {})
            ck("the same settings with another on_fail run both: a project may be stricter than the factory",
               len(LOG) == 2, LOG)
            clear()
            store.save_guardrail_pair("factory", None, hook, "gr_a", {"url": "u", "token": SECRETS[0]}, "block")
            store.save_guardrail_pair("project", shop, hook, "gr_a", {"url": "u", "token": SECRETS[2]}, "block")
            say()
            await hooks.run(hook, shop, {})
            ck("with another secret both run, each with its own settings",
               [x["settings"] for x in LOG] == [{"url": "u", "token": SECRETS[0]}, {"url": "u", "token": SECRETS[2]}], LOG)
            clear()
            store.save_guardrail_pair("factory", None, hook, "gr_a", {"url": "u", "token": SECRETS[0]}, "block")
            store.save_guardrail_pair("project", shop, hook, "gr_a", {"url": "u"}, "block")
            say()
            await hooks.run(hook, shop, {})
            ck("a project pair with no secret does not get the factory's: the guardrail sees no token",
               [x["settings"] for x in LOG] == [{"url": "u", "token": SECRETS[0]}, {"url": "u"}], LOG)
            clear()
            store.save_guardrail_pair("factory", None, "pre_release", "gr_a", {"url": "u"}, "block")
            store.save_guardrail_pair("project", shop, hook, "gr_a", {"url": "u"}, "block")
            say()
            await hooks.run(hook, shop, {})
            ck("the same integration and settings at another hook point is not deduped",
               len(LOG) == 1 and LOG[0]["stage"] == hook, LOG)
            await hooks.run("pre_release", shop, {})
            ck("it runs there too, once", len(LOG) == 2 and LOG[1]["stage"] == "pre_release", LOG)

            print("== run: block and warn ==")
            for on_fail, warn_only in (("block", False), ("warn", True)):
                clear()
                store.save_guardrail_pair("factory", None, hook, "gr_a", {}, on_fail)
                say(gr_a=deny("Too big"))
                out = await hooks.run(hook, shop, {})
                ck(f"on_fail {on_fail}: a denial is {'blocking' if not warn_only else 'a warning'}, reason kept",
                   len(out) == 1 and out[0].allow is False and out[0].warn_only is warn_only
                   and out[0].reason == "Too big" and out[0].integration == "gr_a", out)
                say(gr_a=Decision(allow=True, reason="fine"))
                out = await hooks.run(hook, shop, {})
                ck(f"on_fail {on_fail}: an allow with a reason passes through", len(out) == 1 and out[0].allow is True
                   and out[0].warn_only is False, out)
                say(gr_a=Decision(allow=False, reason="heads up", warn_only=True))
                out = await hooks.run(hook, shop, {})
                ck(f"on_fail {on_fail}: a warn-only answer stays a warning", len(out) == 1 and out[0].warn_only is True, out)
                say(gr_a=Decision(allow=True))
                ck(f"on_fail {on_fail}: an allow with no reason adds nothing", await hooks.run(hook, shop, {}) == [])
                say(gr_a=None)
                ck(f"on_fail {on_fail}: None adds nothing", await hooks.run(hook, shop, {}) == [])
            g = await hooks.gate(hook, shop, {})
            ck("gate shows the decisions with warn_only", g["ok"] is True and g["decisions"] == [] , g)
            say(gr_a=deny("Too big"))
            g = await hooks.gate(hook, shop, {})
            ck("gate: a warn pair is a warning and does not block, warn_only is in the decision",
               g["ok"] is True and g["reasons"] == [] and g["warnings"] == ["gr_a: Too big"]
               and g["decisions"][0]["warn_only"] is True, g)
            clear()
            store.save_guardrail_pair("factory", None, hook, "gr_a", {}, "block")
            g = await hooks.gate(hook, shop, {})
            ck("gate: a block pair blocks with the name and reason", g["ok"] is False
               and g["reasons"] == ["Blocked by gr_a: Too big"] and g["decisions"][0]["warn_only"] is False, g)

            print("== run: a guardrail that does not answer ==")
            old_seconds = hooks.PAIR_SECONDS
            hooks.PAIR_SECONDS = 0.2
            try:
                for on_fail, warn_only in (("block", False), ("warn", True)):
                    for how, ans in (("raises", RuntimeError(f"connection refused {SECRETS[6]}")), ("never answers", "sleep")):
                        clear()
                        store.save_guardrail_pair("factory", None, hook, "gr_a", {"url": "u", "token": SECRETS[0]}, on_fail)
                        say(gr_a=ans)
                        out = await hooks.run(hook, shop, {})
                        text = json.dumps([d.__dict__ for d in out])
                        ck(f"on_fail {on_fail}, a guardrail that {how}: the pair failed, warn_only {warn_only}",
                           len(out) == 1 and out[0].allow is False and out[0].warn_only is warn_only
                           and out[0].reason == "guardrail did not answer: gr_a" and out[0].integration == "gr_a", out)
                        ck("and the cause and the secrets are not in the answer",
                           SECRETS[6] not in text and "refused" not in text and SECRETS[0] not in text, text)
                clear()
                store.save_guardrail_pair("factory", None, hook, "gr_gone", {}, "block")
                saved = registry._loaded.pop(("guardrails", "gr_gone"))
                say()
                out = await hooks.run(hook, shop, {})
                ck("a pair whose integration is not installed anymore did not answer",
                   len(out) == 1 and out[0].reason == "guardrail did not answer: gr_gone"
                   and out[0].warn_only is False and LOG == [], out)
                registry._loaded[("guardrails", "gr_gone")] = saved
                clear()
                licensed[0] = True
                store.save_guardrail_pair("factory", None, hook, "gr_ee", {}, "warn")
                licensed[0] = False
                say()
                out = await hooks.run(hook, shop, {})
                ck("a pair that needs a licence the cell lacks did not answer, and was not run",
                   len(out) == 1 and out[0].reason == "guardrail did not answer: gr_ee"
                   and out[0].warn_only is True and LOG == [], (out, LOG))
                licensed[0] = True
                out = await hooks.run(hook, shop, {})
                ck("with the licence it runs", len(LOG) == 1 and out == [], (out, LOG))
                licensed[0] = False
                clear()
                store.save_guardrail_pair("factory", None, hook, "gr_a", {}, "block")
                store.save_guardrail_pair("factory", None, hook, "gr_b", {}, "block")
                store.save_guardrail_pair("project", shop, hook, "gr_c", {}, "warn")
                say(gr_a=RuntimeError("down"), gr_b=deny("No"), gr_c="sleep")
                out = await hooks.run(hook, shop, {})
                ck("one failing pair does not stop the next, and run does not raise",
                   [(d.integration, d.reason, d.warn_only) for d in out] == [
                       ("gr_a", "guardrail did not answer: gr_a", False), ("gr_b", "No", False),
                       ("gr_c", "guardrail did not answer: gr_c", True)], out)
                ck("all three were asked", [x["name"] for x in LOG] == ["gr_a", "gr_b", "gr_c"], LOG)
                g = await hooks.gate(hook, shop, {})
                ck("gate turns that into a block with both reasons", g["ok"] is False and len(g["reasons"]) == 2, g)
            finally:
                hooks.PAIR_SECONDS = old_seconds
            ck("PAIR_SECONDS is a module constant", hooks.PAIR_SECONDS == 10)

            print("== run: the slot's guardrail still runs first ==")
            clear()
            store.set_choice("guardrails", "gr_slot", {}, by="test")
            store.save_guardrail_pair("factory", None, hook, "gr_a", {}, "block")
            say(gr_slot=Decision(allow=True, reason="slot ok", integration="gr_slot"), gr_a=None)
            out = await hooks.run(hook, shop, {})
            ck("the slot's answer comes first, then the pairs", [x["name"] for x in LOG] == ["gr_slot", "gr_a"]
               and [d.reason for d in out] == ["slot ok"], (LOG, out))
            say(gr_slot=RuntimeError("slot down"), gr_a=None)
            try:
                await hooks.run(hook, shop, {})
                ck("the slot guardrail's error still raises", False, "no error")
            except RuntimeError:
                ck("the slot guardrail's error still raises", True)
            g = await hooks.gate(hook, shop, {})
            ck("and gate blocks on it", g["ok"] is False and "could not be checked" in g["reasons"][0], g)
            store.set_choice("guardrails", "tares", {}, by="test")

            print("== merge_ready: block, warn and the ticket's history ==")
            clear()
            store.save_guardrail_pair("factory", None, "pre_merge", "gr_a", {}, "block")
            say(gr_a=deny("Diff too large"))
            a = await mr.merge_ready(store, "acme/shop#7")
            ck("on_fail block: not ready with 'Blocked by <integration>: <reason>'",
               a["ready"] is False and "Blocked by gr_a: Diff too large" in a["reasons"], a)
            ck("the decision is in guardrails", a["guardrails"] == [{"allow": False, "reason": "Diff too large",
                                                                       "integration": "gr_a", "warn_only": False}], a)
            ck("a block writes no history", rows(tid) == [], rows(tid))
            ck("the ticket is in the payload", LOG[0]["ticket"], LOG)
            clear()
            pid = store.save_guardrail_pair("factory", None, "pre_merge", "gr_a", {}, "warn")[0]["id"]
            store.save_guardrail_pair("factory", None, "pre_release", "gr_b", {}, "warn")
            a = await mr.merge_ready(store, "acme/shop#7")
            ck("on_fail warn: ready, no reasons, guardrails lists it with warn_only",
               a["ready"] is True and a["reasons"] == [] and a["guardrails"][0]["warn_only"] is True
               and a["guardrails"][0]["reason"] == "Diff too large", a)
            ck("a pair for another hook point did not run for merge_ready", [x["name"] for x in LOG][-1:] == ["gr_a"]
               and all(x["name"] != "gr_b" for x in LOG))
            h1 = rows(tid)
            ck("the warning is in the ticket's history", len(h1) == 1 and h1[0]["value"] == "Diff too large"
               and h1[0]["reason"] == "pre_merge: gr_a" and h1[0]["role"] == "guardrail" and h1[0]["by"] is None, h1)
            await mr.merge_ready(store, "acme/shop#7")
            await mr.merge_ready(store, "acme/shop#7")
            ck("asking again writes it once", len(rows(tid)) == 1, rows(tid))
            say(gr_a=deny("Touches billing"))
            await mr.merge_ready(store, "acme/shop#7")
            ck("a different warning writes another", [r["value"] for r in rows(tid)] == ["Diff too large", "Touches billing"], rows(tid))
            await mr.merge_ready(store, "acme/shop#7")
            ck("and that one once too", len(rows(tid)) == 2)
            store.save_guardrail_pair("factory", None, "pre_merge", "gr_b", {}, "warn")
            say(gr_a=deny("Touches billing"), gr_b=deny("Second warning"))
            await mr.merge_ready(store, "acme/shop#7")
            await mr.merge_ready(store, "acme/shop#7")
            ck("two warnings at once are each written once", [r["value"] for r in rows(tid)] == [
                "Diff too large", "Touches billing", "Second warning"], rows(tid))
            say(gr_a=None, gr_b=None)
            clear()
            store.save_guardrail_pair("factory", None, "pre_merge", "gr_a", {}, "warn")
            say(gr_a=deny("Brand new warning"))
            n = len(rows(tid))
            orig = store.add_ticket_history

            def boom(*a, **k):
                raise RuntimeError("history is down")
            store.add_ticket_history = boom
            try:
                a = await mr.merge_ready(store, "acme/shop#7")
            finally:
                store.add_ticket_history = orig
            ck("a recording that fails does not break the answer",
               a["ready"] is True and a["guardrails"][0]["warn_only"] is True and len(rows(tid)) == n, a)
            say(gr_a=deny("Brand new warning"))
            out = await hooks.run("pre_merge", shop, {"repo": "acme/shop"})
            ck("run with no ticket writes nothing and does not fail",
               len(out) == 1 and out[0].warn_only is True and len(rows(tid)) == n, out)
            ck("an empty ticket too", len(await hooks.run("pre_merge", shop, {}, ticket="")) == 1 and len(rows(tid)) == n)
            g = await hooks.gate("pre_merge", shop, {}, ticket=tid)
            ck("gate passes the ticket on and exposes warn_only", g["decisions"][0]["warn_only"] is True
               and rows(tid)[-1]["value"] == "Brand new warning", g)

            print("== release_ready and the tool ==")
            clear()
            store.save_guardrail_pair("factory", None, "pre_release", "gr_a", {}, "warn")
            say(gr_a=deny("Friday release"))
            n = len(rows(tid))
            r = ru(await root.get("/api/factory/release-ready", params={"project": shop, "commit": "abc"}))
            ck("a warn: ready, listed in warnings and guardrails",
               r.json()["ready"] is True and r.json()["warnings"] == ["gr_a: Friday release"]
               and r.json()["guardrails"] == [{"allow": False, "reason": "Friday release", "integration": "gr_a",
                                                "warn_only": True}], r.text)
            ck("and nothing is written: it has no ticket", len(rows(tid)) == n and
               all(x["field"] != "guardrail_warn" for x in store.ticket_history(tid) if x["reason"] == "pre_release: gr_a"))
            out = json.loads(await mf.release_ready(project="Shop", commit="abc"))
            ck("the tool carries guardrails", out["ready"] is True and out["guardrails"][0]["warn_only"] is True, out)
            clear()
            store.save_guardrail_pair("factory", None, "pre_release", "gr_a", {}, "block")
            out = json.loads(await mf.release_ready(project="Shop", commit="abc"))
            ck("a block through the tool", out["ready"] is False and out["reasons"] == ["Blocked by gr_a: Friday release"], out)
            r = ru(await root.get("/api/factory/release-ready", params={"project": shop}))
            ck("a missing commit still answers with an empty guardrails list", r.json()["ready"] is False and r.json()["guardrails"] == [], r.text)
            a = json.loads(await mf.merge_ready("acme/shop#7"))
            ck("the merge_ready tool carries guardrails", "guardrails" in a and a["ready"] is True, a)

            print("== the hand-over ==")
            clear()
            store.save_guardrail_pair("factory", None, "pre_build", "gr_a", {}, "block")
            say(gr_a=deny("No builds this week"))
            body = {"repos": [{"path": "/w/blog", "name": "acme/blog"}]}
            r = ru(await root.post(f"/api/projects/{blog}/handover", json=body))
            ck("a block at pre_build is the 409 with the reason", r.status_code == 409 and "No builds this week" in r.text, r.text)
            ck("and nothing was recorded", not (store.get_handover(blog) or {}).get("repos"))
            ck("and no warning either", [x for x in store.ticket_history(tid) if x["reason"] == "pre_build: gr_a"] == [])
            clear()
            store.save_guardrail_pair("factory", None, "pre_build", "gr_a", {}, "warn")
            r = ru(await root.post(f"/api/projects/{blog}/handover", json=body))
            j = r.json()
            ck("a warning lets it through and is returned beside the handover", r.status_code == 200
               and j["handover"]["repos"] and j["warnings"] == ["gr_a: No builds this week"]
               and j["guardrails"][0]["warn_only"] is True, r.text)
            say(gr_a=None)
            r = ru(await root.post(f"/api/projects/{blog}/handover", json=body))
            ck("nothing to say: empty guardrails and warnings", r.status_code == 200 and r.json()["guardrails"] == []
               and r.json()["warnings"] == [], r.text)
            say(gr_a=deny("No builds this week"))
            out = await mf.hand_over(project="Blog", repo="/w/blog2")
            ck("the tool shows the warning", "Guardrail warning: gr_a: No builds this week" in out, out)
            clear()
            store.save_guardrail_pair("factory", None, "pre_build", "gr_a", {}, "block")
            out = await mf.hand_over(project="Blog", repo="/w/blog3")
            ck("the tool shows a block's reason", "No builds this week" in out, out)

            print("== deleting a project, /health ==")
            clear()
            store.create_project("p3", "custom", "gamma", {}, lead_person_id=ann["id"])
            store.save_guardrail_pair("project", "p3", "pre_merge", "gr_a", {}, "block")
            store.save_guardrail_pair("project", "p1", "pre_merge", "gr_a", {}, "block")
            store.save_guardrail_pair("factory", None, "pre_merge", "gr_a", {}, "block")
            store.delete_project("p3")
            ck("a deleted project's pairs go", store.list_guardrail_pairs("project", "p3") == [])
            ck("another project's and the factory's stay", len(store.list_guardrail_pairs("project", "p1")) == 1
               and len(store.list_guardrail_pairs("factory")) == 1)
            h = (await root.get("/health")).json()
            ck("/health lists guardrail_pairs", "guardrail_pairs" in h.get("capabilities", []), h)

            print("== no secret in any response, no em dash ==")
            blob = "\n".join(ALL)
            leaked = [s for s in SECRETS if s in blob]
            ck("no response held a secret value", leaked == [], leaked)
            ck("the responses carry no em dash", chr(0x2014) not in blob)
            for path in ("tares/guardrails_api.py", "tares/integrations/hooks.py", "tests/test_guardrail_pairs.py",
                         "changelog.d/TR-863.md"):
                ck(f"{path} has no em dash", chr(0x2014) not in open(os.path.join(ROOT, path)).read())

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
