"""The 60-second watch scans a day of events for every service only on a cell that uses the AI SRE
(TR-896).

An AI SRE cell scans. A platform cell scans once it has a watcher agent, as before. A factory
cell, and a platform cell with no watcher, never schedule the scan: it would read 24 hours of
events for state nobody looks at. The incident hooks that run on every tick still run on every
cell, and a cell switched to the AI SRE edition starts scanning on its next tick.

Run: .venv/bin/python tests/test_watch_scan_edition.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

from fixtures.sre_cell import build
from tares import edition as edition_mod
from tares import sre_store, watch as W
from tares.config import catalog_from_db
from tares.envelope import now_utc
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


SCANS = []
_real_scan = sre_store.SreStore.service_window_counts


def _spy(self, *a, **k):
    SCANS.append(1)
    return _real_scan(self, *a, **k)


sre_store.SreStore.service_window_counts = _spy
HOOKED = []
W.EVERY_TICK.append(lambda store, catalog, changes: HOOKED.append(1))


def tick(store):
    """One pass of the loop: (scans, hooks run, rows in watch_state)."""
    SCANS.clear()
    HOOKED.clear()
    W._tick(store, catalog_from_db(store))
    return len(SCANS), len(HOOKED), len(store.watch_rows())


def cell(name, edition, **kw):
    store = Store(os.path.join(TMP, name + ".duckdb"))
    store.set_setting(edition_mod.EDITION_SETTING, edition)
    build(store, now_utc(), events=40, **kw)
    return store


print("== a cell that uses the AI SRE scans ==")
s = cell("sre-watcher", "sre", watcher=True)
scans, hooks, rows = tick(s)
ck("an AI SRE cell scans once a tick", scans == 1, str(scans))
ck("and keeps the state of its services", rows > 0, str(rows))
ck("and runs the tick hooks", hooks == 1, str(hooks))

s = cell("sre-bare", "sre")
scans, hooks, rows = tick(s)
ck("an AI SRE cell with no watcher yet still scans (its home says who is not watched)", scans == 1, str(scans))

print("== a factory cell never schedules it ==")
s = cell("factory", "factory", watcher=True)
for i in range(3):
    scans, hooks, rows = tick(s)
    ck(f"factory tick {i + 1}: no scan, no state kept", (scans, rows) == (0, 0), f"{scans} {rows}")
ck("the tick hooks still run on a factory cell", hooks == 1, str(hooks))

print("== a platform cell scans only with a watcher ==")
s = cell("platform-bare", None)
ck("no edition stored is a platform cell", edition_mod.edition(s)[0] == "platform")
scans, hooks, rows = tick(s)
ck("a platform cell with no watcher: no scan", (scans, rows) == (0, 0), f"{scans} {rows}")
ck("and the hooks run", hooks == 1, str(hooks))

s = cell("platform-watcher", None, watcher=True)
scans, hooks, rows = tick(s)
ck("a platform cell with a watcher scans, as it always did", scans == 1 and rows > 0, f"{scans} {rows}")

print("== switching the edition ==")
s = cell("switch", "factory", watcher=True)
ck("on factory: no scan", tick(s)[0] == 0)
s.set_setting(edition_mod.EDITION_SETTING, "sre")
scans, _h, rows = tick(s)
ck("switched to the AI SRE, the next tick scans and keeps state", scans == 1 and rows > 0, f"{scans} {rows}")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
