"""The `stats` agent tool (TR-319): counts per label value over a set of sources, the last window
against the one before, as a few lines whatever the volume. An agent counts over its project's
sources unless it names others.

Runs against a real store with events placed in the two windows.
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba
from tares.config import catalog_from_db
from tares.envelope import Envelope, now_utc
from tares.store import Store

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def ev(minutes_ago, source="logs", **labels):
    return Envelope(source=source, source_type="application_log",
                    key_value=labels.get("service", "x"), event_type="log", text="line",
                    event_time=now_utc() - timedelta(minutes=minutes_ago), payload={},
                    labels={k: str(v) for k, v in labels.items()})


def main():
    store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    events = []
    # now (last 30m): ui 404s jumped, api steady, docs new
    events += [ev(5, service="ui", code="404") for _ in range(40)]
    events += [ev(10, service="api", code="200") for _ in range(10)]
    events += [ev(12, service="docs", code="404") for _ in range(3)]
    # before (30-60m ago): ui low, api steady, old service gone since
    events += [ev(40, service="ui", code="404") for _ in range(2)]
    events += [ev(45, service="api", code="200") for _ in range(10)]
    events += [ev(50, service="batch", code="200") for _ in range(7)]
    # older than both windows: must not count
    events += [ev(90, service="ui", code="404") for _ in range(100)]
    store.append(events)
    logs = ["logs"]

    out = ba.stats_table(store, logs, "service", "30m", scope="logs")
    print(out)
    lines = out.splitlines()
    rows = {l.split(" | ")[0]: l.split(" | ")[1:] for l in lines[2:] if " | " in l}
    check("ui: now 40, before 2", rows.get("ui", [])[:2] == ["40", "2"], str(rows.get("ui")))
    check("largest change first", lines[2].startswith("ui |"), lines[2])
    check("api unchanged", rows.get("api", [])[:3] == ["10", "10", "+0 (x1.0)"], str(rows.get("api")))
    check("a value only now is new", rows.get("docs", [None, None, ""])[2] == "new", str(rows.get("docs")))
    check("a value only before is gone", rows.get("batch", [None, None, ""])[2] == "gone", str(rows.get("batch")))
    check("events older than both windows are left out", rows.get("total", [])[:2] == ["53", "19"], str(rows.get("total")))

    check("the header names what was counted", lines[0].startswith("stats: logs by=service"), lines[0])

    out = ba.stats_table(store, logs, "service", "30m", where={"code": "404"})
    rows = {l.split(" | ")[0]: l.split(" | ")[1:] for l in out.splitlines()[2:] if " | " in l}
    check("where narrows before counting", set(rows) == {"ui", "docs", "total"}, str(rows))

    out = ba.stats_table(store, logs, "service", "30m", top=2)
    check("top caps the rows and says how many are left",
          "... 2 more values" in out and len([l for l in out.splitlines() if l.count(" | ") == 3]) == 4, out)

    out = ba.stats_table(store, logs, "code", "30m",
                         filters=[{"field": "service", "op": "eq", "value": "api"}])
    check("filters apply", "200 | 10 | 10" in out and "404" not in out.split("\n", 2)[2], out)

    out = ba.stats_table(store, logs, "nope", "30m")
    check("an unknown label says so", "no events with a 'nope' label" in out, out)

    try:
        ba.stats_table(store, logs, "service", "0m")
        check("a zero window is an error", False)
    except ValueError:
        check("a zero window is an error", True)

    check("offered to every agent", any(t["name"] == "stats" for t in ba.TOOL_DEFS))
    stats_def = next(t for t in ba.TOOL_DEFS if t["name"] == "stats")
    check("the tool takes by, where, window, top and sources, and no view",
          set(stats_def["input_schema"]["properties"]) == {"by", "where", "window", "top", "sources"}
          and stats_def["input_schema"]["required"] == ["by"], str(stats_def["input_schema"]))
    check("there is no query tool", not any(t["name"] == "query" for t in ba.TOOL_DEFS))

    # the agent tool: its project's sources by default, the named ones when it names them
    store.upsert_catalog_source("logs", "application_log", "webhook", "5s", {})
    store.upsert_catalog_source("other", "application_log", "webhook", "5s", {})
    store.append([ev(5, source="other", service="zzz", code="500") for _ in range(4)])
    store.upsert_catalog_trigger("t", ["logs"], {"aggregate": "count", "predicate": "> 0",
                                                 "window": "1m"}, {}, "5m")
    store.upsert_catalog_agent("counter", "t", "x")
    store.create_project("uc_stats", "custom", "stats", {"objects": []})
    store.put_in_project("trigger", "t", "uc_stats")
    store.put_in_project("agent", "counter", "uc_stats")
    store.put_in_project("source", "logs", "uc_stats")
    store.normalize_projects()

    class Runtime:
        catalog = catalog_from_db(store)
    runner = ba.AgentRunner.__new__(ba.AgentRunner)
    runner.store, runner.runtime = store, Runtime()
    out = runner._tool("counter", "stats", {"by": "service", "window": "30m"})
    check("the agent counts over its project's sources", "ui |" in out and "zzz" not in out, out)
    out = runner._tool("counter", "stats", {"by": "service", "window": "30m", "sources": ["other"]})
    check("the agent counts over the sources it names", "zzz | 4" in out and "ui |" not in out, out)
    try:
        runner._tool("counter", "stats", {"by": "service", "sources": ["ghost"]})
        check("an unknown source is an error", False)
    except KeyError:
        check("an unknown source is an error", True)
    out = runner._tool("counter", "read", {"selector": {"service": "zzz"}, "window": "30m"})
    check("read defaults to the project's sources too", "[other]" not in out, out[:200])
    out = runner._tool("counter", "read", {"selector": {"service": "zzz"}, "window": "30m",
                                           "sources": ["other"]})
    check("read over named sources", "[other]" in out, out[:200])
    try:
        runner._tool("counter", "query", {"view": "x"})
        check("the query tool is gone", False)
    except ValueError:
        check("the query tool is gone", True)


if __name__ == "__main__":
    main()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
