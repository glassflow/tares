"""The incidents a service page lists come newest first, also in the hour a clock goes back (TR-1386).

The store hands times back in the machine's zone, and Python orders two datetimes of one zone by
their wall clock. In a zone that sets its clock back, one hour is read twice: 00:50 UTC reads 02:50
and 01:05 UTC reads 02:05 on a Berlin clock, so by wall clock the later incident looked older and
the page listed them the wrong way round. The list now sorts by UTC.

The zone is part of the input: TZ for the process (DuckDB) and PGTZ for a Postgres session, which
takes its own zone. SERVICES_TZ=UTC runs the same data in a zone with no such hour.

Run: .venv/bin/python tests/test_sre_services_order.py
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

os.environ["TZ"] = os.environ.get("SERVICES_TZ", "Europe/Berlin")
time.tzset()
os.environ["PGTZ"] = os.environ["TZ"]

TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")

from tares.screens import sre_services  # noqa: E402
from tares.store import Store  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


# Berlin set its clocks back on 2026-10-25 (03:00 CEST to 02:00 CET): 00:00 UTC is 02:00 CEST.
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
CLOCK_BACK = datetime(2026, 10, 25, 0, 0, 0, tzinfo=timezone.utc)


def incident(store, service, run_id, at):
    inc = store.create_incident(service, "prj-order", at)
    store.start_agent_run(run_id, "investigator", "watch-prod", "", service, "h", woken_by="handoff",
                          project="prj-order")
    store.finish_agent_run(run_id, "ok", finding=f"{service} returns errors.", outcome="finding",
                           verdict="rca")
    with store._swrite:
        store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, run_id])
    store.add_incident_note(inc["id"], run_id, at)
    return inc["id"]


def main():
    store = Store(os.path.join(TMP, "t.duckdb"))
    print("== the newest incident first, in the hour that happens twice ==")
    # 00:50 UTC reads 02:50 (CEST), the earlier moment; 01:05 UTC reads 02:05 (CET), the later one
    first = incident(store, "svc-clock", "r1", CLOCK_BACK + timedelta(minutes=50))
    second = incident(store, "svc-clock", "r2", CLOCK_BACK + timedelta(minutes=65))
    shown, total, _open = sre_services._incidents(store, None, ["svc-clock"], store.service_settings(), NOW)
    eq("both incidents are counted", total, 2)
    eq("the one that came later is listed first", [i["id"] for i in shown], [second, first])

    print("== across a longer span, newest first ==")
    third = incident(store, "svc-clock", "r3", CLOCK_BACK + timedelta(hours=30))
    early = incident(store, "svc-clock", "r4", CLOCK_BACK - timedelta(hours=3))
    shown, total, _open = sre_services._incidents(store, None, ["svc-clock"], store.service_settings(), NOW)
    eq("four incidents, by the moment they happened", [i["id"] for i in shown], [third, second, first, early])

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
