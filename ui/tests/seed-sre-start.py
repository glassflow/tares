"""Seed a scratch AI SRE cell for sre-start.e2e.cjs (TR-765): the Start checklist on the AI SRE home.
  new        nothing connected at all (no model key either: the daemon is started without one)
  connected  the shared fixture cell (prod-otlp, docs-site, github, no watcher): signals and services are done
Invented names only.
usage: python seed-sre-start.py <data-dir> <state>   (run from the repo root; prints the made names as JSON)"""
import json, os, sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))

from fixtures.sre_cell import seed_cell  # noqa: E402
from tares import edition as edition_mod  # noqa: E402
from tares.store import Store  # noqa: E402

STATES = ("new", "connected")
home, state = sys.argv[1], sys.argv[2]
if state not in STATES:
    raise SystemExit(f"state must be one of {', '.join(STATES)}")

if state == "new":
    os.makedirs(home, exist_ok=True)
    store = Store(os.path.join(home, "tares.duckdb"))
    try:
        store.set_setting(edition_mod.EDITION_SETTING, "sre")
    finally:
        store.con.close()
    print(json.dumps({"services": []}))
else:
    print(json.dumps(seed_cell(home, "sre")))
