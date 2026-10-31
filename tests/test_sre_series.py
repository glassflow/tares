"""Event counts over time and what counts as an error (TR-767).

is_error / is_error_where (a level label in any case, an ERROR or FATAL event type, a status code
of 500 or more, a span with status.code ERROR; a value that is not a number never matches, and the
Python and SQL rules agree on every event), series_for (the windows and their buckets), and
SreStore.event_series / event_series_many (the right number of buckets aligned to `since`, zeros
filled, errors_only, a merged service counting the services merged into it, one query for many
services, the 200,000-event cell answering in under a second).

Run: .venv/bin/python tests/test_sre_series.py
"""
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_WATCH_SECONDS"] = "3600"

from fixtures.sre_cell import build
from tares import sre
from tares.connectors import source_type_for
from tares.envelope import Envelope
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
BUCKET = 420   # 7 minutes: the window below is not a whole number of them, and epoch-aligned
               # buckets would not line up with it
SINCE = NOW - timedelta(minutes=150)
UNTIL = NOW - timedelta(minutes=30)
N = math.ceil((UNTIL - SINCE).total_seconds() / BUCKET)   # 18


def fresh(**kw):
    store = Store(os.path.join(tempfile.mkdtemp(dir=TMP), "s.duckdb"))
    return store, build(store, NOW, **kw)


def ev(service, bucket, event_type="INFO", labels=None, source="prod-otlp", text="line"):
    """An event of `service` that lands in bucket `bucket` of the window."""
    t = SINCE + timedelta(seconds=bucket * BUCKET + 60)
    lab = {"service": service} if source == "prod-otlp" else {"project": service}
    return Envelope(source=source, source_type=source_type_for(
        "otlp" if source == "prod-otlp" else "vercel"), key_value=service, event_type=event_type,
        text=text, event_time=t, payload={}, labels={**lab, **(labels or {})}, ingest_time=t)


def counts(series):
    return [n for _t, n in series]


def pure():
    print("== is_error ==")
    yes = [
        ({"level": "error"}, None), ({"level": "ERROR"}, None), ({"level": "Fatal"}, None),
        ({"severity": "critical"}, None), ({"severity_text": "CRITICAL"}, None),
        ({}, "ERROR"), ({}, "FATAL"), ({}, "error"),
        ({"nginx_status_code": "502"}, None), ({"status": "500"}, None),
        ({"status_code": 503}, None), ({"http.status_code": "599"}, None),
        ({"http_status": "504"}, "INFO"),
        ({"status.code": "ERROR"}, "span"),
    ]
    no = [
        ({"level": "info"}, None), ({"level": "warn"}, "WARN"), ({"severity": "debug"}, None),
        ({"nginx_status_code": "404"}, None), ({"status": "499"}, None), ({"status": "200"}, None),
        ({"status": "n/a"}, None), ({"status_code": ""}, None), ({"status": None}, None),
        ({}, None), ({}, "INFO"), ({"service": "edge-proxy"}, "log"),
        ({"status.code": "ERROR"}, "INFO"), ({"status.code": "OK"}, "span"),
        ({"level": "info", "status": "200"}, "INFO"),
    ]
    for labels, et in yes:
        ck(f"error: {labels} {et}", sre.is_error({"labels": labels, "event_type": et}) is True)
    for labels, et in no:
        ck(f"not an error: {labels} {et}", sre.is_error({"labels": labels, "event_type": et}) is False)
    eq("an event with no labels key", sre.is_error({}), False)
    eq("the ticket's check", (sre.is_error({"labels": {"nginx_status_code": "502"}}),
                              sre.is_error({"labels": {"nginx_status_code": "404"}})), (True, False))

    print("== the SQL rule says the same as the Python rule, event by event ==")
    store, _ = fresh()
    cases = yes + no
    store.append([ev("service-b", i, et or "INFO", {k: v for k, v in labels.items() if v is not None},
                     text=str(i)) for i, (labels, et) in enumerate(cases)])
    # event types in the cases above that the Envelope needs as text; None means plain
    want = [sre.is_error({"labels": {k: v for k, v in lab.items() if v is not None},
                          "event_type": et or "INFO"}) for lab, et in cases]
    where, params = sre.is_error_where({"prod-otlp": "service"})
    with store._lock:
        got = store.con.execute(
            f"SELECT text FROM events WHERE key_value = 'service-b' AND {where} ORDER BY CAST(text AS INT)",
            params).fetchall()
    eq("the same events are errors", [int(r[0]) for r in got], [i for i, w in enumerate(want) if w])
    eq("no source, no error", sre.is_error_where({}), ("FALSE", []))

    print("== series_for ==")
    s = sre.series_for
    for window, n, secs in (("10m", 1, 600), ("1h", 60, 60), ("2h", 60, 120), ("24h", 60, 1440),
                            ("7d", 84, 7200)):
        since, until, b = s(window, NOW)
        eq(f"{window}: ends at now, {n} buckets of {secs}s",
           (until, b, (until - since).total_seconds() / b), (NOW, secs, n))
    eq("24h with 20 points is 72 minutes a bucket", s("24h", NOW, 20)[2], 72 * 60)
    eq("24h with 144 points is 10 minutes a bucket", s("24h", NOW, 144)[2], 600)
    eq("a count that does not divide the window rounds the bucket up (1h in 7: 515 s, 7 buckets)",
       (s("1h", NOW, 7)[2], math.ceil(3600 / s("1h", NOW, 7)[2])), (515, 7))
    eq("it starts a window back", s("1h", NOW)[0], NOW - timedelta(hours=1))
    for bad in (("3d", None), ("1h", 0), ("1h", -1)):
        try:
            s(bad[0], NOW, bad[1])
            ck(f"{bad} is refused", False)
        except ValueError:
            ck(f"{bad} is refused", True)


def series():
    print("== event_series ==")
    store, _ = fresh()
    plain = [ev("edge-proxy", b) for b in (0, 2, 4, 6, 8, 10, 12, 14, 16)]
    plain.append(ev("edge-proxy", 9, labels={"level": "info", "nginx_status_code": "200"}))
    # the 10 plain lines include one whose status is not a number
    errs = [ev("edge-proxy", 1, labels={"nginx_status_code": "502"}),
            ev("edge-proxy", 1, labels={"nginx_status_code": "502"}),
            ev("edge-proxy", 5, labels={"nginx_status_code": "502"}),
            ev("edge-proxy", 7, event_type="ERROR"), ev("edge-proxy", 11, event_type="ERROR")]
    plain[0] = ev("edge-proxy", 0, labels={"nginx_status_code": "n/a"})
    other = [ev("edge-proxy", 3, labels={"nginx_status_code": "404"})]
    store.append(plain + errs + other)
    eq("the cell holds 16 lines for edge-proxy in the window", len(plain + errs + other), 16)

    all_ = store.event_series("edge-proxy", SINCE, UNTIL, BUCKET)
    eq("one point per bucket, ceil((until - since) / bucket)", len(all_), N)
    eq("the first point is `since`", all_[0][0], SINCE.isoformat())
    eq("points are `bucket` seconds apart from `since`", [t for t, _ in all_],
       [(SINCE + timedelta(seconds=i * BUCKET)).isoformat() for i in range(N)])
    want = [0] * N
    for e in plain + errs + other:
        want[int((e.ingest_time - SINCE).total_seconds() // BUCKET)] += 1
    eq("every event is in the bucket it arrived in", counts(all_), want)
    eq("16 in all", sum(counts(all_)), 16)
    ck("empty buckets are 0, not left out", 0 in counts(all_) and want.count(0) > 0)
    eq("the last bucket is partial and counted", all_[-1][1], want[-1])

    errors = store.event_series("edge-proxy", SINCE, UNTIL, BUCKET, errors_only=True)
    eq("errors_only keeps the same points", [t for t, _ in errors], [t for t, _ in all_])
    eq("errors_only counts 5: 502 x3 and the two ERROR, not the 404", sum(counts(errors)), 5)
    ewant = [0] * N
    for b in (1, 1, 5, 7, 11):
        ewant[b] += 1
    eq("each in its bucket", counts(errors), ewant)
    ck("errors never exceed the total in any bucket",
       all(e <= a for e, a in zip(counts(errors), counts(all_))))

    print("== a line that looks fine is not counted, and nothing raises ==")
    eq("level info with status 200, and a status of n/a, add nothing to errors",
       sum(counts(errors)), 5)
    eq("a service nobody sent is all zeros", counts(store.event_series(
        "nobody", SINCE, UNTIL, BUCKET)), [0] * N)
    eq("a window with nothing in it is all zeros", sum(counts(store.event_series(
        "edge-proxy", NOW - timedelta(days=3), NOW - timedelta(days=2), 3600))), 0)
    try:
        store.event_series("edge-proxy", UNTIL, SINCE, BUCKET)
        ck("a window that ends before it starts is refused", False)
    except ValueError:
        ck("a window that ends before it starts is refused", True)

    print("== levels, statuses and spans ==")
    kinds = [  # (event, is an error)
        (ev("service-b", 2, labels={"level": "ERROR"}), True),
        (ev("service-b", 2, labels={"level": "error"}), True),
        (ev("service-b", 2, labels={"severity": "Fatal"}), True),
        (ev("service-b", 2, labels={"severity_text": "CRITICAL"}), True),
        (ev("service-b", 3, labels={"http.status_code": "503"}), True),
        (ev("service-b", 3, labels={"status": "500"}), True),
        (ev("service-b", 4, event_type="span", labels={"status.code": "ERROR"}), True),
        (ev("service-b", 4, event_type="span", labels={"status.code": "OK"}), False),
        (ev("service-b", 5, labels={"status": "499"}), False),
        (ev("service-b", 5, labels={"status_code": "abc"}), False),
        (ev("service-b", 5, labels={"level": "warn"}), False),
        (ev("service-b", 5, labels={"level": "info", "status": "200"}), False),
    ]
    store.append([e for e, _ in kinds])
    b_all = store.event_series("service-b", SINCE, UNTIL, BUCKET)
    b_err = store.event_series("service-b", SINCE, UNTIL, BUCKET, errors_only=True)
    eq("service-b total", sum(counts(b_all)), len(kinds))
    eq("service-b errors", sum(counts(b_err)), sum(1 for _e, bad in kinds if bad))
    eq("service-b errors by bucket", counts(b_err)[2:5], [4, 2, 1])
    eq("the series agrees with is_error for each", sum(counts(b_err)), sum(
        1 for e, _ in kinds if sre.is_error({"labels": e.labels, "event_type": e.event_type})))

    print("== the window includes `since` and leaves out `until` ==")

    def at(t):
        return Envelope("prod-otlp", source_type_for("otlp"), "service-c", "INFO", "edge", t, {},
                        {"service": "service-c"}, t)
    store.append([at(SINCE - timedelta(seconds=1)), at(SINCE), at(UNTIL - timedelta(seconds=1)),
                  at(UNTIL)])
    edge_c = store.event_series("service-c", SINCE, UNTIL, BUCKET)
    eq("a line at `since` and the last second count, one at `until` or before `since` does not",
       (sum(counts(edge_c)), edge_c[0][1], edge_c[-1][1]), (2, 1, 1))

    print("== event_series_many ==")
    many = store.event_series_many(["edge-proxy", "service-b", "nobody"], SINCE, UNTIL, BUCKET)
    eq("one entry per service asked", list(many), ["edge-proxy", "service-b", "nobody"])
    for name in many:
        eq(f"{name}: the same as a single call", many[name],
           store.event_series(name, SINCE, UNTIL, BUCKET))
    many_e = store.event_series_many(["edge-proxy", "service-b"], SINCE, UNTIL, BUCKET, True)
    eq("errors_only: the same as single calls", many_e,
       {n: store.event_series(n, SINCE, UNTIL, BUCKET, True) for n in ("edge-proxy", "service-b")})
    eq("no services, no entries", store.event_series_many([], SINCE, UNTIL, BUCKET), {})

    print("== a merged service counts what is merged into it ==")
    docs = ev("docs-site", 6, source="docs-site", event_type="deployment")
    store.append([docs])
    before = sum(counts(store.event_series("edge-proxy", SINCE, UNTIL, BUCKET)))
    eq("before the merge docs-site's line is its own", (before, sum(counts(
        store.event_series("docs-site", SINCE, UNTIL, BUCKET)))), (16, 1))
    store.set_service("docs-site", "tester", merged_into="edge-proxy")
    merged = store.event_series("edge-proxy", SINCE, UNTIL, BUCKET)
    eq("edge-proxy now counts docs-site (another source)", sum(counts(merged)), 17)
    eq("in the bucket the line arrived in", counts(merged)[6], want[6] + 1)
    eq("asking for docs-site gives edge-proxy's series", store.event_series(
        "docs-site", SINCE, UNTIL, BUCKET), merged)
    eq("errors_only follows the merge too", sum(counts(store.event_series(
        "edge-proxy", SINCE, UNTIL, BUCKET, True))), 5)
    both = store.event_series_many(["edge-proxy", "docs-site", "service-b"], SINCE, UNTIL, BUCKET)
    eq("many: docs-site and edge-proxy are the same series", both["docs-site"], both["edge-proxy"])
    eq("many: service-b is not touched", both["service-b"], b_all)
    store.set_service("docs-site", "tester", merged_into=None)
    eq("unmerged, edge-proxy is back to 16", sum(counts(store.event_series(
        "edge-proxy", SINCE, UNTIL, BUCKET))), 16)


def big():
    print("== 200,000 events ==")
    t0 = time.time()
    store, made = fresh(events=200_000)
    print(f"  build {time.time() - t0:.1f}s")
    since, until, b = sre.series_for("24h", NOW, 20)
    names = made["services"]
    t0 = time.time()
    many = store.event_series_many(names, since, until, b)
    took = time.time() - t0
    print(f"  event_series_many for {len(names)} services, 24 h, 20 points, "
          f"{made['events'] + 26:,} events: {took:.3f}s")
    ck("under 1 second", took < 1.0, f"{took:.2f}s")
    eq("20 points each", {n: len(v) for n, v in many.items()}, dict.fromkeys(names, 20))
    eq("edge-proxy and service-a hold the lines", (sum(counts(many["edge-proxy"])),
       sum(counts(many["service-a"]))), (5 + 100_000, 6 + 100_000))
    t0 = time.time()
    err = store.event_series_many(names, since, until, b, errors_only=True)
    print(f"  the same with errors_only: {time.time() - t0:.3f}s")
    eq("errors: the one 502 and the one failed job", (sum(counts(err["edge-proxy"])),
       sum(counts(err["service-a"]))), (1, 1))
    eq("a single call matches the many call", store.event_series("edge-proxy", since, until, b),
       many["edge-proxy"])


pure()
series()
big()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
