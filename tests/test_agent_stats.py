"""The `stats` agent tool (TR-319): counts per label value through a view, the last window against
the one before, as a few lines whatever the volume.

Runs against a real store with events placed in the two windows.
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba
from tares.config import ViewCfg
from tares.envelope import Envelope, now_utc
from tares.store import Store

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def ev(minutes_ago, **labels):
    return Envelope(source="logs", source_type="application_log",
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
    view = ViewCfg(name="logs", key_field="service", sources=["logs"])

    out = ba.stats_table(store, view, "service", "30m")
    print(out)
    lines = out.splitlines()
    rows = {l.split(" | ")[0]: l.split(" | ")[1:] for l in lines[2:] if " | " in l}
    check("ui: now 40, before 2", rows.get("ui", [])[:2] == ["40", "2"], str(rows.get("ui")))
    check("largest change first", lines[2].startswith("ui |"), lines[2])
    check("api unchanged", rows.get("api", [])[:3] == ["10", "10", "+0 (x1.0)"], str(rows.get("api")))
    check("a value only now is new", rows.get("docs", [None, None, ""])[2] == "new", str(rows.get("docs")))
    check("a value only before is gone", rows.get("batch", [None, None, ""])[2] == "gone", str(rows.get("batch")))
    check("events older than both windows are left out", rows.get("total", [])[:2] == ["53", "19"], str(rows.get("total")))

    out = ba.stats_table(store, view, "service", "30m", where={"code": "404"})
    rows = {l.split(" | ")[0]: l.split(" | ")[1:] for l in out.splitlines()[2:] if " | " in l}
    check("where narrows before counting", set(rows) == {"ui", "docs", "total"}, str(rows))

    out = ba.stats_table(store, view, "service", "30m", top=2)
    check("top caps the rows and says how many are left",
          "... 2 more values" in out and len([l for l in out.splitlines() if l.count(" | ") == 3]) == 4, out)

    view_f = ViewCfg(name="api-only", key_field="service", sources=["logs"],
                     filters=[{"field": "service", "op": "eq", "value": "api"}])
    out = ba.stats_table(store, view_f, "code", "30m")
    check("the view's filters apply", "200 | 10 | 10" in out and "404" not in out.split("\n", 2)[2], out)

    out = ba.stats_table(store, view, "nope", "30m")
    check("an unknown label says so", "no events with a 'nope' label" in out, out)

    try:
        ba.stats_table(store, view, "service", "0m")
        check("a zero window is an error", False)
    except ValueError:
        check("a zero window is an error", True)

    check("offered to every agent", any(t["name"] == "stats" for t in ba.TOOL_DEFS))


if __name__ == "__main__":
    main()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
