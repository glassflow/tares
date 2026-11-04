"""A private project stays out of the AI SRE screens (TR-892).

Lee leads Zephyrwatch, a private project that holds a watcher, an investigator and an incident on
the service zephyr-api. Kim is a teammate with an admin key who is not on it. The SRE screens
answer for the person asking: the incidents list leaves the incident, its service, its counts and
its words out, the incident page and its actions answer 404 exactly as for an incident that does
not exist, and the home, services, settings, runbooks and timeline screens do not name the project,
its agents or its service. Lee, the cell's own key and a key for the project still see all of it.
An open project's watcher and incident show to everyone, and with auth off everything shows.

Run: .venv/bin/python tests/test_sre_private_screens.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _env import scrub_os_environ
scrub_os_environ()
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="tares-sre-private-screens-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-sre-private"
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import build
from tares import incidents, sre_home
from tares import slack as slack_mod
from tares.screens.sre_runbooks import skill_name

P = F = 0
UTC = timezone.utc
LEAKS = ("zephyrwatch", "zephyr-api", "zephyr-seer", "zephyr-digger", "zephyrwatch broke")


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def leaks(body, *extra):
    text = json.dumps(body, default=str).lower()
    return [w for w in (*LEAKS, *extra) if w.lower() in text]


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    keys, pid = {}, {}

    async def call(who, method, path, body=None, **params):
        hdr = root if who == "root" else keys[who]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body, params=params or None)

    async def get(who, path, **params):
        sre_home.clear_cache()
        return await call(who, "GET", path, **params)

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        base = datetime.now(UTC).replace(microsecond=0)
        incidents.clock = lambda: base
        for who in ("kim", "lee"):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": ["admin"],
                "person": {"email": f"{who}-sp@example.com", "name": who.capitalize()}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]

        build(store, base)   # the cell's services; the pair of agents is Zephyrwatch's alone
        open_uid = "prj-edgewatch"   # an open project, no agents
        store.create_project(open_uid, "custom", "Edgewatch", {}, goal="Keep edge-proxy healthy")

        # Zephyrwatch: private, led by Lee, its own trigger, watcher and investigator
        vault = "prj-zephyrwatch"
        store.create_project(vault, "custom", "Zephyrwatch", {}, goal="Keep zephyr-api healthy",
                             lead_person_id=pid["lee"])
        store.set_project_scope(vault, "private")
        store.upsert_catalog_trigger("watch-zephyr", ["prod-otlp"], {"every": "10m"}, {}, "10m")
        handoffs = [{"verdict": "investigate", "agent": "zephyr-digger", "cooldown": "1h"}]
        store.upsert_catalog_agent("zephyr-seer", "watch-zephyr", "Look at zephyr-api.",
                                   budget_usd=25.0, budget_period="month", concludes=True,
                                   handoffs=handoffs)
        store.upsert_catalog_agent("zephyr-digger", "watch-zephyr", "Find the cause.",
                                   budget_usd=25.0, concludes=True)
        store.put_in_project("trigger", "watch-zephyr", vault)
        for a in ("zephyr-seer", "zephyr-digger"):
            store.put_in_project("agent", a, vault)
        store.set_wake(vault, "watch-zephyr", "zephyr-seer", enabled=True)
        store.set_handoffs(vault, "zephyr-seer", handoffs)
        # Edgewatch's own watcher, wired after the private one: a teammate edits and raises this one
        store.upsert_catalog_trigger("watch-edge", ["prod-otlp"], {"every": "30m"}, {}, "30m")
        store.upsert_catalog_agent("edge-seer", "watch-edge", "Look at edge-proxy.", budget_usd=20.0,
                                   budget_period="month", concludes=True)
        store.put_in_project("trigger", "watch-edge", open_uid)
        store.put_in_project("agent", "edge-seer", open_uid)
        store.set_wake(open_uid, "watch-edge", "edge-seer", enabled=True)
        runtime.reload_catalog()

        def run(rid, agent, key, finding, at, project):
            store.start_agent_run(rid, agent, "watch-zephyr" if project == vault else "watch-prod",
                                  "", key, "h", project=project)
            store.finish_agent_run(rid, "ok", finding=finding, outcome="finding", verdict="rca")
            with store._swrite:
                store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])

        def incident(service, project, rid, finding, minutes):
            at = base - timedelta(minutes=minutes)
            run(rid, "zephyr-digger" if project == vault else "investigator", service, finding, at,
                project)
            inc = store.create_incident(service, project, at)
            store.add_incident_note(inc["id"], rid, at)
            return inc["id"]

        secret = incident("zephyr-api", vault, "i-zephyr", "Zephyrwatch broke the queue.", 30)
        # a private incident on a service the whole cell sees
        shared = incident("service-a", vault, "i-shared", "Zephyrwatch retried service-a.", 25)
        with store._swrite:   # the private investigator read service-a's runbook this week
            store.sc.execute("UPDATE agent_runs SET skills = ? WHERE id = ?",
                             [json.dumps([skill_name("service-a")]), "i-shared"])
        shown = incident("edge-proxy", open_uid, "i-open", "502s on edge-proxy.", 20)
        # an incident that belongs to no project (made before projects): everyone sees it
        legacy = incident("docs-site", None, "i-legacy", "docs-site was slow.", 15)

        def raised(rid, agent, trigger, service, project, minutes):
            at = base - timedelta(minutes=minutes)
            store.start_agent_run(rid, agent, trigger, "", service, "h", project=project)
            store.finish_agent_run(rid, "ok", finding=f"{service} returns errors.", outcome="finding",
                                   verdict="investigate")
            with store._swrite:
                store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])

        raised("z-raised", "zephyr-seer", "watch-zephyr", "zephyr-api", vault, 12)
        raised("e-raised", "edge-seer", "watch-edge", "edge-proxy", open_uid, 11)

        print("== a teammate who is not on Zephyrwatch ==")
        r = await get("kim", "/api/sre/incidents")
        ck("the incidents list answers", r.status_code == 200, r.text[:120])
        body = r.json()
        ck("the list leaves the private incidents out",
           secret not in json.dumps(body) and shared not in json.dumps(body))
        ck("the list names nothing of the project", not leaks(body), str(leaks(body)))
        ck("the open incident is there", shown in json.dumps(body))
        ck("and the one that belongs to no project", legacy in json.dumps(body))
        ck("the counts leave them out", body["counts"]["open"] == 2, str(body["counts"]))
        r = await get("kim", "/api/sre/incidents", show="all")
        ck("the raised findings list holds the open project's and not the private one's",
           r.status_code == 200 and "e-raised" in json.dumps(r.json())
           and "z-raised" not in json.dumps(r.json()) and not leaks(r.json()), r.text[:300])
        r = await get("kim", "/api/sre/incidents", service="Zephyr-API")
        ck("a filter on a private service by another spelling is empty too",
           r.status_code == 200 and sum(len(g["items"]) for g in r.json()["groups"]) == 0
           and r.json()["counts"] == {"open": 0, "on_it": 0, "resolved": 0}, r.text[:200])
        r = await get("kim", "/api/sre/incidents", service="zephyr-api")
        ck("a filter on its service is empty", r.status_code == 200 and not leaks(r.json())
           and sum(len(g["items"]) for g in r.json()["groups"]) == 0, r.text[:200])
        r = await get("kim", f"/api/sre/incidents/{secret}")
        gone = await get("kim", "/api/sre/incidents/inc_doesnotexist")
        ck("its page is a 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        ck("the 404 says what an unknown incident says",
           r.json() == gone.json() and not leaks(r.json()), f"{r.text} vs {gone.text}")
        for act in ("on_it", "resolve", "reopen", "worth_yes"):
            r = await call("kim", "POST", f"/api/sre/incidents/{secret}/{act}")
            ck(f"{act} is a 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")
            inc = store.get_incident(secret)
            ck(f"{act} changed nothing", inc["state"] == "open" and not inc.get("owner"),
               str(inc["state"]))
        r = await get("kim", f"/api/sre/incidents/{shown}")
        ck("the open incident's page opens", r.status_code == 200, r.text[:120])

        for path in ("/api/home", "/api/sre/services", "/api/sre/settings", "/api/sre/runbooks",
                     "/api/sre/timeline"):
            r = await get("kim", path)
            ck(f"{path} answers", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
            if r.status_code == 200:
                ck(f"{path} names nothing of the project", not leaks(r.json(), vault),
                   str(leaks(r.json(), vault)))
        r = await get("kim", "/api/home")
        home = r.json()
        ck("the home counts only what she can see", home["headline"]["text"] == "2 incidents open"
           and home["incidents_more"] == 0 and len(home["incidents"]) == 2,
           f"{home['headline']['text']} more={home['incidents_more']}")
        r = await get("kim", "/api/sre/services")
        rows = {x["name"]: x for x in r.json()["services"]}
        ck("a shared service with a private incident reads as normal on the services page",
           rows["service-a"]["state"] == "normal", rows["service-a"]["state"])
        ck("the watch words come from the watchers she can see",
           all("10 minutes" not in x["watch_words"] for x in rows.values()),
           str([x["watch_words"] for x in rows.values()]))
        r = await call("kim", "PUT", "/api/sre/settings/watching", {"budget_usd": 7})
        ck("a change to the watcher touches the one she can see, not the private one",
           (store.get_catalog_agent("zephyr-seer", project=vault) or {}).get("budget_usd") == 25.0
           and (store.get_catalog_agent("edge-seer", project=open_uid) or {}).get("budget_usd") == 7.0,
           f"{r.status_code} {r.text[:160]}")
        store.upsert_catalog_agent("edge-seer", "watch-edge", "Look at edge-proxy.", budget_usd=20.0,
                                   budget_period="month", concludes=True)
        r = await call("kim", "POST", "/api/home/actions/raise_budget", {"amount_usd": 5})
        ck("raising the budget answers", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        ck("it raises the watcher she can see and not the private one",
           (store.get_catalog_agent("zephyr-seer", project=vault) or {}).get("budget_usd") == 25.0
           and (store.get_catalog_agent("edge-seer", project=open_uid) or {}).get("budget_usd") == 25.0,
           str([(a, (store.get_catalog_agent(a) or {}).get("budget_usd")) for a in ("zephyr-seer", "edge-seer")]))
        ck("and the home it answers with names nothing of the private project",
           r.status_code == 200 and not leaks(r.json(), vault), str(leaks(r.json(), vault)))
        # the Start checklist counts the Slack channel of an investigator she can see; the private
        # one has a channel and the cell has a token, so a leak would tick that step
        store.upsert_catalog_agent("zephyr-digger", "watch-zephyr", "Find the cause.", slack_channel="#zephyr",
                                   budget_usd=25.0, concludes=True)
        runtime.reload_catalog()
        store.set_setting(slack_mod.SETTING_KEY, "xoxb-test-not-a-real-token")
        try:
            r = await call("kim", "POST", "/api/home/start/hide")
            steps = {x["key"]: x for x in r.json().get("steps", [])} if r.status_code == 200 else {}
            ck("her Start checklist does not count the private investigator's channel",
               r.status_code == 200 and steps.get("slack", {}).get("done") is False, r.text[:300])
        finally:
            store.set_setting(slack_mod.SETTING_KEY, None)
        r = await get("kim", "/api/sre/services/service-a")
        ck("a shared service's page opens", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        if r.status_code == 200:
            ck("and lists none of the private incidents", r.json()["incidents_total"] == 0
               and not leaks(r.json(), vault), str(r.json()["incidents_total"]))
        r = await get("kim", "/api/sre/runbooks")
        ck("the runbooks name no private agent", "zephyr" not in json.dumps(r.json()).lower())
        ck("and count no read by a private agent",
           all(x["used_words"] is None for x in r.json()["services"]), r.text[:200])
        r = await call("kim", "POST", "/api/sre/runbooks/service-a/lines", {"text": "check the queue"})
        rb = skill_name("service-a")
        ck("a runbook line is written into the project she can see and not the private one",
           r.status_code == 200 and "zephyr" not in r.text.lower()
           and "check the queue" in ((store.get_skill(open_uid, rb) or {}).get("body") or "")
           and "check the queue" not in ((store.get_skill(vault, rb) or {}).get("body") or ""),
           f"{r.status_code} {r.text[:120]}")

        print("== the people who can see Zephyrwatch ==")
        for who in ("lee", "root"):
            r = await get(who, "/api/sre/incidents")
            ck(f"{who} sees the incident", secret in json.dumps(r.json()))
            ck(f"{who}: the counts", r.json()["counts"]["open"] == 4, str(r.json()["counts"]))
            r = await get(who, "/api/sre/incidents", show="all")
            ck(f"{who}: both raised findings", "z-raised" in json.dumps(r.json())
               and "e-raised" in json.dumps(r.json()), r.text[:200])
            r = await get(who, "/api/sre/services")
            ck(f"{who}: the shared service reads as having an incident",
               {x["name"]: x for x in r.json()["services"]}["service-a"]["state"] == "incident")
            r = await get(who, f"/api/sre/incidents/{secret}")
            ck(f"{who} opens its page", r.status_code == 200, r.text[:120])
            r = await get(who, "/api/sre/settings")
            ck(f"{who} sees the watcher in settings", "zephyr-seer" in json.dumps(r.json()).lower())
            r = await get(who, "/api/sre/runbooks")
            ck(f"{who}: the runbooks count the read by the private agent",
               any(x["used_words"] for x in r.json()["services"]), r.text[:200])
            r = await get(who, "/api/sre/services/service-a")
            ck(f"{who}: the service page lists the private incident",
               r.status_code == 200 and r.json()["incidents_total"] == 1, r.text[:120])

        print("== a member added to Zephyrwatch ==")
        sre_home.clear_cache()
        r = await call("kim", "GET", "/api/home")      # cached for this person while a stranger
        ck("before: the home has no private watcher", "zephyr" not in json.dumps(r.json()).lower())
        store.add_project_member(vault, pid["kim"])
        r = await call("kim", "GET", "/api/home")      # no cache clear: the cache must know who sees what
        ck("after: the same person's home shows it at once",
           "zephyr-seer" in json.dumps(r.json()).lower(), "the home cache served the old body")
        r = await get("kim", "/api/sre/incidents")
        ck("Kim now sees the incident", secret in json.dumps(r.json()))
        r = await get("kim", f"/api/sre/incidents/{secret}")
        ck("and opens its page", r.status_code == 200, r.text[:120])

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
