"""The Tares guardrails as readable rules (TR-862).

One in-process cell with auth on. Covered:
 - the built-in `tares` guardrails integration is installed, is the default for the guardrails slot, and
   its check always allows; /health lists the guardrail_rules capability;
 - GET /api/factory/guardrails/rules (an admin) lists the defaults (id, kind, role, text, reason, source
   default) and a station key gets 403;
 - a default cannot be edited, deleted or posted over: 403 on PUT, DELETE and a POST that names its id;
 - POST a deny rule (factory or project scope): a duplicate is not added twice, an empty, blank or too
   long text is 422, an allow rule is 422 pointing to the grants, a bad scope or kind is 422, a project
   scope without a project is 422, an unknown project is 404; a read key, a person's key (a project's
   lead included) and a station get 403: writing a rule is an admin's;
 - PUT changes the text, DELETE removes it, unknown is 404, a grant id is 422;
 - GET ?station=<name> renders the allow and soft_deny sentences for that station's role: the defaults
   of its role, the active team grants, the project's grants and the deny rules of the factory and the
   project, with {prod_pattern} filled from the crew settings; another role's defaults are absent; a
   revoked or personal grant is absent; a deny rule of another project is absent; an unknown role is
   422; a station reads its own rules with its own headers, an admin reads any;
 - deleting a project removes its rules.

Run: python tests/test_guardrail_rules.py
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-guardrail-rules-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-guardrails"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares.integrations import registry, resolve
from tares.integrations.builtin import guardrails_tares as g

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


URL = "/api/factory/guardrails/rules"


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def mk(token=TOKEN, station=None, role="builder"):
        hdr = {"Authorization": f"Bearer {token}"}
        if station:
            hdr.update({"X-Tares-Station": station, "X-Tares-Role": role})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)

    async with app.router.lifespan_context(app):
        ann = store.create_person("Ann", email="ann@example.com")
        ben = store.create_person("Ben", email="ben@example.com")
        store.create_project("p1", "custom", "alpha", {}, lead_person_id=ann["id"])
        store.create_project("p2", "custom", "beta", {}, lead_person_id=ben["id"])

        print("== the integration ==")
        registry.load()
        ck("the tares guardrails integration is installed",
           registry.get("guardrails", "tares") is g.TaresGuardrails)
        ck("it is the default of the guardrails slot", resolve.DEFAULTS["guardrails"] == "tares")
        d = await g.TaresGuardrails().check("pre_merge", {})
        ck("its check always allows", d.allow is True and d.integration == "tares", d)

        async with mk() as root:
            async def key(name, scopes, person=None):
                body = {"name": name, "scopes": scopes}
                if person:
                    body["person_id"] = person["id"]
                return (await root.post("/api/keys", json=body)).json()["secret"]

            reader = await key("reader", ["read"])
            ann_k = await key("ann", ["read", "ingest"], ann)
            ben_k = await key("ben", ["read", "ingest"], ben)

            h = (await root.get("/health")).json()
            ck("/health lists guardrail_rules", "guardrail_rules" in h.get("capabilities", []), h)

            print("== the defaults ==")
            r = await root.get(URL)
            ck("an admin lists", r.status_code == 200, r.text[:200])
            body = r.json()
            defaults = body["defaults"]
            ck("the defaults are listed, readable and read-only",
               len(defaults) >= 8 and all(x["text"] and x["reason"] and x["id"].startswith("default-")
                                          and x["source"] == "default" and x["kind"] in ("allow", "deny")
                                          for x in defaults), defaults[:1])
            ck("a fresh cell has no rule of its own", body["rules"] == [], body["rules"])
            async with mk(station="shop-build-x") as cx:
                ck("a station may not list everything", (await cx.get(URL)).status_code == 403)
            async with mk(reader) as cx:
                ck("a read key may not list everything", (await cx.get(URL)).status_code == 403)

            print("== a default is read-only ==")
            did = defaults[0]["id"]
            r = await root.put(f"{URL}/{did}", json={"text": "changed"})
            ck("editing a default is 403", r.status_code == 403 and "default" in r.text, (r.status_code, r.text))
            r = await root.delete(f"{URL}/{did}")
            ck("deleting a default is 403", r.status_code == 403, (r.status_code, r.text))
            r = await root.post(URL, json={"id": did, "kind": "deny", "text": "over it"})
            ck("posting over a default is 403", r.status_code == 403, (r.status_code, r.text))
            after = (await root.get(URL)).json()["defaults"]
            ck("the defaults are unchanged", after == defaults)

            print("== adding a deny rule ==")
            r = await root.post(URL, json={"scope": "factory", "kind": "deny",
                                           "text": "  Never   touch\nthe billing   tables.  "})
            ck("an admin adds a factory rule", r.status_code == 201 and r.json()["added"] is True, r.text)
            rule = r.json()["rule"]
            ck("the text is trimmed to one line",
               rule["text"] == "Never touch the billing tables." and rule["scope"] == "factory"
               and rule["project_uid"] is None and rule["kind"] == "deny", rule)
            r = await root.post(URL, json={"scope": "factory", "kind": "deny",
                                           "text": "Never touch the billing tables."})
            ck("the same rule is not added twice",
               r.json()["added"] is False and r.json()["rule"]["id"] == rule["id"], r.text)
            r = await root.post(URL, json={"scope": "project", "project": "p1", "kind": "deny",
                                           "text": "Do not edit the vendored folder."})
            prule = r.json()["rule"]
            ck("an admin adds a project rule", r.status_code == 201 and prule["project_uid"] == "p1", r.text)

            def bad(label, body, code, needle=""):
                async def go(cx=root):
                    r = await cx.post(URL, json=body)
                    ck(label, r.status_code == code and needle in r.text, (r.status_code, r.text[:200]))
                return go
            await bad("an empty text is 422", {"kind": "deny", "text": ""}, 422, "empty")()
            await bad("a blank text is 422", {"kind": "deny", "text": "   \n "}, 422, "empty")()
            await bad("a missing text is 422", {"kind": "deny"}, 422, "empty")()
            await bad("a null text is 422", {"kind": "deny", "text": None}, 422, "empty")()
            await bad("a too long text is 422", {"kind": "deny", "text": "x" * 501}, 422, "keep it")()
            ck("a text of the limit is accepted",
               (await root.post(URL, json={"kind": "deny", "text": "y" * 500})).status_code == 201)
            await bad("an allow rule is 422 and points to add_grant", {"kind": "allow", "text": "ok"}, 422, "add_grant")()
            await bad("an unknown kind is 422", {"kind": "maybe", "text": "ok"}, 422, "deny")()
            await bad("an unknown scope is 422", {"scope": "galaxy", "kind": "deny", "text": "ok"}, 422, "scope")()
            await bad("a project scope without a project is 422",
                      {"scope": "project", "kind": "deny", "text": "ok"}, 422, "project")()
            await bad("a factory rule with a project is 422",
                      {"scope": "factory", "project": "p1", "kind": "deny", "text": "ok"}, 422, "project")()
            await bad("an unknown project is 404",
                      {"scope": "project", "project": "nope", "kind": "deny", "text": "ok"}, 404)()

            print("== who may write ==")
            ck("a read key may not add", (await mk(reader).post(URL, json={"kind": "deny", "text": "nope"})).status_code == 403)
            ck("a person's own key may not add",
               (await mk(ann_k).post(URL, json={"kind": "deny", "text": "nope"})).status_code == 403)
            ck("not even to the project it leads",
               (await mk(ann_k).post(URL, json={"scope": "project", "project": "p1", "kind": "deny",
                                                "text": "nope"})).status_code == 403)
            ck("a station may not add", (await mk(station="shop-build-x").post(
                URL, json={"kind": "deny", "text": "nope"})).status_code == 403)
            for who, cx in (("a person", mk(ann_k)), ("a station", mk(station="shop-build-x"))):
                ck(f"{who} may not edit", (await cx.put(f"{URL}/{rule['id']}", json={"text": "x"})).status_code == 403)
                ck(f"{who} may not delete", (await cx.delete(f"{URL}/{rule['id']}")).status_code == 403)
            ck("the refused writes changed nothing",
               store.get_guardrail_rule(rule["id"])["text"] == "Never touch the billing tables.")

            print("== change and remove ==")
            r = await root.put(f"{URL}/{rule['id']}", json={"text": "Never touch billing or invoices."})
            ck("PUT changes the text", r.status_code == 200 and r.json()["rule"]["text"]
               == "Never touch billing or invoices.", r.text)
            r = await root.put(f"{URL}/{rule['id']}", json={"text": "  "})
            ck("PUT with a blank text is 422", r.status_code == 422, r.text)
            r = await root.put(f"{URL}/R9999", json={"text": "x"})
            ck("PUT of an unknown rule is 404", r.status_code == 404, r.text)
            r = await root.delete(f"{URL}/R9999")
            ck("DELETE of an unknown rule is 404", r.status_code == 404, r.text)
            tmp = (await root.post(URL, json={"kind": "deny", "text": "Temporary rule."})).json()["rule"]
            r = await root.delete(f"{URL}/{tmp['id']}")
            ck("DELETE removes it", r.status_code == 200 and r.json()["deleted"] is True
               and store.get_guardrail_rule(tmp["id"]) is None, r.text)

            print("== grants are the allow rules ==")
            team = store.add_grant_row("team", "Run the test suite freely", given_by=ann["id"])
            proj = store.add_grant_row("project", "Edit the alpha docs", project="p1", given_by=ann["id"])
            other = store.add_grant_row("project", "Beta only grant", project="p2", given_by=ben["id"])
            gone = store.add_grant_row("team", "Revoked words", given_by=ann["id"], state="revoked")
            mine = store.add_grant_row("personal", "Personal words", holder=ann["id"], given_by=ann["id"])
            r = (await root.get(URL)).json()
            ids = {x["id"]: x for x in r["rules"]}
            ck("the team grant is listed as an allow rule in place",
               ids[team["id"]]["kind"] == "allow" and ids[team["id"]]["source"] == "grant"
               and ids[team["id"]]["scope"] == "factory" and ids[team["id"]]["text"] == "Run the test suite freely",
               ids.get(team["id"]))
            ck("a project grant is listed with its project", ids[proj["id"]]["project_uid"] == "p1", ids.get(proj["id"]))
            ck("a revoked and a personal grant are not listed", gone["id"] not in ids and mine["id"] not in ids)
            r = (await root.get(URL, params={"project": "p1"})).json()
            ids = {x["id"] for x in r["rules"]}
            ck("the project filter leaves out another project's rules and grants",
               proj["id"] in ids and other["id"] not in ids and prule["id"] in ids and team["id"] in ids, ids)
            r = await root.put(f"{URL}/{team['id']}", json={"text": "x"})
            ck("a grant id is 422 and points to the grants", r.status_code == 422 and "grant" in r.text, r.text)
            r = await root.delete(f"{URL}/{proj['id']}")
            ck("deleting a grant id is 422", r.status_code == 422, r.text)
            ck("the grant is untouched", store.get_grant_row(proj["id"])["state"] == "active")

            print("== what a station runs under ==")
            r = await root.get(URL, params={"station": "shop-build-api", "project": "p1"})
            ck("an admin reads the rules of a builder", r.status_code == 200, r.text[:300])
            b = r.json()
            ck("the builder's role is inferred from the name", b["role"] == "builder" and b["station"] == "shop-build-api", b)
            ck("it holds the defaults of its role and of all roles",
               any("authorized" in s and "factory/*" in s for s in b["allow"])
               and any("Creating and editing files" in s for s in b["allow"]), b["allow"][:2])
            ck("it holds no other role's default",
               not any("tares factory spawn" in s for s in b["allow"])
               and not any("factory/review" in s for s in b["allow"])
               and not any("kind cluster" in s for s in b["allow"]), b["allow"])
            ck("the active team and project grants are allow sentences",
               any("Run the test suite freely" in s for s in b["allow"])
               and any("Edit the alpha docs" in s for s in b["allow"]), b["allow"])
            ck("the other project's, the revoked and the personal grants are not",
               not any(w in s for s in b["allow"] for w in ("Beta only", "Revoked words", "Personal words")), b["allow"])
            ck("the project's deny rule is a soft_deny sentence",
               any("Do not edit the vendored folder." in s for s in b["soft_deny"]), b["soft_deny"])
            ck("the factory's deny rule is a soft_deny sentence",
               any("Never touch billing or invoices." in s for s in b["soft_deny"]), b["soft_deny"])
            ck("the production default is in soft_deny with the pattern filled in",
               any("(prod|production)" in s and "{prod_pattern}" not in s for s in b["soft_deny"]), b["soft_deny"])
            ck("every rule is listed with its source",
               {x["source"] for x in b["rules"]} == {"default", "grant", "own"}
               and len(b["rules"]) == len(b["allow"]) + len(b["soft_deny"]), b["rules"])

            r = (await root.get(URL, params={"station": "shop-build-api", "project": "p2"})).json()
            ck("another project sees its own rules only",
               not any("vendored" in s for s in r["soft_deny"])
               and not any("Edit the alpha docs" in s for s in r["allow"])
               and any("Beta only grant" in s for s in r["allow"]), r)
            r = (await root.get(URL, params={"station": "shop-build-api"})).json()
            ck("without a project only the factory's rules apply",
               any("billing" in s for s in r["soft_deny"]) and not any("vendored" in s for s in r["soft_deny"])
               and not any("Edit the alpha docs" in s for s in r["allow"]), r)

            for name, role, mark in (("crew-orchestrator", "orchestrator", "tares factory spawn"),
                                     ("crew-reviewer", "reviewer", "factory/review"),
                                     ("crew-releaser", "releaser", "kind cluster")):
                r = (await root.get(URL, params={"station": name})).json()
                ck(f"{name} is a {role} and holds its own default",
                   r["role"] == role and any(mark in s for s in r["allow"]), r["role"])
            r = (await root.get(URL, params={"station": "builder"})).json()
            ck("a bare role name is that role (the ticket's check)",
               r["role"] == "builder" and len(r["allow"]) >= 4 and len(r["soft_deny"]) >= 1, r["role"])
            r = await root.get(URL, params={"station": "mystery"})
            ck("a name that says no role is 422", r.status_code == 422 and "role" in r.text, r.text)
            r = (await root.get(URL, params={"station": "mystery", "role": "reviewer"}))
            ck("a role param settles it", r.status_code == 200 and r.json()["role"] == "reviewer", r.text)
            r = await root.get(URL, params={"station": "mystery", "role": "wizard"})
            ck("an unknown role is 422", r.status_code == 422, r.text)
            r = await root.get(URL, params={"station": "shop-build-api", "project": "nope"})
            ck("an unknown project is 404", r.status_code == 404, r.text)

            async with mk(station="shop-build-api", role="builder") as cx:
                r = await cx.get(URL, params={"station": "shop-build-api", "project": "p1"})
                ck("a station reads its own rules", r.status_code == 200 and r.json()["role"] == "builder", r.text)
            async with mk(station="shop-helper-x", role="reviewer") as cx:
                r = await cx.get(URL, params={"station": "shop-helper-x"})
                ck("a station's header role settles its rules", r.status_code == 200 and r.json()["role"] == "reviewer", r.text)
            async with mk(reader) as cx:
                ck("a read key may not read a station's rules",
                   (await cx.get(URL, params={"station": "shop-build-api"})).status_code == 403)

            print("== the crew's production pattern ==")
            import json as _json
            store.set_setting("crew_settings", _json.dumps({"prod_pattern": "(^|-)live($|-)"}))
            r = (await root.get(URL, params={"station": "shop-build-api"})).json()
            ck("the crew's own pattern is filled in",
               any("(^|-)live($|-)" in s for s in r["soft_deny"]), r["soft_deny"])

            print("== a project's rules go with it ==")
            before = [x["id"] for x in store.list_guardrail_rules() if x["project"] == "p1"]
            ck("p1 has rules before", len(before) >= 1, before)
            store.delete_project("p1")
            ck("deleting the project removes its rules",
               [x for x in store.list_guardrail_rules() if x["project"] == "p1"] == []
               and any(x["scope"] == "factory" for x in store.list_guardrail_rules()))

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
