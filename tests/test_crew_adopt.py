"""adopt_unrecorded records a live builder whose folder has a backslash in it (TR-1167).

A Windows folder reads `C:\\Users\\me\\repo`. views.adopt_unrecorded used to compare the repo path from Tares (raw)
with the session's cwd after jq @tsv escaping (backslashes doubled), so such a builder was never adopted. Runs on
every OS: the paths are plain text, nothing is read from disk but the throwaway crew home.

Run: python tests/test_crew_adopt.py
"""
from __future__ import annotations

import functools
import json
import ntpath
import posixpath
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares.crew import claude, views  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def project_repo_rows(self):
        return self.rows


def adopt(cwd, repo_rows, name="argus-build-m2-charts", pathmod=None):
    """Run adopt_unrecorded for one live session; return the station as stations.json then holds it. `cwd` may be a
    function of the crew home. `pathmod` (ntpath or posixpath) makes folders compare the
    way Windows or Linux does on any machine; default the real OS."""
    home = Path(tempfile.mkdtemp(prefix="tares-crew-adopt-"))
    try:
        (home / "crew.json").write_text("{}\n", encoding="utf-8")
        paths = CrewPaths.at(home)
        cwd = cwd(paths.raw) if callable(cwd) else cwd
        row = {"id": "0000beef", "name": name, "cwd": cwd, "sessionId": "s-beef", "state": "working", "pid": 4242}
        with mock.patch.object(views, "_sessions_or_none", lambda binary: [row]), \
                mock.patch.object(claude, "job_state", lambda sid: {}), \
                mock.patch.object(claude, "same_dir",
                                  functools.partial(claude.same_dir, pathmod=pathmod) if pathmod else claude.same_dir):
            views.adopt_unrecorded(paths, FakeClient(repo_rows), "claude")
        if not (home / "stations.json").is_file():
            return None
        return json.loads((home / "stations.json").read_text(encoding="utf-8")).get(name)
    finally:
        shutil.rmtree(home, ignore_errors=True)


WIN = "C:\\Users\\runneradmin\\Jane Doe\\tmp\\argus/charts"  # the mixed spelling the parity scenario builds on Windows
st = adopt(WIN, [("argus", "C:\\Users\\runneradmin\\Jane Doe\\tmp\\argus/core"), ("argus", WIN)])
ck("a builder whose folder has backslashes is adopted with its project", bool(st) and st.get("project") == "argus", st)
ck("...and its repo and cwd are recorded as Claude Code wrote them, not doubled",
   bool(st) and st.get("repo") == WIN and st.get("cwd") == WIN, st)

st = adopt("/srv/argus/charts", [("argus", "/srv/argus/core"), ("argus", "/srv/argus/charts")])
ck("a plain path is adopted as before", bool(st) and st.get("project") == "argus" and st.get("repo") == "/srv/argus/charts", st)

st = adopt(WIN, [("argus", "C:\\Users\\other\\argus/charts")])
ck("a folder no project lists is not adopted", st is None, st)

# TR-1308: the recorded folder and the session's folder are one place in two spellings (Windows: slash direction, case,
# a trailing separator). These fail on the raw string compare.
st = adopt("C:\\Users\\me\\argus\\charts", [("argus", "C:/Users/me/argus/charts")], pathmod=ntpath)
ck("a builder is adopted when Tares lists its folder with forward slashes",
   bool(st) and st.get("project") == "argus" and st.get("repo") == "C:\\Users\\me\\argus\\charts", st)

st = adopt("c:\\users\\ME\\Argus\\charts\\", [("argus", "C:/Users/me/argus/charts")], pathmod=ntpath)
ck("...and when only the letter case and a trailing separator differ", bool(st) and st.get("project") == "argus", st)

st = adopt("C:\\Users\\me\\argus\\core", [("argus", "C:/Users/me/argus/charts")], pathmod=ntpath)
ck("...but another folder is still not adopted", st is None, st)

st = adopt("/srv/argus/Charts", [("argus", "/srv/argus/charts")], pathmod=posixpath)
ck("on a case-sensitive system a different case is a different folder", st is None, st)

st = adopt(lambda home: home + "/", [], name="crew-orchestrator", pathmod=ntpath)
ck("the orchestrator is adopted when its folder has a trailing separator",
   bool(st) and st.get("role") == "orchestrator", st)

st = adopt(lambda home: home + "/other", [], name="crew-orchestrator", pathmod=ntpath)
ck("...and not when it runs somewhere else", st is None, st)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
