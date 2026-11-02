"""The AI SRE Services page and a page per service (TR-750): GET /api/sre/services, GET
/api/sre/services/{name} and PUT /api/sre/services/{name}/settings.

The cell is the shared fixture (tests/fixtures/sre_cell.py, blind=True), invented names only. The
last case builds a cell of 24 services and 200,000 events and times the reads.
"""
import asyncio
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="tares-sre-services-")
ROOT_TOKEN = "root-token-sre-services"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT_TOKEN
os.environ["TARES_EDITION"] = "sre"                     # before make_app(): the first start picks it
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import PROJECT, _otlp, build
from tares import sre, sre_home
from tares import words
from tares.builtin_agents import AgentRunner
from tares.screens import sre_services

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    concluded.update({"outcome": "finding", "summary": "looked", "verdict": None, "key": None,
                      "label": None, "headline": None, "next_step": None, "produced": []})
    return "looked", 1, 1, [], False, ""


AgentRunner._loop = fake_loop

UTC = timezone.utc
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
MIDNIGHT = datetime(NOW.year, NOW.month, NOW.day, tzinfo=UTC)
UID = "prj-" + PROJECT
REPO = "example-org/service-a"
SEEN: list = []                       # every body built, checked for words at the end
SKIP = ("name,about,display_name,merge_options,repo_options,merged_names,title,what,detail,label,"
        "repo,updated_by,to").split(",")
LIST_KEYS = {"name", "about", "state", "state_words", "watched", "watch_words", "signals_words",
             "feeds_words", "events_24h_words", "last_words", "spark", "hidden"}
PAGE_KEYS = {"name", "about", "state", "state_words", "hidden", "watched", "watch_words",
             "signals_words", "feeds_words", "last_words", "events_24h_words", "chart", "incidents",
             "incidents_total", "incidents_open", "raised", "raised_total", "blind_spots_words",
             "blind_spots", "repos", "settings"}


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


def p95(values):
    return sorted(values)[max(0, int(len(values) * 0.95 + 0.999999) - 1)]


def ok_run(store, agent, rid, minutes_ago=3):
    """A finished check of `agent`, so the watcher has checked."""
    store.start_agent_run(rid, agent, "watch-prod", "d", "old", "h")
    store.finish_agent_run(rid, "ok")
    store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
                     [NOW - timedelta(minutes=minutes_ago), rid])


def raise_run(store, rid, service, minutes):
    """A finished check of the watcher that raised something about `service`."""
    at = max(NOW - timedelta(minutes=minutes), MIDNIGHT + timedelta(seconds=300 - minutes))
    store.start_agent_run(rid, "watcher", "watch-prod", "", service, "h", project=UID)
    store.finish_agent_run(rid, "ok", finding=f"{service} returns errors.", outcome="finding",
                           verdict="investigate")
    with store._swrite:
        store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])


def incident(store, service, minutes_ago, resolved=False):
    at = NOW - timedelta(minutes=minutes_ago)
    inc = store.create_incident(service, UID, at)
    rid = "r_" + uuid.uuid4().hex[:8]
    store.start_agent_run(rid, "investigator", "watch-prod", "", service, "h", woken_by="handoff",
                          project=UID)
    store.finish_agent_run(rid, "ok", finding=f"{service} returns errors.", outcome="finding",
                           verdict="rca")
    with store._swrite:
        store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])
    store.add_incident_note(inc["id"], rid, at)
    if resolved:
        store.set_incident_state(inc["id"], "resolved", None, None, "test", at=at, resolution="itself")
    return inc


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        auth = {"Authorization": f"Bearer {ROOT_TOKEN}"}
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=auth)
        store, runtime = app.state.store, app.state.runtime
        made = build(store, NOW, watcher=True, investigator=True, blind=True)
        runtime.reload_catalog()
        store.set_fired("watch-prod", "watch-prod", NOW - timedelta(minutes=3))
        sre_home.clear_cache()
        cat = runtime.catalog

        async def get(path, **kw):
            r = await cx.get(path, **kw)
            if r.status_code == 200:
                SEEN.append(r.json())
            return r

        async def put(name, body, **kw):
            r = await cx.put(f"/api/sre/services/{name}/settings", json=body, **kw)
            if r.status_code == 200:
                SEEN.append(r.json())
            return r

        # ── the list ────────────────────────────────────────────────────────
        print("== the list ==")
        r = await get("/api/sre/services")
        eq("GET /api/sre/services answers", r.status_code, 200)
        body = r.json()
        eq("the list has these keys", set(body), {"services", "hidden_count", "merged_words"})
        names = [s["name"] for s in body["services"]]
        eq("not watched first, then silent, no signal of its own, normal; then by name",
           [(s["name"], s["state"]) for s in body["services"]],
           [("docs-site", "not_watched"), ("service-a-receiver", "silent"), ("service-a-ui", "no_own_signal"),
            ("edge-proxy", "normal"), ("service-a", "normal"), ("service-b", "normal")])
        eq("every service is there", set(names), set(made["services"]) | {"service-a-ui"})
        eq("a row has these keys", {frozenset(s) for s in body["services"]}, {frozenset(LIST_KEYS)})
        eq("nothing is hidden or merged", (body["hidden_count"], body["merged_words"]), (0, None))
        row = {s["name"]: s for s in body["services"]}
        eq("watched says whether the watcher reads it", {n: r["watched"] for n, r in row.items()},
           {"docs-site": False, "service-a-receiver": True, "service-a-ui": None, "edge-proxy": True,
            "service-a": True, "service-b": True})
        eq("a watched service says by whom", row["edge-proxy"]["watch_words"], "Watched every 10 minutes")
        eq("one nobody can check says so", row["service-a-ui"]["watch_words"],
           "Nothing to watch, it has no events of its own")
        eq("it counts what came in", row["service-a"]["events_24h_words"].endswith(" in 24 hours"), True)
        eq("it says where the data comes from", row["edge-proxy"]["feeds_words"], "from prod-otlp")
        eq("a spark is 24 numbers", {len(s["spark"]) for s in body["services"]}, {24})

        print("== the state and words are the home page's ==")
        home = sre_home.build(store, cat, None, now=NOW)
        direct = sre_services.services_screen(store, cat, NOW)
        SEEN.append(direct)
        home_rows = {s["name"]: s for s in home["services"]}
        eq("the same services", sorted(s["name"] for s in direct["services"]), sorted(s["name"] for s in home["services"]))
        for s in direct["services"]:
            h = home_rows[s["name"]]
            eq(f"{s['name']}: state, words and about are the home page's",
               (s["state"], s["state_words"], s["about"], s["last_words"], s["spark"]),
               (h["state"], h["state_words"], h["about"], h["last_words"], h["spark"]))
        eq("a silent service says for how long", row["service-a-receiver"]["state_words"],
           "Sent nothing for 3 days")
        eq("and is silent", row["service-a-receiver"]["state"], "silent")
        eq("one seen through another says so", row["service-a-ui"]["state_words"],
           "Seen only through edge-proxy")

        # ── a hidden service ────────────────────────────────────────────────
        print("== a hidden service ==")
        r = await put("docs-site", {"hidden": True})
        eq("hide docs-site", r.status_code, 200)
        r = await get("/api/sre/services")
        eq("it is left out of the list", "docs-site" in [s["name"] for s in r.json()["services"]], False)
        eq("and counted", r.json()["hidden_count"], 1)
        r = await get("/api/sre/services?hidden=1")
        rows = {s["name"]: s for s in r.json()["services"]}
        eq("?hidden=1 includes it", "docs-site" in rows, True)
        eq("marked hidden", rows["docs-site"]["hidden"], True)
        eq("the rest are not", [n for n, s in rows.items() if s["hidden"]], ["docs-site"])
        eq("and still counted", r.json()["hidden_count"], 1)
        eq("a hidden service has a page", (await get("/api/sre/services/docs-site")).json()["hidden"], True)
        eq("its watch line says it is not checked", (rows["docs-site"]["watch_words"], rows["docs-site"]["last_words"]),
           ("Not checked, it is hidden", "not checked, it is hidden"))
        eq("and so does its page", (await get("/api/sre/services/docs-site")).json()["watch_words"], "Not checked, it is hidden")
        eq("which does not say it was checked",
           (await get("/api/sre/services/docs-site")).json()["blind_spots_words"],
           "Not checked while the service is hidden")
        await put("docs-site", {"hidden": False})
        eq("shown again", "docs-site" in [s["name"] for s in (await get("/api/sre/services")).json()["services"]], True)

        # ── a merged service ────────────────────────────────────────────────
        print("== a merged service ==")
        r = await put("service-a-receiver", {"merged_into": "service-a"})
        eq("merge service-a-receiver into service-a", r.status_code, 200)
        eq("the answer is the page of service-a", r.json()["name"], "service-a")
        eq("which lists the name it took", r.json()["settings"]["merged_names"], ["service-a-receiver"])
        body = (await get("/api/sre/services")).json()
        eq("the merged name is not a row any more", "service-a-receiver" in [s["name"] for s in body["services"]], False)
        eq("and the list names it", body["merged_words"], "service-a-receiver is shown as part of service-a")
        eq("its page is service-a's", (await get("/api/sre/services/service-a-receiver")).json()["name"], "service-a")
        r = await put("service-a-receiver", {"merged_into": None})
        eq("the settings can take it back out", (r.status_code, r.json()["settings"]["merged_names"]), (200, []))
        eq("and it is a row again", "service-a-receiver" in [s["name"] for s in (await get("/api/sre/services")).json()["services"]], True)

        # ── the page of a service ───────────────────────────────────────────
        print("== the page of a service ==")
        r = await get("/api/sre/services/nope")
        eq("an unknown service is 404", (r.status_code, r.json()), (404, {"detail": "No service called nope"}))
        r = await cx.put("/api/sre/services/nope/settings", json={"hidden": True})
        eq("so is a settings change on it", (r.status_code, r.json()), (404, {"detail": "No service called nope"}))
        r = await get("/api/sre/services/edge-proxy")
        page = r.json()
        eq("the page has these keys", set(page), PAGE_KEYS)
        eq("the chart has these keys", set(page["chart"]), {"window", "since", "until", "bucket_seconds", "errors", "traffic", "deploys"})
        eq("the default window is 24 hours", page["chart"]["window"], "24h")
        for window, n in (("1h", 60), ("24h", 60), ("7d", 84)):
            c = (await get(f"/api/sre/services/edge-proxy?window={window}")).json()["chart"]
            eq(f"{window}: {n} buckets of traffic and errors", (len(c["traffic"]), len(c["errors"])), (n, n))
            ck(f"{window}: never more errors than traffic in a bucket",
               all(e <= t for e, t in zip(c["errors"], c["traffic"])), (c["errors"], c["traffic"]))
        c = page["chart"]
        eq("edge-proxy sent five lines and one was an error", (sum(c["traffic"]), sum(c["errors"])), (5, 1))
        real = store.event_series

        def skewed(service, since, until, bucket_s, errors_only=False):
            rows = real(service, since, until, bucket_s, errors_only)
            return [(t, n + 3) for t, n in rows] if errors_only else rows      # a row landing between the two reads

        store.event_series = skewed
        try:
            c = sre_services.service_screen(store, cat, "edge-proxy", "24h", NOW)["chart"]
        finally:
            store.event_series = real
        ck("even when the two reads disagree, no bucket has more errors than traffic",
           all(e <= t for e, t in zip(c["errors"], c["traffic"])), (c["errors"], c["traffic"]))
        eq("a window that does not exist is 400", (await cx.get("/api/sre/services/edge-proxy?window=3d")).status_code, 400)
        eq("the same page by function", sre_services.service_screen(store, cat, "edge-proxy", "24h", NOW)["chart"]["window"], "24h")
        eq("an unknown name by function is None", sre_services.service_screen(store, cat, "nope", now=NOW), None)

        print("== deploys on the chart ==")
        eq("nothing is linked, so no deploys", (page["chart"]["deploys"], page["repos"]), ([], []))
        store.set_service("service-a", "test", repos=[REPO])
        sre_home.clear_cache()
        page = (await get("/api/sre/services/service-a")).json()
        marks = page["chart"]["deploys"]
        eq("service-a shows the pushes, the merge and the commits", sorted(m["label"] for m in marks),
           sorted(["add job metrics", "fix retry in job runner", "Merged fix flaky retry",
                   f"{REPO} pushed to main"]))
        eq("each says which repo", {m["repo"] for m in marks}, {REPO})
        ck("and when", all(re.match(r"\d{4}-\d\d-\d\dT", m["at"]) for m in marks), marks)
        eq("a side branch is not a deploy", any("side" in m["label"] for m in marks), False)
        hour = (await get("/api/sre/services/service-a?window=1h")).json()["chart"]["deploys"]
        eq("an hour shows only the last two", len(hour), 2)
        eq("the linked repo is listed", [r["name"] for r in page["repos"]], [REPO])
        eq("a repo whose name is the service's was matched by name", page["repos"][0]["linked_by"], "Matched by name")
        ck("and says when it last sent", page["repos"][0]["linked_words"].startswith("Last event "), page["repos"][0])
        store.set_service("edge-proxy", "test", repos=[REPO])
        eq("a repo with another name was linked in settings",
           (await get("/api/sre/services/edge-proxy")).json()["repos"][0]["linked_by"], "Linked in settings")
        store.set_service("edge-proxy", "test", repos=None)

        print("== blind spots ==")
        for s in ("edge-proxy", "docs-site", "service-b", "service-a-receiver"):
            store.set_service(s, "test", repos=[REPO])
        sre_home.clear_cache()
        page = (await get("/api/sre/services/edge-proxy")).json()
        eq("a service nobody has checked yet does not say it is fine",
           (page["blind_spots"], page["blind_spots_words"]), ([], "Not checked yet"))
        ok_run(store, "watcher", "check1")
        sre_home.clear_cache()
        page = (await get("/api/sre/services/edge-proxy")).json()
        eq("once the watcher has looked it can say so", page["blind_spots_words"], "No blind spots found")
        page = (await get("/api/sre/services/service-a-receiver")).json()
        eq("a silent service counts its blind spot", page["blind_spots_words"], "1 blind spot found")
        b = page["blind_spots"][0]
        eq("with a title, what it means and where to fix it", (set(b), b["kind"], b["action"]["to"].startswith("/")),
           ({"kind", "title", "detail", "action"}, "silent", True))
        eq("the action has a label", bool(b["action"]["label"]), True)
        eq("service-a-ui is seen through edge-proxy", (await get("/api/sre/services/service-a-ui")).json()["blind_spots"][0]["kind"],
           "seen_via_other")

        # ── incidents and what was raised ───────────────────────────────────
        print("== incidents ==")
        page = (await get("/api/sre/services/service-a")).json()
        eq("none yet", (page["incidents"], page["incidents_total"], page["incidents_open"]), ([], 0, 0))
        for i in range(2):
            incident(store, "service-a", 200 + i, resolved=True)
        for i in range(3):
            incident(store, "service-a", 60 + 10 * i)
        sre_home.clear_cache()
        page = (await get("/api/sre/services/service-a")).json()
        eq("five incidents", (len(page["incidents"]), page["incidents_total"], page["incidents_open"]), (5, 5, 3))
        eq("the open count matches the list", sum(1 for i in page["incidents"] if i["state"] != "resolved"), page["incidents_open"])
        eq("an incident has these keys", {frozenset(i) for i in page["incidents"]},
           {frozenset({"id", "title", "state", "state_words", "opened_words"})})
        eq("newest first", [i["state"] for i in page["incidents"]], ["open", "open", "open", "resolved", "resolved"])
        for i in range(7):
            incident(store, "service-a", 300 + i)
        sre_home.clear_cache()
        page = (await get("/api/sre/services/service-a")).json()
        eq("twelve incidents: ten show", (len(page["incidents"]), page["incidents_total"], page["incidents_open"]), (10, 12, 10))
        eq("another service has none of them", (await get("/api/sre/services/edge-proxy")).json()["incidents_total"], 0)
        eq("the list counts it as an incident", {s["name"]: s["state"] for s in (await get("/api/sre/services")).json()["services"]}["service-a"], "incident")
        eq("and puts it before the one that is not watched", [s["name"] for s in (await get("/api/sre/services")).json()["services"]][:2],
           ["service-a", "docs-site"])

        print("== what the watcher raised ==")
        page = (await get("/api/sre/services/service-b")).json()
        eq("nothing raised yet", (page["raised"], page["raised_total"]), ([], 0))
        for n in range(10):
            raise_run(store, f"rz{n}", "service-b", 5 + 10 * n)
        raise_run(store, "rz-other", "edge-proxy", 4)
        sre_home.clear_cache()
        page = (await get("/api/sre/services/service-b")).json()
        eq("ten raised: eight show", (len(page["raised"]), page["raised_total"]), (8, 10))
        eq("a raised item has these keys", {frozenset(r) for r in page["raised"]},
           {frozenset({"id", "when_words", "what", "outcome_words", "worth"})})
        eq("newest first", [r["id"] for r in page["raised"]][:3], ["rz0", "rz1", "rz2"])
        ck("worth is words to read", {r["worth"] for r in page["raised"]} <= {"Worth waking someone", "Worth a look", "For the record"}, page["raised"][0])
        eq("another service has its own", (await get("/api/sre/services/edge-proxy")).json()["raised_total"], 1)

        # ── the settings ────────────────────────────────────────────────────
        print("== the settings ==")
        page = (await get("/api/sre/services/service-a")).json()
        s = page["settings"]
        eq("the settings have these keys", set(s), {"display_name", "hidden", "merged_into", "merged_names",
                                                     "merge_options", "repo_options", "updated_by"})
        eq("merge_options leaves out the service itself", "service-a" in s["merge_options"], False)
        ck("and offers the others", {"edge-proxy", "docs-site"} <= set(s["merge_options"]), s["merge_options"])
        eq("repo_options are the repos GitHub sent", s["repo_options"], [REPO])
        key = (await cx.post("/api/keys", json={"name": "ops", "scopes": ["admin"]})).json()
        r = await put("service-a", {"display_name": "Service A", "repos": [REPO]},
                      headers={"Authorization": f"Bearer {key['secret']}"})
        eq("a rename saves", r.status_code, 200)
        eq("and comes back in the page", (r.json()["about"], r.json()["settings"]["display_name"]), ("Service A", "Service A"))
        eq("the answer is the page with its day chart", (r.json()["chart"]["window"], len(r.json()["chart"]["traffic"])), ("24h", 60))
        eq("by the credential's name", r.json()["settings"]["updated_by"], "ops")
        eq("the list shows it", {s["name"]: s["about"] for s in (await get("/api/sre/services")).json()["services"]}["service-a"], "Service A")
        eq("a read of the page agrees", (await get("/api/sre/services/service-a")).json()["settings"], r.json()["settings"])
        r = await put("service-a", {"display_name": None})
        eq("null clears the name", (r.json()["about"], r.json()["settings"]["display_name"], r.json()["settings"]["updated_by"]),
           (None, "", "auth token (env)"))
        r = await put("service-a", {"repos": []})
        eq("no repos unlinks", (r.status_code, r.json()["repos"], r.json()["chart"]["deploys"]), (200, [], []))
        r = await put("service-a", {"repos": [REPO], "hidden": True, "display_name": "A"})
        eq("several fields at once", (r.json()["hidden"], r.json()["about"], len(r.json()["repos"])), (True, "A", 1))
        await put("service-a", {"hidden": False, "display_name": None})
        for label, body in (("itself", {"merged_into": "service-a"}), ("nothing", {}), ("a field it does not have", {"colour": "red"}),
                            ("a repo nobody sent", {"repos": ["example-org/other"]}), ("a hidden that is not true or false", {"hidden": "yes"})):
            r = await cx.put("/api/sre/services/service-a/settings", json=body)
            eq(f"a change refused: {label}", r.status_code, 400)
        eq("a body that is not an object is 400", (await cx.put("/api/sre/services/service-a/settings", json=[1])).status_code, 400)

        print("== a name with a slash ==")
        store.append([_otlp(NOW, 2, "k8s/payments", "INFO", "handled")])
        store.__dict__.pop("services_cache", None)
        sre_home.clear_cache()
        r = await get("/api/sre/services/k8s/payments")
        eq("the page opens", (r.status_code, r.json().get("name")), (200, "k8s/payments"))
        r = await put("k8s/payments", {"display_name": "Payments"})
        eq("the settings save", (r.status_code, r.json().get("about")), (200, "Payments"))
        r = await put("k8s/payments", {"display_name": None})
        eq("and clear again", (r.status_code, r.json().get("about")), (200, None))
        r = await cx.put("/api/sre/services/k8s/nope/settings", json={"hidden": True})
        eq("an unknown one is a 404, not a 405", (r.status_code, r.json()), (404, {"detail": "No service called k8s/nope"}))

        print("== the home page follows a change ==")
        before = {s["name"]: s["about"] for s in (await get("/api/home")).json()["services"]}
        eq("the home page has no display name for service-a yet", before.get("service-a"), None)
        await put("service-a", {"display_name": "Service A"})
        after = {s["name"]: s["about"] for s in (await get("/api/home")).json()["services"]}
        eq("a rename shows on the home page at once, not after its cache", after.get("service-a"), "Service A")
        await put("service-a", {"display_name": None})

        print("== a read key ==")
        key = (await cx.post("/api/keys", json={"name": "viewer", "scopes": ["read"]})).json()
        read = {"Authorization": f"Bearer {key['secret']}"}
        before = store.service_settings().get("service-a")
        r = await cx.put("/api/sre/services/service-a/settings", headers=read, json={"hidden": True})
        eq("a read key may not change the settings", r.status_code, 403)
        eq("and nothing changed", store.service_settings().get("service-a"), before)
        eq("but it reads the list", (await cx.get("/api/sre/services", headers=read)).status_code, 200)
        eq("and the page", (await cx.get("/api/sre/services/service-a", headers=read)).status_code, 200)
        eq("no key is 401", (await httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t").get(
            "/api/sre/services")).status_code, 401)

        print("== the words ==")
        bad = []
        for body in SEEN:
            bad += words.offenders_in(body, SKIP)
        eq("no word a person should not read, in any answer above", sorted(set(bad)), [])
        dashes = [s for body in SEEN for s in strings(body) if chr(0x2014) in s]
        eq("no em dash in any string", dashes, [])
        every = [s for body in SEEN for s in strings(body)]
        ck("never 'Linked by name'", not any("Linked by name" in s for s in every))
        long = [s for s in every if any(int(m) >= 24 for m in re.findall(r"(?<!in )(\d+) hours", s))]
        ck("no duration counts days in hours", not long, long[:3])
        ck("no 'about 1 minutes'", not any(re.search(r"\b1 minutes\b|\b1 hours\b|\b1 days\b", s) for s in every))
        ck("every answer above was checked", len(SEEN) >= 40, len(SEEN))

    # ── the cost of a read ──────────────────────────────────────────────────
    print("== the cost of a read: 24 services, 200,000 events ==")
    import tares.daemon as daemon
    daemon.DB_PATH = os.path.join(TMP, "load.duckdb")      # a cell of its own; the daemon read the path at import
    big = make_app()
    async with big.router.lifespan_context(big):
        store, runtime = big.state.store, big.state.runtime
        t0 = time.time()
        made = build(store, NOW, watcher=True, investigator=True, services=20, events=200_000)
        print(f"  (cell built in {time.time() - t0:.0f} s)")
        runtime.reload_catalog()
        store.set_fired("watch-prod", "watch-prod", NOW - timedelta(minutes=3))
        for i in range(3):
            incident(store, made["services"][i], 20 + i)
        store.set_service("service-a", "test", repos=[REPO])
        bx = httpx.AsyncClient(transport=httpx.ASGITransport(app=big), base_url="http://t",
                               headers={"Authorization": f"Bearer {ROOT_TOKEN}"})
        sre_home.clear_cache()
        store.__dict__.pop("services_cache", None)
        t0 = time.perf_counter()
        r = await bx.get("/api/sre/services")
        first = (time.perf_counter() - t0) * 1000
        eq("the list answers on the big cell", (r.status_code, len(r.json()["services"]) >= 24), (200, True))
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            r = await bx.get("/api/sre/services")
            times.append((time.perf_counter() - t0) * 1000)
        slow = p95(times)
        print(f"  list, first read with nothing held: {first:.0f} ms")
        print(f"  list x20: min {min(times):.0f} ms, median {sorted(times)[10]:.0f} ms, p95 {slow:.0f} ms, max {max(times):.0f} ms")
        ck("list p95 under 300 ms", slow < 300, f"{slow:.0f} ms")
        eq("all 24 services are in it", len(r.json()["services"]), 24)
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            r = await bx.get("/api/sre/services/service-a")
            times.append((time.perf_counter() - t0) * 1000)
        slow = p95(times)
        print(f"  page x20: min {min(times):.0f} ms, median {sorted(times)[10]:.0f} ms, p95 {slow:.0f} ms, max {max(times):.0f} ms")
        ck("page p95 under 300 ms", slow < 300, f"{slow:.0f} ms")
        eq("and it is the whole page", (r.status_code, set(r.json()) == PAGE_KEYS, len(r.json()["chart"]["traffic"])), (200, True, 60))
        ck("with the traffic of 200,000 events in it", sum(r.json()["chart"]["traffic"]) > 90_000, sum(r.json()["chart"]["traffic"]))


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
