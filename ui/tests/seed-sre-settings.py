"""Seed three scratch AI SRE cells for sre-settings.e2e.cjs (TR-764): the Settings page as a cell with a
watcher and an investigator shows it, and as a new cell with neither. Invented names only.
  <busy>  the shared sre cell with a watcher (every 10 minutes, $25 a month) and an investigator ($25 for
          good, waits 1 hour); the e2e saves, creates keys and switches the edition on it;
  <ro>    a copy of it, for the daemon the e2e starts with an auth token (a read key sees plain text);
  <new>   the same cell with no watcher and no investigator.
No daemon runs here: the cells are written straight into the stores (tests/fixtures/sre_cell.py), so the
e2e starts each daemon itself. Prints a JSON map of the names on the last line.
usage: python seed-sre-settings.py <busy-dir> <ro-dir> <new-dir>   (from the repo root)"""
import json, os, shutil, sys

busy, ro, new = sys.argv[1:4]
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
from fixtures.sre_cell import seed_cell  # noqa: E402

made = seed_cell(busy, "sre", watcher=True, investigator=True)
shutil.copytree(busy, ro, dirs_exist_ok=True)
seed_cell(new, "sre")
print(json.dumps({"busy": busy, "ro": ro, "new": new, "cell": sorted(made) if isinstance(made, dict) else []}, default=str))
