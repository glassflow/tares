"""Blind spots: what the cell cannot see (TR-747).

via_name (a port, a known suffix, the namespace prefix), services seen only through another
(listed with seen_via, no signals, no watcher row), the four blind-spot kinds over the shared AI SRE
fixture (silent, seen_via_other, rule_matches_nothing, no_deploys), repo links and a service's
deploys, the sre.via_labels setting, the words check on every title and detail, hidden services,
a cell without the new labels staying exactly as it was, the routes (and who may read them), and
the 200,000-event cell answering GET /api/blind-spots in under 2 seconds.

Run: .venv/bin/python tests/test_blind_spots.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
ROOT = "root-token-blind-spots"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["TARES_WATCH_SECONDS"] = "3600"   # a watch pass mid-build would cache half-built counts

import httpx

from fixtures.sre_cell import build
from tares import sre, watch, words
from tares.config import Catalog, catalog_from_db
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
COND = {"aggregate": "count", "predicate": "> 0", "window": "1m"}
FIVEXX = [{"field": "nginx_status_code", "op": "gte", "value": 500}]


def fresh(**kw):
    store = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "s.duckdb"))
    made = build(store, NOW, **kw)
    return store, catalog_from_db(store), made


def kinds(spots, kind, service=...):
    return [b for b in spots if b["kind"] == kind and (service is ... or b["service"] == service)]


def rule(store, name, sources, filters, days=40, **kw):
    """A rule made `days` days before NOW."""
    store.upsert_catalog_trigger(name, sources, kw.pop("condition", COND), {}, "5m", filters=filters)
    with store._swrite:
        store.sc.execute("UPDATE catalog_triggers SET created_at = ? WHERE name = ?",
                         [NOW - timedelta(days=days), name])
    return catalog_from_db(store)


def clean(spots):
    """Every title and detail passes the words check and has no dash."""
    bad = [(b["title"], b["detail"]) for b in spots
           if words.offenders(b["title"]) or words.offenders(b["detail"])
           or "\u2014" in b["title"] + b["detail"] or "\u2013" in b["title"] + b["detail"]]
    return bad


def pure():
    print("== via_name ==")
    known = {"service-a", "edge-proxy", "a", "api-service-a"}
    eq("a port is dropped", sre.via_name("default-service-a-ui-80", set(), "default"), "service-a-ui")
    eq("a known service ending the rest wins", sre.via_name("prod-service-a-8080", known, "other"),
       "service-a")
    eq("the longest known service wins", sre.via_name("prod-api-service-a-80", known), "api-service-a")
    eq("a known name needs a dash before it", sre.via_name("xservice-a-80", known - {"a"}), "xservice-a")
    eq("the namespace prefix is cut when the event's namespace is it",
       sre.via_name("shop-cart-80", set(), "shop"), "cart")
    eq("another namespace leaves the value", sre.via_name("shop-cart-80", set(), "other"), "shop-cart")
    eq("no port, no namespace: the value", sre.via_name("cart", set()), "cart")
    eq("a name that is only digits after the dash is a port", sre.via_name("cart-8080", set()), "cart")
    eq("a known name that ends in digits is tried before the port is cut",
       sre.via_name("prod-payments-2024", {"payments-2024", "payments"}), "payments-2024")
    eq("and a port after it is still cut", sre.via_name("prod-payments-2024-80", {"payments-2024"}),
       "payments-2024")
    ck("an address is not a service",
       all(sre.is_address(v) for v in ("10.0.3.4:8080", "10.0.3.4:8080, 10.0.3.5:8080", "[::1]:80", "10.0.0.1")))
    ck("a service name is not an address",
       not any(sre.is_address(v) for v in ("default-service-a-ui-80", "cafe1", "service-a", "10-a")))


def default_branch():
    print("== a polling source follows a default branch that is not main (F1) ==")
    from tares.config import _source_from_dict
    from tares.connectors.github import GithubConnector
    from tares.connectors import source_type_for
    from tares.envelope import Envelope

    def conn(**cfg):
        return GithubConnector(_source_from_dict({"name": "poll", "connector": "github", "config": {
            "repo": "example-org/service-a", **cfg,
            "labels": [{"name": "repo", "field": "repo", "primary": True}]}}), store=None)

    commit = {"sha": "abc1234", "commit": {"message": "ship it", "author": {
        "name": "Alex", "date": "2026-10-30T10:00:00Z"}}, "author": {"login": "alex"}}
    followed = conn()._commit_envelope({**commit, "_branch": "develop"}, "example-org/service-a")
    eq("a followed default branch is labelled", (followed.labels["branch"], followed.labels.get("on_default_branch")),
       ("develop", True))
    named = conn(branch="release")._commit_envelope({**commit, "_branch": "release"}, "example-org/service-a")
    ck("a branch named in the config is not said to be the default", "on_default_branch" not in named.labels,
       str(named.labels))

    # the poller itself: a stubbed GitHub, one commit on whatever branch is asked for
    pstore = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "p.duckdb"))

    def polled(meta_fails=False, **cfg):
        c = conn(prs=False, **cfg)
        c.store = pstore
        c.cfg.name = f"poll-{len(cfg)}-{meta_fails}-{cfg.get('branch', '')}"

        async def fake_get(cx, url, token, params, etag_key, repo):
            if url.endswith("/commits"):
                return [commit]
            if meta_fails:
                raise ValueError("GitHub returned 500")
            return {"default_branch": "develop"}
        c._get = fake_get
        return asyncio.run(c.poll())

    flags = lambda envs: [(e.labels.get("branch"), e.labels.get("on_default_branch")) for e in envs]
    eq("polling with no branch follows the default and says so", flags(polled()), [("develop", True)])
    eq("polling the branch discovery filled in, which is the default, says so too",
       flags(polled(branch="develop")), [("develop", True)])
    eq("polling a pinned side branch does not say so", flags(polled(branch="release")), [("release", None)])
    eq("a default branch GitHub will not name is no reason to lose the commits",
       flags(polled(meta_fails=True, branch="develop")), [("develop", None)])

    store = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "d.duckdb"))
    for name, extra in (("poll", {}), ("pinned", {"branch": "release"})):
        store.upsert_catalog_source(name, source_type_for("github"), "github", "1h", {
            "repo": "example-org/service-a", **extra,
            "labels": [{"name": "repo", "field": "repo", "primary": True}]})
    cat = catalog_from_db(store)
    t = NOW - timedelta(minutes=10)

    def put(source, text, labels):
        store.append([Envelope(source=source, source_type=source_type_for("github"),
                               key_value="example-org/service-a", event_type="commit", text=text,
                               event_time=t, payload={}, ingest_time=t,
                               labels={"repo": "example-org/service-a", **labels})])

    put("poll", "stamped on develop", {"branch": "develop", "on_default_branch": "true"})
    put("poll", "stored before the stamp", {"branch": "trunk"})
    put("pinned", "on a pinned branch", {"branch": "release"})
    put("poll", "no branch at all", {})
    got = sorted(d["text"] for d in store.deploy_events(
        sre.github_sources(cat), ["example-org/service-a"], None, 50,
        [s.name for s in cat.sources.values() if s.name == "poll"]))
    eq("a stamped commit, an older one from the same source and one with no branch count",
       got, ["no branch at all", "stamped on develop", "stored before the stamp"])
    eq("without the source in the fallback, an older develop commit is dropped",
       sorted(d["text"] for d in store.deploy_events(sre.github_sources(cat), ["example-org/service-a"], None, 50)),
       ["no branch at all", "stamped on develop"])
    store.set_service("service-a", "tester", repos=["example-org/service-a"])
    eq("deploys() works the fallback out from the source's config",
       sorted(d["text"] for d in sre.deploys(store, cat, "service-a", None)),
       ["no branch at all", "stamped on develop", "stored before the stamp"])


def reading():
    print("== a cell without via labels or GitHub stays as it was ==")
    store, cat, made = fresh()
    items = sre.list_services(store, cat, NOW)
    eq("the four services", [i["name"] for i in items],
       ["service-a", "edge-proxy", "docs-site", "service-a-receiver"])
    ck("no seen_via key on any", not any("seen_via" in i for i in items))
    spots = sre.blind_spots(store, cat, NOW)
    eq("nothing is silent or seen through another: only the deploys are not tied",
       sorted({b["kind"] for b in spots}), ["no_deploys"])
    eq("with GitHub connected but no repo linked, each service with signals gets one",
       sorted(b["service"] for b in spots), ["docs-site", "edge-proxy", "service-a", "service-a-receiver"])
    empty = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "n.duckdb"))
    eq("an empty cell has none", sre.blind_spots(empty, Catalog({}, []), NOW), [])

    print("== the fixture cell with its blind spots ==")
    store, cat, made = fresh(blind=True)
    items = sre.list_services(store, cat, NOW)
    got = {i["name"]: i for i in items}
    eq("service-a-ui is listed, with no events of its own",
       (got["service-a-ui"]["signals"], got["service-a-ui"]["events_24h"],
        got["service-a-ui"]["events_10m"], got["service-a-ui"]["seen_via"]),
       ([], 0, 0, ["edge-proxy"]))
    ck("and the other services have no seen_via", not any(
        "seen_via" in i for n, i in got.items() if n != "service-a-ui"))
    eq("its feeds and last seen are empty", (got["service-a-ui"]["feeds"], got["service-a-ui"]["last_seen"]),
       ([], None))
    one = sre.get_service(store, cat, "service-a-ui", NOW)
    eq("get_service answers for it", (one["name"], one["seen_via"], one["by_feed"], one["repos"],
                                      one["last_deploy"]), ("service-a-ui", ["edge-proxy"], [], [], None))
    ck("the watch table does not count it", "service-a-ui" not in watch.compute(store, cat, NOW))
    ck("and does count the others", "service-a" in watch.compute(store, cat, NOW))

    spots = sre.blind_spots(store, cat, NOW)
    kind_order = [b["kind"] for b in spots]
    eq("sorted: silent, seen_via_other, rule_matches_nothing, no_deploys", kind_order,
       sorted(kind_order, key=["silent", "seen_via_other", "rule_matches_nothing", "no_deploys"].index))
    silent = kinds(spots, "silent")
    eq("only the receiver is silent", [b["service"] for b in silent], ["service-a-receiver"])
    s = silent[0]
    eq("its words", (s["title"], s["detail"], s["since"]),
       ("service-a-receiver has sent nothing for 3 days", "It last sent on 27 Oct, 12:00.",
        "2026-10-27T12:00:00+00:00"))
    eq("its fix", (s["fix"], s["rule"]), ({"label": "Check its connection", "action": "service"}, None))
    ck("service-a is not silent", not kinds(spots, "silent", "service-a"))
    ck("service-b, with 3 events ever, is not silent (too few)",
       "service-b" in {i["name"] for i in items} and not kinds(spots, "silent", "service-b"))

    via = kinds(spots, "seen_via_other")
    eq("service-a-ui is seen through edge-proxy", [(b["service"], b["title"], b["detail"], b["fix"])
                                                   for b in via],
       [("service-a-ui", "service-a-ui is seen only through edge-proxy",
         "Only errors at the ingress show it. Connect its own logs to see why it fails.",
         {"label": "Connect its logs", "action": "connect"})])
    ck("service-a-ui has no deploys item (it has no signals)", not kinds(spots, "no_deploys", "service-a-ui"))
    eq("no rule yet", kinds(spots, "rule_matches_nothing"), [])

    print("== an upstream that is an address is not a service (F7) ==")
    from fixtures.sre_cell import _otlp
    store.append([_otlp(NOW, 3, "edge-proxy", "INFO", "GET /ui 200",
                        {"namespace": "default", "nginx_status_code": "200",
                         "proxy_upstream_name": "10.0.3.4:8080"})])
    after = sre.list_services(store, cat, NOW)
    eq("no service is made from the address", sorted(i["name"] for i in after),
       sorted(i["name"] for i in items))
    ck("and no seen_via_other item names it", not any(
        "10.0.3.4" in (b["service"] + b["title"] + b["detail"])
        for b in kinds(sre.blind_spots(store, cat, NOW), "seen_via_other")))

    print("== repo links and deploys ==")
    eq("service-a has a deploys item before it is linked", len(kinds(spots, "no_deploys", "service-a")), 1)
    eq("the item", kinds(spots, "no_deploys", "service-a")[0]["title"], "Deploys are not tied to service-a")
    eq("its fix", kinds(spots, "no_deploys", "service-a")[0]["fix"], {"label": "Link a repo", "action": "link_repo"})
    eq("github's repos", sre.github_repos(store, cat).keys(), {"example-org/service-a"})
    for bad in ("example-org/x", "", 7):
        try:
            sre.patch_service(store, cat, "service-a", {"repos": [bad]}, "tester")
            ck(f"repos {bad!r} is refused", False)
        except ValueError as e:
            ck(f"repos {bad!r} is refused", True)
            if bad == "example-org/x":
                eq("and says why", str(e), "example-org/x has sent nothing to this workspace yet; connect it first")
    try:
        sre.patch_service(store, cat, "service-a", {"repos": [f"example-org/r{i}" for i in range(21)]}, "tester")
        ck("21 repos are refused", False)
    except ValueError as e:
        ck("21 repos are refused", "at most 20" in str(e), str(e))
    eq("nothing was stored", store.service_settings().get("service-a"), None)
    eq("deploys before linking are empty", sre.deploys(store, cat, "service-a", None), [])
    out = sre.patch_service(store, cat, "service-a", {"repos": ["example-org/service-a"]}, "tester")
    eq("the patch answers with the repos", out["repos"], ["example-org/service-a"])
    d = sre.deploys(store, cat, "service-a", None)
    eq("deploys: the push, the merge and the two commits, newest first",
       [(x["kind"], x["text"]) for x in d],
       [("push", "push to main"), ("merge", "merge fix flaky retry"),
        ("commit", "fix retry in job runner"), ("commit", "add job metrics")])
    ck("a push to a side branch is not a deploy", not any("side" in x["text"] for x in d))
    eq("a deploy's fields", sorted(d[0]), ["at", "kind", "repo", "text", "url"])
    eq("the url is the payload's html_url, else the label", (d[0]["url"], d[1]["url"]),
       ("https://example.invalid/example-org/service-a/commit/abc", "https://example.invalid/example-org/service-a/pull/7"))
    eq("a window limits them", [x["kind"] for x in sre.deploys(store, cat, "service-a", NOW - timedelta(minutes=100))],
       ["push", "merge", "commit"])
    eq("a limit", len(sre.deploys(store, cat, "service-a", None, limit=1)), 1)
    got = sre.get_service(store, cat, "service-a", NOW)
    eq("get_service has the repos and the last deploy", (got["repos"], got["last_deploy"]),
       (["example-org/service-a"], {"at": d[0]["at"], "text": "push to main"}))
    eq("a service with no repos has a null last_deploy",
       sre.get_service(store, cat, "edge-proxy", NOW)["last_deploy"], None)
    spots = sre.blind_spots(store, cat, NOW)
    eq("service-a's no_deploys is gone", kinds(spots, "no_deploys", "service-a"), [])
    ck("the others keep theirs", kinds(spots, "no_deploys", "edge-proxy"))
    eq("get_service has only its own spots", sre.get_service(store, cat, "service-a-receiver", NOW)["blind_spots"],
       [b for b in spots if b["service"] == "service-a-receiver"])
    store.set_service("service-b", "tester", merged_into="service-a")
    eq("repos of a merged name belong to its target",
       sre._linked_repos(store.service_settings(), "service-a"), ["example-org/service-a"])
    store.set_service("service-b", "tester", merged_into=None)
    store.set_service("service-a", "tester", repos=[])

    print("== a cell with no GitHub events ==")
    bcat = Catalog({k: v for k, v in cat.sources.items() if k != "github"}, cat.triggers)
    cell = kinds(sre.blind_spots(store, bcat, NOW), "no_deploys")
    eq("one cell-level item", [(b["service"], b["title"], b["fix"]) for b in cell],
       [(None, "No code changes are connected", {"label": "Connect GitHub", "action": "connect"})])

    print("== rules that match nothing ==")
    cat = rule(store, "five-hundreds", ["docs-site"], FIVEXX)
    spots = sre.blind_spots(store, cat, NOW)
    r = kinds(spots, "rule_matches_nothing")
    eq("one rule", [(b["rule"], b["service"], b["since"], b["fix"]) for b in r],
       [("five-hundreds", None, None, {"label": "Review the rule", "action": "rule"})])
    eq("the title", r[0]["title"], "A rule has matched nothing for 30 days")
    ck("the detail starts with the rule in words", r[0]["detail"].startswith("Wakes when "), r[0]["detail"])
    ck("and says the field no longer arrives",
       r[0]["detail"].endswith(" It looks for nginx_status_code, which no longer arrives."), r[0]["detail"])
    ck("without the rule's name", "five-hundreds" not in r[0]["detail"] + r[0]["title"])
    eq("the title uses RULE_DAYS", r[0]["title"], f"A rule has matched nothing for {sre.RULE_DAYS} days")
    with store._lock:
        store._truncate_entity_label("docs-site", "nginx_status_code")
    ck("a field too busy to count is not said to be gone",
       "no longer arrives" not in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")[0]["detail"])
    with store._lock:
        store.con.execute("UPDATE entity_label_state SET truncated = FALSE")
    eq("sorted after seen_via_other and before no_deploys", [b["kind"] for b in spots][:3],
       ["silent", "seen_via_other", "rule_matches_nothing"])

    cat = rule(store, "edge-fivexx", ["prod-otlp"], [{"field": "service", "op": "eq", "value": "edge-proxy"}, *FIVEXX])
    r = {b["rule"]: b for b in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")}
    eq("a filter on the service label sets the service", r["edge-fivexx"]["service"], "edge-proxy")
    ck("a field its sources still carry is not reported as gone",
       "no longer arrives" not in r["edge-fivexx"]["detail"], r["edge-fivexx"]["detail"])
    store.set_service("edge-proxy", "tester", hidden=True)
    ck("hiding the service takes its rule away",
       "edge-fivexx" not in {b["rule"] for b in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")})
    store.set_service("edge-proxy", "tester", hidden=False)

    store.set_fired("five-hundreds", "five-hundreds", NOW - timedelta(days=10))
    ck("a firing 10 days ago clears it",
       "five-hundreds" not in {b["rule"] for b in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")})
    store.set_fired("five-hundreds", "five-hundreds", NOW - timedelta(days=31))
    ck("a firing 31 days ago does not",
       "five-hundreds" in {b["rule"] for b in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")})
    cat = rule(store, "tick", ["prod-otlp"], [], condition={"every": "10m"})
    ck("a schedule never appears",
       "tick" not in {b["rule"] for b in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")})
    cat = rule(store, "young", ["docs-site"], FIVEXX, days=5)
    ck("a rule under 30 days old does not appear",
       "young" not in {b["rule"] for b in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")})
    cat = rule(store, "paused-one", ["docs-site"], FIVEXX)
    store.set_trigger_paused("paused-one", True)
    cat = catalog_from_db(store)
    ck("a paused rule does not appear",
       "paused-one" not in {b["rule"] for b in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")})
    cat = rule(store, "notes-only", ["findings"], [])
    ck("a rule reading only the findings does not appear",
       "notes-only" not in {b["rule"] for b in kinds(sre.blind_spots(store, cat, NOW), "rule_matches_nothing")})

    print("== words ==")
    spots = sre.blind_spots(store, cat, NOW)
    ck("a spot of every kind is checked", {b["kind"] for b in spots}
       == {"silent", "seen_via_other", "rule_matches_nothing", "no_deploys"}, str({b["kind"] for b in spots}))
    eq("every title and detail passes the words check and has no dash", clean(spots), [])
    eq("and the cell-level one", clean(cell), [])
    eq("every item has the same keys", {tuple(sorted(b)) for b in spots},
       {("detail", "fix", "kind", "rule", "service", "since", "title")})

    print("== hidden services ==")
    store.set_service("service-a-receiver", "tester", hidden=True)
    store.set_service("service-a-ui", "tester", hidden=True)
    store.set_service("service-a", "tester", hidden=True)
    spots = sre.blind_spots(store, cat, NOW)
    eq("hiding removes their items", [b for b in spots if b["service"] in
                                      ("service-a-receiver", "service-a-ui", "service-a")], [])
    ck("others stay", kinds(spots, "no_deploys", "edge-proxy"))

    print("== the via labels setting ==")
    store, cat, made = fresh(blind=True)
    store.set_setting(sre.VIA_SETTING, json.dumps({"prod-otlp": []}))
    ck("an empty list turns the default off", "service-a-ui" not in
       {i["name"] for i in sre.list_services(store, cat, NOW)})
    store.set_setting(sre.VIA_SETTING, json.dumps({"prod-otlp": ["nothing_here"]}))
    ck("a label the source does not carry finds nothing", "service-a-ui" not in
       {i["name"] for i in sre.list_services(store, cat, NOW)})
    store.set_setting(sre.VIA_SETTING, json.dumps({"prod-otlp": ["proxy_upstream_name"]}))
    ck("naming the label finds it", "service-a-ui" in {i["name"] for i in sre.list_services(store, cat, NOW)})
    store.set_setting(sre.VIA_SETTING, "not json")
    ck("a broken setting falls back to the default", "service-a-ui" in
       {i["name"] for i in sre.list_services(store, cat, NOW)})
    store.set_setting(sre.VIA_SETTING, json.dumps({"prod-otlp": "proxy_upstream_name", "x": [7]}))
    ck("a malformed entry gives none for that source", "service-a-ui" not in
       {i["name"] for i in sre.list_services(store, cat, NOW)})
    store.set_setting(sre.VIA_SETTING, None)
    store.set_service("service-a-ui", "tester", display="Service A UI")
    eq("a via-only service can be renamed", sre.get_service(store, cat, "service-a-ui", NOW)["display"], "Service A UI")
    eq("and the spot uses the name", kinds(sre.blind_spots(store, cat, NOW), "seen_via_other")[0]["title"],
       "Service A UI is seen only through edge-proxy")
    store.set_service("service-a-ui", "tester", merged_into="service-a")
    ck("merged into a service it is gone from the list", "service-a-ui" not in
       {i["name"] for i in sre.list_services(store, cat, NOW)})


async def over_http():
    from tares import daemon
    app = daemon.make_app()
    H = {"Authorization": f"Bearer {ROOT}"}
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H)
        store = app.state.store
        print("== the routes on an empty cell ==")
        eq("blind spots", (await cx.get("/api/blind-spots")).json(), {"blind_spots": []})
        eq("repos", (await cx.get("/api/repos")).json(), {"repos": []})
        eq("a refused link", (await cx.patch("/api/services/x", json={"repos": ["nobody/nothing"]})).status_code, 400)
        eq("deploys of an unknown service", (await cx.get("/api/services/x/deploys")).status_code, 404)

        now = datetime.now(timezone.utc)
        build(store, now, blind=True)
        app.state.runtime.reload_catalog()
        store.clear_services_cache()

        print("== GET /api/services, /api/repos, /api/blind-spots ==")
        body = (await cx.get("/api/services")).json()["services"]
        got = {i["name"]: i for i in body}
        eq("service-a-ui, seen through edge-proxy", (got["service-a-ui"]["seen_via"], got["service-a-ui"]["signals"],
                                                   got["service-a-ui"]["watch"]["watched"]), (["edge-proxy"], [], None))
        eq("repos for the picker", (await cx.get("/api/repos")).json()["repos"][0]["repo"], "example-org/service-a")
        eq("with no service yet", (await cx.get("/api/repos")).json()["repos"][0]["services"], [])
        r = (await cx.get("/api/blind-spots")).json()["blind_spots"]
        eq("the kinds", sorted({b["kind"] for b in r}), ["no_deploys", "seen_via_other", "silent"])
        eq("and the words", clean(r), [])

        print("== linking a repo ==")
        r = await cx.patch("/api/services/service-a", json={"repos": ["example-org/nope"]})
        eq("an unknown repo is 400 with the repo in the message", (r.status_code, "example-org/nope has sent nothing" in r.text),
           (400, True))
        r = await cx.patch("/api/services/service-a", json={"repos": ["example-org/service-a"]})
        eq("a known one links", (r.status_code, r.json()["repos"], r.json()["last_deploy"] is not None),
           (200, ["example-org/service-a"], True))
        eq("the picker shows the link", (await cx.get("/api/repos")).json()["repos"][0]["services"], ["service-a"])
        one = (await cx.get("/api/services/service-a")).json()
        eq("the service has the repos, the last deploy and its spots", (one["repos"], sorted(one["last_deploy"]),
           one["blind_spots"]), (["example-org/service-a"], ["at", "text"], []))
        r = await cx.get("/api/services/service-a/deploys?window=7d")
        d = r.json()["deploys"]
        eq("deploys, newest first", (r.status_code, [x["kind"] for x in d]), (200, ["push", "merge", "commit", "commit"]))
        eq("a short window", [x["kind"] for x in (await cx.get("/api/services/service-a/deploys?window=2h")).json()["deploys"]],
           ["push", "merge", "commit", "commit"][:3])
        eq("a window that is not one", (await cx.get("/api/services/service-a/deploys?window=3y")).status_code, 400)
        eq("an unknown service", (await cx.get("/api/services/nope/deploys")).status_code, 404)
        eq("a service whose name has a slash", (await cx.get("/api/services/a/b/deploys")).status_code, 404)
        eq("the service route itself still answers", (await cx.get("/api/services/service-a")).status_code, 200)
        eq("service-a-receiver's own spots", [b["kind"] for b in (await cx.get(
            "/api/services/service-a-receiver")).json()["blind_spots"]], ["silent", "no_deploys"])

        print("== PUT /api/settings/via-labels ==")
        r = await cx.put("/api/settings/via-labels", json={"prod-otlp": []})
        eq("it answers the mapping", (r.status_code, r.json()), (200, {"prod-otlp": []}))
        ck("and the list drops service-a-ui at once", "service-a-ui" not in
           {i["name"] for i in (await cx.get("/api/services")).json()["services"]})
        for bad in ([], {"prod-otlp": "proxy_upstream_name"}, {"prod-otlp": ["bad label"]}, {"prod-otlp": [3]}):
            eq(f"{bad!r} is 400", (await cx.put("/api/settings/via-labels", json=bad)).status_code, 400)
        eq("and left the setting", store.get_setting(sre.VIA_SETTING), json.dumps({"prod-otlp": []}))
        store.set_setting(sre.VIA_SETTING, json.dumps({"prod-otlp": ["proxy_upstream_name"]}))
        ck("a setting saved another way shows without waiting for the cache", "service-a-ui" in
           {i["name"] for i in (await cx.get("/api/services")).json()["services"]})
        r = await cx.put("/api/settings/via-labels", json={})
        ck("an empty mapping puts the default back", "service-a-ui" in
           {i["name"] for i in (await cx.get("/api/services")).json()["services"]})

        print("== who may read it ==")
        pid = (await cx.post("/api/projects", json={"template": "custom", "name": "Alpha",
                                                    "objects": []})).json()["id"]
        pk = (await cx.post(f"/api/projects/{pid}/keys", json={"name": "agent"})).json()["secret"]
        rk = (await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})).json()["secret"]
        for path in ("/api/blind-spots", "/api/repos", "/api/services/service-a/deploys"):
            eq(f"a project key gets 403 on {path}",
               (await cx.get(path, headers={"Authorization": f"Bearer {pk}"})).status_code, 403)
            eq(f"a read key reads {path}",
               (await cx.get(path, headers={"Authorization": f"Bearer {rk}"})).status_code, 200)
            eq(f"no credential gets 401 on {path}", (await httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t").get(path)).status_code, 401)
        eq("a read key cannot link a repo", (await cx.patch("/api/services/service-a", json={"repos": []},
           headers={"Authorization": f"Bearer {rk}"})).status_code, 403)

        print("== 200,000 events ==")
        t0 = time.time()
        made = build(store, now, events=200_000, blind=True)
        built = time.time() - t0
        store.clear_services_cache()
        t0 = time.time()
        r = await cx.get("/api/blind-spots")
        took = time.time() - t0
        print(f"  build {built:.1f}s; GET /api/blind-spots with {made['events']:,}+ events took {took:.3f}s")
        spots = r.json()["blind_spots"]
        ck("the spots are there", r.status_code == 200 and {"silent", "seen_via_other"} <=
           {b["kind"] for b in spots}, r.text[:200])
        ck("the whole list answers in under 2 seconds", took < 2.0, f"{took:.2f}s")
        t0 = time.time()
        await cx.get("/api/blind-spots")
        print(f"  and cached: {time.time() - t0:.3f}s")


pure()
default_branch()
reading()
asyncio.run(over_http())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
