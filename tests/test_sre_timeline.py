"""The AI SRE Timeline page (TR-752): GET /api/sre/timeline and GET /api/sre/timeline/raw.

A cell with three services over the last hour (edge-proxy with a spike, a stop and a restart,
service-a and service-b steady), a push to a repo linked to service-a and one to a repo linked to
nothing, an alert that fired and resolved, watcher checks quiet and raised, and an incident taken
and resolved. Covers: the window roll-up text and its math, the quiet windows folded into one
item, a deploy only from a linked repo, the check roll-up, the incident item and its link, the
service and kind filters, paging with `before`, the raw route (count, limit, newest first), the
empty words, no forbidden word or em dash in any item, hour windows for a long range, bad input
answered 400, a read key calling both routes, and a 24-hour range on a 200,000-event cell of 20
services answering in under 2 seconds.

Run: .venv/bin/python tests/test_sre_timeline.py
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

TOKEN = "root-token"
TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["TARES_AUTH_TOKEN"] = TOKEN

import httpx  # noqa: E402

from fixtures.sre_cell import _otlp, build  # noqa: E402
from tares import words  # noqa: E402
from tares.config import catalog_from_db  # noqa: E402
from tares.connectors import source_type_for  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.screens import sre_timeline as st  # noqa: E402
from tares.store import Store  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)   # on a window edge
HOUR_AGO = NOW - timedelta(hours=1)


def hm(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%H:%M")


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def lines(service, bucket, n, errors=0):
    """`n` log lines of `service` inside 10-minute bucket `bucket` (0 is NOW-10 to NOW)."""
    return [_otlp(NOW, 10 * bucket + 0.2 + (i * 9.5) / n, service, "ERROR" if i < errors else "INFO",
                  f"handled request {i}") for i in range(n)]


def run(store, project, rid, key, minutes_ago, finding, outcome, verdict=None):
    """A finished watcher run from `minutes_ago` minutes back."""
    store.start_agent_run(rid, "watcher", "watch-prod", "", key, "h", project=project)
    store.finish_agent_run(rid, "ok", finding=finding, outcome=outcome, verdict=verdict)
    with store._swrite:
        store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
                         [NOW - timedelta(minutes=minutes_ago), rid])


def seed(store):
    """The cell above. Returns the incident id."""
    made = build(store, NOW - timedelta(days=10), watcher=True)   # its own lines are 10 days back
    store.upsert_catalog_source("alerts-prod", source_type_for("alertmanager"), "alertmanager",
                                "5s", {"labels": [{"name": "service", "field": "service",
                                                   "primary": True}]})
    env = []
    # an alert counts as an event of its service, so the two alerts below make up 1 of the 60 in
    # bucket 3 and 1 of the 10 in bucket 0
    for b, n, e in ((6, 10, 0), (5, 10, 0), (4, 10, 0), (3, 59, 12), (2, 10, 0), (0, 9, 0)):
        env += lines("edge-proxy", b, n, e)
    for b in range(7):
        env += lines("service-a", b, 10)
        env += lines("service-b", b, 5)

    def other(source, connector, key, kind, text, minutes_ago, labels):
        t = NOW - timedelta(minutes=minutes_ago)
        return Envelope(source=source, source_type=source_type_for(connector), key_value=key,
                        event_type=kind, text=text, event_time=t, payload={}, labels=labels,
                        ingest_time=t)

    env += [
        other("github", "github_app", "example-org/service-a", "push", "push to main", 35,
              {"repo": "example-org/service-a", "branch": "main", "author": "Alex", "commits": "3"}),
        other("github", "github_app", "example-org/other", "push", "push to main", 36,
              {"repo": "example-org/other", "branch": "main", "author": "Kim", "commits": "1"}),
        other("alerts-prod", "alertmanager", "edge-proxy", "HighErrorRate", "error rate high", 33,
              {"service": "edge-proxy", "severity": "page", "status": "firing"}),
        other("alerts-prod", "alertmanager", "edge-proxy", "HighErrorRate", "error rate ok", 5,
              {"service": "edge-proxy", "severity": "page", "status": "resolved"}),
    ]
    store.append(env)
    uid = made["project"]
    run(store, uid, "r1", "service-a", 50, "Nothing unusual on service-a.", "no_op")
    run(store, uid, "r2", "service-a", 40, "Nothing unusual on service-a.", "no_op")
    run(store, uid, "r3", "edge-proxy", 45, "Nothing unusual on edge-proxy.", "no_op")
    run(store, uid, "r4", "edge-proxy", 32, "Errors on edge-proxy rose from none to 12.",
        "finding", "investigate")
    inc = store.create_incident("edge-proxy", uid, NOW - timedelta(minutes=30))
    store.add_incident_note(inc["id"], "r4", NOW - timedelta(minutes=30))
    store.log_incident(inc["id"], "opened", None, "watcher", "runner",
                       NOW - timedelta(minutes=30))
    store.set_incident_state(inc["id"], "on_it", "u1", "Kim", "ui", at=NOW - timedelta(minutes=25))
    store.set_incident_state(inc["id"], "resolved", "u2", "Lee", "ui",
                             at=NOW - timedelta(minutes=5), resolution="person")
    return inc["id"]


def texts(out):
    return [i["text"] for i in out["items"]]


def of(store, cat, **kw):
    kw.setdefault("start", HOUR_AGO)
    kw.setdefault("end", NOW)
    return st.timeline_screen(store, cat, now=NOW, **kw)


def cell():
    store = Store(os.path.join(TMP, "cell.duckdb"))
    iid = seed(store)
    cat = catalog_from_db(store)

    print("== window roll-up ==")
    out = of(store, cat, service="edge-proxy", kinds="logs")
    got = [(i["kind"], i["text"], i["tone"]) for i in out["items"]]
    eq("five log items for edge-proxy, newest first (restart, stop, fall, spike, folded)", got, [
        ("window", "edge-proxy: 10 events, started again after nothing in the 10 minutes before",
         "info"),
        ("window", "edge-proxy: nothing arrived, 10 events in the 10 minutes before", "warn"),
        ("window", "edge-proxy: 10 events, events down 6x on the 10 minutes before", "info"),
        ("window", "edge-proxy: 60 events, 12 errors, errors up from none and events up 6x on "
                   "the 10 minutes before", "bad"),
        ("quiet", f"edge-proxy: normal from {hm(60)} to {hm(40)}", "ok")])
    spike = out["items"][3]
    eq("the spike spans its window", (spike["at"], spike["end"], spike["time_words"]),
       (iso(40), iso(30), f"{hm(40)} to {hm(30)}"))
    eq("and carries the raw range and count",
       spike["raw"], {"from": iso(40), "to": iso(30), "count": 60})
    eq("a folded stretch counts what it folded", out["items"][4]["raw"],
       {"from": iso(60), "to": iso(40), "count": 20})
    eq("the answer's shape", sorted(out), ["empty_words", "items", "next_before", "range_words",
                                          "services"])
    eq("the range in words", out["range_words"], "Last hour")
    eq("the picker lists every service by name", out["services"],
       sorted(out["services"], key=str.lower))
    ck("and has the three seeded ones", {"edge-proxy", "service-a", "service-b"} <= set(
        out["services"]), out["services"])
    eq("no empty words when there are items", out["empty_words"], None)
    eq("an item has only the contract's fields", sorted(spike), [
        "at", "end", "id", "kind", "link", "raw", "service", "text", "time_words", "tone"])

    print("== quiet folding ==")
    for svc, n in (("service-a", 60), ("service-b", 30)):
        out = of(store, cat, service=svc, kinds="logs")
        eq(f"{svc}: one normal item for the whole hour",
           [(i["kind"], i["text"], i["raw"]["count"]) for i in out["items"]],
           [("quiet", f"{svc}: normal from {hm(60)} to {hm(0)}", n)])

    print("== deploys come from a linked repo only ==")
    out = of(store, cat, service="service-a", kinds="deploys")
    eq("no repo linked: no deploy item", out["items"], [])
    eq("and the empty words say so", out["empty_words"],
       "No deploys for service-a in the last hour.")
    store.set_service("service-a", "tester", repos=["example-org/service-a"])
    out = of(store, cat, service="service-a", kinds="deploys")
    eq("linked: the push", [(i["kind"], i["text"], i["tone"], i["at"]) for i in out["items"]],
       [("deploy", "example-org/service-a pushed to main by Alex: 3 commits", "info", iso(35))])
    eq("the unlinked repo's push is on no service",
       [t for t in texts(of(store, cat)) if "example-org/other" in t], [])
    eq("edge-proxy has no deploy", of(store, cat, service="edge-proxy", kinds="deploys")["items"],
       [])

    print("== alerts ==")
    out = of(store, cat, service="edge-proxy", kinds="alerts")
    eq("an alert that fired and resolved", [(i["text"], i["tone"]) for i in out["items"]], [
        ("Alert HighErrorRate on edge-proxy resolved", "ok"),
        ("Alert HighErrorRate is active on edge-proxy (severity page)", "bad")])

    print("== checks and the incident ==")
    out = of(store, cat, service="service-a", kinds="checks")
    eq("two quiet checks are one item", [(i["text"], i["tone"]) for i in out["items"]],
       [("service-a: 2 checks, all quiet", "ok")])
    out = of(store, cat, service="edge-proxy", kinds="checks")
    by_kind = {}
    for i in out["items"]:
        by_kind.setdefault(i["kind"], []).append(i)
    eq("edge-proxy: one quiet check rolled up, one raised",
       sorted(i["text"] for i in by_kind["check"] if "quiet" in i["text"]),
       ["edge-proxy: 1 check, quiet"])
    ck("the raised check is its own warn item",
       [i["tone"] for i in by_kind["check"] if "quiet" not in i["text"]] == ["warn"],
       by_kind["check"])
    inc = by_kind["incident"]
    eq("three incident items: opened, taken, resolved (newest first)",
       [(i["tone"], i["at"]) for i in inc], [("ok", iso(5)), ("warn", iso(25)), ("bad", iso(30))])
    eq("each links to the incident", {i["link"] for i in inc}, {f"/incidents/{iid}"})
    ck("and says who", inc[0]["text"].startswith("Lee resolved: ") and
       inc[1]["text"].startswith("Kim is on it: ") and
       inc[2]["text"].startswith("Incident opened: "), [i["text"] for i in inc])
    eq("only the incident kind", [i["kind"] for i in of(store, cat, kinds="incident")["items"]],
       ["incident"] * 3)

    print("== service and kind filters ==")
    full = of(store, cat)
    ck("every item is dated inside the range, newest first", [
        i["end"] or i["at"] for i in full["items"]] == sorted(
        (i["end"] or i["at"] for i in full["items"]), reverse=True))
    eq("all kinds are in the unfiltered list",
       {i["kind"] for i in full["items"]}, {"window", "quiet", "deploy", "alert", "check",
                                            "incident"})
    eq("a service filter keeps that service's items",
       {i["service"] for i in of(store, cat, service="service-b")["items"]}, {"service-b"})
    eq("service names match without case", of(store, cat, service="SERVICE-B")["items"],
       of(store, cat, service="service-b")["items"])
    eq("two kinds", {i["kind"] for i in of(store, cat, kinds="alerts,deploys")["items"]},
       {"alert", "deploy"})
    eq("an item kind works as a kind", {i["kind"] for i in of(store, cat, kinds="quiet")["items"]},
       {"quiet"})
    eq("a service and a kind", {(i["service"], i["kind"]) for i in
                               of(store, cat, service="edge-proxy", kinds="alerts")["items"]},
       {("edge-proxy", "alert")})
    for bad in ("nonsense", "logs,nonsense"):
        try:
            of(store, cat, kinds=bad)
            ck(f"kinds={bad!r} is refused", False)
        except ValueError as e:
            ck(f"kinds={bad!r} is refused", "logs, metrics, alerts, deploys and checks" in str(e),
               str(e))

    print("== empty words ==")
    out = of(store, cat, service="service-a-receiver")
    eq("a service with nothing in the range", (out["items"], out["empty_words"]),
       ([], "Nothing arrived for service-a-receiver in the last hour."))
    out = of(store, cat, service="no-such-service")
    eq("an unknown service", (out["items"], out["empty_words"]),
       ([], "Nothing arrived for no-such-service in the last hour."))
    out = st.timeline_screen(store, cat, start=NOW - timedelta(hours=8), end=NOW - timedelta(hours=6),
                             now=NOW)
    eq("a range with nothing, in dates", (out["items"], out["empty_words"] is not None),
       ([], True))
    out = st.timeline_screen(store, cat, service="service-b", now=NOW)
    eq("the default range is the last 6 hours", out["range_words"], "Last 6 hours")
    out = st.timeline_screen(store, cat, kinds="alerts", start=NOW - timedelta(hours=8),
                             end=NOW - timedelta(hours=7), now=NOW)
    eq("a kind with nothing", out["empty_words"].startswith("No alerts from "), True)

    print("== paging with before ==")
    keep = st.PAGE
    st.PAGE = 4
    try:
        first = of(store, cat)
        every = []
        page, guard = first, 0
        while True:
            every += page["items"]
            guard += 1
            if not page["next_before"] or guard > 50:
                break
            page = of(store, cat, before=page["next_before"])
            ck(f"page {guard + 1} has no empty words", page["empty_words"] is None)
    finally:
        st.PAGE = keep
    unpaged = full["items"]
    eq("the first page holds four items, even where some share a moment (an incident opens when a window ends)",
       len(first["items"]), 4)
    ck("it points on", first["next_before"] is not None)
    eq("the pages together are the whole list, no repeats, none missing",
       [i["id"] for i in every], [i["id"] for i in unpaged])
    ck("the last page has no next_before", page["next_before"] is None)
    eq("a page past the end is empty, with no empty words",
       (of(store, cat, before=iso(61))["items"], of(store, cat, before=iso(61))["empty_words"]),
       ([], None))

    print("== raw rows ==")
    raw = st.raw_screen(store, cat, "edge-proxy", HOUR_AGO, NOW, limit=5, now=NOW)
    eq("raw: the count is every event in the range", (raw["count"], len(raw["rows"]), raw["more"]),
       (100, 5, True))
    eq("a row's shape", sorted(raw["rows"][0]), ["at", "feed", "fields", "text"])
    ck("newest first", [r["at"] for r in raw["rows"]] == sorted(
        (r["at"] for r in raw["rows"]), reverse=True))
    eq("the feed names the connection and the kind", raw["rows"][0]["feed"], "prod-otlp logs")
    eq("fields carry the labels", raw["rows"][0]["fields"].get("service"), "edge-proxy")
    spike = of(store, cat, service="edge-proxy", kinds="logs")["items"][3]["raw"]
    win = st.raw_screen(store, cat, "edge-proxy", spike["from"], spike["to"], now=NOW)
    eq("a window's raw range holds exactly its count", (win["count"], len(win["rows"]), win["more"]),
       (spike["count"], 60, False))
    eq("limit 0 is one row", len(st.raw_screen(store, cat, "edge-proxy", HOUR_AGO, NOW, limit=0,
                                               now=NOW)["rows"]), 1)
    eq("a huge limit stops at 1000", st.raw_screen(store, cat, "service-a", NOW - timedelta(days=1),
                                                  NOW, limit=99999, now=NOW)["more"], False)
    eq("nothing in range", st.raw_screen(store, cat, "service-a-receiver", HOUR_AGO, NOW,
                                         now=NOW)["rows"], [])

    print("== words ==")
    bad = [(t, words.offenders(t)) for t in
           texts(of(store, cat, start=NOW - timedelta(hours=8))) if words.offenders(t)]
    eq("no forbidden word and no em dash in any item", bad, [])
    ck("empty words are clean too", not words.offenders(
        of(store, cat, service="service-a-receiver")["empty_words"]))

    print("== a long range uses hour windows ==")
    out = st.timeline_screen(store, cat, service="service-a", kinds="logs",
                             start=NOW - timedelta(days=7), end=NOW, now=NOW)
    ck("the range in words", out["range_words"] == "Last 7 days", out["range_words"])
    ck("every window is an hour", out["items"] and all(
        datetime.fromisoformat(i["end"]) - datetime.fromisoformat(i["at"]) == timedelta(hours=1)
        for i in out["items"]), [(i["at"], i["end"]) for i in out["items"]])
    ck("and compares to the hour before", any("the hour before" in i["text"] for i in out["items"]),
       texts(out))

    print("== bad input ==")
    for label, kw in (("a time that is not one", {"start": "yesterday"}),
                      ("to before from", {"start": NOW, "end": NOW - timedelta(hours=1)}),
                      ("more than 31 days", {"start": NOW - timedelta(days=40), "end": NOW}),
                      ("a bad before", {"before": "soon"})):
        try:
            st.timeline_screen(store, cat, now=NOW, **kw)
            ck(label + " is refused", False)
        except ValueError:
            ck(label + " is refused", True)
    return iid


# ── the routes ───────────────────────────────────────────────────────────────
async def routes():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        store = app.state.store
        seed(store)
        app.state.runtime.reload_catalog()
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {TOKEN}"})
        win = {"from": HOUR_AGO.isoformat().replace("+00:00", "Z"),
               "to": NOW.isoformat().replace("+00:00", "Z")}

        print("== the routes ==")
        r = await cx.get("/api/sre/timeline", params={**win, "service": "edge-proxy",
                                                      "kinds": "logs"})
        eq("the timeline answers", r.status_code, 200)
        want = st.timeline_screen(store, app.state.runtime.catalog, service="edge-proxy",
                                  start=HOUR_AGO, end=NOW, kinds="logs", now=NOW)
        eq("with the screen's items", r.json()["items"], want["items"])
        r = await cx.get("/api/sre/timeline")
        ck("the default range answers", r.status_code == 200 and r.json()["range_words"] ==
           "Last 6 hours", r.text[:200])
        r = await cx.get("/api/sre/timeline", params={"from": "x"})
        eq("a bad time is a 400", r.status_code, 400)
        r = await cx.get("/api/sre/timeline", params={"kinds": "x"})
        eq("a bad kind is a 400", r.status_code, 400)
        r = await cx.get("/api/sre/timeline/raw", params={**win, "service": "edge-proxy",
                                                          "limit": 7})
        j = r.json()
        eq("raw answers", (r.status_code, j["count"], len(j["rows"]), j["more"]),
           (200, 100, 7, True))
        r = await cx.get("/api/sre/timeline/raw", params={"to": win["from"], "from": win["to"]})
        eq("raw with to before from is a 400", r.status_code, 400)

        key = (await cx.post("/api/keys", json={"name": "viewer", "scopes": ["read"]})).json()
        read = {"Authorization": f"Bearer {key['secret']}"}
        for path in ("/api/sre/timeline", "/api/sre/timeline/raw"):
            r = await cx.get(path, params=win, headers=read)
            eq(f"a read key may GET {path}", r.status_code, 200)
        r = await cx.get("/api/sre/timeline", headers={"Authorization": "Bearer nope"})
        eq("a wrong token may not", r.status_code, 401)
        await cx.aclose()


# ── speed ────────────────────────────────────────────────────────────────────
def speed():
    print("== a 24-hour range on 200,000 events ==")
    store = Store(os.path.join(TMP, "big.duckdb"))
    t0 = time.time()
    made = build(store, NOW, watcher=True, events=200_000, spread=20)
    print(f"  (built the cell in {time.time() - t0:.0f} s)")
    cat = catalog_from_db(store)
    ck("twenty services or more", len(made["services"]) >= 20, made["services"])
    t0 = time.time()
    out = st.timeline_screen(store, cat, start=NOW - timedelta(hours=24), end=NOW, now=NOW)
    secs = time.time() - t0
    print(f"  24-hour timeline on 200,000 events and 20 services: {secs:.2f} seconds, "
          f"{len(out['items'])} items")
    ck("it answers in under 2 seconds", secs < 2.0, f"{secs:.2f} s")
    ck("a full page, with a way on", len(out["items"]) == st.PAGE and out["next_before"],
       (len(out["items"]), out["next_before"]))
    t0 = time.time()
    raw = st.raw_screen(store, cat, "", NOW - timedelta(hours=24), NOW, now=NOW)
    secs = time.time() - t0
    print(f"  24-hour raw rows: {secs:.2f} seconds")
    ck("raw counts them all, returns a page", raw["count"] >= 200_000 and len(raw["rows"]) == 200
       and raw["more"], (raw["count"], len(raw["rows"])))
    ck("and answers in under 2 seconds", secs < 2.0, f"{secs:.2f} s")


if __name__ == "__main__":
    cell()
    asyncio.run(routes())
    speed()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
