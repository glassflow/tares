"""The richer summary a schedule hands its agent (TR-400): labels counted per entity, a baseline
from the windows before, a volume floor, numeric fields, sample lines for what moved, and
earlier findings on the same entities. Without options the summary is the plain tables, as
before.

Runs against a real store with events placed in 10-minute windows.
"""
import os
import sys
import tempfile
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tares import schedule
from tares.config import (CatalogError, Condition, TriggerCfg, normalize_summary_options,
                          validate_trigger_dict)
from tares.envelope import Envelope, now_utc
from tares.store import Store

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def ev(minutes_ago, source="logs", text="line", **labels):
    return Envelope(source=source, source_type="application_log",
                    key_value=labels.get("service", "x"), event_type="log", text=text,
                    event_time=now_utc() - timedelta(minutes=minutes_ago), payload={},
                    labels={k: str(v) for k, v in labels.items()})


def trig(summary=None):
    cond = Condition(aggregate="count", predicate="> 0", window="10m", every=600.0,
                     summary_by=["service", "code"],
                     summary=normalize_summary_options(summary))
    return TriggerCfg(name="watch", sources=["logs"], condition=cond, key_field="service",
                      project="p1")


def main():
    store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    catalog = SimpleNamespace(sources={})
    events = []
    # six windows of history: api steady at 100 with 30 x 401 each window, ui steady at 20
    for w in range(2, 8):
        m = w * 10 + 5
        events += [ev(m, service="api", code="200", ms=50) for _ in range(70)]
        events += [ev(m, service="api", code="401", ms=50) for _ in range(30)]
        events += [ev(m, service="ui", code="200", ms=80) for _ in range(20)]
    # the window before: the same
    events += [ev(15, service="api", code="200", ms=50) for _ in range(70)]
    events += [ev(15, service="api", code="401", ms=50) for _ in range(30)]
    events += [ev(15, service="ui", code="200", ms=80) for _ in range(20)]
    # this window: api's 401s jump to 120, slower; docs ticks 1 -> 2 (small); ui steady
    events += [ev(5, service="api", code="200", ms=300) for _ in range(70)]
    events += [ev(4, service="api", code="401", ms=300, text="POST /v1/traces 401") for _ in range(120)]
    events += [ev(5, service="ui", code="200", ms=80) for _ in range(20)]
    events += [ev(5, service="docs", code="200") for _ in range(2)]
    events += [ev(15, service="docs", code="200")]
    # an earlier finding on api, in this project and in another one
    events.append(Envelope(source="findings", source_type="finding", key_value="api",
                           event_type="finding", text="f", event_time=now_utc() - timedelta(hours=2),
                           payload={"agent": "rca", "verdict": "resolved", "project": "p1",
                                    "headline": "401s from one client with an expired key"},
                           labels={"service": "api"}))
    events.append(Envelope(source="findings", source_type="finding", key_value="ui",
                           event_type="finding", text="f", event_time=now_utc() - timedelta(hours=1),
                           payload={"agent": "x", "project": "other", "headline": "not ours"},
                           labels={"service": "ui"}))
    store.append(events)

    print("== no options: the plain summary, unchanged ==")
    plain = schedule.summary(store, catalog, trig())
    check("plain tables, then the newest lines", "service | now | before | change" in plain
          and "most recent lines:" in plain and "usual" not in plain, plain[:400])

    print("== options ==")
    check('"rich" is the defaults', normalize_summary_options("rich") == {
        "by_entity": True, "baseline": 6, "min_count": 5, "examples": 2, "findings": "6h"})
    for bad, why in (({"nope": 1}, "unknown"), ({"baseline": 99}, "between"),
                     ({"numbers": ["a b"]}, "numeric field"), ({"findings": "soon"}, "duration")):
        try:
            normalize_summary_options(bad)
            check(f"{bad} is refused", False)
        except ValueError as e:
            check(f"{bad} is refused ({why})", why in str(e), str(e))
    try:
        validate_trigger_dict({"name": "w", "sources": ["logs"], "condition": {
            "every": "10m", "summary": {"baseline": -1}}}, {"logs"})
        check("a trigger with bad summary options is refused", False)
    except CatalogError as e:
        check("a trigger with bad summary options is refused", "baseline" in str(e), str(e))

    print("== rich ==")
    rich = schedule.summary(store, catalog, trig({**normalize_summary_options("rich"),
                                                  "numbers": ["ms"]}))
    print(rich)
    lines = rich.splitlines()
    svc = next((l for l in lines if l.startswith("api | ")), "")
    check("api against its usual: now 190, before 100, usual 100, z well above 0",
          svc.startswith("api | 190 | 100 | 100 ± 0 | +") and float(svc.split(" | ")[4]) > 5, svc)
    check("the most unusual row first", lines[lines.index("service | now | before | usual ± spread | z | change") + 1].startswith("api |"))
    check("small values are left out and counted", "docs |" not in rich
          and "small values left out" in rich, rich[:900])
    by = [l for l in lines if l.startswith("401 | api | ")]
    check("401 tied to api in the by-entity table", "code by service" in rich and by
          and by[0].startswith("401 | api | 120 | 30 | 30 ± 0"), str(by))
    check("numbers: avg and max per entity, now against before",
          "ms per service" in rich and any(l.startswith("api | 300 | 50 | 300 | 50") for l in lines), rich)
    check("an earlier finding on api from this project", "- api: resolved by rca" in rich
          and "expired key" in rich, rich)
    check("another project's finding is not shown", "not ours" not in rich)
    check("sample lines for what moved, not the newest lines", "sample lines for what moved most" in rich
          and "POST /v1/traces 401" in rich and "most recent lines:" not in rich, rich[-800:])
    check("bounded", len(rich) <= schedule.RICH_MAX_CHARS + 60)

    print("== each part alone ==")
    only = schedule.summary(store, catalog, trig({"min_count": 5}))
    check("floor only: no baseline columns, no by-entity, newest lines",
          "usual" not in only and "by service" not in only and "most recent lines:" in only
          and "small values left out" in only, only)


if __name__ == "__main__":
    main()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
