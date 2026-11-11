#!/usr/bin/env python3
"""The parity harness: a factory scenario runs against the bash engine and against the Python engine,
and what they print and write must be the same.

    python tests/factory/parity.py <scenario>            run one scenario on both engines (a name with * or ?, such as tidy_*, runs every match)
    python tests/factory/parity.py --list                the scenarios defined here
    python tests/factory/parity.py --coverage            every scenario of every bash suite, and how
                                                         many of them pass against Python
    python tests/factory/parity.py --coverage --require-full   the same, exit 1 below 100%

A scenario is defined here, in Python (steps to run, checks on the result), so it runs on Windows too,
where the bash suites cannot. It is run twice, each time in a throwaway crew with the Python fake
Claude Code (tests/factory/fake_claude.py): with TARES_FACTORY_ENGINE=bash and with =python, through
`python -m tares.crew`. The bash suites in this folder stay the reference; a scenario here is one of
their checks moved over, with the same assertions.

What is compared, after normalising: the exit code, stdout and stderr of every step; every file in the
crew home; the calls made to Claude Code and the sessions it lists. Normalised away: the temp folder,
timestamps, pids, session ids and the key order of JSON. Ids and epoch times are only replaced where they
appear as such (after `backgrounded ·`, `job`, `at`, `since`, under an id or time key in JSON): a plain
eight-digit or ten-digit number is data and is compared as it is.

Result, per side: PASS, FAIL, or SKIP. The Python engine answers exit 2 with "is not yet ported" for a
command that has not moved over; that side prints `SKIP python: not ported`. Without bash (Windows)
the bash side prints `SKIP bash: ...`. Exit 0 when the bash side passes and the Python side passes
(same normalised output as bash) or is skipped. Exit 1 when the bash side fails, or when Python runs
and fails a check or differs from bash. On Windows a skipped bash side is expected and the Python side
stands alone; anywhere else a skipped bash side (no bash or jq) is an error: exit 1, and the scenario
does not count in --coverage.

Coverage. A "scenario" of a bash suite (the suites named in tests/factory/ci_list.txt) is one `check
"<label>"` call: a source line that starts, after any indentation, with `check "`. A check in a loop
counts once. A suite with no such line (guard, heavy, parse) counts as one scenario, named after the
suite, except a wrapper (WRAPPERS below): a suite that runs another suite on Python only, has no check line of
its own and counts for nothing, since its checks belong to the suite it runs. A scenario here lists the labels it covers (`covers`), and counts as passing against Python
when its run passes on the Python side (a SKIP does not count). Percentage = passing / all scenarios.

Test hooks (for tests/test_parity.py): PARITY_CMD_BASH and PARITY_CMD_PYTHON replace the command a side
runs (default `python -m tares.crew`); PARITY_FAIL=bash|python|both adds a failing check to that side.

Standard library only.
"""
from __future__ import annotations

import difflib
from concurrent.futures import ProcessPoolExecutor
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FAKE_CLAUDE = HERE / "fake_claude.py"
sys.path.insert(0, str(HERE))
from fake_up_tares import FakeTares as StandInTares  # noqa: E402  (the stand-in daemon `crew up` talks to; FakeTares below is the route table of the read commands)
CI_LIST = HERE / "ci_list.txt"
ENGINES = ("bash", "python")
STEP_TIMEOUT = 180
NOT_PORTED = "not yet ported"


# ---------------------------------------------------------------------------------------------
# Scenarios

@dataclass
class Step:
    argv: list           # everything after `tares factory`; "{tmp}" in an argument or env value is the scenario's folder,
                         # "{tares}" in an env value is the address of the scenario's fake Tares
    env: object = field(default_factory=dict)   # a dict, or fn(Run) -> dict when it names the throwaway folders
    stdin: str = ""
    cwd: str = ""        # the folder the command runs in ("{tmp}" works); default the repo root
    before: object = None   # fn(tmp) run before the step: change files between two commands
    after: object = None    # fn(tmp) -> anything, run after the step; Run.snap(i) gives it back to a check
    run_before: object = None   # fn(Run) run just before the step: files the suite writes between its commands
    drop: tuple = ()        # regexes: printed lines that match one are left out of the comparison (not of the checks)


@dataclass
class Scenario:
    name: str
    suite: str           # the bash suite it comes from (tests/factory/<suite>_test.sh)
    steps: list
    checks: list         # (label, fn(Run) -> bool), the assertions of the bash suite, kept as they are
    covers: list         # labels of the suite's `check` calls this scenario stands for
    about: str = ""
    stand_in: object = None  # None: no stand-in. A dict (settings, people, grants): a stand-in Tares (fake_up_tares.py)
                             # on a free port, TARESD_URL and TARES_FACTORY_MCP_URL point at it
    prepare: object = None   # fn(Run) run once before the first step: ~/.claude.json, crew.json, ...
    setup: object = None    # fn(tmp, env) run once before the first step: build files, a repo, a link
    tares: dict | None = None   # routes of a fake Tares to run for the scenario (see FakeTares); None: no Tares
    not_windows: str = ""   # why the Python side is skipped on Windows (the reason is printed), e.g. a claude.cmd cannot take a prompt of several lines
    driver: str = ""     # a script of this folder that runs a function of the engine instead of a command
                         # (`<driver> <engine> <argv...>`), for code that has no command yet
    ignore_calls: tuple = ()  # argument prefixes of calls to Claude Code that are left out of the comparison
    ignore_asked: tuple = ()  # routes left out of the "asked of Tares" comparison: "PUT /api/x" itself, or every route
                              # that starts with an entry ending in a space ("GET ": the reads, which the engines make
                              # in a different number and order)


class Run:
    """What a scenario's checks can look at: the steps' output and the crew's state on disk."""

    def __init__(self, tmp: Path, results: list, tares=None, stand_in=None):
        self.tmp = tmp
        self.tares = tares      # the route table (FakeTares) of the read commands, or None
        self.crew = tmp / "crew"
        self.state = tmp / "claude"
        self.home = tmp / "home"
        self.results = results
        self.stand_in = stand_in    # the stand-in Tares (fake_up_tares.py) of `crew up`, or None
        self.ignore_calls: tuple = ()
        self.marks: dict = {}   # what a step's `before` kept for the checks (see mark())

    def station(self, name: str, key: str, home=None):
        """A field of a station in stations.json (the suites' `st`); "" when it is not there. `home`: another
        crew's home folder than the default crew's."""
        try:
            v = json.loads(((home or self.crew) / "stations.json").read_text(encoding="utf-8")).get(name, {}).get(key)
        except (OSError, ValueError, AttributeError):
            return ""
        return "" if v is None or v is False else v

    def station_settings(self, name: str, home=None) -> dict:
        try:
            return json.loads(((home or self.crew) / "stations" / f"{name}.settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def crew_json(self, home=None) -> dict:
        try:
            return json.loads(((home or self.crew) / "crew.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def trust(self, *folders) -> None:
        """Claude Code's trust of a folder, recorded as its trust prompt does ($HOME/.claude.json)."""
        f = self.home / ".claude.json"
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        for d in folders:
            Path(d).mkdir(parents=True, exist_ok=True)
            doc.setdefault("projects", {}).setdefault(str(d), {})["hasTrustDialogAccepted"] = True
        f.write_text(json.dumps(doc), encoding="utf-8")

    def out(self, i: int = -1) -> str:
        """stdout and stderr of step i together, as the bash suites capture them (`>out 2>&1`)."""
        r = self.results[i]
        return r["stdout"] + r["stderr"]

    def code(self, i: int = -1) -> int:
        return self.results[i]["code"]

    def snap(self, i: int = -1):
        """What step i's `after` function returned (None when it has none)."""
        return self.results[i].get("snap")

    def calls(self) -> list:
        try:
            lines = (self.state / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(x) for x in lines if x.strip()]

    def launches(self) -> int:
        """Sessions started with `claude --bg` (the suites' `launches`)."""
        return sum(1 for c in self.calls() if "--bg" in c["args"])


SCENARIOS: dict[str, Scenario] = {}


def scenario(s: Scenario) -> Scenario:
    SCENARIOS[s.name] = s
    return s


# crew_test.sh, its first scenario: "No Tares: every command says so plainly and starts nothing."
# Same assertions as the bash check: the message names the address and `tares up`, nothing is launched.
scenario(Scenario(
    name="crew_basic",
    suite="crew",
    about="without a Tares, `crew up --yes` says so plainly and starts nothing",
    steps=[Step(["crew", "up", "--yes"], env={"TARESD_URL": "http://127.0.0.1:9"})],
    checks=[("without Tares crew up stops plainly",
             lambda r: "no Tares answers at http://127.0.0.1:9" in r.out(0)
             and "tares up" in r.out(0) and r.launches() == 0)],
    covers=["without Tares crew up stops plainly"],
))


# --- crew up (TR-956): crew_test.sh, the parts that run `crew up` -------------------------------------------------
# Same assertions as the bash checks, on a stand-in Tares (fake_up_tares.py) instead of a daemon. The labels in
# `covers` are the bash suite's own words, `$r` included, as `--coverage` matches them. The assertions on the
# settings and the grant are read from what the stand-in kept; the milestone runs of crew_test.sh itself use
# a real daemon.

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
GRANT = "the releaser may restart the staging database"
KIT = os.path.realpath(ROOT / "claude-plugin")
PROD = "(^|[-_.])(prod|production)([-_.]|$)"
SAVED = {"autonomy": "L3-review", "releaser": True, "release_profile": "kind", "prod_pattern": PROD,
         "challenger": False, "builders_max": 3}
ROLES = ("orchestrator", "reviewer", "releaser")
# The language-server offer (TR-970) is not ported: the bash side asks Claude Code which plugins are on, the
# Python side does not, so that one call is left out of the compared record.
LSP_PROBE = (("plugin", "list"),)


def mark(name):
    """A step's `before`: keep the launches so far under `name`, for a check to read."""
    def before(r):
        r.marks[name] = r.launches()
    return before


def before_all(*fns):
    def before(r):
        for f in fns:
            f(r)
    return before


def trust_crew(r):
    r.trust(r.crew)


def plugin_installed_elsewhere(r):
    d = r.home / ".claude" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"tares@tares": [{"scope": "project", "projectPath": "/w/main", "version": "0.2.1"}]}}),
        encoding="utf-8")


def env_of(r, station, key):
    v = r.station_settings(station).get("env", {}).get(key)
    return "" if v is None or v is False else v


def allow_has(r, station, text):
    allow = r.station_settings(station).get("autoMode", {}).get("allow", [])
    return any(isinstance(x, str) and text in x for x in allow)


def started(r):
    """The station names `claude --bg` was called for, in order."""
    return [c["args"][2] for c in r.calls() if c["args"][:1] == ["--" + "bg"]]


def role_checks():
    out = []
    for role in ROLES:
        s = "crew-" + role
        out += [
            ("crew-$r recorded (%s)" % role, lambda r, s=s: r.station(s, "status") == "active"),
            ("crew-$r runs in the crew home (%s)" % role, lambda r, s=s: r.station(s, "cwd") == str(r.crew)),
            ("crew-$r env FACTORY_STATION (%s)" % role, lambda r, s=s: env_of(r, s, "FACTORY_STATION") == s),
            ("crew-$r env FACTORY_ROLE (%s)" % role, lambda r, s=s, role=role: env_of(r, s, "FACTORY_ROLE") == role),
            ("crew-$r env FACTORY_PARENT empty (%s)" % role,
             lambda r, s=s: r.station_settings(s).get("env", {}).get("FACTORY_PARENT") == ""),
            ("crew-$r has no TARES_PROJECT (%s)" % role, lambda r, s=s: env_of(r, s, "TARES_PROJECT") == ""),
            ("crew-$r reaches the same Tares (%s)" % role,
             lambda r, s=s: env_of(r, s, "TARES_FACTORY_URL") == r.stand_in.url),
            ("crew-$r is told the shared MCP endpoint (%s)" % role,
             lambda r, s=s: env_of(r, s, "TARES_FACTORY_MCP_URL") == r.stand_in.mcp_url),
        ]
    return out


scenario(Scenario(
    name="crew_up_basic",
    ignore_calls=LSP_PROBE,
    suite="crew",
    about="crew up stops plainly in an untrusted crew home, and warns of another installed tares plugin",
    stand_in={"settings": {**SAVED, "releaser": False, "builders_max": 2}},
    steps=[
        Step(["crew", "up", "--yes"], stdin=""),
        Step(["crew", "up", "--yes"], stdin="",
             run_before=before_all(mark("untrusted"), trust_crew, plugin_installed_elsewhere)),
    ],
    checks=[
        ("an untrusted crew home stops crew up plainly",
         lambda r: "Claude Code has not trusted the crew home %s yet" % r.crew in r.out(0)
         and 'pick "Yes, I trust this folder"' in r.out(0) and r.code(0) != 0 and r.marks["untrusted"] == 0),
        ("an installed older tares plugin is named with how to remove it",
         lambda r: "warning: another version of the tares plugin is installed" in r.out(1)
         and "claude plugin uninstall tares@tares --scope project (run it in /w/main)" in r.out(1)),
        ("...and crew up still starts the crew", lambda r: r.code(1) == 0 and r.launches() == 2),
    ],
    covers=["an untrusted crew home stops crew up plainly",
            "an installed older tares plugin is named with how to remove it"],
))

scenario(Scenario(
    name="crew_up_yes",
    ignore_calls=LSP_PROBE,
    suite="crew",
    about="crew up --yes uses the answers saved on Tares and asks nothing; a second run starts nothing",
    stand_in={"settings": dict(SAVED), "grants": ['- "%s" (%s)' % (GRANT, TODAY)]},
    prepare=trust_crew,
    steps=[Step(["crew", "up", "--yes"], stdin=""), Step(["crew", "up"], stdin="")],
    checks=[
        ("crew up --yes exits 0 and asks nothing",
         lambda r: r.code(0) == 0 and "Choice [" not in r.out(0)),
        ("three crew stations started (a releaser was asked for)", lambda r: r.launches() == 3),
        *role_checks(),
        ("no Rius, no comms", lambda r: not any(w in (r.crew / "stations" / "crew-reviewer.settings.json").read_text(
            encoding="utf-8").lower() for w in ("rius", "comms"))),
        ("orchestrator started first", lambda r: started(r)[:1] == ["crew-orchestrator"]),
        ("stations turn the plugin on, whatever the person's settings say",
         lambda r: r.station_settings("crew-reviewer").get("enabledPlugins", {}).get("tares@inline") is True),
        ("stations load this plugin", lambda r: {c["args"][c["args"].index("--plugin-dir") + 1]
                                                 for c in r.calls() if c["args"][:1] == ["--" + "bg"]} == {KIT}),
        ("settings not kept locally", lambda r: r.crew_json().get("settings") is None),
        ("no local desk or grant files", lambda r: not (r.crew / "DESK.md").exists() and not (r.crew / "GRANTS.md").exists()),
        ("grant quoted in the allow rules",
         lambda r: allow_has(r, "crew-releaser", 'granted this on %s for all projects, in their own words: "%s". Acting' % (TODAY, GRANT))),
        ("orchestrator may spawn",
         lambda r: "Bash(factory spawn *)" in r.station_settings("crew-orchestrator").get("permissions", {}).get("allow", [])),
        ("stations launch with a 200k autocompact window",
         lambda r: any(c["args"][2] == "crew-orchestrator" and "--autocompact" in c["args"]
                       and c["args"][c["args"].index("--autocompact") + 1] == "200000"
                       for c in r.calls() if c["args"][:1] == ["--" + "bg"])),
        ("nothing goes to production without the person",
         lambda r: any("needs" in x and "prod" in x for x in r.station_settings("crew-releaser").get("autoMode", {}).get("soft_deny", []))
         and "L5" not in (r.crew / "stations" / "crew-releaser.settings.json").read_text(encoding="utf-8")),
        ("second crew up asks nothing", lambda r: "Choice [" not in r.out(1) and r.code(1) == 0),
        ("second crew up starts nothing", lambda r: r.launches() == 3),
        ("nothing is written to Tares: every answer was already there", lambda r: r.stand_in.written() == []),
    ],
    covers=["three crew stations started (a releaser was asked for)", "no Rius, no comms",
            "orchestrator started first", "stations turn the plugin on, whatever the person's settings say",
            "stations load this plugin", "settings not kept locally", "no local desk or grant files",
            "grant quoted in the allow rules", "orchestrator may spawn",
            "stations launch with a 200k autocompact window", "nothing goes to production without the person",
            "second crew up asks nothing", "second crew up starts nothing"],
))

scenario(Scenario(
    name="crew_up_questions",
    ignore_calls=LSP_PROBE,
    suite="crew",
    about="the first crew up asks the settings one at a time and keeps the answers and one grant on Tares",
    stand_in={},
    prepare=trust_crew,
    steps=[Step(["crew", "up"], stdin="L3-review\nyes\n\nno\n3\n%s\n\n" % GRANT)],
    checks=[
        ("crew up exits 0", lambda r: r.code(0) == 0),
        ("the settings are asked one at a time",
         lambda r: all(q in r.out(0) for q in ("Who merges pull requests?", "Should the crew release and deploy finished work?",
                                               "How does the releaser ship?", "Should a second AI (Codex) check every commit before review?",
                                               "How many builders may work at the same time?"))),
        ("settings saved on Tares", lambda r: [r.stand_in.settings.get(k) for k in
                                               ("autonomy", "releaser", "release_profile", "challenger", "builders_max")]
         == ["L3-review", True, "kind", False, 3]),
        ("grant saved on Tares as the person said it", lambda r: r.stand_in.grants == ['- "%s" (%s)' % (GRANT, TODAY)]),
        ("crew up points at the Crew page", lambda r: "<TARES>/crew" in r.out(0).replace(r.stand_in.url, "<TARES>")),
        ("three stations started, a releaser among them",
         lambda r: r.launches() == 3 and r.station("crew-releaser", "status") == "active"),
    ],
    covers=["crew up exits 0", "settings saved on Tares", "grant saved on Tares as the person said it",
            "crew up points at the Crew page"],
))


# --- crewname_test.sh (TR-541), the parts that run `crew up` -------------------------------------------------
# Two crews and the default one on one machine and one Tares, one fake Claude Code. The roster and the builders
# are not ported yet (TR-957, TR-953): the scenario with the roster's title warning runs on bash alone until then.

def crew_at(name):
    """A step's env: crew `name` in its own folder beside the default crew's."""
    return lambda r: {"FACTORY_CREW_HOME": str(r.tmp / ("crew-" + name)), "FACTORY_CREW_NAME": name}


def crew_at_as(name, as_name):
    """Crew `name`'s folder, said to be named `as_name`."""
    return lambda r: {"FACTORY_CREW_HOME": str(r.tmp / ("crew-" + name)), "FACTORY_CREW_NAME": as_name}


def crew_at_unnamed(name):
    return lambda r: {"FACTORY_CREW_HOME": str(r.tmp / ("crew-" + name))}


def trust_all(r):
    r.trust(r.crew, r.tmp / "crew-a", r.tmp / "crew-b")


def home_of(r, name):
    return r.tmp / ("crew-" + name)


def second_person(r):
    r.stand_in.people.extend([{"id": "p2", "name": "Two"}, {"id": "p3", "name": "Three"}])   # bash asks for a name above one


def agent_names(r):
    try:
        rows = json.loads((r.state / "agents.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [x.get("name", "") for x in rows]


def two_crew_checks():
    out = []
    for role in ("orchestrator", "reviewer"):
        s = "crew-" + role
        out += [
            ("default crew: crew-$r is recorded (%s)" % role, lambda r, s=s: r.station(s, "status") == "active"),
            ("default crew: crew-$r env FACTORY_CREW (%s)" % role, lambda r, s=s: env_of(r, s, "FACTORY_CREW") == "crew"),
            ("default crew: crew-$r env FACTORY_ORCHESTRATOR (%s)" % role,
             lambda r, s=s: env_of(r, s, "FACTORY_ORCHESTRATOR") == "crew-orchestrator"),
        ]
    for c in ("a", "b"):
        for role in ("orchestrator", "reviewer"):
            s = "%s-%s" % (c, role)
            out += [
                ("crew $c: $c-$r is recorded and active (%s)" % s,
                 lambda r, c=c, s=s, role=role: r.station(s, "status", home_of(r, c)) == "active"
                 and r.station(s, "role", home_of(r, c)) == role),
                ("crew $c: $c-$r runs in its own crew home (%s)" % s,
                 lambda r, c=c, s=s: r.station(s, "cwd", home_of(r, c)) == str(home_of(r, c))),
                ("crew $c: $c-$r env FACTORY_STATION (%s)" % s,
                 lambda r, c=c, s=s: env_of_in(r, home_of(r, c), s, "FACTORY_STATION") == s),
                ("crew $c: $c-$r env FACTORY_CREW (%s)" % s,
                 lambda r, c=c, s=s: env_of_in(r, home_of(r, c), s, "FACTORY_CREW") == c),
                ("crew $c: $c-$r env FACTORY_ORCHESTRATOR (%s)" % s,
                 lambda r, c=c, s=s: env_of_in(r, home_of(r, c), s, "FACTORY_ORCHESTRATOR") == c + "-orchestrator"),
            ]
        out += [
            ("crew $c: the name is kept in crew.json (%s)" % c, lambda r, c=c: r.crew_json(home_of(r, c)).get("crew_name") == c),
            ("crew $c: no station of the default crew is recorded (%s)" % c,
             lambda r, c=c: r.station("crew-orchestrator", "status", home_of(r, c)) == ""
             and r.station("crew-reviewer", "status", home_of(r, c)) == ""),
        ]
    return out


scenario(Scenario(
    name="crew_up_old_tares",
    suite="crew",
    about="a Tares that does not list the integrations capability: crew up stops with the upgrade message, starting nothing",
    stand_in={"settings": {**SAVED, "releaser": False}, "capabilities": []},
    prepare=trust_all,
    ignore_calls=LSP_PROBE,
    steps=[Step(["crew", "up", "--yes"], stdin="")],
    checks=[
        ("crew up fails and names the missing capability",
         lambda r: r.code(0) != 0 and "This needs a newer Tares on the cell (missing integrations)" in r.out(0)),
        ("nothing was started and nothing was saved", lambda r: started(r) == [] and r.stand_in.written() == []),
    ],
    covers=[],
))


def env_of_in(r, home, station, key):
    v = r.station_settings(station, home).get("env", {}).get(key)
    return "" if v is None or v is False else v


scenario(Scenario(
    name="crewname_two_crews",
    suite="crewname",
    about="two named crews and the default crew side by side; the name comes from FACTORY_CREW_NAME, then crew.json",
    stand_in={"settings": {**SAVED, "releaser": False}},
    prepare=trust_all,
    ignore_calls=LSP_PROBE,
    steps=[
        Step(["crew", "up", "--yes"], stdin=""),                                   # 0 the default crew
        Step(["crew", "up", "--yes"], env=crew_at("a"), stdin=""),                 # 1
        Step(["crew", "up", "--yes"], env=crew_at("b"), stdin=""),                 # 2
        Step(["crew", "up", "--yes"], env=crew_at_unnamed("a"), stdin="",          # 3 crew.json alone names it
             run_before=mark("before_again")),
        Step(["crew", "up", "--yes"], env=crew_at_as("a", "zzz"), stdin=""),        # 4 refused, not quietly applied
        Step(["crew", "up", "--yes"], stdin="", run_before=second_person),             # 5 the default crew, running
        Step(["crew", "up", "--yes"], env={"FACTORY_CREW_NAME": "zzz"}, stdin=""), # 6 refused, nothing stored
    ],
    checks=[
        ("default crew: up exits 0", lambda r: r.code(0) == 0 and "<TARES>/crew" in r.out(0).replace(r.stand_in.url, "<TARES>")),
        *two_crew_checks(),
        ("default crew: no crew_name is stored, one person", lambda r: "crew_name" not in r.crew_json()),
        ("default crew: orchestrator started first, under today's name", lambda r: started(r)[:1] == ["crew-orchestrator"]),
        ("both crews and the default one run: no station was started twice",
         lambda r: r.launches() == 6 and len(started(r)) == len(set(started(r)))),
        ("the fake Claude holds six sessions with six different names",
         lambda r: len({n for n in agent_names(r) if n in ("crew-orchestrator", "crew-reviewer", "a-orchestrator",
                                                           "a-reviewer", "b-orchestrator", "b-reviewer")}) == 6),
        ("crew a: up again starts nothing and keeps the name",
         lambda r: r.code(3) == 0 and r.marks["before_again"] == 6 and r.crew_json(home_of(r, "a")).get("crew_name") == "a"),
        ("crew a: another FACTORY_CREW_NAME is refused",
         lambda r: r.code(4) != 0 and "this crew is named a" in r.out(4)
         and r.crew_json(home_of(r, "a")).get("crew_name") == "a" and r.launches() == 6),
        ("running default crew: crew up does not ask, rename or start anything",
         lambda r: r.code(5) == 0 and "What should this crew be called" not in r.out(5)
         and "crew_name" not in r.crew_json() and r.launches() == 6),
        ("running default crew: another FACTORY_CREW_NAME is refused and nothing is stored",
         lambda r: r.code(6) != 0 and "already running under the name crew" in r.out(6)
         and "crew_name" not in r.crew_json() and r.launches() == 6),
    ],
    covers=["default crew: up exits 0",
            "default crew: crew-$r is recorded", "default crew: crew-$r env FACTORY_CREW",
            "default crew: crew-$r env FACTORY_ORCHESTRATOR",
            "default crew: no crew_name is stored, one person",
            "default crew: orchestrator started first, under today's name",
            "crew $c: $c-$r is recorded and active", "crew $c: $c-$r runs in its own crew home",
            "crew $c: $c-$r env FACTORY_STATION", "crew $c: $c-$r env FACTORY_CREW",
            "crew $c: $c-$r env FACTORY_ORCHESTRATOR", "crew $c: the name is kept in crew.json",
            "crew $c: no station of the default crew is recorded",
            "both crews and the default one run: no station was started twice",
            "the fake Claude holds six sessions with six different names",
            "crew a: up again starts nothing and keeps the name", "crew a: another FACTORY_CREW_NAME is refused",
            "running default crew: crew up does not ask, rename or start anything",
            "running default crew: another FACTORY_CREW_NAME is refused and nothing is stored"],
))


def rename_orchestrator_session(r):
    """The orchestrator's session carries another title than the name messages go to."""
    f = r.state / "agents.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for x in rows:
        if x.get("name") == "a-orchestrator":
            x["name"] = "orchestrator-of-a"
    f.write_text(json.dumps(rows), encoding="utf-8")


scenario(Scenario(
    name="crewname_roster_title",
    suite="crewname",
    about="the roster warns when the orchestrator's session title is not the name messages go to",
    stand_in={"settings": {**SAVED, "releaser": False}},
    prepare=trust_all,
    ignore_calls=LSP_PROBE,
    steps=[
        Step(["crew", "up", "--yes"], env=crew_at("a"), stdin=""),
        Step(["crew", "up", "--yes"], env=crew_at("b"), stdin=""),
        Step(["roster"], env=crew_at_unnamed("a"), run_before=rename_orchestrator_session),
        Step(["roster"], env=crew_at_unnamed("b")),
    ],
    checks=[
        ("roster warns of a different orchestrator session title",
         lambda r: "Orchestrator session title is orchestrator-of-a, expected a-orchestrator: messages to it will not arrive" in r.out(2)),
        ("roster stays quiet when the title is the name", lambda r: "Orchestrator session title" not in r.out(3)),
    ],
    covers=["roster warns of a different orchestrator session title", "roster stays quiet when the title is the name"],
))


# ---------------------------------------------------------------------------------------------
# tidy_test.sh: tidy removes inside the crew's own folders and nowhere else. The bash suite hands a
# project over to a running Tares so its repo's .factory/worktrees is a root; here FACTORY_REPO names the
# same root, and TARESD_URL points at a closed port so a Tares on the host cannot add roots.

NO_TARES = {"TARESD_URL": "http://127.0.0.1:9"}


def _git_env(tmp: Path) -> dict:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}


def _git(tmp: Path, *args, cwd=None) -> None:
    subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, env=_git_env(tmp), check=True,
                   capture_output=True)


def _link_dir(target: Path, link: Path) -> None:
    """A link to a folder: a symlink, or on Windows a junction (no privilege needed)."""
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link)


def _tidy_base(tmp: Path) -> Path:
    """The crew's worktrees and tmp, an `outside` folder that must survive, and a repo with a remote."""
    for d in ("crew/worktrees", "crew/tmp", "outside/node_modules"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    (tmp / "outside" / "node_modules" / "keep").write_text("keep\n")
    (tmp / "outside" / "file").write_text("keep\n")
    r = tmp / "repo"
    _git(tmp, "init", "-q", "--bare", str(tmp / "origin.git"))
    _git(tmp, "clone", "-q", str(tmp / "origin.git"), str(r))
    (r / "tracked.txt").write_text("a\n")
    _git(tmp, "add", ".", cwd=r)
    _git(tmp, "commit", "-q", "-m", "init", cwd=r)
    _git(tmp, "push", "-q", "origin", "HEAD", cwd=r)
    return r


def _worktree(tmp: Path, name: str) -> Path:
    w = tmp / name
    _git(tmp, "worktree", "add", "-q", "--detach", str(w), "HEAD", cwd=tmp / "repo")
    return w


def _setup_worktrees(tmp: Path, env: dict) -> None:
    _tidy_base(tmp)
    w = _worktree(tmp, "crew/worktrees/p-pr1")
    _link_dir(tmp / "outside" / "node_modules", w / "node_modules")
    (w / "scratch.txt").write_text("x\n")
    (_worktree(tmp, "repo/.factory/worktrees/T2") / "tracked.txt").write_text("changed\n")
    (_worktree(tmp, "repo/.factory/worktrees/T3") / "new-file.txt").write_text("new\n")


def _wip_commit(tmp: Path) -> None:
    b = tmp / "repo" / ".factory" / "worktrees" / "T2"
    _git(tmp, "checkout", "-q", "--", "tracked.txt", cwd=b)
    _git(tmp, "commit", "-q", "--allow-empty", "-m", "wip", cwd=b)


def _listed(tmp: Path, name: str) -> bool:
    out = subprocess.run(["git", "worktree", "list"], cwd=str(tmp / "repo"), env=_git_env(tmp),
                         capture_output=True, text=True).stdout
    return name in out


def _exists(*rel):
    return lambda tmp: all((tmp.joinpath(*r.split("/"))).exists() for r in rel)


_REPO = {"FACTORY_REPO": "{tmp}/repo", **NO_TARES}

scenario(Scenario(
    name="tidy_worktrees",
    suite="tidy",
    about="tidy removes a worktree through git, and refuses one with work in it unless --force",
    setup=_setup_worktrees,
    steps=[
        Step(["tidy", "{tmp}/crew/worktrees/p-pr1"], env=_REPO, after=_exists("crew/worktrees/p-pr1")),
        Step(["tidy", "--force", "{tmp}/crew/worktrees/p-pr1"], env=_REPO,
             after=lambda tmp: (_exists("crew/worktrees/p-pr1")(tmp), _listed(tmp, "p-pr1"),
                                _exists("outside/node_modules/keep")(tmp))),
        Step(["tidy", "{tmp}/repo/.factory/worktrees/T2"], env=_REPO, after=_exists("repo/.factory/worktrees/T2")),
        Step(["tidy", "{tmp}/repo/.factory/worktrees/T2"], env=_REPO, before=_wip_commit,
             after=_exists("repo/.factory/worktrees/T2")),
        Step(["tidy", "--force", "{tmp}/repo/.factory/worktrees/T2"], env=_REPO,
             after=_exists("repo/.factory/worktrees/T2")),
        Step(["tidy", "{tmp}/repo/.factory/worktrees/T3"], env=_REPO,
             after=_exists("repo/.factory/worktrees/T3/new-file.txt")),
        Step(["tidy", "--force", "{tmp}/repo/.factory/worktrees/T3"], env=_REPO),
    ],
    checks=[
        ("a review worktree with scratch files needs --force", lambda r: r.code(0) != 0 and r.snap(0)),
        ("a review worktree is removed with --force",
         lambda r: r.code(1) == 0 and not r.snap(1)[0] and not r.snap(1)[1]),
        ("its node_modules link was not followed", lambda r: r.snap(1)[2]),
        ("a worktree with uncommitted changes is refused",
         lambda r: r.code(2) != 0 and r.snap(2) and "--force" in r.out(2)),
        ("a worktree with unpushed commits is refused", lambda r: r.code(3) != 0 and r.snap(3)),
        ("--force removes it", lambda r: r.code(4) == 0 and not r.snap(4)),
        ("a worktree with new, never-added files is refused", lambda r: r.code(5) != 0 and r.snap(5)),
    ],
    covers=["a review worktree with scratch files needs --force", "a review worktree is removed with --force",
            "its node_modules link was not followed", "a worktree with uncommitted changes is refused",
            "a worktree with unpushed commits is refused", "--force removes it",
            "a worktree with new, never-added files is refused"],
))


def _setup_scratch(tmp: Path, env: dict) -> None:
    _tidy_base(tmp)
    crew_tmp = tmp / "crew" / "tmp"
    _link_dir(tmp / "outside", crew_tmp / "link")
    (crew_tmp / "inner").mkdir()
    (crew_tmp / "inner" / "keep").write_text("keep\n")
    _link_dir(crew_tmp / "inner", crew_tmp / "lg")
    (crew_tmp / "s" / "deep").mkdir(parents=True)
    (crew_tmp / "f.txt").write_text("x\n")
    (tmp / "repo" / ".factory" / "worktrees" / "T1" / "x").mkdir(parents=True)
    for v in ("v1", "v2"):
        (tmp / "job" / "tmp" / v).mkdir(parents=True)


def _gone(*rel):
    return lambda r: all(not r.tmp.joinpath(*x.split("/")).exists() for x in rel)


def _kept(*rel):
    return lambda r: all(r.tmp.joinpath(*x.split("/")).exists() for x in rel)


scenario(Scenario(
    name="tidy_links_scratch",
    suite="tidy",
    about="tidy removes a link and not what it points at, and scratch folders of the crew, a repo and a job",
    setup=_setup_scratch,
    steps=[
        Step(["tidy", "{tmp}/crew/tmp/link"], env=_REPO),
        Step(["tidy", "{tmp}/crew/tmp/lg/"], env=_REPO),
        Step(["tidy", "{tmp}/crew/tmp/s", "{tmp}/crew/tmp/f.txt"], env=_REPO),
        Step(["tidy", "{tmp}/repo/.factory/worktrees/T1"], env=_REPO),
        Step(["tidy", "{tmp}/job/tmp/v1"], env={**NO_TARES, "CLAUDE_JOB_DIR": "{tmp}/job"}),
        Step(["tidy", "{tmp}/job/tmp/v2"], env=NO_TARES),
    ],
    checks=[
        ("a link is removed, not followed",
         lambda r: not os.path.lexists(r.tmp / "crew" / "tmp" / "link") and _kept("outside/file")(r)),
        ("a link given with a trailing slash goes, its target stays",
         lambda r: not os.path.lexists(r.tmp / "crew" / "tmp" / "lg") and _kept("crew/tmp/inner/keep")(r)),
        ("a scratch folder and a file are removed", _gone("crew/tmp/s", "crew/tmp/f.txt")),
        ("a project's .factory/worktrees is inside", _gone("repo/.factory/worktrees/T1")),
        ("the job's own scratch folder is inside", _gone("job/tmp/v1")),
        ("another job's scratch folder is not", _kept("job/tmp/v2")),
    ],
    covers=["a link is removed, not followed", "a link given with a trailing slash goes, its target stays",
            "a scratch folder and a file are removed", "a project's .factory/worktrees is inside",
            "the job's own scratch folder is inside", "another job's scratch folder is not"],
))


def _setup_refusals(tmp: Path, env: dict) -> None:
    _tidy_base(tmp)
    (tmp / "crew" / "worktrees" / "a").mkdir()


def _link_root(tmp: Path) -> None:
    _link_dir(tmp / "outside", tmp / "linkroot")


def _home_x(tmp: Path) -> None:
    (tmp / "home" / "x").mkdir()


_HOME_ROOT = {**NO_TARES, "FACTORY_WORKTREES": "{tmp}/home"}

scenario(Scenario(
    name="tidy_refusals",
    suite="tidy",
    about="tidy refuses what is outside the roots, a root itself, .., the home folder and a root that is a link",
    setup=_setup_refusals,
    steps=[
        Step(["tidy", "{tmp}/outside"], env=NO_TARES),
        Step(["tidy", "{tmp}/crew/worktrees"], env=NO_TARES),
        Step(["tidy", "{tmp}/crew/worktrees/a/..", "{tmp}/crew/worktrees/a/../../crew-x"], env=NO_TARES),
        Step(["tidy", "{tmp}/home"], env=NO_TARES),
        Step(["tidy", "{tmp}/linkroot/file"], env={**NO_TARES, "FACTORY_WORKTREES": "{tmp}/linkroot"},
             before=_link_root),
        Step(["tidy", "{tmp}/home/.claude"], env=_HOME_ROOT),
        Step(["tidy", "{tmp}/home/x"], env=_HOME_ROOT, before=_home_x),
        Step(["tidy", "{tmp}/gone"], env=NO_TARES),
    ],
    checks=[
        ("outside the crew's folders is refused",
         lambda r: r.code(0) != 0 and "outside the crew" in r.out(0) and _kept("outside")(r)),
        ("a root itself is refused", _kept("crew/worktrees")),
        ("an escape through .. is refused", _kept("crew/worktrees/a")),
        ("the home folder is refused", _kept("home")),
        ("a root that is a link is not a root", _kept("outside/file")),
        ("the home folder is never a root", _kept("home/x")),
        ("something already gone is fine", lambda r: r.code(7) == 0 and "already gone" in r.out(7)),
    ],
    covers=["outside the crew's folders is refused", "a root itself is refused",
            "an escape through .. is refused", "the home folder is refused",
            "a root that is a link is not a root", "the home folder is never a root",
            "something already gone is fine"],
))


def _setup_escape(tmp: Path, env: dict) -> None:
    _tidy_base(tmp)
    _link_dir(tmp / "outside", tmp / "crew" / "tmp" / "door")
    objects = tmp / "crew" / "tmp" / "scratch" / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    for n in ("cdef01", "cdef02"):
        f = objects / n
        f.write_text("object\n")
        f.chmod(0o444)  # git writes its objects read-only; Windows will not delete such a file


scenario(Scenario(
    name="tidy_link_escape",
    suite="tidy",
    about="a path that reaches outside the roots through a link or junction is refused; read-only git objects go",
    setup=_setup_escape,
    steps=[
        Step(["tidy", "{tmp}/crew/tmp/door/node_modules"], env=NO_TARES),
        Step(["tidy", "{tmp}/crew/tmp/scratch"], env=NO_TARES),
    ],
    checks=[
        ("a path through a link out of the roots is refused",
         lambda r: r.code(0) != 0 and "outside the crew" in r.out(0) and _kept("outside/node_modules/keep")(r)),
        ("a folder of read-only git objects is removed",
         lambda r: r.code(1) == 0 and _gone("crew/tmp/scratch")(r)),
    ],
    covers=[],
))

# launch_*: building and starting a station (write_settings, launch, boot_prompt, grant_rules,
# compact_window). Not a command yet, so each runs through tests/factory/launch_support.py, which calls
# the function of the bash engine (sourced from a copy of the kit) or of tares.crew.launch, on a fake
# Tares of its own. The assertions are those of crew_test.sh, read off the files the station is given.

LAUNCH_DRIVER = "launch_support.py"
LAUNCH_GRANT = '- "the releaser may restart the staging database" (2026-01-02)\n'


def lstep(op: str, *args: str, world: dict | None = None, setup: dict | None = None, stdin: str = "",
          env: dict | None = None) -> Step:
    """One call of the driver: the function, its arguments, the fake Tares and the crew home it runs on."""
    e = dict(env or {})
    if world is not None:
        e["LAUNCH_WORLD"] = json.dumps(world)
    if setup is not None:
        e["LAUNCH_SETUP"] = json.dumps(setup)
    return Step([op, *args], env=e, stdin=stdin)


def lworld(autonomy: str = "L4-ship", projects: list | None = None, grants: str = "", project_grants: dict | None = None,
           **settings) -> dict:
    return {"settings": {"autonomy": autonomy, "releaser": True, "release_profile": "kind", "challenger": False,
                         "builders_max": 3, **settings},
            "projects": projects or [], "grants": {"all": grants, "projects": project_grants or {}}}


def station_json(r: "Run", station: str) -> dict:
    return json.loads((r.crew / "stations" / f"{station}.settings.json").read_text(encoding="utf-8"))


def allow_of(r: "Run", station: str) -> list:
    return station_json(r, station)["autoMode"]["allow"]


def fwd(path) -> str:
    """A path with forward slashes: how the Python engine writes it into a settings file on Windows."""
    return str(path).replace("\\", "/")


def tmp_path(r: "Run", *parts: str) -> str:
    return fwd(r.tmp.joinpath(*parts))


def relayed(allow: list) -> bool:
    return any(re.search(r"relayed .*yes", a) for a in allow)


def per_role(label: str, fn) -> list:
    """The checks of a crew_test.sh loop over `for r in orchestrator reviewer releaser`: one label (with its
    literal `$r`), true when fn(run, role) holds for every role."""
    return [(label, lambda r, fn=fn: all(fn(r, role) for role in ("orchestrator", "reviewer", "releaser")))]


_ROLE_STEPS = [lstep("write_settings", f"crew-{role}", role, "", "", "",
                     world=lworld("L3-review", grants=LAUNCH_GRANT)) for role in ("orchestrator", "reviewer", "releaser")]

scenario(Scenario(
    name="launch_settings_crew",
    suite="crew",
    about="the settings file of the orchestrator, reviewer and releaser (env, plugin, permissions, grants)",
    driver=LAUNCH_DRIVER,
    steps=_ROLE_STEPS,
    checks=[
        *per_role("crew-$r env FACTORY_STATION", lambda r, role: station_json(r, f"crew-{role}")["env"]["FACTORY_STATION"] == f"crew-{role}"),
        *per_role("crew-$r env FACTORY_ROLE", lambda r, role: station_json(r, f"crew-{role}")["env"]["FACTORY_ROLE"] == role),
        *per_role("crew-$r env FACTORY_PARENT empty", lambda r, role: station_json(r, f"crew-{role}")["env"]["FACTORY_PARENT"] == ""),
        *per_role("crew-$r has no TARES_PROJECT", lambda r, role: not station_json(r, f"crew-{role}")["env"].get("TARES_PROJECT")),
        *per_role("crew-$r is told the shared MCP endpoint",
                  lambda r, role: station_json(r, f"crew-{role}")["env"]["TARES_FACTORY_MCP_URL"] == "http://127.0.0.1:8788/mcp"),
        ("no Rius, no comms", lambda r: not re.search("rius|comms", json.dumps(station_json(r, "crew-reviewer")), re.I)),
        ("stations turn the plugin on, whatever the person's settings say",
         lambda r: station_json(r, "crew-reviewer")["enabledPlugins"]["tares@inline"] is True),
        ("orchestrator may spawn",
         lambda r: "Bash(factory spawn *)" in station_json(r, "crew-orchestrator")["permissions"]["allow"]),
        ("nothing goes to production without the person",
         lambda r: any("needs" in x and "prod" in x for x in station_json(r, "crew-releaser")["autoMode"]["soft_deny"])
         and "L5" not in json.dumps(station_json(r, "crew-releaser"))),
        ("grant quoted in the allow rules",
         lambda r: any('granted this on 2026-01-02 for all projects, in their own words: '
                       '"the releaser may restart the staging database". Acting' in x for x in allow_of(r, "crew-releaser"))),
    ],
    covers=["crew-$r env FACTORY_STATION", "crew-$r env FACTORY_ROLE", "crew-$r env FACTORY_PARENT empty",
            "crew-$r has no TARES_PROJECT", "crew-$r is told the shared MCP endpoint", "no Rius, no comms",
            "stations turn the plugin on, whatever the person's settings say", "orchestrator may spawn",
            "nothing goes to production without the person", "grant quoted in the allow rules"],
))

_LINKS = {"id": "uc_links", "name": "Links App", "repos": [{"path": "{TMP}/links"}]}
scenario(Scenario(
    name="launch_settings_builder",
    suite="crew",
    about="a builder in a repo of its project, at L3 (settings and the project's id in stations.json)",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "links-app-build-m1", "builder", "crew-orchestrator", "Links App", "{TMP}/links",
                 world=lworld("L3-review", projects=[_LINKS]))],
    checks=[
        ("builder TARES_PROJECT is the project id, the name beside it",
         lambda r: station_json(r, "links-app-build-m1")["env"]["TARES_PROJECT"] == "uc_links"
         and station_json(r, "links-app-build-m1")["env"]["TARES_PROJECT_NAME"] == "Links App"),
        ("builder FACTORY_PARENT", lambda r: station_json(r, "links-app-build-m1")["env"]["FACTORY_PARENT"] == "crew-orchestrator"),
        ("builder FACTORY_REPO", lambda r: station_json(r, "links-app-build-m1")["env"]["FACTORY_REPO"] == tmp_path(r, "links")),
        ("builder worktrees in its repo",
         lambda r: station_json(r, "links-app-build-m1")["env"]["FACTORY_WORKTREES"] == tmp_path(r, "links") + "/.factory/worktrees"),
        ("L3: builder waits for the relayed yes", lambda r: relayed(allow_of(r, "links-app-build-m1"))),
        ("L3: the no-CI clause holds at L3 too",
         lambda r: any(re.search(r"relayed .*yes", a) and "no CI checks configured" in a for a in allow_of(r, "links-app-build-m1"))),
        ("L3: no pre-authorized merge",
         lambda r: not any("pre-authorized at level" in a for a in allow_of(r, "links-app-build-m1"))),
        ("stations leave cmux hooks on, so its permission alerts stay",
         lambda r: "CMUX_CLAUDE_HOOKS_DISABLED" not in station_json(r, "links-app-build-m1")["env"]),
    ],
    covers=["builder TARES_PROJECT is the project id, the name beside it", "builder FACTORY_PARENT", "builder FACTORY_REPO",
            "builder worktrees in its repo", "L3: builder waits for the relayed yes",
            "L3: the no-CI clause holds at L3 too", "L3: no pre-authorized merge",
            "stations leave cmux hooks on, so its permission alerts stay"],
))

_CORE = {"id": "uc_core", "name": "strict", "repos": [{"path": "{TMP}/core"}]}
_LOOSE = {"id": "uc_loose", "name": "loose", "repos": [{"path": "{TMP}/core"}]}
scenario(Scenario(
    name="launch_settings_levels",
    suite="crew",
    about="a project handed over stricter keeps its level, one at the crew's level merges on the gate",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "strict-build-m1", "builder", "crew-orchestrator", "strict", "{TMP}/core",
                 world=lworld("L4-ship", projects=[_CORE, _LOOSE]),
                 setup={"stations": {"strict-build-m1": {"autonomy": "L3-review", "project_id": "uc_core"}}}),
           lstep("write_settings", "loose-build-m1", "builder", "crew-orchestrator", "loose", "{TMP}/core",
                 world=lworld("L4-ship", projects=[_CORE, _LOOSE]))],
    checks=[
        ("a project handed over stricter keeps it", lambda r: relayed(allow_of(r, "strict-build-m1"))),
        ("L4: the builder merges on the gate",
         lambda r: any("pre-authorized at level L4-ship" in a for a in allow_of(r, "loose-build-m1"))),
    ],
    covers=["a project handed over stricter keeps it", "L4: the builder merges on the gate"],
))

scenario(Scenario(
    name="launch_settings_lowered",
    suite="crew",
    about="a crew lowered after the hand-over takes the project's builder down with it",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "loose-build-m1", "builder", "crew-orchestrator", "loose", "{TMP}/core",
                 world=lworld("L3-review", projects=[_LOOSE]),
                 setup={"stations": {"loose-build-m1": {"autonomy": "L4-ship", "project_id": "uc_loose"}}})],
    checks=[("a crew lowered later takes the project down with it", lambda r: relayed(allow_of(r, "loose-build-m1")))],
    covers=["a crew lowered later takes the project down with it"],
))

_ARGUS = {"id": "uc_argus", "name": "argus", "repos": [{"path": "{TMP}/argus/core"}, {"path": "{TMP}/argus/charts"}]}
scenario(Scenario(
    name="launch_settings_multi",
    suite="crew",
    about="one project with two repos: each builder is scoped to its repo, the reviewer sees both",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "argus-build-m1-charts", "builder", "crew-orchestrator", "argus", "{TMP}/argus/charts",
                 world=lworld("L4-ship", projects=[_LINKS, _ARGUS])),
           lstep("write_settings", "crew-reviewer", "reviewer", "", "", "", world=lworld("L4-ship", projects=[_LINKS, _ARGUS]))],
    checks=[
        ("multi: each builder runs in its repo",
         lambda r: station_json(r, "argus-build-m1-charts")["env"]["FACTORY_REPO"] == tmp_path(r, "argus", "charts")),
        ("multi: a builder may push and merge only in its own repo",
         lambda r: (lambda rules: len(rules) == 2 and all(tmp_path(r, "argus", "charts") in x and tmp_path(r, "argus", "core") not in x
                                                           for x in rules))(
             [x for x in allow_of(r, "argus-build-m1-charts") if re.match(r"^(Pushing|Merging)", x)])),
        ("multi: a builder's worktrees stay within its project",
         lambda r: "repositories of project argus: " in allow_of(r, "argus-build-m1-charts")[1]
         and tmp_path(r, "argus", "core") in allow_of(r, "argus-build-m1-charts")[1]
         and tmp_path(r, "links") not in allow_of(r, "argus-build-m1-charts")[1]),
        ("multi: the reviewer's grant names every repo",
         lambda r: any(tmp_path(r, "argus", "core") in x and tmp_path(r, "argus", "charts") in x and "factory/review" in x
                       for x in allow_of(r, "crew-reviewer"))),
    ],
    covers=["multi: each builder runs in its repo", "multi: a builder may push and merge only in its own repo",
            "multi: a builder's worktrees stay within its project", "multi: the reviewer's grant names every repo"],
))


def bg_calls(r: "Run") -> list:
    return [c for c in r.calls() if c["args"] and c["args"][0] == "--bg"]


def arg_after(args: list, flag: str):
    return args[args.index(flag) + 1] if flag in args else None


_UP_WORLD = lworld("L3-review", grants=LAUNCH_GRANT)
_UP = [lstep("launch", f"crew-{role}", role, "", "", "", "{TMP}/crew", model, "high", f"Boot as the {role} station", world=_UP_WORLD)
       for role, model in (("orchestrator", "opus"), ("reviewer", "sonnet"), ("releaser", "sonnet"))]
scenario(Scenario(
    name="launch_argv",
    suite="crew",
    about="the claude command line of the three crew stations, and what stations.json records for them",
    driver=LAUNCH_DRIVER,
    steps=_UP,
    checks=[
        *per_role("crew-$r recorded", lambda r, role: json.loads((r.crew / "stations.json").read_text(encoding="utf-8"))[f"crew-{role}"]["status"] == "active"),
        *per_role("crew-$r runs in the crew home",
                  lambda r, role: json.loads((r.crew / "stations.json").read_text(encoding="utf-8"))[f"crew-{role}"]["cwd"].replace("\\", "/") == fwd(r.crew)),
        ("orchestrator started first", lambda r: bg_calls(r)[0]["args"][2] == "crew-orchestrator"),
        ("stations load this plugin",
         lambda r: {arg_after(c["args"], "--plugin-dir") for c in bg_calls(r)} == {str((r.tmp / "kit").resolve())}),
        ("stations launch with a 200k autocompact window",
         lambda r: arg_after(next(c for c in bg_calls(r) if c["args"][2] == "crew-orchestrator")["args"], "--autocompact") == "200000"),
        ("launched in the folder asked for, prompt last, model and effort given",
         lambda r: all(c["cwd"] == str(r.crew) and c["args"][-1].startswith("Boot as the ") and arg_after(c["args"], "--effort") == "high"
                       for c in bg_calls(r)) and arg_after(bg_calls(r)[1]["args"], "--model") == "sonnet"),
        ("the grants it started with are kept for the watch",
         lambda r: (r.crew / "stations" / "crew-releaser.grants").is_file()),
    ],
    covers=["crew-$r recorded", "crew-$r runs in the crew home", "orchestrator started first", "stations load this plugin",
            "stations launch with a 200k autocompact window"],
))

scenario(Scenario(
    name="launch_resume",
    suite="crew",
    about="a station restarted from its session: --resume with the id, the prompt still last",
    driver=LAUNCH_DRIVER,
    steps=[lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "sonnet", "high", "carry on", "sess-123",
                 world=lworld("L3-review"))],
    checks=[("--resume carries the session id and the prompt follows it",
             lambda r: arg_after(bg_calls(r)[0]["args"], "--resume") == "sess-123" and bg_calls(r)[0]["args"][-1] == "carry on")],
    covers=[],
))

scenario(Scenario(
    name="launch_fail",
    suite="crew",
    about="a station is refused (Haiku, an unreadable level) or does not start: said plainly, nothing recorded",
    driver=LAUNCH_DRIVER,
    steps=[lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "haiku", "high", "x", world=lworld("L3-review")),
           lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "sonnet", "high", "x", world=lworld("L9-mad")),
           lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "sonnet", "high", "x", world=lworld("L3-review"),
                 env={"FAKE_CLAUDE_FAIL_FRESH": "1"})],
    checks=[("the three refusals each exit 1 and say why",
             lambda r: [r.code(i) for i in range(3)] == [1, 1, 1] and "Auto mode does not run on Haiku" in r.out(0)
             and "no valid crew autonomy" in r.out(1) and "could not start crew-reviewer" in r.out(2)),
            ("nothing is recorded for a station that did not start",
             lambda r: "crew-reviewer" not in json.loads((r.crew / "stations.json").read_text(encoding="utf-8")))],
    covers=[],
))

scenario(Scenario(
    name="launch_boot",
    suite="crew",
    about="the boot prompt of a station, with and without the idle wake note and a task",
    driver=LAUNCH_DRIVER,
    steps=[lstep("boot_prompt", "orchestrator", world=lworld("L3-review")),
           lstep("boot_prompt", "reviewer", world=lworld("L3-review", idle_wake=True)),
           lstep("boot_prompt", "builder", "work T1 and T2", world=lworld("L3-review", idle_wake=False)),
           lstep("idle_wake_note", world=lworld("L3-review"))],
    checks=[("the prompts name the role, the READY message and the task",
             lambda r: "follow the Boot section" in r.out(0) and "send crew-orchestrator READY" in r.out(1)
             and "Your task:\nwork T1 and T2" in r.out(2))],
    covers=[],
))

scenario(Scenario(
    name="launch_grants_compact",
    suite="crew",
    about="grant lines into allow rules (all-project and project, dated, undated, noise), and the autocompact window",
    driver=LAUNCH_DRIVER,
    steps=[lstep("grant_rules", "Links App", "Pat", stdin=(
               '- "restart staging" (2026-01-02)\n@project - "push to main on Fridays" (2026-02-03)\n'
               '* 2026-03-04: deploy the docs\n2026-03-05 - tidy the cache\n- no date at all\nnot a grant\n- ""\n'
               '- "dropped, the last line has no newline" (2026-04-05)'),
                 world=lworld()),
           lstep("compact_window", world=lworld(), setup={"crew": {"limits": {"autocompact_tokens": 100000}}}),
           lstep("compact_window", world=lworld(), setup={"crew": {"limits": {"autocompact_tokens": "auto"}}}),
           lstep("compact_window", world=lworld(), setup={"crew": {"limits": {"autocompact_tokens": "oops"}}})],
    checks=[("grants read as bash reads them", lambda r: r.out(0).count("granted this") == 6 and "(undated)" in r.out(0)
             and "for project Links App" in r.out(0) and "dropped" not in r.out(0)),
            ("a window under 150k is raised and said", lambda r: "stations use 150000" in r.out(1) and r.results[1]["stdout"].strip() == "150000"),
            ("auto stays auto, junk gives 200000", lambda r: r.out(2).strip() == "auto" and r.out(3).strip() == "200000")],
    covers=[],
))

scenario(Scenario(
    name="launch_window",
    suite="crew",
    about="a window under 150k in crew.json: the station starts with --autocompact 150000, and it is said",
    driver=LAUNCH_DRIVER,
    steps=[lstep("launch", "crew-orchestrator", "orchestrator", "", "", "", "{TMP}/crew", "opus", "high", "go",
                 world=lworld("L3-review"), setup={"crew": {"limits": {"autocompact_tokens": 100000}}})],
    checks=[("window under 150k raised",
             lambda r: arg_after(bg_calls(r)[0]["args"], "--autocompact") == "150000" and "stations use 150000" in r.out(0))],
    covers=["window under 150k raised"],
))


# ---------------------------------------------------------------------------------------------
# Spawn (TR-954): `factory spawn builder|helper|releaser-helper` on a fake Tares that answers /health, the
# crew settings, the projects and an MCP initialize. The repos are made by the driver (setup "gitrepos").

def stations_json(r: "Run") -> dict:
    return json.loads((r.crew / "stations.json").read_text(encoding="utf-8"))


def bg_call_of(r: "Run", station: str) -> dict:
    return next(c for c in bg_calls(r) if c["args"][2] == station)


def same_dir(a, b) -> bool:
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def spawn_step(*args: str, **kw) -> Step:
    return lstep("spawn", *args, **kw)


WIN_NO_MULTILINE = ("a station is started through claude.cmd here, and cmd.exe cannot take the boot prompt of a builder or helper "
                    "(several lines); the claude.exe form waits for TR-946")

_SP_REPOS = {"gitrepos": ["{TMP}/links", "{TMP}/other"]}
_SP_WORLD = lworld("L3-review", projects=[_LINKS])
_B = "links-app-build-m1"

scenario(Scenario(
    name="spawn_builder",
    suite="crew",
    about="a builder starts in a repo of its project, with the project in its env; refusals start nothing; a helper keeps its parent's project",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_MULTILINE,
    steps=[spawn_step("builder", "Links App", "m1", "{TMP}/links", "work T1 and T2", world=_SP_WORLD, setup=_SP_REPOS),
           spawn_step("builder", "Links App", "m1", "{TMP}/links", "again", world=_SP_WORLD),
           spawn_step("builder", "Nobody's", "m1", "{TMP}/links", "x", world=_SP_WORLD),
           spawn_step("builder", "Links App", "m2", "{TMP}/other", "x", world=_SP_WORLD),
           spawn_step("builder", "Links App", "m3", "{TMP}/nowhere", "x", world=_SP_WORLD),
           spawn_step("builder", "Links App", "m4", world=_SP_WORLD),
           spawn_step("helper", _B, "rebase", "rebase T2", world=_SP_WORLD),
           spawn_step("helper", "nobody", "probe", "x", world=_SP_WORLD),
           spawn_step("helper", _B, world=_SP_WORLD),
           spawn_step("sprite", world=_SP_WORLD)],
    checks=[
        ("builder recorded", lambda r: stations_json(r)[_B]["role"] == "builder"),
        ("builder runs in its repo", lambda r: same_dir(bg_call_of(r, _B)["cwd"], r.tmp / "links")),
        ("builder TARES_PROJECT is the project id, the name beside it",
         lambda r: station_json(r, _B)["env"]["TARES_PROJECT"] == "uc_links" and station_json(r, _B)["env"]["TARES_PROJECT_NAME"] == "Links App"),
        ("builder FACTORY_PARENT", lambda r: station_json(r, _B)["env"]["FACTORY_PARENT"] == "crew-orchestrator"),
        ("builder FACTORY_REPO", lambda r: same_dir(station_json(r, _B)["env"]["FACTORY_REPO"], r.tmp / "links")),
        ("builder worktrees in its repo",
         lambda r: fwd(station_json(r, _B)["env"]["FACTORY_WORKTREES"]).endswith("/links/.factory/worktrees")),
        ("L3: builder waits for the relayed yes", lambda r: relayed(allow_of(r, _B))),
        ("L3: no pre-authorized merge", lambda r: not any("pre-authorized at level" in x for x in allow_of(r, _B))),
        ("no duplicate builder", lambda r: r.code(1) == 1 and f"{_B} is already running; message it instead" in r.out(1)),
        ("a project not handed over is refused",
         lambda r: r.code(2) == 1 and "is not handed to the crew on Tares" in r.out(2) and "Nobody's" in r.out(2)),
        ("a repo the project was not handed over with is refused",
         lambda r: r.code(3) == 1 and "is not one of Links App's repos on Tares" in r.out(3)),
        ("a folder that is no repository is refused", lambda r: r.code(4) == 1 and "nowhere is not a git repository" in r.out(4)),
        ("too few arguments say how to call it",
         lambda r: r.code(5) == 1 and 'usage: factory spawn builder [--autonomy L3-review] <project> <slug> <repo path> "<task>"' in r.out(5)
         and r.code(8) == 1 and 'usage: factory spawn helper <parent-station> <slug> "<task>"' in r.out(8)),
        ("the repo hides .factory from git and has a worktrees folder",
         lambda r: ".factory/" in (r.tmp / "links" / ".git" / "info" / "exclude").read_text(encoding="utf-8").split("\n")
         and (r.tmp / "links" / ".factory" / "worktrees").is_dir()),
        ("only the first builder started, then the helper", lambda r: r.launches() == 2),
        ("helper name", lambda r: stations_json(r)[f"{_B}-rebase"]["role"] == "helper"),
        ("helper parent", lambda r: station_json(r, f"{_B}-rebase")["env"]["FACTORY_PARENT"] == _B),
        ("helper project",
         lambda r: station_json(r, f"{_B}-rebase")["env"]["TARES_PROJECT"] == "uc_links"
         and station_json(r, f"{_B}-rebase")["env"]["TARES_PROJECT_NAME"] == "Links App"),
        ("the helper's prompt names its parent's manual and its task",
         lambda r: bg_call_of(r, f"{_B}-rebase")["args"][-1].startswith(
             f"Boot as a helper of the station {_B}: load the skills factory-protocol and station-builder, do only the task below")
         and bg_call_of(r, f"{_B}-rebase")["args"][-1].endswith("Your task:\nrebase T2")),
        ("a helper needs a recorded parent", lambda r: r.code(7) == 1 and "nobody is not a recorded station" in r.out(7)),
        ("spawn takes builder or helper", lambda r: r.code(9) == 1 and "spawn takes builder or helper" in r.out(9)),
    ],
    covers=["builder recorded", "builder runs in its repo", "builder TARES_PROJECT is the project id, the name beside it",
            "builder FACTORY_PARENT", "builder FACTORY_REPO", "builder worktrees in its repo", "L3: builder waits for the relayed yes",
            "L3: no pre-authorized merge", "no duplicate builder", "a project not handed over is refused",
            "a repo the project was not handed over with is refused", "helper name", "helper parent", "helper project"],
))

_SHOP = {"id": "uc_shop", "name": "shop", "repos": [{"path": "{TMP}/shop"}]}
_BLOG = {"id": "uc_blog", "name": "blog", "repos": [{"path": "{TMP}/blog"}]}
_CREWTOOLS = {"id": "uc_ct", "name": "Crew Tools", "repos": [{"path": "{TMP}/blog"}]}
_SP_LIMIT_WORLD = lworld("L4-ship", projects=[_SHOP, _BLOG, _CREWTOOLS], builders_max=2)

scenario(Scenario(
    name="spawn_limits",
    suite="crew",
    about="builders_max is honoured; a project named like the crew is refused before it takes a place",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_MULTILINE,
    steps=[spawn_step("builder", "Crew Tools", "m1", "{TMP}/blog", "x", world=_SP_LIMIT_WORLD,
                      setup={"gitrepos": ["{TMP}/shop", "{TMP}/blog"]}),
           spawn_step("builder", "shop", "m1", "{TMP}/shop", "work T1", world=_SP_LIMIT_WORLD),
           spawn_step("builder", "shop", "m2", "{TMP}/shop", "work T2", world=_SP_LIMIT_WORLD),
           spawn_step("builder", "blog", "m1", "{TMP}/blog", "work T1", world=_SP_LIMIT_WORLD),
           spawn_step("builder", "blog", "m1", "{TMP}/blog", "work T1", world=lworld("L4-ship", projects=[_SHOP, _BLOG], builders_max=3))],
    checks=[
        ("a project can't be named like the crew",
         lambda r: r.code(0) == 1 and "can't start with" in r.out(0) and "crew-tools-build-m1" not in stations_json(r)),
        ("builders_max is honoured",
         lambda r: r.code(3) == 1 and "already 2 active builders (crew setting builders_max is 2); retire one first" in r.out(3)),
        ("a higher builders_max lets the next builder in",
         lambda r: r.code(4) == 0 and stations_json(r)["blog-build-m1"]["role"] == "builder"),
        ("each builder of the two projects started", lambda r: r.launches() == 3),
        ("L4: the builder merges on the gate",
         lambda r: any("pre-authorized at level L4-ship" in x for x in allow_of(r, "shop-build-m1"))),
    ],
    covers=["a project can't be named like the crew", "builders_max is honoured", "L4: the builder merges on the gate"],
))

_STRICT = {"id": "uc_strict", "name": "strict", "autonomy": "L3-review", "repos": [{"path": "{TMP}/core"}]}
_LOOSE = {"id": "uc_loose", "name": "loose", "repos": [{"path": "{TMP}/core"}]}
_SP_LEVEL_WORLD = lworld("L4-ship", projects=[_STRICT, _LOOSE])

scenario(Scenario(
    name="spawn_autonomy",
    suite="crew",
    about="a project handed over stricter keeps it; --autonomy may only be stricter than the crew",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_MULTILINE,
    steps=[spawn_step("builder", "strict", "m1", "{TMP}/core", "T1", world=_SP_LEVEL_WORLD, setup={"gitrepos": ["{TMP}/core"]}),
           spawn_step("builder", "--autonomy", "L5-dark", "strict", "m2", "{TMP}/core", "T2", world=_SP_LEVEL_WORLD),
           spawn_step("builder", "loose", "m1", "{TMP}/core", "T1", world=_SP_LEVEL_WORLD),
           spawn_step("builder", "--autonomy", "L3-review", "loose", "m2", "{TMP}/core", "T2", world=_SP_LEVEL_WORLD),
           spawn_step("builder", "--autonomy", "L4-ship", "loose", "m3", "{TMP}/core", "T3", world=lworld("L3-review", projects=[_LOOSE]))],
    checks=[
        ("a project handed over stricter keeps it", lambda r: relayed(allow_of(r, "strict-build-m1"))),
        ("L5 is no level any more", lambda r: r.code(1) == 1 and "autonomy must be L3-review or L4-ship" in r.out(1)),
        ("L4: the builder merges on the gate",
         lambda r: any("pre-authorized at level L4-ship" in x for x in allow_of(r, "loose-build-m1"))),
        ("--autonomy makes a builder stricter than the crew", lambda r: r.code(3) == 0 and relayed(allow_of(r, "loose-build-m2"))),
        ("--autonomy never makes it looser than the crew",
         lambda r: r.code(4) == 1 and "a project can only be stricter than the crew (crew autonomy is L3-review)" in r.out(4)
         and "loose-build-m3" not in stations_json(r)),
    ],
    covers=["a project handed over stricter keeps it", "L5 is no level any more", "L4: the builder merges on the gate"],
))

_UP_STEPS = [lstep("launch", f"crew-{role}", role, "", "", "", "{TMP}/crew", model, "high", f"Boot as the {role} station", world=_UP_WORLD)
             for role, model in (("reviewer", "sonnet"), ("releaser", "sonnet"))]
scenario(Scenario(
    name="spawn_helper_crew",
    suite="crew",
    about="a helper of a crew station: by its name, by its role word, of the releaser; one already running is not started twice",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_MULTILINE,
    steps=[*_UP_STEPS,
           spawn_step("helper", "crew-reviewer", "probe", "look at PR 9", world=_UP_WORLD),
           spawn_step("helper", "reviewer", "probe", "look at PR 10", world=_UP_WORLD),
           spawn_step("helper", "reviewer", "second", "look at PR 11", world=_UP_WORLD),
           spawn_step("releaser-helper", "deploy", "ship it", world=_UP_WORLD),
           spawn_step("helper", "releaser", "x", world=_UP_WORLD)],
    checks=[
        ("a crew station's helper is recorded", lambda r: stations_json(r)["crew-reviewer-probe"]["role"] == "helper"),
        ("its parent and its manual",
         lambda r: station_json(r, "crew-reviewer-probe")["env"]["FACTORY_PARENT"] == "crew-reviewer"
         and "load the skills factory-protocol and station-reviewer" in bg_call_of(r, "crew-reviewer-probe")["args"][-1]),
        ("it runs where its parent runs", lambda r: same_dir(bg_call_of(r, "crew-reviewer-probe")["cwd"], r.crew)),
        ("the role word names this crew's station", lambda r: r.code(3) == 1 and "crew-reviewer-probe is already running; message it instead" in r.out(3)),
        ("a second helper of the same parent", lambda r: r.code(4) == 0 and "crew-reviewer-second" in stations_json(r)),
        ("the releaser's helper", lambda r: r.code(5) == 0 and stations_json(r)["crew-releaser-deploy"]["role"] == "helper"
         and station_json(r, "crew-releaser-deploy")["env"]["FACTORY_PARENT"] == "crew-releaser"),
        ("a helper call without a task says how to call it", lambda r: r.code(6) == 1 and "usage: factory spawn helper" in r.out(6)),
        ("only the new helpers started", lambda r: r.launches() == 5),
    ],
    covers=[],
))

scenario(Scenario(
    name="spawn_refused",
    suite="crew",
    about="no MCP server: nothing starts; a halted crew: nothing starts and the HALT file is said",
    driver=LAUNCH_DRIVER,
    steps=[spawn_step("builder", "Links App", "m1", "{TMP}/links", "x", world={**_SP_WORLD, "mcp": False},
                      setup={**_SP_REPOS, "halt": "testing"}),
           spawn_step("builder", "Links App", "m1", "{TMP}/links", "x", world=_SP_WORLD)],
    checks=[
        ("no MCP server: spawn stops too",
         lambda r: r.code(0) == 1 and "no MCP server answers at" in r.out(0) and "tares mcp --transport streamable-http" in r.out(0)
         and r.launches() == 0),
        ("spawn refused while halted",
         lambda r: r.code(1) == 1 and 'the crew is halted ({"at":"2026-01-01T00:00:00Z","reason":"testing"}); tares factory crew resume first' in r.out(1)),
    ],
    covers=["no MCP server: spawn stops too", "spawn refused while halted"],
))


# ---------------------------------------------------------------------------------------------
# The views (TR-957): roster, status, attach and logs read the crew's stations and the sessions the fake
# Claude Code lists. The seeds are written straight into the throwaway crew, so the scenarios run on Windows.
# crewname_test.sh (the roster names its crew) and crew_test.sh are the bash suites they come from.

def _row(state: str, sid: str, name: str, **extra) -> dict:
    live = state != "stopped"
    row = {"id": sid, "name": name, "cwd": "/x", "state": {"working": "working", "idle": "working", "waiting": "blocked",
                                                             "stopped": "stopped"}[state]}
    if state in ("working", "idle"):
        row["status"] = "busy" if state == "working" else "idle"
    if state == "waiting":
        row["status"] = "waiting"
    if live:
        row["pid"] = 40000 + int(sid[-4:], 16)
    row.update(extra)
    return row


def _setup_views(tmp: Path, env: dict) -> None:
    crew = tmp / "crew"
    crew.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", crew / "crew.json")
    base = {"status": "active", "repo": "", "cwd": "", "model": "opus", "effort": "high"}
    stations = {
        "crew-orchestrator": {**base, "role": "orchestrator", "project": "", "id": "aaaa0001"},
        "crew-reviewer": {**base, "role": "reviewer", "project": "", "id": "bbbb0002"},
        "shop-build-m1": {**base, "role": "builder", "project": "shop", "id": "cccc0003"},
        "shop-build-m2": {**base, "role": "builder", "project": "shop", "id": "dddd0004"},
        "old-helper": {**base, "role": "helper", "project": "", "id": "eeee0005", "status": "stopped"},
    }
    (crew / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    agents = [_row("working", "aaaa0001", "crew-orchestrator", version="2.1.310"),
              _row("idle", "bbbb0002", "crew-reviewer", version="2.1.310"),
              _row("waiting", "cccc0003", "shop-build-m1", version="2.1.100")]  # shop-build-m2: no session
    state = tmp / "claude"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")
    (state / "version").write_text("2.1.310\n", encoding="utf-8")


def _attach_calls(r: Run) -> list:
    return [c["args"] for c in r.calls() if c["args"][:1] in (["attach"], ["logs"])]


scenario(Scenario(
    name="views_roster",
    suite="crewname",
    about="roster names the crew, lists the active stations with their turn and Claude Code, and flags an older one",
    setup=_setup_views,
    steps=[Step(["roster"], env=NO_TARES), Step(["roster"], env={**NO_TARES, "FACTORY_CREW_NAME": "ops"})],
    checks=[
        ("default crew: roster names it", lambda r: r.out(0).splitlines()[0] == "crew: crew (orchestrator: crew-orchestrator)"),
        ("roster lists a working orchestrator", lambda r: "crew-orchestrator\torchestrator\t\tworking\t2.1.310" in r.out(0)),
        ("a station with no session is gone", lambda r: "shop-build-m2\tbuilder\tshop\tgone\t" in r.out(0)),
        ("a stopped station is not listed", lambda r: "old-helper" not in r.out(0)),
        ("an older Claude Code is flagged", lambda r: "blocked\t2.1.100 (older than 2.1.310)" in r.out(0)),
        ("FACTORY_CREW_NAME=ops: roster names ops-orchestrator",
         lambda r: r.out(1).splitlines()[0] == "crew: ops (orchestrator: ops-orchestrator)"),
    ],
    covers=["default crew: roster names it"],
))

scenario(Scenario(
    name="views_status",
    suite="crew",
    about="status without a Tares says so plainly; the desk too",
    setup=_setup_views,
    steps=[Step(["status"], env=NO_TARES), Step(["desk"], env=NO_TARES), Step(["crew", "status"], env=NO_TARES)],
    checks=[
        ("status without Tares stops plainly", lambda r: r.code(0) == 1 and "no Tares answers at http://127.0.0.1:9" in r.out(0)),
        ("desk without Tares stops plainly", lambda r: r.code(1) == 1 and "no Tares answers at http://127.0.0.1:9" in r.out(1)),
        ("crew status is status", lambda r: r.code(2) == 1 and r.out(2) == r.out(0)),
    ],
    covers=[],
))

scenario(Scenario(
    name="views_attach_logs",
    suite="crew",
    about="attach and logs open a station's session through Claude Code; an unknown station or none is refused",
    setup=_setup_views,
    steps=[Step(["attach", "reviewer"], env=NO_TARES), Step(["logs", "shop-build-m1"], env=NO_TARES),
           Step(["attach", "nobody"], env=NO_TARES), Step(["crew", "logs", "nobody"], env=NO_TARES)],
    checks=[
        ("attach reviewer opens its session", lambda r: ["attach", "bbbb0002"] in _attach_calls(r)),
        ("logs shop-build-m1 follows its session", lambda r: ["logs", "cccc0003"] in _attach_calls(r)),
        ("an unknown station is refused", lambda r: r.code(2) == 1 and "unknown station nobody" in r.out(2)),
        ("crew logs refuses it the same way", lambda r: r.code(3) == 1 and "unknown station nobody" in r.out(3)),
    ],
    covers=[],
))

# doctor: crew_test.sh, tares_test.sh (the lines that read "doctor"), freeze_test.sh, idle_wake_test.sh and
# claude_update_test.sh each ask doctor a question. Here doctor runs against a fake Tares (fake_tares.py), a
# fake `gh` and a fake `tares` on PATH. The bash doctor does not make the checks that are new in Python
# (NEW_LABELS in tares/crew/doctor.py): those lines are set aside from the comparison, on both sides, with `drop`.

sys.path.insert(0, str(HERE))
import fake_tares  # noqa: E402

NEW_CHECKS = (r"^  (ok|warn|FIX)\s+(engine|tares on PATH|tares version|login key|crew key file|git|Git for Windows)\b",)
DOCTOR_DROP = NEW_CHECKS
# When Tares refuses the key in the environment, the bash doctor stops silently at the crew settings (its
# `crew_settings` dies inside a pipe). The Python doctor says the settings are unanswered and goes on to the
# checks after them. That is the one place the two differ on purpose; a scenario sets those lines aside.
BASH_STOPS_EARLY = (r"^  warn  crew settings not answered yet", r"^  ok    gh signed in", r"^fix the items marked FIX$")


def _fake_tool(tmp: Path, name: str) -> None:
    make_launcher(tmp / "bin" / name, [name], script=HERE / "fake_tares.py")


def _doctor_world(world: "fake_tares.World", repos: list = (), trusted: list = ()):
    """A setup hook: start the fake Tares, put a fake gh and tares on PATH, hand the crew a key file, and
    have Claude Code trust the crew home (and the folders named in `trusted`)."""
    def setup(tmp: Path, env: dict) -> None:
        _fake_tool(tmp, "gh")
        _fake_tool(tmp, "tares")
        for r in repos:
            (tmp / r).mkdir(parents=True, exist_ok=True)
        folders = [str(tmp / "crew")] + [str(tmp / t) for t in trusted]
        (tmp / "home").mkdir(parents=True, exist_ok=True)
        (tmp / "home" / ".claude.json").write_text(
            json.dumps({"projects": {f: {"hasTrustDialogAccepted": True} for f in folders}}), encoding="utf-8")
        _srv, base = fake_tares.serve(world)
        env["TARESD_URL"] = base
        env["TARES_FACTORY_MCP_URL"] = base + "/mcp"
        crew = tmp / "crew"
        crew.mkdir(parents=True, exist_ok=True)
        if world.token:
            f = crew / ".tares-token"
            f.write_text(world.token, encoding="utf-8")
            f.chmod(0o600)
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"  # never the person's keychain
    return setup


scenario(Scenario(
    name="doctor_notares",
    suite="crew",
    about="doctor without a Tares says how to start one",
    setup=lambda tmp, env: (_fake_tool(tmp, "gh"), _fake_tool(tmp, "tares"),
                            env.update(PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")),
    steps=[Step(["doctor"], env={"TARESD_URL": "http://127.0.0.1:9"}, drop=DOCTOR_DROP)],
    checks=[("doctor without Tares says how to start one",
             lambda r: "FIX   no Tares answers at http://127.0.0.1:9 (from the environment): start one with `tares up`, "
                       "connect to your Tares Cloud workspace with `tares login`" in r.out(0) and r.code(0) == 1)],
    covers=["doctor without Tares says how to start one"],
))

_ok_world = fake_tares.World()
scenario(Scenario(
    name="doctor_tares",
    suite="crew",
    about="doctor with a Tares: it answers, lets the crew in, and the MCP endpoint answers",
    setup=_doctor_world(_ok_world),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("doctor: Tares answers",
             lambda r: re.search(r"  ok    Tares answers at http://127\.0\.0\.1:\d+", r.out(0)) is not None
             and "ok    Tares lets this crew in" in r.out(0)),
            ("doctor: the MCP endpoint answers",
             lambda r: re.search(r"^  ok    MCP endpoint answers at http://127\.0\.0\.1:\d+/mcp$", r.out(0), re.M) is not None),
            ("doctor: nothing about tares-mcp on PATH", lambda r: "tares-mcp" not in r.out(0)),
            ("doctor: no Rius", lambda r: "rius" not in r.out(0).lower())],
    covers=["doctor: Tares answers", "doctor: the MCP endpoint answers",
            "doctor: nothing about tares-mcp on PATH", "doctor: no Rius"],
))


def _write(tmp: Path, rel: str, text: str) -> None:
    f = tmp / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8", newline="\n")


def _crew_json(tmp: Path) -> None:
    _write(tmp, "crew/crew.json", json.dumps({"stations": {r: {"model": "sonnet"} for r in
                                                           ("orchestrator", "reviewer", "releaser", "builder")}}))


def _kit_version() -> str:
    return json.loads((ROOT / "claude-plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


def _installed(tmp: Path, version: str) -> None:
    _write(tmp, "home/.claude/plugins/installed_plugins.json", json.dumps(
        {"version": 2, "plugins": {"tares@tares": [{"scope": "project", "projectPath": "/w/main", "version": version}]}}))


def _remove_installed(tmp: Path) -> None:
    (tmp / "home" / ".claude" / "plugins" / "installed_plugins.json").unlink()


scenario(Scenario(
    name="doctor_plugin_clash",
    suite="crew",
    about="doctor names an installed tares plugin of another version, and is quiet about the same version",
    setup=_doctor_world(fake_tares.World()),
    steps=[Step(["doctor"], before=lambda tmp: _installed(tmp, "0.2.1"), drop=DOCTOR_DROP),
           Step(["doctor"], before=lambda tmp: _installed(tmp, _kit_version()), drop=DOCTOR_DROP)],
    checks=[("doctor says the same",
             lambda r: "warn  another version of the tares plugin is installed from a marketplace "
                       "(tares@tares 0.2.1 project (/w/main))" in r.out(0)),
            ("the same version installed is fine", lambda r: "another version of the tares plugin" not in r.out(1))],
    covers=["doctor says the same", "the same version installed is fine"],
))


def _multi(tmp: Path, env: dict) -> None:
    core, charts = tmp / "core", tmp / "ro-charts"
    projects = [{"name": "argus", "repos": [{"path": str(core)}, {"path": str(charts)}]}]
    _doctor_world(fake_tares.World(projects=projects), repos=["core", "ro-charts"], trusted=["core"])(tmp, env)
    _crew_json(tmp)


scenario(Scenario(
    name="doctor_projects",
    suite="crew",
    about="doctor checks each repo of a project: Claude Code's trust and GitHub write access",
    setup=_multi,
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("multi doctor: trust per repo",
             lambda r: re.search(r"ok .*project argus: .+[/\\]core trusted", r.out(0)) is not None
             and re.search(r"FIX .*project argus: Claude Code has not trusted .+[/\\]ro-charts", r.out(0)) is not None),
            ("multi doctor: GitHub write access per repo",
             lambda r: re.search(r"ok .*[/\\]core is o/core on GitHub \(WRITE\)", r.out(0)) is not None
             and "FIX   project argus: gh has READ on o/ro-charts" in r.out(0))],
    covers=["multi doctor: trust per repo", "multi doctor: GitHub write access per repo"],
))


def _no_mcp(tmp: Path, env: dict) -> None:
    w = fake_tares.World()
    w.mcp = False
    _doctor_world(w)(tmp, env)


scenario(Scenario(
    name="doctor_nomcp",
    suite="crew",
    about="doctor without an MCP server says how to start one; taresd itself is not an MCP server",
    setup=_no_mcp,
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("doctor without an MCP server says how to start one",
             lambda r: re.search(r"FIX   no MCP server answers at http://127\.0\.0\.1:\d+/mcp; start one with "
                                 r"tares mcp --transport streamable-http", r.out(0)) is not None)],
    covers=["doctor without an MCP server says how to start one"],
))


# lsp_test.sh, its doctor part (TR-682, ported by TR-970): the languages of each handed-over repo, whether
# Claude Code has a language server for each, and the commands doctor prints for a missing one. The crew up
# cases of that suite (the offer and the install on a yes) wait for `crew up` to move over; tests/test_crew_doctor_lsp.py
# holds the offer's cases until then.

def _lsp_repos(tmp: Path) -> dict:
    """Python at the root with a console one folder down, Go, Rust one folder down, one whose only manifests are
    in places that do not count, a JavaScript-only repo, and a second Go repo."""
    r = {n: tmp / n for n in ("api", "gw", "docs", "engine", "site", "gw2")}
    for rel in ("api/pyproject.toml", "api/ui/package.json", "api/ui/tsconfig.json", "gw/go.mod",
                "docs/node_modules/package.json", "docs/.factory/Cargo.toml", "docs/.venv/requirements.txt",
                "docs/a/b/c/go.mod", "docs/README.md", "engine/native/Cargo.toml", "site/package.json", "gw2/go.mod"):
        _write(tmp, rel, "")
    return r


def _lsp_bins(tmp: Path, *names: str) -> None:
    """Exactly these server binaries are in the one folder the doctor looks in (FACTORY_LSP_PATH)."""
    folder = tmp / "lspbin"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        old.unlink()
    for n in names:
        f = folder / (n + (".exe" if os.name == "nt" else ""))
        f.write_text("", encoding="utf-8")
        f.chmod(0o755)


def _lsp_plugins(tmp: Path, rows: list) -> None:
    _write(tmp, "claude/plugins.json", json.dumps(rows))


def _plugin(name: str, enabled: bool = True, scope: str = "user", path: str = "") -> dict:
    row = {"id": f"{name}@claude-plugins-official", "enabled": enabled, "scope": scope}
    if path:
        row["projectPath"] = path
    return row


def _lsp_world(tmp: Path, env: dict) -> None:
    repos = _lsp_repos(tmp)
    projects = [{"name": "Mix", "repos": [{"path": str(repos[n])} for n in ("api", "gw", "docs", "engine", "gw2", "site")]}]
    _doctor_world(fake_tares.World(projects=projects), trusted=list(repos))(tmp, env)
    _crew_json(tmp)
    env["FACTORY_LSP_PATH"] = str(tmp / "lspbin")
    _lsp_bins(tmp)
    _lsp_plugins(tmp, [])


def _in(r: "Run", i: int, text: str) -> bool:
    return text in r.out(i)


def _lsp_missing_checks() -> list:
    def path(r, n):
        return str(r.tmp / n)

    def out(r):
        return r.out(0)
    return [
        ("doctor names Python and TypeScript for the repo and says neither has a server",
         lambda r: f"Python in {path(r, 'api')} has no language server" in out(r)
         and f"TypeScript in {path(r, 'api')} has no language server" in out(r)),
        ("a package.json alone is JavaScript, and a repo with a tsconfig.json is not also listed as JavaScript",
         lambda r: f"JavaScript in {path(r, 'site')} has no language server" in out(r)
         and f"TypeScript in {path(r, 'site')}" not in out(r) and f"JavaScript in {path(r, 'api')}" not in out(r)),
        ("doctor names Go and Rust for the other repos",
         lambda r: f"Go in {path(r, 'gw')} has no language server" in out(r)
         and f"Rust in {path(r, 'engine')} has no language server" in out(r)),
        ("doctor prints the install command and the plugin that wires it in",
         lambda r: "`npm install -g pyright` then `claude plugin install pyright-lsp@claude-plugins-official`" in out(r)
         and "npm install -g typescript-language-server typescript" in out(r)
         and "go install golang.org/x/tools/gopls@latest" in out(r)
         and "rustup component add rust-analyzer" in out(r)),
        ("doctor leaves out manifests under node_modules, .factory and three folders down",
         lambda r: f"in {path(r, 'docs')} " not in out(r)),
        ("a missing language server is advice, not a FIX",
         lambda r: re.search(r"FIX.*language server", out(r)) is None),
    ]


scenario(Scenario(
    name="lsp_missing",
    suite="lsp",
    about="doctor names each language of each repo and the commands for a missing language server",
    setup=_lsp_world,
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=_lsp_missing_checks(),
    covers=["doctor names Python and TypeScript for the repo and says neither has a server",
            "a package.json alone is JavaScript, and a repo with a tsconfig.json is not also listed as JavaScript",
            "doctor names Go and Rust for the other repos",
            "doctor prints the install command and the plugin that wires it in",
            "doctor leaves out manifests under node_modules, .factory and three folders down",
            "a missing language server is advice, not a FIX"],
))


def _ts_ready(tmp: Path) -> None:
    _lsp_bins(tmp, "pyright-langserver", "typescript-language-server")
    _lsp_plugins(tmp, [_plugin("typescript-lsp"), _plugin("pyright-lsp", enabled=False)])


def _elsewhere(tmp: Path) -> None:
    _lsp_plugins(tmp, [_plugin("pyright-lsp", scope="local", path="/somewhere/else")])


def _repo_only(tmp: Path) -> None:
    _lsp_plugins(tmp, [_plugin("pyright-lsp", scope="local", path=str(tmp / "api"))])


def _ts_no_binary(tmp: Path) -> None:
    _lsp_bins(tmp, "pyright-langserver")
    _lsp_plugins(tmp, [_plugin("typescript-lsp")])


scenario(Scenario(
    name="lsp_servers",
    suite="lsp",
    about="ready needs an enabled user-scope plugin and its binary; claude failing to list plugins is unknown",
    setup=_lsp_world,
    steps=[Step(["doctor"], before=_ts_ready, drop=DOCTOR_DROP),
           Step(["doctor"], before=_elsewhere, drop=DOCTOR_DROP),
           Step(["doctor"], before=_repo_only, drop=DOCTOR_DROP),
           Step(["doctor"], before=_ts_no_binary, drop=DOCTOR_DROP),
           Step(["doctor"], env={"FAKE_CLAUDE_PLUGIN_FAIL": "1"}, drop=DOCTOR_DROP)],
    checks=[
        ("an enabled plugin with its binary is ready",
         lambda r: re.search(r"ok .*TypeScript in " + re.escape(str(r.tmp / "api")) + r" has a language server for "
                             r"Claude Code \(typescript-lsp plugin, typescript-language-server on PATH\)", r.out(0)) is not None),
        ("a disabled plugin is not a server, and doctor says the binary is the part that is there",
         lambda r: re.search(r"Python in " + re.escape(str(r.tmp / "api")) + r" has no language server.*"
                             r"pyright-langserver is installed, the pyright-lsp plugin is not on", r.out(0)) is not None
         and "claude plugin install pyright-lsp@claude-plugins-official" in r.out(0)
         and "npm install -g pyright" not in r.out(0)),
        ("a plugin enabled for another project is not a server here",
         lambda r: f"Python in {r.tmp / 'api'} has no language server" in r.out(1)),
        ("a plugin enabled for the repo path only is not a server for the crew, whose stations run elsewhere",
         lambda r: f"Python in {r.tmp / 'api'} has no language server" in r.out(2)
         and "claude plugin install pyright-lsp@claude-plugins-official" in r.out(2)),
        ("an enabled plugin without its binary says the binary is missing, and prints only its install",
         lambda r: re.search(r"TypeScript in " + re.escape(str(r.tmp / "api")) + r" has no language server.*"
                             r"the typescript-lsp plugin is on, typescript-language-server is not on PATH", r.out(3)) is not None
         and "claude plugin install typescript-lsp" not in r.out(3)),
        ("when claude cannot list its plugins doctor says it cannot tell, and recommends no install",
         lambda r: f"TypeScript in {r.tmp / 'api'}: cannot tell whether Claude Code has a language server, "
                   "because claude plugin list --json failed" in r.out(4)
         and "has no language server" not in r.out(4) and "claude plugin install" not in r.out(4)),
    ],
    covers=["an enabled plugin with its binary is ready",
            "a disabled plugin is not a server, and doctor says the binary is the part that is there",
            "a plugin enabled for another project is not a server here",
            "a plugin enabled for the repo path only is not a server for the crew, whose stations run elsewhere",
            "an enabled plugin without its binary says the binary is missing, and prints only its install",
            "when claude cannot list its plugins doctor says it cannot tell, and recommends no install"],
))


# crew_test.sh: cmux says "Waiting for you" about a minute after any station's turn ends (its agentIdleReminder,
# on by default); doctor reads the person's cmux file, says what to set, and never edits it.

def _cmux_path(tmp: Path) -> Path:
    return tmp / "home" / ".config" / "cmux" / "cmux.json"


def _cmux_file(extra: str):
    def write(tmp: Path) -> None:
        text = ('{\n  "$schema": "https://raw.githubusercontent.com/manaflow-ai/cmux/main/web/data/cmux.schema.json",\n'
                '  "schemaVersion": 1,\n' + extra + '\n  // This file uses JSON with comments (JSONC).\n  //   "app" : {\n'
                '  //     "language" : "system"\n  //   },\n}\n')
        _write(tmp, "home/.config/cmux/cmux.json", text)
    return write


def _cmux_text(text: str):
    return lambda tmp: _write(tmp, "home/.config/cmux/cmux.json", text)


def _cmux_slashed(r: "Run", i: int) -> str:
    return r.out(i).replace("\\", "/")


scenario(Scenario(
    name="doctor_cmux",
    suite="crew",
    about="doctor reads cmux's idle reminder setting from the person's own file and never edits it",
    setup=_doctor_world(fake_tares.World()),
    steps=[Step(["doctor"], drop=DOCTOR_DROP),
           Step(["doctor"], before=_cmux_file(""), drop=DOCTOR_DROP),
           Step(["doctor"], before=_cmux_file('  "notifications": {"agentIdleReminder": false},'), drop=DOCTOR_DROP),
           Step(["doctor"], before=_cmux_file('  "notifications": {"agentIdleReminder": true},'), drop=DOCTOR_DROP),
           Step(["doctor"], before=_cmux_text('{"notifications": {"agentIdleReminder": false}}\n'), drop=DOCTOR_DROP),
           Step(["doctor"], before=_cmux_text("{not json"), drop=DOCTOR_DROP)],
    checks=[
        ("no cmux, nothing said about it", lambda r: "cmux" not in r.out(0).replace(str(r.tmp), "").lower()),
        ("cmux with its default file (trailing comma): doctor says what to set",
         lambda r: re.search(r"warn  cmux shows \"Waiting for you\" about a minute after every station.s turn ends",
                             r.out(1)) is not None
         and f"agentIdleReminder\": false}} in {_cmux_path(r.tmp).as_posix()}" in _cmux_slashed(r, 1)
         and "could not parse" not in r.out(1)),
        ("cmux idle reminder off in cmux's file shape: doctor is content",
         lambda r: re.search(r"ok    cmux.s idle reminder is off", r.out(2)) is not None
         and "Waiting for you" not in r.out(2)),
        ("cmux idle reminder on: doctor warns", lambda r: re.search(r"warn  cmux shows", r.out(3)) is not None),
        ("cmux file without a trailing comma still reads",
         lambda r: re.search(r"ok    cmux.s idle reminder is off", r.out(4)) is not None),
        ("an unparseable cmux file is said, not guessed",
         lambda r: f"warn  could not parse {_cmux_path(r.tmp).as_posix()}" in _cmux_slashed(r, 5)),
        ("doctor never edits the cmux file", lambda r: _cmux_path(r.tmp).read_text(encoding="utf-8") == "{not json"),
    ],
    covers=["no cmux, nothing said about it",
            "cmux with its default file (trailing comma): doctor says what to set",
            "cmux idle reminder off in cmux's file shape: doctor is content",
            "cmux idle reminder on: doctor warns",
            "cmux file without a trailing comma still reads",
            "an unparseable cmux file is said, not guessed",
            "doctor never edits the cmux file"],
))


def _keys(tmp: Path, env: dict) -> None:
    _doctor_world(fake_tares.World(token="stub-token"))(tmp, env)
    _crew_json(tmp)
    env["TARES_AUTH_TOKEN"] = "nvf_dead0000_revoked"


def _drop_key_file(tmp: Path) -> None:
    f = tmp / "crew" / ".tares-token"
    f.unlink()
    f.write_text("", encoding="utf-8")


scenario(Scenario(
    name="doctor_keys",
    suite="tares",
    about="doctor: a key Tares refuses never replaces the crew's; a crew without a key is flagged; no key is printed",
    setup=_keys,
    steps=[Step(["doctor"], after=lambda tmp: (tmp / "crew" / ".tares-token").read_text(encoding="utf-8"),
                drop=DOCTOR_DROP + BASH_STOPS_EARLY),
           Step(["doctor"], before=_drop_key_file, env={"TARES_AUTH_TOKEN": ""}, drop=DOCTOR_DROP + BASH_STOPS_EARLY)],
    checks=[("doctor names the stations' key and finds it good",
             lambda r: "ok    stations use a token with no key id from " in r.out(0)
             and ", and Tares accepts it" in r.out(0)),
            ("a key Tares refuses never replaces the crew's",
             lambda r: "refuses key dead0000 from the environment; the crew keeps the key it has" in r.out(0)
             and r.snap(0) == "stub-token"),
            ("no key value in doctor's output",
             lambda r: "nvf_dead0000_revoked" not in r.out(0) and "stub-token" not in r.out(0)),
            ("doctor flags a crew without a key",
             lambda r: "FIX   the stations have no key in " in r.out(1) and ": run tares login" in r.out(1))],
    covers=["doctor names the stations' key and finds it good", "a key Tares refuses never replaces the crew's",
            "no key value in doctor's output", "doctor flags a crew without a key"],
))


def _frozen(tmp: Path, env: dict) -> None:
    w = fake_tares.World(settings={"kit": {"frozen": {"at": "2026-01-02T03:04:05Z", "by": "ada", "version": "0.0.1"}}})
    _doctor_world(w)(tmp, env)
    _crew_json(tmp)


scenario(Scenario(
    name="doctor_kit",
    suite="freeze",
    about="doctor: the kit follows the checkout, or is frozen (copy missing, or older than the checkout)",
    setup=lambda tmp, env: (_doctor_world(fake_tares.World())(tmp, env), _crew_json(tmp)),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("doctor says the kit follows the checkout",
             lambda r: "ok    the kit follows the plugin checkout (not frozen)" in r.out(0))],
    covers=["doctor says the kit follows the checkout"],
))

scenario(Scenario(
    name="doctor_frozen",
    suite="freeze",
    about="doctor: a frozen kit whose copy is gone, then one whose copy is older than the checkout",
    setup=_frozen,
    steps=[Step(["doctor"], drop=DOCTOR_DROP),
           Step(["doctor"], before=lambda tmp: (tmp / "crew" / "frozen-kit").mkdir(parents=True), drop=DOCTOR_DROP)],
    checks=[("the freeze with no copy is a FIX", lambda r: "FIX   the kit is frozen since 2026-01-02T03:04:05Z by ada, but its copy" in r.out(0)),
            ("doctor warns that a newer kit is not followed",
             lambda r: "warn  the kit is frozen since 2026-01-02T03:04:05Z by ada, and the checkout has a newer kit "
                       "that no station moves onto" in r.out(1))],
    covers=["doctor warns that a newer kit is not followed"],
))


# freeze_test.sh, the part that needs no daemon of its own and no running station: the freeze and the
# unfreeze against a fake Tares that keeps a settings PUT. (The watch's side of the freeze, a revive on the
# copy and the move after an unfreeze, runs in the bash suite itself and in tests/test_crew_kit.py.)
_WORLDS: dict = {}
_KIT = ROOT / "claude-plugin"


def _freeze_world(keep: bool = True):
    def setup(tmp: Path, env: dict) -> None:
        w = fake_tares.World()
        w.keep = keep
        _WORLDS[str(tmp)] = w
        _doctor_world(w)(tmp, env)
        _crew_json(tmp)
    return setup


def _frozen_of(tmp: Path):
    return _WORLDS[str(tmp)].settings.get("kit", {}).get("frozen")


def _kit_events(r: Run, name: str) -> int:
    try:
        lines = (r.crew / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for x in lines if x.strip() and json.loads(x).get("event") == name)


def _same_tree(a: Path, b: Path) -> bool:
    """Both folders hold the same files with the same bytes (`diff -r`; compiled caches left out)."""
    def files(root: Path) -> dict:
        return {f.relative_to(root).as_posix(): f.read_bytes() for f in sorted(root.rglob("*"))
                if f.is_file() and "__pycache__" not in f.parts}
    return a.is_dir() and b.is_dir() and files(a) == files(b)


def _freeze_state(tmp: Path) -> dict:
    """What a freeze left: the setting on Tares and the copy on disk."""
    frozen = _frozen_of(tmp) or {}
    snap = tmp / "crew" / "frozen-kit"
    return {"frozen": bool(frozen), "keys": sorted(frozen), "at": frozen.get("at"), "by": frozen.get("by"),
            "copy": snap.exists(), "same": _same_tree(snap, _KIT), "binary": (snap / "bin" / "factory").exists(),
            "extra": (tmp / "crew" / "frozen-kit.new").exists() or (tmp / "crew" / "freezing").exists()}


scenario(Scenario(
    name="freeze_cycle",
    suite="freeze",
    about="crew freeze keeps the freeze on Tares and a copy of the kit; freezing twice changes nothing; unfreeze clears both",
    setup=_freeze_world(),
    steps=[Step(["crew", "unfreeze"], after=_freeze_state),
           Step(["crew", "freeze"], after=_freeze_state),
           Step(["crew", "freeze"], after=_freeze_state),
           Step(["crew", "unfreeze"], after=_freeze_state)],
    checks=[("an unfrozen crew's setting is null", lambda r: r.snap(0)["frozen"] is False),
            ("unfreeze when not frozen says so and exits 0",
             lambda r: r.code(0) == 0 and "the kit is not frozen" in r.out(0) and not r.snap(0)["copy"]),
            ("freeze exits 0", lambda r: r.code(1) == 0),
            ("freeze keeps when, who and the kit version on Tares",
             lambda r: r.snap(1)["keys"] == ["at", "by", "version"] and r.snap(1)["by"] not in (None, "")),
            ("the copy is the kit as it was", lambda r: r.snap(1)["same"] and r.snap(1)["binary"]),
            ("freeze is in the events", lambda r: _kit_events(r, "freeze") == 1),
            ("freezing twice says so, exits 0 and keeps the first time and who",
             lambda r: r.code(2) == 0 and "already frozen" in r.out(2) and r.snap(2)["at"] == r.snap(1)["at"]
             and r.snap(2)["by"] == r.snap(1)["by"] and _kit_events(r, "freeze") == 1),
            ("freezing again takes no second copy", lambda r: r.snap(2)["same"] and not r.snap(2)["extra"]),
            ("unfreeze exits 0 and clears the setting",
             lambda r: r.code(3) == 0 and r.snap(3)["frozen"] is False and _kit_events(r, "unfreeze") == 1),
            ("unfreeze removes a copy nothing runs from", lambda r: not r.snap(3)["copy"])],
    covers=["an unfrozen crew's setting is null", "unfreeze when not frozen says so and exits 0", "freeze exits 0",
            "freeze keeps when, who and the kit version on Tares", "the copy is the kit as it was",
            "freeze is in the events", "freezing twice says so, exits 0 and keeps the first time and who",
            "freezing again takes no second copy", "unfreeze exits 0 and clears the setting",
            "unfreeze removes a copy nothing runs from"],
))

scenario(Scenario(
    name="freeze_nokeep",
    suite="freeze",
    about="a Tares that answers the freeze without keeping it: freeze dies and leaves nothing behind",
    setup=_freeze_world(keep=False),
    steps=[Step(["crew", "freeze"], after=_freeze_state)],
    checks=[("freeze dies when Tares does not keep the setting",
             lambda r: r.code(0) != 0 and "did not keep the freeze" in r.out(0)),
            ("and leaves no copy, marker or setting behind",
             lambda r: not r.snap(0)["copy"] and not r.snap(0)["extra"] and r.snap(0)["frozen"] is False
             and not (r.crew / "kit-checkout").exists())],
    covers=["freeze dies when Tares does not keep the setting", "and leaves no copy, marker or setting behind"],
))

scenario(Scenario(
    name="freeze_notares",
    suite="freeze",
    about="freeze needs a Tares that answers",
    setup=lambda tmp, env: (_doctor_world(fake_tares.World())(tmp, env), _crew_json(tmp)),
    steps=[Step(["crew", "freeze"], env={"TARESD_URL": "http://127.0.0.1:9"},
                after=lambda tmp: (tmp / "crew" / "frozen-kit").exists())],
    checks=[("freeze needs Tares",
             lambda r: r.code(0) != 0 and re.search(r"no Tares answers|could not read the crew settings", r.out(0)) is not None
             and r.snap(0) is False)],
    covers=["freeze needs Tares"],
))


def _settings(**kw):
    def setup(tmp: Path, env: dict) -> None:
        _doctor_world(fake_tares.World(settings=kw))(tmp, env)
        _crew_json(tmp)
    return setup


scenario(Scenario(
    name="doctor_idle_wake",
    suite="idle_wake",
    about="doctor: idle_wake off says nothing, on without a watch warns",
    setup=_settings(),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("off: status and doctor have no wake line", lambda r: "idle_wake is on" not in r.out(0))],
    covers=["off: status and doctor have no wake line"],
))

scenario(Scenario(
    name="doctor_idle_wake_on",
    suite="idle_wake",
    about="doctor: idle_wake on without a running watch warns",
    setup=_settings(idle_wake=True),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("on: doctor warns that no watch is running",
             lambda r: "warn  idle_wake is on but the watch is not running" in r.out(0)
             and "ok    idle_wake is on" not in r.out(0))],
    covers=[],
))

scenario(Scenario(
    name="doctor_claude_update",
    suite="claude_update",
    about="doctor tells when the watch does not move stations onto a new Claude Code",
    setup=_settings(claude_update=False),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("doctor tells the watch is off", lambda r: "claude_update" in r.out(0).lower())],
    covers=["doctor tells the watch is off"],
))


# ---------------------------------------------------------------------------------------------
# A fake Tares. `projects`, `finish`, `context` and `plan` talk to Tares, and parity has none: a scenario
# with `tares=` gets a small local server that answers the few routes these commands use, with the
# answers the scenario names, and writes down what it was asked (the route and whether a key came with it,
# never the key). TARESD_URL names it for both engines.

class FakeTares:
    """routes: {"GET /api/crew/projects": (status, json document or text)}; unnamed routes answer 404."""

    def __init__(self, routes: dict):
        import http.server
        import threading
        self.routes = {"GET /health": (200, {"ok": True}), "GET /api/crew/settings": (200, {"settings": {}})}
        self.routes.update(routes)
        self.requests: list = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                key = f"{self.command} {self.path}"
                fake.requests.append({"route": key, "auth": bool(self.headers.get("Authorization")), "body": body})
                status, doc = fake.routes.get(key, (404, {"detail": "not found"}))
                data = (doc if isinstance(doc, str) else json.dumps(doc)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _answer

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def asked(self, route: str) -> list:
        return [r for r in self.requests if r["route"] == route]


def routes_in(routes: dict, tmp: Path) -> dict:
    """The routes with "{tmp}" in an answer replaced by the scenario's folder (a path inside a document)."""
    folder = json.dumps(str(tmp))[1:-1]  # as it reads inside a JSON string
    return {k: (st, doc.replace("{tmp}", str(tmp)) if isinstance(doc, str)
                else json.loads(json.dumps(doc).replace("{tmp}", folder)))
            for k, (st, doc) in routes.items()}


def _crew_files(stations: dict):
    """A setup that gives the crew a crew.json and the stations.json named."""
    def setup(tmp: Path, env: dict) -> None:
        crew = tmp / "crew"
        crew.mkdir(parents=True, exist_ok=True)
        (crew / "crew.json").write_text('{"name": "crew"}\n', encoding="utf-8")
        (crew / "stations.json").write_text(json.dumps(stations), encoding="utf-8")
        (tmp / "work").mkdir(exist_ok=True)
    return setup


# ---------------------------------------------------------------------------------------------
# usage_test.sh: what the crew's machine reports. `factory context` reads a station's /context through
# a copy of its session that is never saved, `factory plan` reads /usage; both send the reading to Tares.
# The station and its session come from stations.json; Claude Code is the Python fake.

def _usage_stations(tmp: Path) -> dict:
    return {"crew-orchestrator": {"role": "orchestrator", "id": "deadbeef", "sessionId": "sess-orch-1",
                                  "cwd": str(tmp / "work")}}


def _setup_usage(tmp: Path, env: dict) -> None:
    _crew_files(_usage_stations(tmp))(tmp, env)


def _only_call(r: Run, needle: str) -> dict:
    calls = [c for c in r.calls() if needle in c["args"]]
    return calls[-1] if calls else {}


scenario(Scenario(
    name="usage_context",
    suite="usage",
    about="`context` reads a station through an unsaved copy of its session and sends the reading to Tares",
    tares={"POST /ingest/claude_code": (200, {"ok": True})},
    setup=_setup_usage,
    steps=[Step(["context", "orchestrator"]), Step(["context", "nobody"])],
    checks=[
        ("factory context reads the station",
         lambda r: r.code(0) == 0 and "crew-orchestrator: 95300 of 200000 tokens (48%), sent to Tares" in r.out(0)),
        ("through a copy that is never saved",
         lambda r: (lambda a: a[a.index("--resume") + 1] == "sess-orch-1" and "--fork-session" in a
                    and "--no-session-persistence" in a and "-p" in a)(_only_call(r, "/context").get("args", ["--resume", ""]))),
        ("in the station's folder, with its plugin",
         lambda r: _only_call(r, "/context").get("cwd") == str(r.tmp / "work")
         and "--plugin-dir" in _only_call(r, "/context").get("args", [])),
        ("with its settings, hooks and the status line off",
         lambda r: json.loads((r.state / "p-settings.json").read_text(encoding="utf-8")).get("disableAllHooks") is True),
        ("Tares has the reading",
         lambda r: len(r.tares.asked("POST /ingest/claude_code")) == 1
         and json.loads(r.tares.asked("POST /ingest/claude_code")[0]["body"]).get("type") == "session_context"),
        ("an unknown station: said plainly", lambda r: r.code(1) != 0 and "unknown station nobody" in r.out(1)),
    ],
    covers=["factory context reads the station", "through a copy that is never saved",
            "in the station's folder, with its plugin", "with its settings, hooks and the status line off",
            "Tares has the reading", "an unknown station: said plainly"],
))

scenario(Scenario(
    name="usage_plan",
    suite="usage",
    about="`plan` reads /usage with hooks off and nothing saved, sends the limits, and says plainly when there is no plan",
    tares={"POST /ingest/claude_code": (200, {"ok": True})},
    setup=_setup_usage,
    steps=[Step(["plan"]), Step(["plan"], env={"FAKE_CLAUDE_NO_PLAN": "1"})],
    checks=[
        ("factory plan reads and sends the limits", lambda r: r.code(0) == 0 and "session: 27% used" in r.out(0)),
        ("with hooks off, nothing saved",
         lambda r: "--no-session-persistence" in _only_call(r, "/usage").get("args", [])
         and json.loads((r.state / "p-settings.json").read_text(encoding="utf-8")).get("disableAllHooks") is True),
        ("Tares has /usage's limits",
         lambda r: json.loads(r.tares.asked("POST /ingest/claude_code")[0]["body"]).get("type") == "claude_plan"),
        ("no plan to read (an API key): said plainly", lambda r: r.code(1) != 0 and "Pro or Max" in r.out(1)),
    ],
    covers=["factory plan reads and sends the limits", "with hooks off, nothing saved",
            "Tares has /usage's limits", "no plan to read (an API key): said plainly"],
))


# ---------------------------------------------------------------------------------------------
# crew_test.sh and tares_test.sh: `projects` lists what was handed to the crew, `finish` is the person's.

PROJECTS = [
    {"id": "p1", "name": "Links App", "tickets_done": 1, "tickets_total": 2,
     "milestone": {"name": "Links", "done": 1, "total": 2},
     "repos": [{"name": "acme/links", "path": "/work/links"}]},
    {"id": "p2", "name": "shop", "tickets_done": 0, "tickets_total": 0, "milestone": None,
     "repos": [{"path": "/work/shop"}, {"name": "acme/shop-api", "path": "/work/shop-api"}]},
    {"id": "p3", "name": "A very long project name that is cut", "tickets_done": 3, "tickets_total": 3,
     "milestone": None, "repos": []},
]
BUILDERS = {
    "links-app-build-m1": {"role": "builder", "project": "Links App", "status": "active"},
    "links-app-build-m2": {"role": "builder", "project": "Links App", "status": "retired"},
    "shop-build-m1": {"role": "builder", "project": "shop", "status": "active"},
    "shop-build-m2": {"role": "builder", "project": "shop", "status": "active"},
    "crew-reviewer": {"role": "reviewer", "status": "active"},
}


def _setup_projects(tmp: Path, env: dict) -> None:
    _crew_files(BUILDERS)(tmp, env)


scenario(Scenario(
    name="projects_list",
    suite="crew",
    about="`projects` lists each handed-over project with its progress, builders and repos",
    tares={"GET /api/crew/projects": (200, {"projects": PROJECTS})},
    setup=_setup_projects,
    steps=[Step(["projects"])],
    checks=[
        ("projects lists each handed-over project with its builders and repos",
         lambda r: r.code(0) == 0
         and re.search(r"^Links App +1/2 +Links 1/2 +1 +acme/links$", r.out(0), re.M) is not None
         and re.search(r"^shop +0/0 +- +2 +/work/shop, acme/shop-api$", r.out(0), re.M) is not None
         and re.search(r"^A very long project name tha 3/3 +all done +0 *$", r.out(0), re.M) is not None),
    ],
    covers=["projects lists each handed-over project with its builders and repos"],
))

scenario(Scenario(
    name="projects_none",
    suite="tares",
    about="`projects` with none handed over says how to start one",
    tares={"GET /api/crew/projects": (200, {"projects": []})},
    setup=_setup_projects,
    steps=[Step(["projects"])],
    checks=[("no project: said plainly", lambda r: r.code(0) == 0 and "no projects handed to the crew yet" in r.out(0))],
    covers=[],
))

FINISH_OPEN = {"detail": "Links App still has open tickets: T1"}


def _finish_scenario(name: str, about: str, routes: dict, steps: list, checks: list, covers: list) -> None:
    scenario(Scenario(name=name, suite="crew", about=about, tares=routes, setup=_setup_projects,
                      steps=steps, checks=checks, covers=covers))


_finish_scenario(
    "projects_finish_open", "`finish` refuses a project with an open ticket and says which",
    {"GET /api/crew/projects": (200, {"projects": PROJECTS}), "POST /api/projects/p1/finish": (409, FINISH_OPEN)},
    [Step(["finish", "Links App", "all shipped"])],
    [("finish refuses a project with an open ticket",
      lambda r: r.code(0) != 0 and "still has open tickets: T1" in r.out(0)
      and json.loads(r.tares.asked("POST /api/projects/p1/finish")[0]["body"]) == {"words": "all shipped"})],
    ["finish refuses a project with an open ticket"])

_finish_scenario(
    "projects_finish_station", "a station cannot run `finish`",
    {"GET /api/crew/projects": (200, {"projects": PROJECTS})},
    [Step(["finish", "Links App"], env={"FACTORY_STATION": "crew-orchestrator"})],
    [("a station can't run finish",
      lambda r: r.code(0) != 0 and "only the person runs this" in r.out(0)
      and not r.tares.asked("POST /api/projects/p1/finish"))],
    ["a station can't run finish"])

_finish_scenario(
    "projects_finish_done", "the person finishes a project; the last one tells how to stop the crew",
    {"GET /api/crew/projects": (200, {"projects": PROJECTS}),
     "POST /api/projects/p1/finish": (200, {"open_projects": 0}),
     "POST /api/projects/p2/finish": (200, {"open_projects": 1})},
    [Step(["finish", "Links App", "all", "shipped"]), Step(["finish", "shop"]), Step(["finish", "nothing"])],
    [("the person finishes it",
      lambda r: r.code(0) == 0 and "✓ Links App is done." in r.out(0) and "crew down" in r.out(0)
      and r.code(1) == 0 and "crew down" not in r.out(1)),
     ("an unknown project lists the open ones",
      lambda r: r.code(2) != 0 and "nothing is not a project the crew holds (open projects: Links App, shop" in r.out(2)),
     ],
    ["the person finishes it"])

_finish_scenario(
    "projects_finish_down", "`finish` says plainly when Tares cannot be reached for the request",
    {"GET /api/crew/projects": (200, {"projects": PROJECTS}), "POST /api/projects/p1/finish": (500, "boom")},
    [Step(["finish", "Links App"])],
    [("a refusal without a detail shows the body", lambda r: r.code(0) != 0 and "Tares did not finish Links App: boom" in r.out(0))],
    [])


# ---------------------------------------------------------------------------------------------
# spec_test.sh and session_test.sh: the person's own Claude Code session. `spec` and `session` open Claude in
# the foreground in the folder they choose, so the scenarios read what the fake Claude Code was called with
# (its arguments, folder and environment) and what the commands asked of a fake Tares. The suites' checks
# that need a real Tares (fixtures, the session-start hook, the skill text) stay in the bash suites.

PLUGIN = str((ROOT / "claude-plugin").resolve())
MCP_ROUTE = {"POST /mcp": (200, {"result": {"serverInfo": {"name": "tares"}}})}
WITH_MCP = {"TARES_FACTORY_MCP_URL": "{tares}/mcp"}
SEEN_ENV = ("TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_SESSION_NAME", "TARES_SESSION_TAKEOVER",
            "TARES_SESSION_KIND", "TARESD_URL", "TARES_FACTORY_URL", "TARES_FACTORY_MCP_URL",
            "TARES_FACTORY_CRED_FILE", "TARES_FACTORY_TOKEN_FILE", "FACTORY_ROLE", "PWD")


def _mkrepo(tmp: Path, rel: str) -> Path:
    p = tmp / rel
    p.mkdir(parents=True, exist_ok=True)
    _git(tmp, "init", "-q", cwd=p)
    (p / "README.md").write_text("x\n")
    _git(tmp, "add", ".", cwd=p)
    _git(tmp, "commit", "-q", "-m", "init", cwd=p)
    return p


def _claude_after(tmp: Path) -> dict:
    """What the fake Claude Code saw on its last call: how many calls so far, the settings file it was
    given, and the environment variables the commands set (a token is only noted as present)."""
    state = tmp / "claude"
    calls = []
    try:
        calls = [json.loads(x) for x in (state / "calls.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        pass
    seen: dict = {}
    keys: set = set()
    try:
        for line in (state / "last.env").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            keys.add(k)
            if k in SEEN_ENV:
                seen[k] = v
    except OSError:
        pass
    settings = None
    if calls:
        a = calls[-1]["args"]
        if "--settings" in a:
            try:
                settings = json.loads(Path(a[a.index("--settings") + 1]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                settings = "<unreadable>"
    return {"n": len(calls), "env": seen, "has_auth_token": "TARES_AUTH_TOKEN" in keys, "settings": settings}


def _same(a, b) -> bool:
    """Two paths that name the same place: the engines write them with / or \\ (the crew's own messages use /,
    git prints C:/...), and Windows ignores case."""
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def _the_call(r: Run, i: int = -1) -> dict:
    calls = r.calls()
    return calls[i] if calls else {"cwd": "", "args": [""] * 3}


def _flag_value(call: dict, flag: str):
    a = call["args"]
    return a[a.index(flag) + 1] if flag in a else None


def _plain_session(call: dict) -> bool:
    return "--bg" not in call["args"]


def _plugin_on(r: Run, i: int) -> bool:
    return (_flag_value(_the_call(r, i), "--plugin-dir") == PLUGIN
            and (r.snap(i) or {}).get("settings") == {"enabledPlugins": {"tares@inline": True}})


def _env_of(r: Run, i: int) -> dict:
    return (r.snap(i) or {}).get("env", {})


def _repo_clean(r: Run, rel: str) -> bool:
    p = subprocess.run(["git", "status", "--porcelain"], cwd=str(r.tmp / rel), capture_output=True, text=True)
    return p.stdout.strip() == "" and not (r.tmp / rel / ".factory").exists()


def _setup_spec(tmp: Path, env: dict) -> None:
    (_mkrepo(tmp, "shop") / "src").mkdir()
    (tmp / "plain").mkdir()
    for name in ("api", "web"):
        _mkrepo(tmp, f"work/{name}")
    (tmp / "work" / "notes").mkdir()
    (tmp / "work" / "zz-not-a-repo").mkdir()
    (tmp / "empty").mkdir()
    (tmp / "fresh").mkdir()
    _git(tmp, "init", "-q", cwd=tmp / "fresh")


def _spec_scenario(name: str, about: str, routes: dict, steps: list, checks: list, covers: list) -> None:
    scenario(Scenario(name=name, suite="spec", about=about, tares={**MCP_ROUTE, **routes}, setup=_setup_spec,
                      steps=steps, checks=checks, covers=covers))


def _sstep(*argv, cwd: str = "{tmp}/shop/src", env: dict | None = None, stdin: str = "") -> Step:
    return Step(list(argv), env={**WITH_MCP, **(env or {})}, cwd=cwd, stdin=stdin, after=_claude_after)


NO_PROJECTS = {"GET /api/projects": (200, {"projects": []}), "GET /api/whoami": (200, {"person": None})}

_spec_scenario(
    "spec_repo", "`spec` opens the person's own Claude in the repo root, named after the idea, with this plugin",
    NO_PROJECTS,
    [_sstep("spec", "an order form for the bakery shop"),
     _sstep("spec", "--project", "Bakery orders", cwd="{tmp}/shop"),
     _sstep("spec", "with a token", cwd="{tmp}/shop", env={"TARES_AUTH_TOKEN": "spec-tok"})],
    [("the spec runs in the repo root", lambda r: _same(_the_call(r, 0)["cwd"], r.tmp / "shop")),
     ("the session is named after the idea", lambda r: _the_call(r, 0)["args"][1] == "an-order-form-for-spec"),
     ("it loads this plugin", lambda r: _flag_value(_the_call(r, 0), "--plugin-dir") == PLUGIN),
     ("it runs /tares:spec with the idea",
      lambda r: _the_call(r, 0)["args"][-1] == "/tares:spec an order form for the bakery shop"),
     ("the spec session turns the plugin on too", lambda r: _plugin_on(r, 0)),
     ("it is the person's own session, not a background one", lambda r: _plain_session(_the_call(r, 0))),
     ("it says the plan goes to Tares", lambda r: f"into a project on Tares ({r.tares.url})" in r.out(0)),
     ("the spec session is told the shared MCP endpoint",
      lambda r: _env_of(r, 0).get("TARES_FACTORY_MCP_URL") == r.tares.url + "/mcp"),
     ("without a token there is no token file to name", lambda r: "TARES_FACTORY_CRED_FILE" not in _env_of(r, 0)),
     ("nothing is written into the repo", lambda r: _repo_clean(r, "shop")),
     ("a project name names the session and goes to /tares:spec",
      lambda r: _the_call(r, 1)["args"][1] == "bakery-orders-spec"
      and _the_call(r, 1)["args"][-1] == "/tares:spec (project name: Bakery orders)"),
     ("with a token the spec session gets the token file under both names",
      lambda r: _same(_env_of(r, 2).get("TARES_FACTORY_CRED_FILE"), r.crew / ".tares-token")
      and _same(_env_of(r, 2).get("TARES_FACTORY_TOKEN_FILE"), r.crew / ".tares-token"))],
    ["the spec runs in the repo root", "the session is named after the idea", "it loads this plugin",
     "it runs /tares:spec with the idea", "the spec session turns the plugin on too",
     "it is the person's own session, not a background one", "it says the plan goes to Tares",
     "the spec session is told the shared MCP endpoint", "without a token there is no token file to name",
     "nothing is written into the repo", "a project name names the session and goes to /tares:spec",
     "with a token the spec session gets the token file under both names"])

_spec_scenario(
    "spec_down", "`spec` says how to start Tares, or an MCP server, and opens nothing",
    NO_PROJECTS,
    [_sstep("spec", "an order form", env={"TARESD_URL": "http://127.0.0.1:9"}),
     _sstep("spec", "an order form", env={"TARES_FACTORY_MCP_URL": "http://127.0.0.1:9/mcp"})],
    [("without Tares the spec says how to start one", lambda r: "no Tares answers" in r.out(0) and not r.calls()),
     ("without an MCP server the spec says how to start one",
      lambda r: "no MCP server answers at http://127.0.0.1:9/mcp" in r.out(1)
      and "tares mcp --transport streamable-http" in r.out(1) and not r.calls())],
    ["without Tares the spec says how to start one", "without an MCP server the spec says how to start one"])

_spec_scenario(
    "spec_folder", "`spec` runs in a folder that holds the repos, and refuses a folder with none or a repo with no commit",
    NO_PROJECTS,
    [_sstep("spec", "orders across api and web", cwd="{tmp}/work"),
     _sstep("spec", "nothing here", cwd="{tmp}/empty"),
     _sstep("spec", "x", cwd="{tmp}/fresh")],
    [("a folder of repos: the spec runs in the folder", lambda r: _same(_the_call(r, 0)["cwd"], r.tmp / "work")),
     ("and names the repos it holds", lambda r: "holds 2 repo(s): api, web" in r.out(0)),
     ("a folder with no repo is refused plainly",
      lambda r: r.code(1) != 0 and "inside the git repo the project builds on, or in a folder that holds its repos" in r.out(1)),
     ("a repo with no commit is refused plainly", lambda r: r.code(2) != 0 and "has no commit yet" in r.out(2))],
    ["a folder of repos: the spec runs in the folder", "and names the repos it holds",
     "a folder with no repo is refused plainly", "a repo with no commit is refused plainly"])

ASHISH = {"id": "u_ash", "name": "Ashish"}
SPEC_PROJECTS = {"projects": [
    {"id": "p_acme", "name": "Acme web", "kind": "software_factory", "lead": ASHISH},
    {"id": "p_nobody", "name": "Nobody leads", "kind": "software_factory", "lead": None},
    {"id": "p_notes", "name": "Plain notes", "lead": ASHISH},
    {"id": "p_secret", "name": "Secret plan", "hidden": True, "lead": ASHISH},
    {"id": "p_lead", "name": "Lead case", "kind": "software_factory", "lead": ASHISH}]}
WANT = "You are contributing to Acme web led by Ashish. Load the spec-project skill, section Contributor."


def _lead_flow(r: Run, i: int, name: str) -> bool:
    return (_the_call(r, min(i, len(r.calls()) - 1))["args"][-1] == f"/tares:spec (project name: {name})"
            and "TARES_PROJECT" not in _env_of(r, i) and "TARES_SESSION_KIND" not in _env_of(r, i)
            and "as a contributor" not in r.out(i))


_spec_scenario(
    "spec_contributor", "`spec` for a project someone else leads opens a contributor session; nothing else does",
    {"GET /api/projects": (200, SPEC_PROJECTS), "GET /api/whoami": (200, {"person": {"id": "u_kir", "name": "Kiran"}})},
    [_sstep("spec", "--project", "Acme web", cwd="{tmp}/plain"),
     _sstep("spec", "acme web"),
     _sstep("spec", "--project", "No such project"),
     _sstep("spec", "--project", "Nobody leads"),
     _sstep("spec", "--project", "Plain notes"),
     _sstep("spec", "--project", "Secret plan")],
    [("a contributor gets the contributor prompt, naming the project and its lead",
      lambda r: r.code(0) == 0 and _the_call(r, 0)["args"][-1] == WANT),
     ("the contributor session is bound to the project", lambda r: _env_of(r, 0).get("TARES_PROJECT") == "Acme web"),
     ("a contributor needs no git repo: it opens in the current folder, named for the project",
      lambda r: _same(_the_call(r, 0)["cwd"], r.tmp / "plain") and _the_call(r, 0)["args"][1] == "acme-web-spec"),
     ("the contributor session carries the contributor marker, so its hook does not say to build",
      lambda r: _env_of(r, 0).get("TARES_SESSION_KIND") == "contributor" and "FACTORY_ROLE" not in _env_of(r, 0)),
     ("a contributor is told it is a contributor session",
      lambda r: "as a contributor to Acme web, which Ashish leads" in r.out(0)),
     ("a contributor session does not run /tares:spec", lambda r: "/tares:spec" not in _the_call(r, 0)["args"][-1]),
     ("it loads this plugin and turns it on", lambda r: _plugin_on(r, 0)),
     ("the idea text finds the project too, and a contributor inside a repo opens at its root",
      lambda r: _the_call(r, 1)["args"][-1] == WANT and _same(_the_call(r, 1)["cwd"], r.tmp / "shop")),
     ("a contributor session writes nothing into the repo", lambda r: _repo_clean(r, "shop")),
     ("an unknown project name stays today's flow", lambda r: _lead_flow(r, 2, "No such project")),
     ("a project with no lead stays today's flow", lambda r: _lead_flow(r, 3, "Nobody leads")),
     ("a project that is not a software factory one, led by someone else, keeps the lead's flow",
      lambda r: _lead_flow(r, 4, "Plain notes")),
     ("a private project the caller is not on keeps the lead's flow", lambda r: _lead_flow(r, 5, "Secret plan"))],
    ["a contributor gets the contributor prompt, naming the project and its lead",
     "the contributor session is bound to the project",
     "a contributor needs no git repo: it opens in the current folder, named for the project",
     "the contributor session carries the contributor marker, so its hook does not say to build",
     "a contributor is told it is a contributor session", "a contributor session does not run /tares:spec",
     "it loads this plugin and turns it on",
     "the idea text finds the project too, and a contributor inside a repo opens at its root",
     "a contributor session writes nothing into the repo", "an unknown project name stays today's flow",
     "a project with no lead stays today's flow",
     "a project that is not a software factory one, led by someone else, keeps the lead's flow",
     "a private project the caller is not on keeps the lead's flow"])

_spec_scenario(
    "spec_lead", "the lead of the project, and a caller with no person, get the usual flow",
    {"GET /api/projects": (200, SPEC_PROJECTS), "GET /api/whoami": (200, {"person": ASHISH})},
    [_sstep("spec", "--project", "Acme web"),
     _sstep("spec", "--project", "Acme web", cwd="{tmp}/plain"),
     _sstep("spec", "--project", "Lead case")],
    [("the lead's session has no contributor marker", lambda r: "TARES_SESSION_KIND" not in _env_of(r, 0)),
     ("the lead keeps today's prompt",
      lambda r: _lead_flow(r, 0, "Acme web") and _same(_the_call(r, 0)["cwd"], r.tmp / "shop")),
     ("the lead outside a repo is still refused as today",
      lambda r: r.code(1) != 0 and "inside the git repo the project builds on" in r.out(1)),
     ("the lead of an existing project gets the usual prompt",
      lambda r: _lead_flow(r, 2, "Lead case") and _same(_the_call(r, -1)["cwd"], r.tmp / "shop"))],
    ["the lead's session has no contributor marker", "the lead keeps today's prompt",
     "the lead outside a repo is still refused as today", "the lead of an existing project gets the usual prompt"])

_spec_scenario(
    "spec_no_person", "a caller with no person (the root token) stays today's flow",
    {"GET /api/projects": (200, SPEC_PROJECTS), "GET /api/whoami": (200, {"person": None})},
    [_sstep("spec", "--project", "Acme web")],
    [("a caller with no person (the root token) stays today's flow", lambda r: _lead_flow(r, 0, "Acme web"))],
    ["a caller with no person (the root token) stays today's flow"])

for _route in ("/api/whoami", "/api/projects"):
    _routes = {"GET /api/projects": (200, SPEC_PROJECTS), "GET /api/whoami": (200, {"person": {"id": "u_kir"}})}
    _routes[f"GET {_route}"] = (500, "boom")
    _spec_scenario(
        "spec_failed_" + _route.rsplit("/", 1)[1], f"a failed {_route} lookup stays today's flow",
        _routes,
        [_sstep("spec", "--project", "Acme web"), _sstep("spec", "--project", "Acme web", cwd="{tmp}/plain")],
        [(f"a failed {_route} lookup stays today's flow", lambda r: _lead_flow(r, 0, "Acme web"))]
        + ([("and a failed lookup outside a repo is refused as today, not opened as a contributor",
             lambda r: r.code(1) != 0 and "inside the git repo the project builds on" in r.out(1))]
           if _route == "/api/whoami" else []),
        ["a failed $route lookup stays today's flow"]
        + (["and a failed lookup outside a repo is refused as today, not opened as a contributor"]
           if _route == "/api/whoami" else []))


SHOP = {"id": "p_shop", "name": "Shop", "kind": "software_factory", "status": "active"}
NOTES = {"id": "p_notes", "name": "Notes app", "kind": "software_factory", "status": "active"}
NOT_FACTORY = {"id": "p_nf", "name": "Not factory"}
LISTED = {"GET /api/projects": (200, {"projects": [SHOP, NOTES, NOT_FACTORY]})}
CREW_NONE = {"GET /api/crew/projects": (200, {"projects": [], "releasing": []})}
SHOP_REPO = {"GET /api/projects/p_shop/crew": (200, {"handover": {"repos": [{"path": "{tmp}/shop", "name": "acme/shop"}]}})}
HELD = {"GET /api/crew/projects": (200, {"projects": [{"id": "p_shop", "name": "Shop", "tickets_done": 1,
                                                       "tickets_total": 2, "milestone": None}], "releasing": []})}
RELEASING = {"GET /api/crew/projects": (200, {"projects": [], "releasing": [{"id": "p_shop", "name": "Shop"}]})}


def _setup_session(tmp: Path, env: dict) -> None:
    _mkrepo(tmp, "shop")
    (tmp / "elsewhere").mkdir()


def _session_scenario(name: str, about: str, routes: dict, steps: list, checks: list, covers: list) -> None:
    scenario(Scenario(name=name, suite="session", about=about, tares={**MCP_ROUTE, **routes}, setup=_setup_session,
                      steps=steps, checks=checks, covers=covers))


def _ss(*argv, cwd: str = "{tmp}/elsewhere", env: dict | None = None, stdin: str = "") -> Step:
    return _sstep(*argv, cwd=cwd, env=env, stdin=stdin)


def _last_arg(r: Run, i: int) -> str:
    return _the_call(r, i)["args"][-1]


_session_scenario(
    "session_start", "`session` opens the person's own Claude on a project, or on an idea, and names the choices",
    {**LISTED, **CREW_NONE},
    [_ss("session", "Notes app"),
     _ss("session", "notes-app"),
     _ss("session", "a tiny url shortener", cwd="{tmp}/shop"),
     _ss("session", "--new", "Shop", cwd="{tmp}/shop"),
     _ss("session", "Widgets", stdin="2\n"),
     _ss("session", "Widgets", stdin="1\n")],
    [("a planned project: the person's own session",
      lambda r: (r.snap(0) or {}).get("n") == 1 and _plain_session(_the_call(r, 0))),
     ("named after the project", lambda r: _the_call(r, 0)["args"][1] == "notes-app-session"),
     ("it loads this plugin and turns it on", lambda r: _plugin_on(r, 0)),
     ("it runs /tares:session with the project", lambda r: _last_arg(r, 0) == "/tares:session Notes app"),
     ("the session is bound to the project",
      lambda r: _env_of(r, 0).get("TARES_PROJECT") == "p_notes" and _env_of(r, 0).get("TARES_PROJECT_NAME") == "Notes app"
      and _env_of(r, 0).get("TARES_SESSION_NAME") == "notes-app-session"
      and _env_of(r, 0).get("TARES_SESSION_TAKEOVER", "") == ""),
     ("and told the Tares and its MCP endpoint",
      lambda r: _env_of(r, 0).get("TARESD_URL") == r.tares.url
      and _env_of(r, 0).get("TARES_FACTORY_MCP_URL") == r.tares.url + "/mcp"),
     ("with no repo of its own it opens where you are", lambda r: _same(_the_call(r, 0)["cwd"], r.tmp / "elsewhere")),
     ("the project's slug finds it too", lambda r: _last_arg(r, 1) == "/tares:session Notes app"),
     ("an idea: plan lightly and build here",
      lambda r: _last_arg(r, 2) == "/tares:session new: a tiny url shortener"
      and _the_call(r, 2)["args"][1] == "a-tiny-url-shortener-session"),
     ("not bound to a project yet", lambda r: _env_of(r, 2).get("TARES_PROJECT", "") == ""),
     ("it opens in the repo you are in", lambda r: _same(_the_call(r, 2)["cwd"], r.tmp / "shop")),
     ("it says the plan goes to Tares and the build happens here", lambda r: "builds it in the same" in r.out(2)),
     ("--new makes even a project name an idea", lambda r: _last_arg(r, 3) == "/tares:session new: Shop"),
     ("one unknown word: no, I meant a project, lists the open ones",
      lambda r: r.code(4) != 0 and "the open projects: " in r.out(4) and "Shop" in r.out(4)
      and "Not factory" not in r.out(4) and (r.snap(4) or {}).get("n") == (r.snap(3) or {}).get("n")),
     ("one unknown word: yes, a new project", lambda r: _last_arg(r, -1) == "/tares:session new: Widgets"),
     ("nothing is written into the repo", lambda r: _repo_clean(r, "shop"))],
    ["a planned project: the person's own session", "named after the project", "it loads this plugin and turns it on",
     "it runs /tares:session with the project", "the session is bound to the project",
     "and told the Tares and its MCP endpoint", "with no repo of its own it opens where you are",
     "the project's slug finds it too", "an idea: plan lightly and build here", "not bound to a project yet",
     "it opens in the repo you are in", "it says the plan goes to Tares and the build happens here",
     "--new makes even a project name an idea", "one unknown word: no, I meant a project, lists the open ones",
     "one unknown word: yes, a new project", "nothing is written into the repo"])

_session_scenario(
    "session_refuse", "`session` says how to start Tares, and a station may not open one",
    {**LISTED, **CREW_NONE},
    [_ss("session", "Shop", env={"TARESD_URL": "http://127.0.0.1:9"}),
     _ss("session", "Shop", env={"FACTORY_STATION": "crew-orchestrator"})],
    [("without Tares it says how to start one", lambda r: "no Tares answers" in r.out(0) and not r.calls()),
     ("a station may not open one",
      lambda r: r.code(1) != 0 and "only the person runs this" in r.out(1) and not r.calls())],
    ["without Tares it says how to start one", "a station may not open one"])

_session_scenario(
    "session_crew_keep", "a project the crew holds is left with it unless the person says yes",
    {**LISTED, **HELD, **SHOP_REPO, "POST /api/projects/p_shop/takeover": (200, {"ok": True})},
    [_ss("session", "Shop", stdin="\n")],
    [("the crew holds it: Enter leaves it with the crew",
      lambda r: "Left with the crew" in r.out(0) and "crew attach orchestrator" in r.out(0) and not r.calls()
      and not r.tares.asked("POST /api/projects/p_shop/takeover"))],
    ["the crew holds it: Enter leaves it with the crew"])

_session_scenario(
    "session_crew_take", "yes takes a project over from the crew, in the person's words, and opens its repo",
    {**LISTED, **HELD, **SHOP_REPO, "POST /api/projects/p_shop/takeover": (200, {"ok": True})},
    [_ss("session", "Shop", stdin="1\nsmall, I will finish it myself\n")],
    [("yes: taken over, in the person's words",
      lambda r: len(r.tares.asked("POST /api/projects/p_shop/takeover")) == 1
      and json.loads(r.tares.asked("POST /api/projects/p_shop/takeover")[0]["body"]) == {"words": "small, I will finish it myself"}),
     ("the session knows it took it over",
      lambda r: _last_arg(r, 0) == "/tares:session Shop (taken over from the crew)"
      and _env_of(r, 0).get("TARES_SESSION_TAKEOVER") == "1" and _env_of(r, 0).get("TARES_PROJECT") == "p_shop"),
     ("it opens in the project's repo", lambda r: _same(_the_call(r, 0)["cwd"], r.tmp / "shop"))],
    ["yes: taken over, in the person's words", "the session knows it took it over", "it opens in the project's repo"])

_session_scenario(
    "session_released", "a project being released carries on its takeover, and the menu marks it",
    {**LISTED, **RELEASING, **SHOP_REPO},
    [_ss("session", "Shop"),
     _ss("session", stdin="\n"),
     _ss("session", stdin="2\n"),
     _ss("session", stdin="3\na habit tracker\n")],
    [("while it is being released, a new session carries on the takeover without asking",
      lambda r: _last_arg(r, 0) == "/tares:session Shop (taken over from the crew)"
      and "was taken over from the crew" in r.out(0)),
     ("no argument lists the open factory projects",
      lambda r: "Shop (taken over, being released)" in r.out(1) and "Notes app" in r.out(1)
      and "something new" in r.out(1) and "Not factory" not in r.out(1)),
     ("picking a project opens it", lambda r: _last_arg(r, 2) == "/tares:session Notes app"),
     ("something new asks what to build", lambda r: _last_arg(r, 3) == "/tares:session new: a habit tracker")],
    ["while it is being released, a new session carries on the takeover without asking",
     "no argument lists the open factory projects", "picking a project opens it", "something new asks what to build"])


# The lifecycle commands (TR-955): retire, down, halt, resume work on the stations in stations.json and the
# sessions of the fake Claude Code, and tell the fake Tares; revive and restart start a session, so they go
# through the launch driver (the Python side does not read GET /api/crew/pace yet, which the driver does not compare).

def _setup_life(tmp: Path, env: dict) -> None:
    _setup_views(tmp, env)
    stations = tmp / "crew" / "stations"
    stations.mkdir(parents=True, exist_ok=True)
    (stations / "crew-reviewer.pace").write_text('{"stretched":true,"held":false}\n', encoding="utf-8")
    (tmp / "crew" / "watch.pid").write_text("999999\n", encoding="utf-8")   # no such process


def _ended(station: str, code: int, doc=None) -> dict:
    return {f"POST /api/crew/{station}/end?crew=crew": (code, doc if doc is not None else {"ok": True})}


def _stations_now(r: Run) -> dict:
    return json.loads((r.crew / "stations.json").read_text(encoding="utf-8"))


def _events(r: Run) -> str:
    p = r.crew / "events.jsonl"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _tares_end_bodies(r: Run, station: str) -> list:
    return [q["body"] for q in r.tares.asked(f"POST /api/crew/{station}/end?crew=crew")]


scenario(Scenario(
    name="lifecycle_retire",
    suite="crew",
    about="retire stops the session, marks the station retired, drops its pace file and tells Tares (200 and 404 silent, else a note)",
    setup=_setup_life,
    tares={**_ended("crew-reviewer", 200), **_ended("shop-build-m1", 404, {"detail": "no such station"}),
           **_ended("crew-orchestrator", 500, "boom")},
    steps=[Step(["retire", "reviewer"]), Step(["retire", "shop-build-m1"]), Step(["retire", "crew-orchestrator"]),
           Step(["retire", "nobody"])],
    checks=[
        ("retire writes status retired", lambda r: r.code(0) == 0 and _stations_now(r)["crew-reviewer"]["status"] == "retired"),
        ("retire says so, and Tares' 200 is silent", lambda r: r.out(0).strip() == "tares factory: retired crew-reviewer"),
        ("retire removes the pace file", lambda r: not (r.crew / "stations" / "crew-reviewer.pace").exists()),
        ("retire stops and removes the session",
         lambda r: ["stop", "bbbb0002"] in [c["args"] for c in r.calls()] and ["rm", "bbbb0002"] in [c["args"] for c in r.calls()]),
        ("retire writes the event", lambda r: '"retired"' in _events(r) or "retired" in _events(r)),
        ("retire tells Tares the station ended",
         lambda r: _tares_end_bodies(r, "crew-reviewer") == ['{"reason":"retired"}']),
        ("a 404 from Tares is silent too", lambda r: r.code(1) == 0 and "could not tell Tares" not in r.out(1)),
        ("any other answer prints could not tell Tares",
         lambda r: r.code(2) == 0 and "could not tell Tares at " in r.out(2) and "that crew-orchestrator ended" in r.out(2)),
        ("an unknown station dies", lambda r: r.code(3) == 1 and "is not a recorded station" in r.out(3)),
    ],
    covers=[],
))

_DESK = {"desk": [
    {"id": "d1", "question": "Is Links App done?", "project_finished": True},
    {"id": "d2", "question": "Shall I stop the crew?"},
    {"id": "d3", "question": "Run crew down now?"},
    {"id": "d4", "question": "Which branch should I use?"},
]}

scenario(Scenario(
    name="lifecycle_down",
    suite="crew",
    about="down stops the watch, retires every active station and withdraws the moot desk questions; a second down finds nothing to stop",
    setup=_setup_life,
    tares={"GET /api/desk?state=open": (200, _DESK),
           **_ended("crew-orchestrator", 200), **_ended("crew-reviewer", 200), **_ended("shop-build-m1", 200),
           **_ended("shop-build-m2", 200),
           "POST /api/desk/d1/withdraw": (200, {"ok": True}), "POST /api/desk/d2/withdraw": (200, {"ok": True}),
           "POST /api/desk/d3/withdraw": (200, {"ok": True}), "POST /api/desk/d4/withdraw": (200, {"ok": True})},
    steps=[Step(["crew", "down"]), Step(["down"])],
    checks=[
        ("down retires every active station",
         lambda r: r.code(0) == 0 and all(_stations_now(r)[s]["status"] == "retired" for s in
                                           ("crew-orchestrator", "crew-reviewer", "shop-build-m1", "shop-build-m2"))
         and _stations_now(r)["old-helper"]["status"] == "stopped"),
        ("down clears the watch", lambda r: not (r.crew / "watch.pid").exists()),
        ("down says what it did",
         lambda r: "retired crew-orchestrator" in r.out(0) and "all stations stopped. Tares keeps the projects, tickets and the desk;" in r.out(0)),
        ("down withdraws the finished project's and the stop questions, not the others",
         lambda r: sorted({q["route"] for q in r.tares.requests if q["route"].endswith("/withdraw")})
         == ["POST /api/desk/d1/withdraw", "POST /api/desk/d2/withdraw", "POST /api/desk/d3/withdraw"]),
        ("the finished project's question gives its reason",
         lambda r: json.loads(r.tares.asked("POST /api/desk/d1/withdraw")[0]["body"]) ==
         {"reason": "the crew stopped and the project is finished"}),
        ("the stop questions give theirs",
         lambda r: json.loads(r.tares.asked("POST /api/desk/d2/withdraw")[0]["body"]) == {"reason": "the person stopped the crew"}),
        ("a second down has nothing to retire", lambda r: r.code(1) == 0 and "retired" not in r.out(1)),
    ],
    covers=[],
))

def _setup_life_unrecorded(tmp: Path, env: dict) -> None:
    _setup_life(tmp, env)
    (tmp / "crew" / "stations.json").unlink()


scenario(Scenario(
    name="lifecycle_down_unrecorded",
    suite="crew",
    about="down with no stations.json says so",
    setup=_setup_life_unrecorded,
    steps=[Step(["down"], env=NO_TARES)],
    checks=[("down with no stations recorded says so", lambda r: r.code(0) == 0 and "no stations recorded" in r.out(0)
             and not (r.crew / "watch.pid").exists())],
    covers=[],
))

def _halt_after(tmp: Path):
    p = tmp / "crew" / "HALT"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


scenario(Scenario(
    name="lifecycle_halt",
    suite="crew",
    about="halt writes HALT with the default or the given reason and prints both lines; the second halt replaces the first",
    setup=_setup_life,
    steps=[Step(["halt"], env=NO_TARES, after=_halt_after), Step(["crew", "halt", "out of money", "ignored"], env=NO_TARES, after=_halt_after)],
    checks=[
        ("halt without a reason says why",
         lambda r: r.code(0) == 0 and r.snap(0)["reason"] == "halted by the human"),
        ("halt prints both lines",
         lambda r: "crew halted: stations finish their current step and start nothing risky (merges, pushes, deploys, tags, new sessions)." in r.out(0)
         and "crew resume lifts it." in r.out(0) and "crew attach orchestrator) so it sends HALT to every station" in r.out(0)),
        ("halt takes the first word as the reason", lambda r: r.code(1) == 0 and r.snap(1)["reason"] == "out of money"),
        ("halt is in the event log", lambda r: "halt" in _events(r)),
    ],
    covers=[],
))

scenario(Scenario(
    name="lifecycle_resume",
    suite="crew",
    about="resume lifts a halt; with no halt it still says so",
    setup=_setup_life,
    steps=[Step(["halt", "pause"], env=NO_TARES), Step(["resume"], env=NO_TARES, after=_halt_after),
           Step(["crew", "resume"], env=NO_TARES, after=_halt_after)],
    checks=[
        ("resume removes HALT", lambda r: r.code(1) == 0 and r.snap(1) is None and "crew resumed" in r.out(1)),
        ("resume with no halt is the same", lambda r: r.code(2) == 0 and "crew resumed" in r.out(2)),
        ("resume is in the event log", lambda r: "resume" in _events(r)),
    ],
    covers=[],
))


# revive and restart, on the launch driver. The first step starts crew-reviewer; `_stop_all` then leaves its session
# stopped, as a station that died is found.

_LIFE_WORLD = lworld("L3-review", grants=LAUNCH_GRANT)
_LIFE_LAUNCH = lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "sonnet", "high",
                     "Boot as the reviewer station", world=_LIFE_WORLD)


def _stop_all(tmp: Path) -> None:
    p = tmp / "claude" / "agents.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    for row in rows:
        row["state"] = "stopped"
        row.pop("pid", None)
        row.pop("status", None)
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _retire_row(tmp: Path) -> None:
    p = tmp / "crew" / "stations.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["crew-reviewer"]["status"] = "retired"
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def lifecycle_step(op: str, *args: str, before=None, env: dict | None = None) -> Step:
    s = lstep(op, *args, world=_LIFE_WORLD, env=env)
    s.before = before
    return s


def _resumes(r: Run) -> list:
    return [arg_after(c["args"], "--resume") for c in bg_calls(r)]


scenario(Scenario(
    name="lifecycle_revive",
    suite="crew",
    about="revive resumes a stopped station's session, --fresh starts a new one; a running or retired station and an unknown one are refused",
    driver=LAUNCH_DRIVER,
    steps=[_LIFE_LAUNCH,
           lifecycle_step("revive", "crew-reviewer", before=_stop_all),
           lifecycle_step("revive", "crew-reviewer", "--fresh", "start over", before=_stop_all),
           lifecycle_step("revive", "crew-reviewer"),
           lifecycle_step("revive", "crew-reviewer", before=_retire_row),
           lifecycle_step("revive", "nobody")],
    checks=[
        ("revive resumes the recorded session",
         lambda r: r.code(1) == 0 and "started crew-reviewer (sonnet, high)" in r.out(1) and _resumes(r)[1] is not None
         and _resumes(r)[1] == _stations_now(r)["crew-reviewer"]["earlier"][0]["sessionId"]),
        ("revive keeps the replaced session in the station's history",
         lambda r: len(_stations_now(r)["crew-reviewer"].get("earlier", [])) == 2),
        ("revive --fresh starts without --resume", lambda r: r.code(2) == 0 and _resumes(r)[2] is None),
        ("revive writes revived and restarted-fresh events", lambda r: "revived" in _events(r) and "restarted-fresh" in _events(r)),
        ("a running station is refused", lambda r: r.code(3) == 1 and "crew-reviewer is running; message it instead" in r.out(3)),
        ("a retired station is refused", lambda r: r.code(4) == 1 and "crew-reviewer was retired; factory spawn starts it again" in r.out(4)),
        ("an unknown station dies", lambda r: r.code(5) == 1 and "nobody is not a recorded station" in r.out(5)),
    ],
    covers=[],
))

scenario(Scenario(
    name="lifecycle_restart",
    suite="crew",
    about="restart stops the session and revives it; it refuses when it cannot see the station stop; an unknown station dies",
    driver=LAUNCH_DRIVER,
    steps=[_LIFE_LAUNCH,
           lifecycle_step("restart", "crew-reviewer", env={"FACTORY_STOP_WAIT": "3"}),
           lifecycle_step("restart", "crew-reviewer", env={"FAKE_CLAUDE_AGENTS_FAIL": "1"}),
           lifecycle_step("restart", "nobody")],
    checks=[
        ("restart stops the old session and starts the station again",
         lambda r: r.code(1) == 0 and any(c["args"][:1] == ["stop"] for c in r.calls()) and r.launches() == 2),
        ("restart refuses when it cannot see the station stop",
         lambda r: r.code(2) == 1 and "claude agents --json); not stopping crew-reviewer without being able to see it stop" in r.out(2)
         and r.launches() == 2),
        ("an unknown station dies", lambda r: r.code(3) == 1 and "nobody is not a recorded station" in r.out(3)),
    ],
    covers=[],
))


# cc_move_*: the watch moves stations onto a newer Claude Code (claude_update_test.sh, TR-669 and TR-687), one idle
# station a round, the orchestrator last. Each scenario runs `crew watch --once`, a round at a time, on a crew of
# three stations (a builder or a helper where the case wants one) whose sessions the fake Claude Code lists. The
# fake `claude --version` says the installed version (claude/version); a station's row says the version it runs
# (`version`), and a restart's new row carries the installed one (claude/stamp-version), so neither engine needs
# a process listing.
# The fake versions are all below 2, so a real Claude Code spare on the host is never "older" and never stopped.
# These scenarios read the machine's real process table (the spare-pruning ones start spare-like processes), so
# run them alone: two runs at once, or one beside test_crew_cc_update.py, stop each other's spares.

_CC_TARES = {"GET /api/crew/settings": (200, {"settings": {"autonomy": "L4-ship", "releaser": True, "release_profile": "kind",
                                                           "challenger": False, "builders_max": 3}}),
             "GET /api/crew/projects": (200, {"projects": []}), "GET /api/grants": (200, {"all": ""})}
# Left out of the comparison: every read of Tares (the other steps of a round read settings, health and pace in a
# different order and number in each engine; the pace itself is real in Python since TR-962), and how often the session list and `--version` are read. What is written to Tares, started and stopped
# is compared.
_CC_NOT_YET = ("GET ",)
_CC_READS = (("agents",), ("--version",))
_CC_IDS = {"crew-orchestrator": "aaaa0001", "crew-reviewer": "bbbb0002", "crew-releaser": "cccc0003"}


def _cc_kit(plugin: Path | None = None) -> str:
    """The fingerprint of the kit both engines run on (a station of this kit is not due a plugin upgrade)."""
    sys.path.insert(0, str(ROOT))
    from tares.crew import launch
    return launch.kit_version(plugin or ROOT / "claude-plugin")


def _cc_plugin(tmp: Path, env: dict) -> Path:
    """A copy of the plugin outside any git checkout, set as the run's TARES_PLUGIN_DIR: the kit step only
    moves a station onto a new kit when the plugin's checkout is clean, and the real checkout is not
    (edited files while one works, git's own line-ending settings on Windows). The copy is always clean."""
    copy = tmp / "kit" / "claude-plugin"
    shutil.copytree(ROOT / "claude-plugin", copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    env["TARES_PLUGIN_DIR"] = str(copy)
    return copy


def _cc_setup(have: str = "1.0.0", installed: str = "1.1.0", versions: dict | None = None, extra=(), done=(), files=None):
    """A setup: the three stations idle, each running `have` (versions overrides per station), `installed`
    the version of `claude --version`. `extra` are (station, row fields) of more stations, and their sessions;
    `done` names builders that are finished (stations/<s>.done); `files` are more files of the fake Claude Code."""
    def setup(tmp: Path, env: dict) -> None:
        _setup_views(tmp, env)
        crew, state = tmp / "crew", tmp / "claude"
        kit = _cc_kit(_cc_plugin(tmp, env))
        base = {"status": "active", "repo": "", "project": "", "cwd": str(crew), "model": "opus", "effort": "high", "kit": kit}
        rows, agents = {}, []
        for name, sid in _CC_IDS.items():
            role = name.split("-", 1)[1]
            rows[name] = {**base, "role": role, "id": sid, "sessionId": f"old-{role}"}
            agents.append(_row("idle", sid, name, cwd=str(crew), version=(versions or {}).get(name, have)))
        for name, fields in extra:
            rows[name] = {**base, **fields}
            agents.append(_row(fields.get("_state", "idle"), fields["id"], name, cwd=str(crew),
                               version=fields.get("_version", have)))
            rows[name].pop("_state", None)
            rows[name].pop("_version", None)
        (crew / "stations.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        (state / "agents.json").write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")
        (state / "version").write_text(installed + "\n", encoding="utf-8")
        (state / "stamp-version").write_text("1\n", encoding="utf-8")
        for name in done:
            (crew / "stations").mkdir(exist_ok=True)
            (crew / "stations" / f"{name}.done").write_text("", encoding="utf-8")
        for name, text in (files or {}).items():
            (state / name).write_text(text, encoding="utf-8")
    return setup


def _all_idle(tmp: Path) -> None:
    """Every session that runs is between turns again (a restarted one starts on its prompt, a busy turn)."""
    p = tmp / "claude" / "agents.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    for row in rows:
        if "pid" in row:
            row["state"], row["status"] = "working", "idle"
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _cc_installed(version: str):
    def before(tmp: Path) -> None:
        (tmp / "claude" / "version").write_text(version + "\n", encoding="utf-8")
        _all_idle(tmp)
    return before


def _cc_busy(*names: str, flight=()):
    """Sessions of these stations are in a turn; `flight` names stations with a subagent running."""
    def before(tmp: Path) -> None:
        _all_idle(tmp)
        ids = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))
        p = tmp / "claude" / "agents.json"
        rows = json.loads(p.read_text(encoding="utf-8"))
        for row in rows:
            if row.get("id") in [ids[n]["id"] for n in names]:
                row["status"] = "busy"
        p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        for v in ids.values():   # no subagent runs, but where `flight` names one
            shutil.rmtree(tmp / "home" / ".claude" / "jobs" / v["id"], ignore_errors=True)
        for n in flight:
            job = tmp / "home" / ".claude" / "jobs" / ids[n]["id"]
            job.mkdir(parents=True, exist_ok=True)
            (job / "state.json").write_text(json.dumps({"inFlight": {"tasks": 2, "queued": 0, "kinds": ["local_agent"]}}), encoding="utf-8")
    return before


def _watch_once(before=None, env: dict | None = None, after=None) -> Step:
    s = Step(["crew", "watch", "--once"], env=env or {}, before=before, after=after)
    return s


def _stn(r: Run, name: str, field: str = "id"):
    return _stations_now(r)[name].get(field)


def _launched(r: Run) -> list:
    """The names of the sessions started with --bg, in order."""
    return [c["args"][2] for c in bg_calls(r)]


def _bg_of(r: Run, name: str) -> list:
    return [c["args"] for c in bg_calls(r) if c["args"][2] == name]


def _cc_checks_moved(r: Run, name: str, old_session: str, version: str) -> bool:
    """The last start of the station resumed its old conversation and told it Claude Code was updated."""
    args = _bg_of(r, name)[-1]
    return arg_after(args, "--resume") == old_session and f"Claude Code was updated to {version}" in args[-1]


_CC_ORDER = ["crew-reviewer", "crew-releaser", "crew-orchestrator"]

scenario(Scenario(
    name="cc_move_order",
    suite="claude_update",
    about="a newer Claude Code: one idle station a round in its own conversation, the orchestrator last; then nothing is left to move",
    setup=_cc_setup(),
    tares=_CC_TARES,
    ignore_asked=_CC_NOT_YET,
    ignore_calls=_CC_READS,
    steps=[_watch_once(), _watch_once(before=_all_idle), _watch_once(before=_all_idle), _watch_once(before=_all_idle)],
    checks=[
        ("a round moves one idle station: the reviewer first, in its own conversation, told why",
         lambda r: r.code(0) == 0 and _launched(r)[:1] == ["crew-reviewer"] and _cc_checks_moved(r, "crew-reviewer", "old-reviewer", "1.1.0")),
        ("the move is said and recorded",
         lambda r: "crew-reviewer runs an older Claude Code than 1.1.0; restarting it on 1.1.0 now that it's idle, in the same conversation" in r.out(0)
         and "crew-reviewer runs Claude Code 1.1.0" in r.out(0) and '"event":"claude-updated"' in _events(r).replace(" ", "")),
        ("the orchestrator waits while the releaser is behind", lambda r: _launched(r)[:2] == ["crew-reviewer", "crew-releaser"]),
        ("the orchestrator moves last", lambda r: _launched(r) == _CC_ORDER),
        ("all on the new version: nothing more moves", lambda r: r.code(3) == 0 and len(_launched(r)) == 3 and r.out(3).strip() == ""),
    ],
    covers=["the orchestrator waits while others are behind", "the orchestrator still waits", "the orchestrator moves last",
            "all on 1.1.5: nothing more moves"],
))


def _cc(name: str, about: str, steps: list, checks: list, covers: list, setup=None, tares=None, not_windows: str = "") -> Scenario:
    return scenario(Scenario(name=name, suite="claude_update", about=about, setup=setup or _cc_setup(), tares=tares or _CC_TARES,
                             steps=steps, checks=checks, covers=covers, not_windows=not_windows,
                             ignore_asked=_CC_NOT_YET, ignore_calls=_CC_READS))


def _cc_tares(**settings) -> dict:
    """The fake Tares of these scenarios, with more crew settings."""
    doc = dict(_CC_TARES["GET /api/crew/settings"][1]["settings"], **settings)
    return {**_CC_TARES, "GET /api/crew/settings": (200, {"settings": doc})}


def _runs(r: Run, name: str):
    """The Claude Code version the station's current session shows in the list."""
    sid = _stn(r, name)
    rows = json.loads((r.state / "agents.json").read_text(encoding="utf-8"))
    return next((x.get("version") for x in rows if x.get("id") == sid), None)


def _moved(r: Run, name: str, old_session: str, version: str) -> bool:
    """The station was restarted on `version` in its old conversation, told why."""
    return bool(_bg_of(r, name)) and _cc_checks_moved(r, name, old_session, version)


_CC_PROCS: dict = {}   # (scenario folder, key) -> a process that stands for someone attached, or for a spare


def _cc_process(tmp: Path, key: str, argv0: str) -> None:
    """A process whose command line starts with argv0 (`exec -a`), for `attached` and the process listing to find."""
    import time
    _CC_PROCS[(str(tmp), key)] = subprocess.Popen(["bash", "-c", f"exec -a '{argv0}' sleep 120"],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)   # until bash has exec'd


def _cc_reap(tmp: Path) -> dict:
    """End the processes of this scenario's folder; which of them were still running (key -> bool)."""
    import time
    time.sleep(0.2)
    alive = {}
    for (folder, key), p in list(_CC_PROCS.items()):
        if folder != str(tmp):
            continue
        alive[key] = p.poll() is None
        if alive[key]:
            p.kill()
            p.wait()
        del _CC_PROCS[(folder, key)]
    return alive


def _cc_after(reap: bool = False):
    """After a round: the sessions started so far (name, in order), and with `reap` the processes that were left."""
    def after(tmp: Path) -> dict:
        names = []
        try:
            for line in (tmp / "claude" / "calls.jsonl").read_text(encoding="utf-8").splitlines():
                args = json.loads(line)["args"]
                if "--bg" in args:
                    names.append(args[2])
        except OSError:
            pass
        return {"launched": names, "alive": _cc_reap(tmp) if reap else {}}
    return after


def _round(before=None, env: dict | None = None, reap: bool = False) -> Step:
    """One `crew watch --once`; its snap says what had been started by then."""
    return _watch_once(before=before, env=env, after=_cc_after(reap))


def _seen(r: Run, i: int) -> list:
    return r.snap(i)["launched"]


def _cc_attach(name: str):
    def before(tmp: Path) -> None:
        _all_idle(tmp)
        sid = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))[name]["id"]
        _cc_process(tmp, f"attach {name}", f"claude attach {sid}")
    return before


def _cc_detach(name: str):
    def before(tmp: Path) -> None:
        _all_idle(tmp)
        p = _CC_PROCS.pop((str(tmp), f"attach {name}"), None)
        if p:
            p.kill()
            p.wait()
    return before


_WIN_NO_EXEC_A = "a process is given its command line with bash `exec -a`, which Windows has no counterpart for"
_R, _L, _O = "crew-reviewer", "crew-releaser", "crew-orchestrator"
_NOTHING = "nothing is said and nothing is started"

# A busy station and one with a subagent running wait, and the orchestrator with them; a busy station first in line
# does not hold up an idle one behind it.
_cc("cc_move_busy", "a busy station and one with a subagent running wait (the orchestrator too); the releaser moves once its subagent is done",
    [_round(before=_cc_busy(_R, flight=(_L,))), _round(before=_cc_busy(_R))],
    [("a busy station and one with a subagent running wait, and so does the orchestrator",
      lambda r: r.code(0) == 0 and r.out(0).strip() == "" and _seen(r, 0) == []),
     ("the idle releaser behind the busy reviewer moves, in the same conversation, told why",
      lambda r: _seen(r, 1) == [_L] and _moved(r, _L, "old-releaser", "1.1.0") and _runs(r, _L) == "1.1.0"),
     ("the move is recorded, and the orchestrator still waits",
      lambda r: '"event":"claude-updated"' in _events(r).replace(" ", "") and _stn(r, _O) == "aaaa0001")],
    ["a busy station and one with a subagent running wait", "the orchestrator waits while others are behind",
     "busy reviewer waits, the idle releaser behind it moves, in the same conversation, told why", "the move is recorded",
     "the orchestrator still waits"])

# Someone has a station open: it waits, the orchestrator with it; it moves once it is closed.
_cc("cc_move_attached", "a station someone has open waits (the orchestrator too), the one behind it moves, and it moves once it is closed",
    [_round(before=_cc_attach(_R)), _round(before=_all_idle), _round(before=_cc_detach(_R), reap=True)],
    [("an attached station waits, the releaser behind it moves, in the same conversation, told why",
      lambda r: _seen(r, 0) == [_L] and _runs(r, _L) == "1.1.0"
      and _moved(r, _L, "old-releaser", "1.1.0")),
     ("still attached: the reviewer waits, the orchestrator too",
      lambda r: _seen(r, 1) == [_L] and r.out(1).strip() == ""),
     ("no longer attached: the reviewer moves, not the orchestrator",
      lambda r: _seen(r, 2) == [_L, _R] and _moved(r, _R, "old-reviewer", "1.1.0") and _runs(r, _R) == "1.1.0" and _stn(r, _O) == "aaaa0001")],
    ["an attached station waits (the other one moves)", "the moved station runs 1.1.5, in the same conversation, told why",
     "still attached: the reviewer waits, the orchestrator too", "no longer attached: the reviewer moves, not the orchestrator"],
    not_windows=_WIN_NO_EXEC_A)

# The setting off, a halted crew, a version that does not move anything.
_cc("cc_move_off", "the claude_update setting off: nothing moves",
    [_round()],
    [("the setting off: nothing moves", lambda r: r.code(0) == 0 and r.out(0).strip() == "" and _seen(r, 0) == [] and r.launches() == 0)],
    ["the setting off: nothing moves"], tares=_cc_tares(claude_update=False))

_cc("cc_move_halted", "a halted crew moves nothing; resumed, a station moves",
    [_round(before=lambda tmp: (tmp / "crew" / "HALT").write_text('{"at":1700000000,"reason":"testing"}\n', encoding="utf-8")),
     _round(before=lambda tmp: ((tmp / "crew" / "HALT").unlink(), _all_idle(tmp)))],
    [("halted: nothing moves", lambda r: r.code(0) == 0 and r.out(0).strip() == "" and _seen(r, 0) == []),
     ("resumed: a station moves again", lambda r: _seen(r, 1) == [_R])],
    ["halted: nothing moves"])

_cc("cc_move_same", "the same version, an installed one older than the stations' and no readable version move nothing",
    [_round(), _round(before=_cc_installed("1.9.0")), _round(before=_cc_installed(""))],
    [("the same version: nothing moves", lambda r: r.out(0).strip() == "" and _seen(r, 0) == []),
     ("a station on a newer version than installed is left (1.10.0 against 1.9.0)", lambda r: r.out(1).strip() == "" and _seen(r, 1) == []),
     ("no readable version: nothing moves", lambda r: r.out(2).strip() == "" and _seen(r, 2) == [] and r.launches() == 0)],
    ["the same version: nothing moves", "a station on a newer version than installed is left"],
    setup=_cc_setup(have="1.10.0", installed="1.10.0"))

# `claude --version` says a different thing each time it is asked (the installer swaps a symlink): no release.
_cc("cc_move_flip", "a version that flips within a round moves nothing; once it holds still a station moves",
    [_round(), _round(before=lambda tmp: [(tmp / "claude" / n).unlink(missing_ok=True) for n in ("version-flip", "version-calls")])],
    [("a version that flips within a round moves nothing", lambda r: r.out(0).strip() == "" and _seen(r, 0) == []),
     ("it holds still: a station moves", lambda r: _seen(r, 1) == [_R])],
    ["a version that flips within a round moves nothing"],
    setup=_cc_setup(files={"version-flip": "1.2.0\n"}))

# Numbers compare as numbers: 1.10.0 is newer than 1.5.x (a plain sort puts it before them).
_cc("cc_move_numbers", "1.10.0 is newer than the 1.5.x the stations run: the watch moves a station onto it",
    [_round()],
    [("1.10.0 is newer than 1.5.x: the watch moves a station onto it",
      lambda r: _seen(r, 0) == [_R] and _runs(r, _R) == "1.10.0" and _moved(r, _R, "old-reviewer", "1.10.0"))],
    ["1.10.0 is newer than 1.5.x: the watch moves a station onto it"],
    setup=_cc_setup(have="1.5.4", installed="1.10.0"))

# A helper follows its station: after the others, before the orchestrator, in the same conversation. A busy helper is left
# alone and does not hold the orchestrator back. A finished builder is not moved and does not hold it back either.
_HELPER = ("crew-reviewer-probe", {"role": "helper", "parent": "crew-reviewer", "id": "dddd0004", "sessionId": "old-probe"})
_BUILDER = ("shop-build-m1", {"role": "builder", "project": "shop", "id": "eeee0005", "sessionId": "old-builder"})
_H = _HELPER[0]
_cc("cc_move_helper", "a helper moves after the other stations and before the orchestrator, in its conversation, told to load its parent's skill",
    [_round(), _round(before=_all_idle), _round(before=_all_idle), _round(before=_all_idle)],
    [("the helper moves with the watch: four restarts, the helper before the orchestrator, which is last",
      lambda r: _seen(r, 3) == [_R, _L, _H, _O] and _runs(r, _H) == "1.1.0" and _runs(r, _O) == "1.1.0"),
     ("the helper keeps its conversation and is told to load its parent role skill",
      lambda r: _moved(r, _H, "old-probe", "1.1.0") and "station-reviewer" in _bg_of(r, _H)[-1][-1])],
    ["the helper moves with the watch: four restarts, the helper before the orchestrator, which is last",
     "the helper keeps its conversation and is told to load its parent role skill"],
    setup=_cc_setup(extra=(_HELPER,)))

_cc("cc_move_helper_busy", "a busy helper is left alone and does not hold the orchestrator back; once it is idle it moves too",
    [_round(before=_cc_busy(_H)), _round(before=_cc_busy(_H)), _round(before=_cc_busy(_H)), _round(before=_all_idle)],
    [("a busy helper is left alone, and does not hold the orchestrator back",
      lambda r: _seen(r, 2) == [_R, _L, _O] and _runs(r, _O) == "1.1.0" and _runs(r, _R) == "1.1.0"),
     ("once the helper is idle it moves too", lambda r: _seen(r, 3) == [_R, _L, _O, _H] and _runs(r, _H) == "1.1.0")],
    ["a busy helper is left alone, and does not hold the orchestrator back", "once the helper is idle it moves too"],
    setup=_cc_setup(extra=(_HELPER,)))

_cc("cc_move_builder_done", "a finished builder is not moved and does not hold the orchestrator back",
    [_round(), _round(before=_all_idle), _round(before=_all_idle), _round(before=_all_idle)],
    [("the builder that is done stays as it is, the others move, the orchestrator last",
      lambda r: _seen(r, 3) == [_R, _L, _O] and _stn(r, _BUILDER[0]) == "eeee0005" and _runs(r, _BUILDER[0]) == "1.0.0")],
    [], setup=_cc_setup(extra=(_BUILDER,), done=(_BUILDER[0],)))


# An older pre-warmed spare comes back from Claude Code's pool: the restart is tried again, and a spare nobody claimed is
# stopped first. The spares are real processes the process listing finds (the same for both engines).
def _cc_spares(tmp: Path) -> None:
    _all_idle(tmp)
    for sock in ("unclaimed", "new"):
        (tmp / f"{sock}.claim.sock").write_text("", encoding="utf-8")   # not claimed yet
    for key, sock, version in (("unclaimed", "unclaimed", "1.1.0"), ("claimed", "gone", "1.1.0"), ("fresh", "new", "1.2.0")):
        _cc_process(tmp, key, f"claude bg-pty-host --bg-pty-host {tmp}/{sock}.pty.sock 200 50 -- /x/versions/{version} "
                              f"--bg-spare {tmp}/{sock}.claim.sock")


_cc("cc_move_stale_spare", "a restart that lands on an older spare is tried again; an unclaimed older spare is stopped, a claimed one and a new one are left",
    [_round(before=_cc_spares, reap=True)],
    [("a stale spare is noticed and the restart tried again",
      lambda r: len(_bg_of(r, _R)) == 2 and _runs(r, _R) == "1.2.0" and "crew-reviewer came back on Claude Code 1.1.0, an older spare" in r.out(0)
      and '"event":"stale-spare"' in _events(r).replace(" ", "")),
     ("the move succeeded in the end", lambda r: "crew-reviewer runs Claude Code 1.2.0" in r.out(0)),
     ("an unclaimed older spare was stopped, recorded", lambda r: r.snap(0)["alive"]["unclaimed"] is False and '"event":"spare-pruned"' in _events(r).replace(" ", "")),
     ("a claimed one (a session runs in it) and a new one were left", lambda r: r.snap(0)["alive"]["claimed"] and r.snap(0)["alive"]["fresh"])],
    ["a stale spare is noticed and the restart tried again", "an unclaimed older spare was stopped",
     "a claimed one (a session runs in it) and a new one were left", "the move succeeded in the end"],
    setup=_cc_setup(installed="1.2.0", files={"spawn-version": "1.1.0\n", "stale-left": "1\n"}), not_windows=_WIN_NO_EXEC_A)


def _cc_resumed(r: Run, name: str) -> int:
    return sum(1 for a in _bg_of(r, name) if "--resume" in a)


def _cc_marks(r: Run, name: str) -> list:
    return [_stn(r, name, k) for k in ("cc_failed", "cc_fails", "cc_retry_at", "cc_fresh")]


# Three tries land on an old spare: the fourth starts the station again, in a new conversation, once for the version.
_cc("cc_move_fresh", "three resumed tries land on old spares: the fourth starts the station again in a new conversation on the new version",
    [_round()],
    [("three resumed tries land on old spares, the fourth is a new conversation on 1.2.1",
      lambda r: len(_bg_of(r, _R)) == 4 and _cc_resumed(r, _R) == 3 and "--resume" not in _bg_of(r, _R)[-1] and _runs(r, _R) == "1.2.1"),
     ("it is told it was started again, and the event is recorded",
      lambda r: "started you again under the same name, in a new conversation" in _bg_of(r, _R)[-1][-1]
      and "starting it again in a new conversation on 1.2.1" in r.out(0) and '"event":"restarted-fresh"' in _events(r).replace(" ", "")),
     ("a station that got there is not marked: no failure, no retry", lambda r: _cc_marks(r, _R) == [None, None, None, None])],
    ["three resumed tries land on old spares, the fourth is a new conversation on 1.2.1",
     "it is told it was started again, and the event is recorded", "a station that got there is not marked: no failure, no retry"],
    setup=_cc_setup(installed="1.2.1", files={"spawn-version": "1.1.0\n", "stale-left": "3\n"}))

# Old spares every time: three tries, the new conversation, then it stops and says it will retry (the backoff itself is TR-964).
_cc("cc_move_old_spares", "old spares every time: three tries and the new conversation, then the station is marked with a retry time ten minutes on",
    [_round()],
    [("old spares every time: three tries, the new conversation, then it stops and says it will retry",
      lambda r: len(_bg_of(r, _R)) == 4 and _cc_resumed(r, _R) == 3 and "keeps coming back on an older Claude Code than 1.2.1" in r.out(0)
      and "the watch tries again in 10 min" in r.out(0) and _runs(r, _R) == "1.1.0"),
     ("it is marked: the version, one failed move, a retry time ten minutes on, the new conversation done",
      lambda r: _cc_marks(r, _R)[:2] == ["1.2.1", 1] and _cc_marks(r, _R)[3] == "1.2.1"
      and 540 < int(_cc_marks(r, _R)[2]) - __import__("time").time() <= 600)],
    ["old spares every time: three tries, the new conversation, then it stops and says it will retry",
     "it is marked: the version, one failed move, a retry time ten minutes on, the new conversation done"],
    setup=_cc_setup(installed="1.2.1", files={"spawn-version": "1.1.0\n", "stale-left": "all\n"}))


# ---------------------------------------------------------------------------------------------
# The watch's revive step (TR-959): one round of `cmd_watch --once` on each engine, through the watch driver. The
# crews here have nothing for the other branches of the round to do (no pace file, no newer plugin or Claude Code,
# no grants to give), so the round is the revive and the stuck restart.

WATCH_DRIVER = "watch_support.py"
# The bash round also reads `claude --version` and the session list for the steps that are not ported yet (updates,
# regrant, idle wake); those are not compared. What the round starts and stops (--bg, stop, rm) is.
WATCH_QUIET = (["--version"], ["agents"])
THRASH_LINE = ('{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"Autocompact is '
               'thrashing: the context refilled to the limit within 3 turns of the previous compact, 3 times in a row."}]}}')
ORDINARY_LINE = '{"type":"assistant","message":{"model":"claude-opus-5-5","content":[{"type":"text","text":"tick done"}]}}'
WOKEN_LINE = '{"type":"user","message":{"role":"user","content":"carry on with T4"}}'


def _transcript(tmp: Path, session: str, *lines: str) -> None:
    folder = tmp / "home" / ".claude" / "projects" / "-crew"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{session}.jsonl", "a", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(line + "\n")


def _watch_setup(rows: list, transcripts: dict | None = None, done: tuple = ()):
    """rows: (name, role, parent, state) -> a station on a session of that state; transcripts: session -> lines."""
    def setup(tmp: Path, env: dict) -> None:
        sys.path.insert(0, str(HERE))
        sys.path.insert(0, str(ROOT))
        import launch_support as LS
        from tares.crew import launch
        kit = launch.kit_version(LS.make_kit(tmp))   # a station on this kit is not one to move onto a new plugin
        work = tmp / "work"
        work.mkdir(parents=True, exist_ok=True)
        stations, agents = {}, []
        for i, (name, role, parent, st) in enumerate(rows, 1):
            sid = f"aaaa{i:04x}"
            stations[name] = {"role": role, "status": "active", "parent": parent, "project": "shop" if role == "builder" else "",
                              "repo": "", "cwd": str(work), "model": "opus", "effort": "high", "id": sid,
                              "sessionId": f"sess-{i}", "kit": kit}
            agents.append(_row(st, sid, name, cwd=str(work)))
        LS.make_crew(tmp, {"stations": stations})
        for name in done:
            (tmp / "crew" / "stations" / f"{name}.done").write_text("", encoding="utf-8")
        state = tmp / "claude"
        state.mkdir(parents=True, exist_ok=True)
        (state / "agents.json").write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")
        (state / "version").write_text("2.1.310\n", encoding="utf-8")
        for sess, lines in (transcripts or {}).items():
            _transcript(tmp, sess, *lines)
    return setup


def _bg_names(r: Run) -> list:
    return [arg_after(c["args"], "-n") for c in bg_calls(r)]


_WATCH_ONCE = Step(["once"])

scenario(Scenario(
    name="watch_revive_stopped",
    suite="crew",
    about="a round revives a stopped station in its own conversation; a running station, a helper and a finished builder are left",
    driver=WATCH_DRIVER,
    ignore_calls=WATCH_QUIET,
    setup=_watch_setup([("shop-build-m1", "builder", "crew-orchestrator", "stopped"),
                        ("crew-reviewer", "reviewer", "", "idle"),
                        ("shop-build-m2", "builder", "crew-orchestrator", "stopped"),
                        ("shop-build-m1-probe", "helper", "shop-build-m1", "stopped"),
                        ("crew-releaser", "releaser", "", "stopped")],
                       done=("shop-build-m2",)),
    steps=[_WATCH_ONCE, _WATCH_ONCE],
    checks=[
        ("a stopped station is revived under the same name",
         lambda r: r.code(0) == 0 and "shop-build-m1 stopped; reviving it under the same name" in r.out(0)
         and "shop-build-m1" in _bg_names(r)),
        ("...resuming its recorded conversation", lambda r: "sess-1" in [arg_after(c["args"], "--resume") for c in bg_calls(r)]),
        ("a station that runs is left", lambda r: "crew-reviewer" not in _bg_names(r)),
        ("a finished builder and a helper are left",
         lambda r: "shop-build-m2" not in _bg_names(r) and "shop-build-m1-probe" not in _bg_names(r)),
        ("a releaser is revived whatever else is done", lambda r: "crew-releaser" in _bg_names(r)),
        ("the next round finds them running and starts nothing", lambda r: r.launches() == 2 and r.code(1) == 0),
        ("the revives are in the event log", lambda r: _events(r).count("revived") >= 2),
    ],
    covers=[],
))

# ---------------------------------------------------------------------------------------------
# The watch's regrant step (TR-960): crew_test.sh, "A permission the person allows in the console is a grant on Tares".
# `watch_regrant` runs one round of the step on the launch driver (bash: the regrant branches of cmd_watch's loop,
# Python: regrant.step). A station is started with the grants of the world; the next step's world has more, fewer
# or none, and the station is idle, busy, waiting on a person, done, open in a terminal or without a session.

REGRANT_GRANT = '- "the reviewer may label pull requests" (2026-02-03)\n'
REGRANT_SHOP = '- "shop-build-m1 may run gh pr merge 12 --squash in shop" (2026-02-04)\n'


def _agent_of(tmp: Path, station: str) -> str:
    return json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))[station]["id"]


def _set_agent(tmp: Path, station: str, **fields) -> None:
    p = tmp / "claude" / "agents.json"
    sid = _agent_of(tmp, station)
    rows = json.loads(p.read_text(encoding="utf-8"))
    for row in rows:
        if row.get("id") == sid:
            row.update(fields)
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _idle(*stations: str):
    """Between turns (done): the session is alive and idle."""
    def run(tmp: Path) -> None:
        for s in stations:
            _set_agent(tmp, s, state="working", status="idle")
    return run


def _busy(*stations: str):
    def run(tmp: Path) -> None:
        for s in stations:
            _set_agent(tmp, s, state="working", status="busy")
    return run


def _waiting(station: str):
    """Blocked on a person (a question or a permission prompt)."""
    def run(tmp: Path) -> None:
        _set_agent(tmp, station, state="blocked", status="waiting")
    return run


def _set_station(station: str, **fields):
    def run(tmp: Path) -> None:
        p = tmp / "crew" / "stations.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data[station].update(fields)
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return run


def _both(*fns):
    def run(tmp: Path) -> None:
        for fn in fns:
            fn(tmp)
    return run


def _touch_done(station: str):
    def run(tmp: Path) -> None:
        (tmp / "crew" / "stations" / f"{station}.done").write_text("", encoding="utf-8")
    return run


def _forget_kept(station: str):
    def run(tmp: Path) -> None:
        (tmp / "crew" / "stations" / f"{station}.grants").unlink()
    return run


_ATTACHED: dict = {}


def _attach(station: str):
    """Someone has the session open in a terminal: a process whose command line says `attach <id>`."""
    def run(tmp: Path) -> None:
        _ATTACHED[station] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "attach",
                                                _agent_of(tmp, station)])
        time.sleep(0.5)
    return run


def _leave(station: str):
    def run(tmp: Path) -> None:
        p = _ATTACHED.pop(station, None)
        if p is not None:
            p.terminate()
            p.wait()
    return run


def _halted(tmp: Path) -> None:
    (tmp / "crew" / "HALT").write_text(json.dumps({"at": "2026-01-01T00:00:00Z", "reason": "testing"}, separators=(",", ":")) + "\n",
                                       encoding="utf-8")


def _resumed(tmp: Path) -> None:
    (tmp / "crew" / "HALT").unlink()


def _sessions_started(tmp: Path) -> int:
    try:
        lines = (tmp / "claude" / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for x in lines if x.strip() and "--bg" in json.loads(x)["args"])


def _sessions_and(station: str):
    """After the step: the sessions started so far, the station's allow list and its row."""
    def run(tmp: Path):
        settings = json.loads((tmp / "crew" / "stations" / f"{station}.settings.json").read_text(encoding="utf-8"))
        row = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))[station]
        return {"started": _sessions_started(tmp), "allow": settings["autoMode"]["allow"], "row": row}
    return run


def wstep(world: dict, before=None, env: dict | None = None, after=None) -> Step:
    s = lstep("watch_regrant", world=world, env=env)
    s.before = before
    s.after = after or _sessions_started   # the sessions started so far, as this step left them
    return s


def _launch(station: str, role: str, world: dict, parent: str = "", project: str = "") -> Step:
    return lstep("launch", station, role, parent, project, "", "{TMP}/crew", "sonnet", "high", f"Boot as the {role} station", world=world)


def _station(r: "Run", station: str) -> dict:
    return _stations_now(r)[station]


def _prompt_of(r: "Run", n: int = -1) -> str:
    return bg_calls(r)[n]["args"][-1]


def _resumed_session(r: "Run", n: int = -1):
    return arg_after(bg_calls(r)[n]["args"], "--resume")


_W0 = lworld("L3-review", grants=LAUNCH_GRANT)
_W1 = lworld("L3-review", grants=LAUNCH_GRANT + REGRANT_GRANT)
_W_GONE = lworld("L3-review", grants="")
_W_UNREAD = {**lworld("L3-review", grants=LAUNCH_GRANT + REGRANT_GRANT), "grants_status": 500}

WIN_NO_MULTILINE_REGRANT = ("a station is restarted through claude.cmd here, and cmd.exe cannot take the regrant prompt "
                            "(several lines); the claude.exe form waits for TR-946")

scenario(Scenario(
    name="watch_regrant_restart",
    suite="crew",
    about="a station between turns whose grants changed is restarted in its own conversation, told only what is new, once per change",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_MULTILINE_REGRANT,
    steps=[_launch("crew-reviewer", "reviewer", _W0),
           wstep(_W1, before=_busy("crew-reviewer")),                      # 1: busy: waits
           wstep(_W1, before=_idle("crew-reviewer"), after=_sessions_and("crew-reviewer")),   # 2: between turns: restarted
           wstep(_W1, before=_idle("crew-reviewer")),                      # 3: once per change
           wstep(_W_GONE, before=_idle("crew-reviewer"))],                 # 4: the grants shrank: a new change, nothing added
    checks=[
        ("a busy station waits for its new grant", lambda r: r.code(1) == 0 and r.snap(1) == 1),
        ("between turns it is restarted with the grant",
         lambda r: r.code(2) == 0 and r.snap(2)["started"] == 2 and "crew-reviewer has new permissions from Tares" in r.out(2)
         and '"event":"regranted"' in _events(r).replace(" ", "")),
        ("in the same conversation", lambda r: _resumed_session(r, 1) == "session-1"),
        ("its fresh settings have the grant",
         lambda r: any('"the reviewer may label pull requests"' in a for a in r.snap(2)["allow"])),
        ("its prompt quotes the new grant and says to run the refused command again",
         lambda r: "allowed new permissions in Tares" in _prompt_of(r, 1) and '"the reviewer may label pull requests"' in _prompt_of(r, 1)
         and "run it again now" in _prompt_of(r, 1)),
        ("and only the new one", lambda r: "staging database" not in _prompt_of(r, 1)),
        ("once per change", lambda r: r.code(3) == 0 and r.snap(3) == 2),
        ("a change with nothing added says so",
         lambda r: r.code(4) == 0 and r.snap(4) == 3 and "changed what the crew is allowed in Tares" in _prompt_of(r, 2)
         and "read_grants shows them" in _prompt_of(r, 2)),
    ],
    covers=[],
))

scenario(Scenario(
    name="watch_revive_unreadable",
    suite="crew",
    about="a round that cannot read Claude Code's session list revives nothing; the next one that can does",
    driver=WATCH_DRIVER,
    ignore_calls=WATCH_QUIET,
    setup=_watch_setup([("shop-build-m1", "builder", "crew-orchestrator", "stopped")]),
    steps=[Step(["once"], env={"FAKE_CLAUDE_AGENTS_FAIL": "1"}), _WATCH_ONCE],
    checks=[
        ("nothing is revived, and it says so",
         lambda r: "nothing is revived or restarted this round" in r.out(0) and r.code(0) == 0),
        ("the next round revives it",
         lambda r: "shop-build-m1 stopped; reviving it under the same name" in r.out(1) and r.launches() == 1),
    ],
    covers=[],
))

scenario(Scenario(
    name="watch_regrant_waits",
    suite="crew",
    about="nothing is restarted for a Tares that cannot give the grants, while the crew is halted, for a station without a conversation, one that is done, or one open in a terminal",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_MULTILINE_REGRANT,
    steps=[_launch("crew-reviewer", "reviewer", _W0),
           _launch("crew-releaser", "releaser", _W0),
           wstep(_W_UNREAD, before=_idle("crew-reviewer", "crew-releaser")),                # 2: grants unreadable
           wstep(_W1, before=_both(_idle("crew-reviewer", "crew-releaser"), _halted)),      # 3: halted
           wstep(_W1, before=_both(_resumed, _set_station("crew-reviewer", sessionId=""))),  # 4: no conversation (the releaser moves)
           wstep(_W1, before=_both(_touch_done("crew-releaser"), _idle("crew-releaser"))),   # 5: done: left alone
           wstep(_W1, before=_both(_set_station("crew-reviewer", sessionId="session-1"), _attach("crew-reviewer"),
                                   _idle("crew-reviewer"))),                                # 6: open in a terminal
           wstep(_W1, before=_leave("crew-reviewer"))],                                     # 7: they left
    checks=[
        ("grants that can't be read restart nothing", lambda r: r.code(2) == 0 and r.snap(2) == 2 and "new permissions" not in r.out(2)),
        ("nothing restarts while halted", lambda r: r.code(3) == 0 and r.snap(3) == 2),
        ("a station with no conversation to resume isn't restarted, the other is",
         lambda r: r.code(4) == 0 and r.snap(4) == 3 and "crew-releaser has new permissions" in r.out(4)
         and "crew-reviewer has new permissions" not in r.out(4)),
        ("a station that is done is left alone", lambda r: r.code(5) == 0 and r.snap(5) == 3),
        ("a station open in a terminal waits", lambda r: r.code(6) == 0 and r.snap(6) == 3),
        ("and gets it once they leave",
         lambda r: r.code(7) == 0 and r.snap(7) == 4 and "crew-reviewer has new permissions" in r.out(7)),
    ],
    covers=[],
))

scenario(Scenario(
    name="watch_revive_station_skipped",
    suite="crew",
    about="a station whose state cannot be told is skipped for the round and the rest go on",
    driver=WATCH_DRIVER,
    ignore_calls=WATCH_QUIET,
    setup=_watch_setup([("shop-build-m1", "builder", "crew-orchestrator", "stopped"),
                        ("shop-build-m2", "builder", "crew-orchestrator", "stopped")]),
    steps=[Step(["once"], env={"FAKE_CLAUDE_AGENTS_FAIL_AFTER": "1"}), _WATCH_ONCE],
    checks=[
        ("it says it skips each, and starts nothing",
         lambda r: "skipping shop-build-m1 this round" in r.out(0) and "skipping shop-build-m2 this round" in r.out(0)
         and "reviving it" not in r.out(0)),
        ("the next round revives them, only then",
         lambda r: r.launches() == 2 and "skipping" not in r.out(1) and "reviving it" in r.out(1)),
    ],
    covers=[],
))

scenario(Scenario(
    name="watch_revive_fails",
    suite="crew",
    about="a revive that fails is reported and tried again next round; the other stations are still revived",
    driver=WATCH_DRIVER,
    ignore_calls=WATCH_QUIET,
    setup=_watch_setup([("shop-build-m1", "builder", "crew-orchestrator", "stopped"),
                        ("shop-build-m2", "builder", "crew-orchestrator", "stopped")]),
    steps=[Step(["once"], env={"FAKE_CLAUDE_FAIL_RESUME": "1"}), _WATCH_ONCE],
    checks=[
        ("each failure is reported with its retry",
         lambda r: "could not revive shop-build-m1; will try again next round" in r.out(0)
         and "could not revive shop-build-m2; will try again next round" in r.out(0)),
        ("the next round revives them", lambda r: "could not revive" not in r.out(1) and "reviving it" in r.out(1)),
    ],
    covers=[],
))

scenario(Scenario(
    name="watch_stuck_restart",
    suite="crew",
    about="a station Claude Code stopped for thrashing on autocompact is started fresh; a working, a woken and an ordinary one are left",
    driver=WATCH_DRIVER,
    ignore_calls=WATCH_QUIET,
    setup=_watch_setup([("crew-orchestrator", "orchestrator", "", "idle"),
                        ("crew-reviewer", "reviewer", "", "idle"),
                        ("crew-releaser", "releaser", "", "working"),
                        ("shop-build-m1", "builder", "crew-orchestrator", "idle")],
                       transcripts={"sess-1": [ORDINARY_LINE, THRASH_LINE], "sess-2": [THRASH_LINE, ORDINARY_LINE],
                                    "sess-3": [THRASH_LINE], "sess-4": [THRASH_LINE, WOKEN_LINE]}),
    steps=[Step(["once"], env={"FACTORY_STOP_WAIT": "3"}), _WATCH_ONCE],
    checks=[
        ("the stuck station is said to be stuck, stopped and started fresh",
         lambda r: r.code(0) == 0 and "crew-orchestrator is stuck: Claude Code stopped it for thrashing on autocompact; "
         "starting it fresh under the same name" in r.out(0) and ["stop", "aaaa0001"] in [c["args"] for c in r.calls()]
         and _bg_names(r) == ["crew-orchestrator"]),
        ("...in a new conversation: no --resume", lambda r: all(arg_after(c["args"], "--resume") is None for c in bg_calls(r))),
        ("...with thrashing and restarted-fresh events", lambda r: "thrashing" in _events(r) and "restarted-fresh" in _events(r)),
        ("an ordinary last reply, a working station and a woken one are left",
         lambda r: all(n not in _bg_names(r) for n in ("crew-reviewer", "crew-releaser", "shop-build-m1"))),
        ("the new conversation is not restarted in the next round", lambda r: r.launches() == 1),
    ],
    covers=[],
))

scenario(Scenario(
    name="watch_stuck_stopped",
    suite="crew",
    about="a station that stopped on a thrashed conversation comes back in a new one, not resumed",
    driver=WATCH_DRIVER,
    ignore_calls=WATCH_QUIET,
    setup=_watch_setup([("crew-releaser", "releaser", "", "stopped")], transcripts={"sess-1": [THRASH_LINE]}),
    steps=[_WATCH_ONCE],
    checks=[
        ("it says why the conversation is new",
         lambda r: r.code(0) == 0 and "crew-releaser stopped; reviving it under the same name, in a new conversation "
         "since its last one thrashed" in r.out(0)),
        ("not resumed", lambda r: all(arg_after(c["args"], "--resume") is None for c in bg_calls(r))),
        ("restarted-fresh is in the event log", lambda r: "restarted-fresh" in _events(r) and "thrashing" not in _events(r)),
    ],
    covers=[],
))

scenario(Scenario(
    name="watch_regrant_roles",
    suite="crew",
    about="a station blocked on a person is restarted at once; a helper and a project's builder get their grants; a station that kept no grants is quoted all of them",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_MULTILINE_REGRANT,
    steps=[_launch("crew-reviewer", "reviewer", _W0),
           _launch("shop-build-m1", "builder", _W0, parent="crew-orchestrator", project="shop"),
           _launch("shop-build-m1-e2e", "helper", _W0, parent="shop-build-m1", project="shop"),
           wstep(lworld("L3-review", grants=LAUNCH_GRANT + REGRANT_GRANT, project_grants={"shop": REGRANT_SHOP}),
                 before=_both(_waiting("crew-reviewer"), _idle("shop-build-m1-e2e"), _busy("shop-build-m1"))),     # 3
           wstep(lworld("L3-review", grants=LAUNCH_GRANT + REGRANT_GRANT, project_grants={"shop": REGRANT_SHOP}),
                 before=_both(_idle("shop-build-m1"), _forget_kept("shop-build-m1")))],                            # 4
    checks=[
        ("a station blocked on a person gets the grant at once",
         lambda r: r.code(3) == 0 and "crew-reviewer has new permissions" in r.out(3)),
        ("a live helper is restarted with its grant",
         lambda r: "shop-build-m1-e2e has new permissions" in r.out(3)
         and any("gh pr merge 12 --squash" in a for a in allow_of(r, "shop-build-m1-e2e"))),
        ("a busy builder of the same project still waits", lambda r: "shop-build-m1 has new permissions" not in r.out(3)),
        ("a builder that kept no grants is quoted all of them, the project's with its scope",
         lambda r: "shop-build-m1 has new permissions" in r.out(4) and "staging database" in _prompt_of(r)
         and "for project shop, in their own words" in _prompt_of(r)),
    ],
    covers=[],
))


# ---------------------------------------------------------------------------------------------
# The pace (TR-962): with the crew setting pace on, a pass of the watch round tells every station, once, to
# stretch its tick before the plan's limit, a builder to hold at the next level, and each to go back when the
# plan has room. pace_test.sh is the bash suite these come from; it runs a real Tares fed fake /usage
# readings, here a fake one answers GET /api/crew/pace with the same documents (the reading Tares would
# have made), and the round is the pace pass alone (`pace_round` of launch_support.py: the reading, the tidy,
# then each live station's plan and send). What the revive step asks of the pace (`pace_down`) is run
# for the stations that are down. A restarted station is idle again before the next round, as it is when its
# turn ends.

PC_IDS = {"crew-orchestrator": "aaaa0001", "crew-reviewer": "bbbb0002", "crew-releaser": "bbbb0003",
          "links-app-build-m1": "cccc0004"}
PC_STATIONS = tuple(PC_IDS)
PC_B = "links-app-build-m1"
PC_N0 = 2_000_000_000
PC_UNTIL = "2033-05-18T05:00:00Z"


def _setup_pace(tmp: Path, env: dict) -> None:
    crew = tmp / "crew"
    crew.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", crew / "crew.json")
    base = {"status": "active", "repo": "", "cwd": str(tmp), "model": "opus", "effort": "high"}
    roles = {"crew-orchestrator": ("orchestrator", ""), "crew-reviewer": ("reviewer", ""),
             "crew-releaser": ("releaser", ""), PC_B: ("builder", "links-app")}
    stations = {n: {**base, "role": r, "project": pr, "id": PC_IDS[n], "sessionId": f"sess-{PC_IDS[n]}",
                    "parent": "crew-orchestrator" if n != "crew-orchestrator" else ""} for n, (r, pr) in roles.items()}
    (crew / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    agents = [{"id": PC_IDS[n], "name": n, "cwd": str(tmp), "state": "done", "pid": 40000 + int(PC_IDS[n][-4:], 16)}
              for n in stations]
    state = tmp / "claude"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")
    (state / "version").write_text("2.1.310\n", encoding="utf-8")
    for sid in PC_IDS.values():
        job = tmp / "home" / ".claude" / "jobs" / sid
        job.mkdir(parents=True, exist_ok=True)
        (job / "state.json").write_text(json.dumps({"sessionId": f"sess-{sid}"}) + "\n", encoding="utf-8")


def _pc_idle(tmp: Path) -> None:
    """Every session idle again (a restarted one is, when its turn ends); a station restarted by the fake keeps its row."""
    f = tmp / "claude" / "agents.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for row in rows:
        row["state"] = "done"
    f.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _pc_state(tmp: Path) -> dict:
    """After a step: the pace events so far as (station, detail), each station's pace file, and the crew's pace.json."""
    events, files = [], {}
    ev = tmp / "crew" / "events.jsonl"
    for line in (ev.read_text(encoding="utf-8").splitlines() if ev.is_file() else []):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "pace":
            events.append((e.get("station"), str(e.get("detail", ""))))
    st = tmp / "crew" / "stations"
    for p in sorted(st.glob("*.pace")) if st.is_dir() else []:
        files[p.name[:-len(".pace")]] = json.loads(p.read_text(encoding="utf-8"))
    pj = tmp / "crew" / "pace.json"
    return {"events": events, "files": files, "pace": json.loads(pj.read_text(encoding="utf-8")) if pj.is_file() else None}


def _pc_doc(session, weekly, reset=False, known=True, on=True) -> dict:
    """The document GET /api/crew/pace gives for a reading: the levels (85% of the window or 90% of the week
    slow the crew, 95% of the window holds builders, 100% of either limit closes the door), the words of the note."""
    if not on:
        return {"on": False, "known": False}
    if not known:
        return {"on": True, "known": False}
    full = (session >= 100 or weekly >= 100) and not reset
    level = 2 if session >= 95 else 1 if (session >= 85 or weekly >= 90) else 0
    state = "limit" if full else "hold" if level == 2 else "slow" if level == 1 else "ok"
    what = f"the 5-hour window is at {session}%" if session >= 85 else f"the weekly limit is at {weekly}%"
    return {"on": True, "known": True, "level": level, "at_limit": full, "state": state, "until": PC_UNTIL,
            "session_pct": session, "weekly_pct": weekly, "note": f"Pacing: {what}." if level else ""}


WIN_NO_PERCENT = ("a station is told through claude.cmd here, and cmd.exe cannot take a note that holds a percent sign ('the 5-hour window is at 85%'); "
                  "the engine says so and tries again next round; the claude.exe form waits for TR-946")


def _pc_world(doc: dict, idle_wake=None) -> dict:
    settings = {"pace": bool(doc.get("on"))}
    if idle_wake is not None:
        settings["idle_wake"] = idle_wake
    return {"settings": settings, "projects": [{"id": "p1", "name": "links-app", "repos": []}],
            "routes": {"/api/crew/pace": [200, doc]}}


def _pc_round(session, weekly, *, idle_wake=None, env=None, before=None, **kw) -> Step:
    doc = _pc_doc(session, weekly, **kw)

    def pre(tmp: Path) -> None:
        _pc_idle(tmp)
        if before:
            before(tmp)
    e = {"FACTORY_NOW": str(PC_N0), "LAUNCH_WORLD": json.dumps(_pc_world(doc, idle_wake)), **(env or {})}
    return Step(["pace_round"], env=e, before=pre, after=_pc_state)


def _pc_n(r: Run, i: int = -1, station: str = "", word: str = "") -> int:
    return sum(1 for s, d in r.snap(i)["events"] if (not station or s == station) and d == (word or d))


def _pc_f(r: Run, i: int, station: str, key: str):
    return r.snap(i)["files"].get(station, {}).get(key)


def _pc_prompts(r: Run, station: str) -> list:
    """The prompts the station was started with, in order."""
    return [c["args"][-1] for c in r.calls() if c["args"][:1] == ["--bg"] and len(c["args"]) > 2 and c["args"][2] == station]


def _pc_resumed(r: Run, station: str) -> bool:
    rows = [c["args"] for c in r.calls() if c["args"][:1] == ["--bg"] and len(c["args"]) > 2 and c["args"][2] == station]
    return bool(rows) and "--resume" in rows[-1]


scenario(Scenario(
    name="pace_off",
    suite="pace",
    about="with the setting unset or false, whatever the plan says: no note, no restart, no pace file",
    driver=LAUNCH_DRIVER,
    setup=_setup_pace,
    steps=[_pc_round(99, 99, on=False) for _ in range(3)] + [_pc_round(99, 99, on=False) for _ in range(2)],
    checks=[
        ("three rounds with the plan at 99%: no note, no restart",
         lambda r: all(r.code(i) == 0 for i in range(5)) and all(r.snap(i)["events"] == [] for i in range(3)) and r.launches() == 0),
        ("no pace file is written", lambda r: all(r.snap(i)["files"] == {} and r.snap(i)["pace"] is None for i in range(5))),
        ("the same on false", lambda r: r.snap(4)["events"] == [] and r.launches() == 0),
    ],
    covers=["unset, with the plan at 99%: three rounds, no note, no restart", "unset: no pace file is written", "false: the same"],
))

scenario(Scenario(
    name="pace_levels",
    suite="pace",
    about="85% of the window or 90% of the week: each station is told once to stretch; 95%: a builder is told to hold; back down: told to go back",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_PERCENT,
    setup=_setup_pace,
    steps=[
        _pc_round(84, 89),      # 0 nothing
        _pc_round(85, 89),      # 1 slow: stretch each
        _pc_round(85, 89), _pc_round(85, 89),   # 2, 3 once
        _pc_round(10, 10),      # 4 release each
        _pc_round(10, 10), _pc_round(10, 10),   # 5, 6 once
        _pc_round(10, 89),      # 7 nothing
        _pc_round(10, 90),      # 8 week slow: stretch again
        _pc_round(10, 90),      # 9 once
        _pc_round(94, 90),      # 10 no hold
        _pc_round(95, 90),      # 11 hold: the builder only
        _pc_round(95, 90), _pc_round(95, 90),   # 12, 13 once
        _pc_round(10, 10),      # 14 back: all four, the builder once
        _pc_round(96, 10),      # 15 stretch and hold in one note for the builder
        _pc_round(90, 10),      # 16 unhold
        _pc_round(90, 10),      # 17 once
        _pc_round(10, 10),      # 18 release
    ],
    checks=[
        ("84% of the window, 89% of the week: nothing", lambda r: r.snap(0)["events"] == [] and r.snap(0)["files"] == {}),
        ("85% of the window: each station is told once to stretch its tick",
         lambda r: all(_pc_n(r, 1, s, "stretch") == 1 for s in PC_STATIONS) and len(r.snap(1)["events"]) == 4
         and all(_pc_f(r, 1, s, "stretched") is True for s in PC_STATIONS) and _pc_f(r, 1, PC_B, "held") is False),
        ("the first note is in the same conversation, with the reading and what to do",
         lambda r: _pc_resumed(r, "crew-reviewer") and "Pacing: the 5-hour window is at 85%" in _pc_prompts(r, "crew-reviewer")[0]
         and "Stretch your tick to 45 minutes" in _pc_prompts(r, "crew-reviewer")[0]
         and "CronList, then CronDelete" in _pc_prompts(r, "crew-reviewer")[0]
         and "Finish the step you are on" not in _pc_prompts(r, PC_B)[0]),
        ("the builder at the hold is told to finish its step, push, report where it is, and wait",
         lambda r: "Finish the step you are on, push, write in your report where you are" in _pc_prompts(r, PC_B)[3]
         and "wait" in _pc_prompts(r, PC_B)[3] and "Finish the step" not in _pc_prompts(r, "crew-reviewer")[-1]),
        ("a release says the tick goes back to its normal interval",
         lambda r: "stopped pacing the crew" in _pc_prompts(r, "crew-reviewer")[1] and "normal interval" in _pc_prompts(r, "crew-reviewer")[1]),
        ("next rounds: not again", lambda r: len(r.snap(2)["events"]) == 4 and len(r.snap(3)["events"]) == 4),
        ("back to 10%: each station is told once to go back, and the pace files are gone",
         lambda r: all(_pc_n(r, 4, s, "release") == 1 for s in PC_STATIONS) and r.snap(4)["files"] == {} and len(r.snap(4)["events"]) == 8),
        ("once: nothing more is sent", lambda r: len(r.snap(6)["events"]) == 8),
        ("89% of the week: nothing", lambda r: len(r.snap(7)["events"]) == 8),
        ("90% of the week: each station is told again, once",
         lambda r: all(_pc_n(r, 8, s, "stretch") == 2 for s in PC_STATIONS) and len(r.snap(8)["events"]) == 12 and len(r.snap(9)["events"]) == 12),
        ("94%: no hold", lambda r: _pc_n(r, 10, PC_B, "hold") == 0 and len(r.snap(10)["events"]) == 12),
        ("95%: only the builder is told to hold, once; the others are not",
         lambda r: _pc_n(r, 11, PC_B, "hold") == 1 and _pc_f(r, 11, PC_B, "held") is True and _pc_f(r, 11, PC_B, "stretched") is True
         and all(_pc_n(r, 11, s, "hold") == 0 and _pc_f(r, 11, s, "held") is False
                 for s in ("crew-orchestrator", "crew-reviewer", "crew-releaser"))
         and len(r.snap(11)["events"]) == 13 and len(r.snap(13)["events"]) == 13),
        ("back to 10%: all four are told to go back, the builder once, and its hold is gone",
         lambda r: all(_pc_n(r, 14, s, "release") == 2 for s in PC_STATIONS) and r.snap(14)["files"] == {}),
        ("96% from nothing: the builder is told to stretch and hold in one note, the others only to stretch",
         lambda r: _pc_f(r, 15, PC_B, "held") is True and _pc_n(r, 15, PC_B, "stretch") == 3 and _pc_n(r, 15, PC_B, "hold") == 1
         and all(_pc_n(r, 15, s, "stretch") == 3 and _pc_f(r, 15, s, "held") is False
                 for s in ("crew-orchestrator", "crew-reviewer", "crew-releaser"))),
        ("back down to 90%: the builder is told once it may carry on, the others hear nothing",
         lambda r: _pc_n(r, 16, PC_B, "unhold") == 1 and _pc_n(r, 17, PC_B, "unhold") == 1 and _pc_f(r, 16, PC_B, "held") is False
         and _pc_f(r, 16, PC_B, "stretched") is True and _pc_n(r, 17, "crew-reviewer", "unhold") == 0),
        ("the last round goes back to normal", lambda r: r.snap(18)["files"] == {} and _pc_n(r, 18, PC_B, "release") == 3),
    ],
    covers=["84% of the window, 89% of the week: nothing", "each station is told once to stretch its tick",
            "next rounds: not again", "back to 10%: each station is told once to go back to its normal tick",
            "and the pace files are gone", "once: nothing more is sent", "89% of the week: nothing",
            "94%: no hold", "once", "back to 10%: all four are told to go back, the builder once, and its hold is gone",
            "96% from nothing: the builder is told to stretch and to hold in one note",
            "the others are only told to stretch"],
))



def _pc_put(doc=None, stretched=(), held=()):
    """Before a step: the crew's pace.json, and a pace file for each station that has been told to stretch."""
    def fn(tmp: Path) -> None:
        crew = tmp / "crew"
        if doc is not None:
            (crew / "pace.json").write_text(json.dumps(doc) + "\n", encoding="utf-8")
        (crew / "stations").mkdir(parents=True, exist_ok=True)
        for s in stretched:
            (crew / "stations" / f"{s}.pace").write_text(json.dumps({"stretched": True, "held": s in held}) + "\n", encoding="utf-8")
    return fn


def _pc_done(tmp: Path) -> None:
    """The builder has finished its tickets (the `.done` file the watch leaves it stopped for)."""
    d = tmp / "crew" / "stations"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{PC_B}.done").write_text("", encoding="utf-8")


def _pc_down(session, weekly, *outcomes, **kw) -> Step:
    """What the revive step asks of the pace for stations that are down ("<station>:ok|fail") or up ("<station>:up")."""
    e = {"FACTORY_NOW": str(PC_N0), "LAUNCH_WORLD": json.dumps(_pc_world(_pc_doc(session, weekly, **kw)))}
    return Step(["pace_down", *outcomes], env=e, before=_pc_idle, after=_pc_state)


def _pc_pace(r: Run, i: int):
    return r.snap(i)["pace"]


def _pc_said(r: Run, i: int) -> int:
    return r.out(i).count("the plan's limit is full until")


scenario(Scenario(
    name="pace_idle_wake",
    suite="pace",
    about="with idle_wake on the stations hold no tick of their own: a pace file is dropped, and nothing is sent",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_PERCENT,
    setup=_setup_pace,
    steps=[_pc_round(85, 10, idle_wake=True, before=_pc_put(stretched=PC_STATIONS)),
           _pc_round(95, 10, idle_wake=True), _pc_round(85, 10, idle_wake=False)],
    checks=[
        ("idle_wake on under pace: the pace files go and no pace note is sent",
         lambda r: r.snap(0)["files"] == {} and r.snap(0)["events"] == [] and r.snap(1)["events"] == [] and r.launches() == 4),
        ("and a hold sends nothing either (the wake machinery has the hold)", lambda r: r.snap(1)["files"] == {}),
        ("idle_wake off again: the stations are told to stretch",
         lambda r: all(_pc_n(r, 2, s, "stretch") == 1 for s in PC_STATIONS)),
    ],
    covers=[],
))

scenario(Scenario(
    name="pace_unknown",
    suite="pace",
    about="a reading that is missing changes nothing: no note, no restart, the pace file as it was",
    driver=LAUNCH_DRIVER,
    setup=_setup_pace,
    steps=[_pc_round(0, 0, known=False, before=_pc_put({"limit_since": 5, "until": PC_UNTIL, "said": True,
                                                         "stopped": ["crew-reviewer"], "tried": []}, stretched=PC_STATIONS)),
           _pc_round(0, 0, known=False), _pc_round(0, 0, known=False)],
    checks=[
        ("unknown: no note and no restart",
         lambda r: all(r.snap(i)["events"] == [] for i in range(3)) and r.launches() == 0),
        ("and the pace files are as they were",
         lambda r: len(r.snap(2)["files"]) == 4 and _pc_pace(r, 2)["stopped"] == ["crew-reviewer"]),
    ],
    covers=["unknown: no note and no restart"],
    # bash lists the sessions before pace_plan declines; Python returns before it asks (an unknown reading needs no list)
    ignore_calls=(("agents",),),
))

scenario(Scenario(
    name="pace_turn_off",
    suite="pace",
    about="turning the setting off while stations are stretched: each is told once to go back, and then nothing",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_PERCENT,
    setup=_setup_pace,
    steps=[_pc_round(85, 10), _pc_round(0, 0, on=False), _pc_round(0, 0, on=False), _pc_round(0, 0, on=False)],
    checks=[
        ("(the stations are stretched)", lambda r: all(_pc_f(r, 0, s, "stretched") is True for s in PC_STATIONS)),
        ("off while stretched: each is told once to go back, and the files are gone",
         lambda r: all(_pc_n(r, 1, s, "release") == 1 for s in PC_STATIONS) and r.snap(1)["files"] == {}),
        ("and then nothing", lambda r: len(r.snap(3)["events"]) == len(r.snap(1)["events"])),
    ],
    covers=["(the stations are stretched)", "and then nothing"],
))

scenario(Scenario(
    name="pace_restart_fails",
    suite="pace",
    about="a note whose restart fails sends no event and marks nothing told; the stations it left stopped are not told again",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_PERCENT,
    setup=_setup_pace,
    steps=[_pc_round(10, 10), _pc_round(85, 10, env={"FAKE_CLAUDE_FAIL_RESUME": "1"}), _pc_round(85, 10)],
    checks=[
        ("a note whose restart fails sends no event and marks nothing told",
         lambda r: r.snap(1)["events"] == [] and r.snap(1)["files"] == {}
         and "could not tell crew-orchestrator about the pace" in r.out(1)),
        ("the stations the failed restart left stopped are not told again by the pace (the revive step brings them back)",
         lambda r: r.snap(2)["events"] == [] and r.snap(2)["files"] == {} and r.launches() == 4),
    ],
    covers=["a note whose restart fails sends no event and marks nothing told"],
))

scenario(Scenario(
    name="pace_limit",
    suite="pace",
    about="a station that is down while a limit is full is not revived into it; after the reset it is revived with a short prompt in its own conversation",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_PERCENT,
    setup=_setup_pace,
    steps=[
        _pc_down(100, 10, "crew-reviewer:ok", "crew-releaser:ok"),    # 0 held down, said once
        _pc_down(100, 10, "crew-reviewer:ok"),                        # 1 not said again
        _pc_round(100, 10),                                           # 2 the running ones are still paced
        _pc_down(10, 10, "crew-reviewer:ok", "crew-releaser:ok"),     # 3 the reset: revived, short prompt
        _pc_down(10, 10, "crew-orchestrator:ok"),                     # 4 down for another reason: no pace prompt
    ],
    checks=[
        ("100%: two stopped stations are held down, not revived",
         lambda r: r.code(0) == 0 and "revive crew-" not in r.out(0) and r.snap(0)["events"] == []),
        ("the watch says so once, with the reset time",
         lambda r: _pc_said(r, 0) == 1 and PC_UNTIL in r.out(0) and _pc_said(r, 1) == 0),
        ("and remembers both, and since when",
         lambda r: set(_pc_pace(r, 1)["stopped"]) == {"crew-reviewer", "crew-releaser"} and _pc_pace(r, 1)["limit_since"] == PC_N0
         and _pc_pace(r, 1)["until"] == PC_UNTIL),
        ("the stations that run are still paced", lambda r: _pc_n(r, 2, "crew-orchestrator", "stretch") == 1),
        ("after the reset both are revived with a short prompt that does not tell them to reload the skills",
         lambda r: r.out(3).count("revive crew-") == 2 and "The plan's limit was reset. Carry on from where you stopped" in r.out(3)
         and "Reload the skills" not in r.out(3)),
        ("and nothing is left to carry out", lambda r: _pc_pace(r, 3) is None),
        ("a station stopped for another reason keeps today's revive prompt",
         lambda r: r.out(4).strip() == "revive crew-orchestrator:"),
    ],
    covers=["the watch says so once, with the reset time", "and remembers both, and since when",
            "with a short prompt that does not tell them to reload the skills", "and nothing is left to carry out",
            "a station stopped for another reason keeps today's revive prompt"],
))

scenario(Scenario(
    name="pace_tried",
    suite="pace",
    about="a resume that fails is tried again in its own conversation; two failures in a row, and the next is a new one; the limit filling again starts over",
    driver=LAUNCH_DRIVER,
    setup=_setup_pace,
    steps=[
        _pc_down(100, 10, "crew-reviewer:ok"),        # 0 held down
        _pc_down(10, 90, "crew-reviewer:fail"),       # 1 resume fails: tried once
        _pc_down(10, 90, "crew-reviewer:fail"),       # 2 tried twice, still its own conversation
        _pc_down(10, 90, "crew-reviewer:ok"),         # 3 now a new one, and it comes up
        _pc_down(100, 10, "crew-reviewer:ok"),        # 4 held down again
        _pc_down(10, 90, "crew-reviewer:fail"),       # 5 one failure
        _pc_down(10, 90, "crew-reviewer:ok"),         # 6 still resumed in its own conversation
        _pc_down(100, 10, "crew-reviewer:ok"),        # 7 held down
        _pc_down(10, 90, "crew-reviewer:fail"),       # 8
        _pc_down(10, 90, "crew-reviewer:fail"),       # 9
        _pc_down(100, 10, "crew-reviewer:ok"),        # 10 the limit fills again: the count starts over
        _pc_down(10, 90, "crew-reviewer:fail"),       # 11
        _pc_down(10, 90, "crew-reviewer:ok"),         # 12 own conversation again
    ],
    checks=[
        ("the resume fails: marked tried once", lambda r: _pc_pace(r, 1)["tried"] == ["crew-reviewer"]),
        ("one failure does not make the next fresh: tried again in its own conversation, and fails again",
         lambda r: "The plan's limit was reset" in r.out(2) and "--fresh" not in r.out(2) and len(_pc_pace(r, 2)["tried"]) == 2),
        ("the next round, after two failures in a row, starts it fresh, and says why and to reload the skills",
         lambda r: "revive crew-reviewer --fresh:" in r.out(3) and "could not be resumed" in r.out(3)
         and "Reload the skills factory-protocol and station-reviewer" in r.out(3)),
        ("then nothing is left to carry out", lambda r: _pc_pace(r, 3) is None),
        ("a single failed resume does not make the next one fresh",
         lambda r: "--fresh" not in r.out(6) and "could not be resumed" not in r.out(6) and _pc_pace(r, 6) is None),
        ("the limit filling again forgets the failed resumes", lambda r: _pc_pace(r, 10)["tried"] == []),
        ("so one failed resume in the new recovery is still tried in its own conversation",
         lambda r: "--fresh" not in r.out(12) and "could not be resumed" not in r.out(12)),
    ],
    covers=["the resume fails: said, its conversation is untouched, and marked tried once",
            "one failure does not make the next fresh: it is tried in its own conversation again, and fails again",
            "and says why, and to reload the skills", "a single failed resume does not make the next one fresh",
            "the limit filling again forgets the failed resumes",
            "so one failed resume in the new recovery is still tried in its own conversation"],
))

scenario(Scenario(
    name="pace_tidy",
    suite="pace",
    about="the pace file forgets a station that will not be revived, says the limit is full again in a new episode, and goes when nothing is left",
    driver=LAUNCH_DRIVER,
    setup=_setup_pace,
    steps=[
        _pc_down(100, 10, "crew-reviewer:ok", "links-app-build-m1:ok"),       # 0 two waiting
        _pc_round(100, 10, before=_pc_done),   # 1 the builder is done
        _pc_round(100, 10),                                                   # 2 the limit is still full: said stays
        _pc_round(10, 90),                                                    # 3 reset: the episode is over, the station is waited for
        _pc_down(100, 10, "crew-reviewer:ok"),                                # 4 a new episode says so again
        _pc_down(10, 10, "crew-reviewer:ok"),                                 # 5 revived with the limit gone: nothing left, the file goes
        _pc_round(10, 10, before=_pc_put({"limit_since": 5, "until": PC_UNTIL, "said": True, "stopped": ["crew-reviewer"], "tried": []})),
        _pc_round(0, 0, on=False),                                            # 7 off: the file goes
    ],
    checks=[
        ("two stations are waiting", lambda r: set(_pc_pace(r, 0)["stopped"]) == {"crew-reviewer", PC_B}),
        ("a builder that finished while it waited is not waited for", lambda r: _pc_pace(r, 1)["stopped"] == ["crew-reviewer"]),
        ("the limit still full: the file is kept, and the limit is said once", lambda r: _pc_pace(r, 2)["said"] is True),
        ("the reset: the episode is over", lambda r: _pc_pace(r, 3)["said"] is False and _pc_pace(r, 3)["stopped"] == ["crew-reviewer"]),
        ("a new limit episode says the limit is full again", lambda r: _pc_said(r, 4) == 1),
        ("with the limit gone and no one left, the pace file is gone", lambda r: _pc_pace(r, 5) is None),
        ("with the pace off the pace file is dropped", lambda r: _pc_pace(r, 6) is not None and _pc_pace(r, 7) is None),
    ],
    covers=["a builder that finished while it waited is not waited for", "a new limit episode says the limit is full again",
            "with the limit gone and no one left, the pace file is gone"],
))


# ---------------------------------------------------------------------------------------------
# The idle wake (TR-961): `crew watch --once` is one round of the watch, and with the crew setting idle_wake
# on it wakes a station only when Tares has work for it. idle_wake_test.sh is the bash suite these come from;
# it runs a real Tares, here a fake one answers with the same documents, and the rounds are stepped with
# FACTORY_NOW. A station that was woken is restarted by the fake Claude Code, and `_idle_all` makes every
# session idle again before a round, as a woken one is when its turn ends.

IW_N0 = 2_000_000_000
IW_IDS = {"crew-orchestrator": "aaaa0001", "crew-reviewer": "bbbb0002", "crew-releaser": "bbbb0003",
          "links-app-build-m1": "cccc0004"}
IW_PROJECTS = {"projects": [{"id": "p1", "name": "links-app", "repos": []}]}


def _setup_wake(tmp: Path, env: dict) -> None:
    crew = tmp / "crew"
    crew.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", crew / "crew.json")
    base = {"status": "active", "repo": "", "cwd": str(tmp), "model": "opus", "effort": "high"}
    roles = {"crew-orchestrator": ("orchestrator", ""), "crew-reviewer": ("reviewer", ""),
             "crew-releaser": ("releaser", ""), "links-app-build-m1": ("builder", "links-app")}
    stations = {n: {**base, "role": r, "project": pr, "id": IW_IDS[n], "sessionId": f"sess-{IW_IDS[n]}",
                    "parent": "crew-orchestrator" if n != "crew-orchestrator" else ""} for n, (r, pr) in roles.items()}
    (crew / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    agents = [{"id": IW_IDS[n], "name": n, "cwd": str(tmp), "state": "done", "pid": 40000 + int(IW_IDS[n][-4:], 16)}
              for n in stations]
    state = tmp / "claude"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")
    (state / "version").write_text("2.1.310\n", encoding="utf-8")
    for n, sid in IW_IDS.items():
        job = tmp / "home" / ".claude" / "jobs" / sid
        job.mkdir(parents=True, exist_ok=True)
        (job / "state.json").write_text(json.dumps({"sessionId": f"sess-{sid}"}) + "\n", encoding="utf-8")


def _idle_all(tmp: Path) -> None:
    f = tmp / "claude" / "agents.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for row in rows:
        row["state"] = "done"
    f.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _wakes(tmp: Path) -> dict:
    """After a round: the wake files of the stations (by name) and the idle_wake events so far, as (station, detail)."""
    files, events = {}, []
    stations = tmp / "crew" / "stations"
    for p in sorted(stations.glob("*.wake")) if stations.is_dir() else []:
        files[p.name[:-len(".wake")]] = json.loads(p.read_text(encoding="utf-8"))
    ev = tmp / "crew" / "events.jsonl"
    for line in (ev.read_text(encoding="utf-8").splitlines() if ev.is_file() else []):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "idle_wake":
            events.append((e.get("station"), str(e.get("detail", ""))))
    return {"files": files, "events": events}


def _n(r: Run, i: int = -1, station: str = "", word: str = "") -> int:
    """idle_wake events up to the end of round i for a station (any when empty) whose reason starts with word."""
    return sum(1 for s, d in r.snap(i)["events"] if (not station or s == station) and d.startswith(word))


def _wf(r: Run, i: int, station: str, key: str):
    return r.snap(i)["files"].get(station, {}).get(key)


def _station_id(tmp: Path, station: str) -> str:
    """The session id the crew has for a station now (a wake restarts it, and the fake Claude Code names the new session)."""
    return json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))[station]["id"]


def _agent_state(tmp: Path, station: str, state: str) -> None:
    f = tmp / "claude" / "agents.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for row in rows:
        if row["id"] == _station_id(tmp, station):
            row["state"] = state
    f.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _touch(station: str, sec: int):
    """The session did something at IW_N0 + sec (its job file's updatedAt)."""
    def fn(tmp: Path) -> None:
        f = tmp / "home" / ".claude" / "jobs" / _station_id(tmp, station) / "state.json"
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["updatedAt"] = _iso(IW_N0 + sec)
        f.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    return fn


def _iso(epoch: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pre(*fns):
    """Before a round: every session idle again, then these."""
    def fn(tmp: Path) -> None:
        _idle_all(tmp)
        for f in fns:
            f(tmp)
    return fn


def _report(text: str):
    def fn(tmp: Path) -> None:
        d = tmp / "crew" / "reports"
        d.mkdir(parents=True, exist_ok=True)
        (d / "crew-orchestrator.md").write_text(text, encoding="utf-8")
    return fn


def _world(setting=True, queue=None, tickets=None, desk=None, projects=None) -> dict:
    """A Tares for one round: the setting, the reviewer's queue, the builder's tickets, the answered desk."""
    return {"settings": {} if setting is None else {"idle_wake": setting},
            "projects": projects if projects is not None else IW_PROJECTS["projects"],
            "routes": {"/api/review-queue?mine=1": [200, {"queue": queue or []}],
                       "/api/projects/p1/tickets": [200, {"tickets": tickets or []}],
                       "/api/desk?state=answered&mine=1": [200, {"desk": desk or []}]}}


def _round(sec: int, world: dict, *pre, env: dict | None = None) -> Step:
    """One idle-wake pass of the watch at `sec` seconds, on a Tares that answers with `world`."""
    e = {"FACTORY_NOW": str(IW_N0 + sec), "LAUNCH_WORLD": json.dumps(world), **(env or {})}
    return Step(["idle_wake_round"], env=e, before=_pre(*pre), after=_wakes)


def _quiet_world(setting) -> dict:
    return _world(setting)


def _head(h: str, verdict=None) -> list:
    return [{"project": "p1", "project_name": "links-app", "ticket": "T1", "head": h, "verdict": verdict}]


B1 = "links-app-build-m1"


def _times(r: Run, first: int, last: int, station: str) -> list:
    """The seconds (since IW_N0) at which a station's last_wake changed, over rounds first..last."""
    out, seen = [], _wf(r, first - 1, station, "last_wake") if first else None
    for i in range(first, last + 1):
        lw = _wf(r, i, station, "last_wake")
        if lw is not None and lw != seen:
            out.append(lw - IW_N0)
            seen = lw
    return out


scenario(Scenario(
    name="idle_wake_off",
    suite="idle_wake",
    about="with the setting unset, and then false, rounds hours apart wake no one and write no wake state",
    driver=LAUNCH_DRIVER,
    setup=_setup_wake,
    steps=[_round(t, _quiet_world(None)) for t in (600, 1800, 3600, 7200, 14400)] + [_round(21600, _quiet_world(False))],
    checks=[
        ("unset: five rounds, hours apart, with an unreviewed PR: no wake",
         lambda r: all(r.code(i) == 0 for i in range(6)) and _n(r) == 0 and r.launches() == 0),
        ("unset: no wake state is written", lambda r: all(r.snap(i)["files"] == {} for i in range(5))),
        ("false: the same", lambda r: r.snap(5)["files"] == {} and _n(r) == 0 and r.launches() == 0),
    ],
    covers=["unset: five rounds, hours apart, with an unreviewed PR: no wake", "unset: no wake state is written", "false: the same"],
))

_LIVE_AT = [0, 840, 900, 1260, 1800, 2700, 3060, 3600, 5400, 5760, 6300]
scenario(Scenario(
    name="idle_wake_liveness",
    suite="idle_wake",
    about="with the setting on and nothing to do: a release wake, then liveness wakes backing off 15/30/45 minutes",
    driver=LAUNCH_DRIVER,
    setup=_setup_wake,
    steps=[_round(t, _world()) for t in _LIVE_AT],
    checks=[
        ("switched on under running stations: the first round tells each one that holds a tick to drop it, once (the builder has no ticket in review, so no tick)",
         lambda r: _n(r, 0) == 3 and all(_n(r, 0, s, "release") == 1 for s in ("crew-reviewer", "crew-orchestrator", "crew-releaser"))
         and _n(r, 0, B1) == 0 and B1 in r.snap(0)["files"]),
        ("reviewer: woken at 15, then 30 later, then 45 later (minutes 15, 45, 90)",
         lambda r: _times(r, 1, 10, "crew-reviewer") == [900, 2700, 5400]),
        ("orchestrator: 20, then 30, then 45 later", lambda r: _times(r, 1, 10, "crew-orchestrator") == [1260, 3060, 5760]),
        ("releaser: 30, then 30, then 45 later", lambda r: _times(r, 1, 10, "crew-releaser") == [1800, 3600, 6300]),
        ("a builder with no ticket in review is not woken", lambda r: _n(r, -1, B1) == 0 and _times(r, 1, 10, B1) == []),
        ("every one of them is a liveness wake",
         lambda r: _n(r, -1, "crew-reviewer", "liveness") == 3 and _n(r) == 12),
        ("each wake is a restart in the same conversation",
         lambda r: any("--resume" in c["args"] for c in r.calls()) and r.launches() >= 12),
    ],
    covers=["switched on under running stations: the first round tells each one that holds a tick to drop it, once (the builder has no ticket in review, so no tick)",
            "reviewer: woken at 15, then 30 later, then 45 later (minutes 15, 45, 90)", "orchestrator: 20, then 30, then 45 later",
            "releaser: 30, then 30, then 45 later", "a builder with no ticket in review is not woken",
            "every one of them is a liveness wake", "each wake is a restart in the same conversation"],
))

scenario(Scenario(
    name="idle_wake_activity",
    suite="idle_wake",
    about="activity after a wake and its settle time pushes the liveness clock back; the session's own turn does not",
    driver=LAUNCH_DRIVER,
    setup=_setup_wake,
    steps=[_round(0, _world()), _round(600, _world()), _round(840, _world()), _round(900, _world()),
           _round(1700, _world(), _touch("crew-reviewer", 1600)), _round(2400, _world()), _round(2500, _world()),
           _round(2600, _world(), _touch("crew-reviewer", 2550))],
    checks=[
        ("before 15 minutes: no liveness wake", lambda r: _n(r, 2, "crew-reviewer", "liveness") == 0),
        ("at 15 minutes: the first", lambda r: _n(r, 3, "crew-reviewer", "liveness") == 1 and _wf(r, 3, "crew-reviewer", "n") == 1),
        ("activity later than the wake and the settle time resets the count and the clock to it",
         lambda r: _wf(r, 4, "crew-reviewer", "n") == 0 and _wf(r, 4, "crew-reviewer", "clock") == IW_N0 + 1600
         and _n(r, 4, "crew-reviewer", "liveness") == 1),
        ("so the next wake is not at 45 minutes", lambda r: _n(r, 5, "crew-reviewer", "liveness") == 1),
        ("but 15 minutes after the activity, at the base again",
         lambda r: _n(r, 6, "crew-reviewer", "liveness") == 2 and _wf(r, 6, "crew-reviewer", "n") == 1),
        ("a session's own turn after a wake does not reset it", lambda r: _wf(r, 7, "crew-reviewer", "n") == 1),
    ],
    covers=["before 15 minutes: no liveness wake", "at 15 minutes: the first",
            "activity later than the wake and the settle time resets the count and the clock to it",
            "so the next wake is not at 45 minutes", "but 15 minutes after the activity, at the base again",
            "a session's own turn after a wake does not reset it"],
))

_H1, _H2, _H3 = "1111111aaaaaaaa", "2222222bbbbbbbb", "3333333cccccccc"
scenario(Scenario(
    name="idle_wake_reviewer",
    suite="idle_wake",
    about="a PR without a current verdict wakes the reviewer once per head, and not in the middle of a turn",
    driver=LAUNCH_DRIVER,
    setup=_setup_wake,
    steps=[_round(0, _world()),
           _round(60, _world(queue=_head(_H1))), _round(120, _world(queue=_head(_H1))), _round(180, _world(queue=_head(_H1))),
           _round(240, _world(queue=_head(_H2))),
           _round(300, _world(queue=_head(_H3)), lambda tmp: _agent_state(tmp, "crew-reviewer", "working")),
           _round(360, _world(queue=_head(_H3))),
           _round(420, _world(queue=_head(_H3, {"current": True, "verdict": "changes"})))],
    checks=[
        ("an unreviewed PR wakes the reviewer", lambda r: _n(r, 1, "crew-reviewer", "work") == 1),
        ("and only the reviewer", lambda r: _n(r, 1) == 4),
        ("the same head wakes it once, not every round", lambda r: _n(r, 3, "crew-reviewer", "work") == 1),
        ("a new head wakes it again", lambda r: _n(r, 4, "crew-reviewer", "work") == 2 and _n(r, 4) == 5),
        ("a reviewer in the middle of a turn is not woken", lambda r: _n(r, 5, "crew-reviewer", "work") == 2),
        ("the next round, once it is idle, wakes it", lambda r: _n(r, 6, "crew-reviewer", "work") == 3),
        ("the wake marks that head as told", lambda r: len(_wf(r, 6, "crew-reviewer", "seen")) == 1),
        ("a PR with a current verdict is no work for the reviewer", lambda r: _n(r, 7, "crew-reviewer", "work") == 3),
    ],
    covers=["an unreviewed PR wakes the reviewer", "and only the reviewer", "the same head wakes it once, not every round",
            "a new head wakes it again", "a reviewer in the middle of a turn is not woken",
            "the next round, once it is idle, wakes it", "the wake marks that head as told",
            "a PR with a current verdict is no work for the reviewer"],
))


def _ticket(label: str, stage: str, head: str = "", ci: str = "", verdict: str = "", holder: str = B1) -> dict:
    t = {"label": label, "holder": holder, "stage": stage, "pr": {"head": head, "ci": ci}}
    if verdict:
        t["verdict"] = {"current": True, "verdict": verdict}
    return t


scenario(Scenario(
    name="idle_wake_builder",
    suite="idle_wake",
    about="a builder is woken once for a verdict, changes asked for or failed CI on a ticket it holds",
    driver=LAUNCH_DRIVER,
    setup=_setup_wake,
    steps=[_round(0, _world()),
           _round(60, _world(tickets=[_ticket("T1", "review", _H3, "success", "changes")])),
           _round(120, _world(tickets=[_ticket("T1", "review", _H3, "success", "changes")])),
           _round(180, _world(tickets=[_ticket("T1", "changes", _H3, "success", "changes")])),
           _round(240, _world(tickets=[_ticket("T1", "changes", _H3, "success", "changes"),
                                       _ticket("T2", "changes", holder="someone-else-build-m1")])),
           _round(300, _world(tickets=[_ticket("T1", "changes", _H3, "success", "changes"), _ticket("T2", "changes")])),
           _round(360, _world(tickets=[_ticket("T1", "changes", _H3, "success", "changes"), _ticket("T2", "changes")])),
           _round(420, _world(tickets=[_ticket("T1", "review", "5555555eeeeeeee", "failure"), _ticket("T2", "doing")])),
           _round(480, _world(tickets=[_ticket("T1", "review", "5555555eeeeeeee", "failure"), _ticket("T2", "doing")]))],
    checks=[
        ("the builder was woken for the verdict on its ticket", lambda r: _n(r, 1, B1, "work") == 1),
        ("once per verdict", lambda r: _n(r, 2, B1, "work") == 1),
        ("changes asked for on the head the verdict is about is the same thing: still once", lambda r: _n(r, 3, B1, "work") == 1),
        ("a ticket held by another builder wakes this one not at all", lambda r: _n(r, 4, B1, "work") == 1),
        ("changes asked for on its own ticket wakes the holder", lambda r: _n(r, 5, B1, "work") == 2),
        ("once", lambda r: _n(r, 6, B1, "work") == 2),
        ("failed CI on the head wakes the holder", lambda r: _n(r, 7, B1, "work") == 3),
        ("and only once", lambda r: _n(r, 8, B1, "work") == 3),
    ],
    covers=["the builder was woken for the verdict on its ticket", "once per verdict",
            "changes asked for on the head the verdict is about is the same thing: still once",
            "a ticket held by another builder wakes this one not at all", "changes asked for on its own ticket wakes the holder",
            "once", "failed CI on the head wakes the holder (and the reviewer for the new head)", "and only once"],
))

_RL1 = "- RELEASE Links App milestone M1 (T1, T2)"
_RL2 = "- RELEASE Links App milestone M2 (T3)"


def _answer(i: str, closed: str, **kw) -> dict:
    return {"id": i, "label": i, "closed_at": closed, **kw}


scenario(Scenario(
    name="idle_wake_orchestrator",
    suite="idle_wake",
    about="a desk answer or a new project wakes the orchestrator once, a RELEASE line under Undelivered wakes the releaser once",
    driver=LAUNCH_DRIVER,
    setup=_setup_wake,
    steps=[_round(0, _world()),
           _round(60, _world(), _report("# crew-orchestrator\n\n## Undelivered\n- T4 done, waiting on nothing\n\n## Notes\n- RELEASE later maybe\n")),
           _round(120, _world(desk=[_answer("D1", _iso(IW_N0 + 90))]), _touch("crew-orchestrator", -9 * 365 * 86400)),
           _round(180, _world(desk=[_answer("D1", _iso(IW_N0 + 90))])),
           _round(240, _world(desk=[_answer("D1", _iso(IW_N0 + 90)), _answer("D2", _iso(IW_N0 + 200))]),
                  _touch("crew-orchestrator", 100000)),
           _round(300, _world(desk=[_answer("D1", _iso(IW_N0 + 90)), _answer("D2", _iso(IW_N0 + 200)),
                                    _answer("D3", _iso(IW_N0 + 250), command="gh pr view 1")]),
                  _touch("crew-orchestrator", 100000)),
           _round(360, _world(desk=[_answer("D1", _iso(IW_N0 + 90))],
                              projects=IW_PROJECTS["projects"] + [{"id": "p2", "name": "Blog", "repos": []}])),
           _round(420, _world(projects=IW_PROJECTS["projects"] + [{"id": "p2", "name": "Blog", "repos": []}]),
                  _report("# crew-orchestrator\n\n## Undelivered\n" + _RL1 + "\n- RELEASED M0 last week\n\n## Notes\n")),
           _round(480, _world(projects=IW_PROJECTS["projects"] + [{"id": "p2", "name": "Blog", "repos": []}])),
           _round(540, _world(projects=IW_PROJECTS["projects"] + [{"id": "p2", "name": "Blog", "repos": []}]),
                  _report("# crew-orchestrator\n\n## Undelivered\n" + _RL1 + "\n" + _RL2 + "\n"))],
    checks=[
        ("the first look only sets the baseline",
         lambda r: _n(r, 0, "crew-orchestrator", "work") == 0 and bool(_wf(r, 0, "crew-orchestrator", "baseline"))),
        ("a RELEASE line outside Undelivered, or none inside it, wakes nobody", lambda r: _n(r, 1, "crew-releaser", "work") == 0),
        ("a desk answer wakes the orchestrator, and only it",
         lambda r: _n(r, 2, "crew-orchestrator", "work") == 1 and _n(r, 2) == _n(r, 1) + 1),
        ("once", lambda r: _n(r, 3, "crew-orchestrator", "work") == 1),
        ("an answer the orchestrator recorded itself is no news to it", lambda r: _n(r, 4, "crew-orchestrator", "work") == 1),
        ("an allowed permission request is the watch's to act on, not a wake for the orchestrator",
         lambda r: _n(r, 5, "crew-orchestrator", "work") == 1),
        ("a project handed over wakes the orchestrator", lambda r: _n(r, 6, "crew-orchestrator", "work") == 2),
        ("a RELEASE line under Undelivered wakes the releaser, and only it",
         lambda r: _n(r, 7, "crew-releaser", "work") == 1 and _n(r, 7) == _n(r, 6) + 1),
        ("once per line", lambda r: _n(r, 8, "crew-releaser", "work") == 1),
        ("a second line is a second wake", lambda r: _n(r, 9, "crew-releaser", "work") == 2),
    ],
    covers=["the first look only sets the baseline", "a RELEASE line outside Undelivered, or none inside it, wakes nobody",
            "a desk answer wakes the orchestrator, and only it", "once",
            "an answer the orchestrator recorded itself is no news to it",
            "an allowed permission request is the watch's to act on, not a wake for the orchestrator",
            "a project handed over wakes the orchestrator", "a RELEASE line under Undelivered wakes the releaser, and only it",
            "once per line", "a second line is a second wake"],
))


def _halt(tmp: Path) -> None:
    (tmp / "crew" / "HALT").write_text('{"at":"2026-01-01T00:00:00Z","reason":"testing the wake"}\n', encoding="utf-8")


def _resume(tmp: Path) -> None:
    (tmp / "crew" / "HALT").unlink()


def _done_mark(tmp: Path) -> None:
    (tmp / "crew" / "stations" / f"{B1}.done").write_text("", encoding="utf-8")


def _undone(tmp: Path) -> None:
    (tmp / "crew" / "stations" / f"{B1}.done").unlink()


def _add_helper(tmp: Path) -> None:
    f = tmp / "crew" / "stations.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc[f"{B1}-probe"] = {**doc[B1], "role": "helper", "parent": B1, "id": "dddd0005", "sessionId": "sess-dddd0005"}
    f.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    ag = tmp / "claude" / "agents.json"
    rows = json.loads(ag.read_text(encoding="utf-8"))
    rows.append({"id": "dddd0005", "name": f"{B1}-probe", "cwd": str(tmp), "state": "done", "pid": 40005})
    ag.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    job = tmp / "home" / ".claude" / "jobs" / "dddd0005"
    job.mkdir(parents=True, exist_ok=True)
    (job / "state.json").write_text('{"sessionId":"sess-dddd0005"}\n', encoding="utf-8")


_V6 = "6666666ffffffff"
scenario(Scenario(
    name="idle_wake_guards",
    suite="idle_wake",
    about="a halted crew, a finished builder and a helper are never woken; a head with a current verdict is no work",
    driver=LAUNCH_DRIVER,
    setup=_setup_wake,
    steps=[_round(0, _world()),
           _round(3600, _world(), _halt), _round(7200, _world()),
           _round(10800, _world(), _resume),
           _round(11400, _world(), _done_mark),
           _round(12000, _world(queue=_head(_V6, {"current": True, "verdict": "changes"}),
                                tickets=[_ticket("T1", "review", _V6, "success", "changes")]), _done_mark),
           _round(12060, _world(tickets=[_ticket("T1", "review", _V6, "success", "changes")]), _undone),
           _round(40000, _world(), _add_helper)],
    checks=[
        ("a halted crew is not woken", lambda r: _n(r, 2) == _n(r, 0) and _n(r, 1) == _n(r, 0)),
        ("a crew that is not halted any more is woken", lambda r: _n(r, 3, "crew-reviewer", "liveness") == 1),
        ("a builder that is done is not woken, whatever its ticket says", lambda r: _n(r, 5, B1) == _n(r, 4, B1)),
        ("a head that already has a current verdict is no work for the reviewer, even though it was never seen",
         lambda r: _n(r, 5, "crew-reviewer", "work") == 0),
        ("(the ticket was real work: once it is not done, the builder is woken for it)", lambda r: _n(r, 6, B1) == _n(r, 4, B1) + 1),
        ("a helper is never woken", lambda r: _n(r, 7, B1 + "-probe") == 0),
    ],
    covers=["a halted crew is not woken", "a builder that is done is not woken, whatever its ticket says",
            "a head that already has a current verdict is no work for the reviewer, even though it was never seen",
            "(the ticket was real work: once it is not done, the builder is woken for it)", "a helper is never woken"],
))

scenario(Scenario(
    name="idle_wake_rearm",
    suite="idle_wake",
    about="switched off again, each station that gave up its tick is woken once to take it back",
    driver=LAUNCH_DRIVER,
    setup=_setup_wake,
    steps=[_round(0, _world()), _round(100, _world(False)), _round(200, _world(False)), _round(90000, _world(False)),
           _round(130000, _world(False))],
    checks=[
        ("off again: every station that gave up its tick is woken to re-arm it",
         lambda r: all(_n(r, 1, s, "rearm") == 1 for s in ("crew-orchestrator", "crew-reviewer", "crew-releaser"))),
        ("and once: nothing more is sent",
         lambda r: _n(r, 3) == _n(r, 1) and _wf(r, 3, "crew-reviewer", "released") is False),
        ("off: the watch is back to doing nothing", lambda r: _n(r, 4) == _n(r, 1)),
    ],
    covers=["and once: nothing more is sent", "off: the watch is back to doing nothing"],
))


# ---------------------------------------------------------------------------------------------
# Normalising

ISO = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?")
# An epoch time is replaced only where it is a time: after a word that says so (`at`, `since`, `until`,
# `ts`, `time`, `epoch`, or a name ending in _at, _ts, _time, At), with a `=`, `:` or space between. A
# plain ten-digit number elsewhere (a port, a count, an id) is kept.
EPOCH = re.compile(r"(?P<pre>(?<![\w.])(?:at|since|until|epoch|ts|time|\w*_at|\w*_ts|\w*_time|\w*At)[\"']?\s*[=:]?\s*[\"']?)"
                   r"1[5-9]\d{8}(?:\d{3})?(?![\d.])")
EPOCH_VALUE = re.compile(r"1[5-9]\d{8}(?:\d{3})?")
# A fake Tares listens on a free port, a different one on each side
PORT = re.compile(r"127\.0\.0\.1:\d{4,5}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
SESSION = re.compile(r"\bsession-\d+\b")
# A job id (eight hex digits) is replaced only where a job id is printed: after `backgrounded ·`, or after
# job, session, id, stop or rm. In JSON, a string under an id key is replaced whole (see ID_KEYS).
JOB_ID = re.compile(r"(?P<pre>\bbackgrounded[^0-9a-f\n]+|\b(?:job|session|id|stop|rm)[ =:]+[\"']?)[0-9a-f]{8}\b")
HEX8 = re.compile(r"[0-9a-f]{8}")
PID_TEXT = re.compile(r"\b(pid|PID)([ =:]+)\d+")
# The port of a throwaway fake Tares (an ephemeral port: five digits); 8787, 8788 and the like stay.
PORT = re.compile(r"(?<=127\.0\.0\.1:)\d{5}\b")
PID_KEYS = {"pid", "ppid", "watch_pid", "watchPid"}
ID_KEYS = {"sessionId", "session_id", "id"}
TIME_KEY = re.compile(r"(^(ts|time|at|since|until)$)|(_at$)|(At$)|(_ts$)|(_time$)", re.I)


TARES_ADDR: list = []   # the stand-in Tares' "127.0.0.1:<port>" while a side runs: its port changes every run


def norm_text(s: str, tmp: Path | None = None) -> str:
    """Text with what changes from run to run replaced: the temp folder, timestamps, pids, ids."""
    s = s.replace("\r\n", "\n")
    for addr in TARES_ADDR:
        s = s.replace(addr, "<TARES>")
    if tmp is not None:
        forms = {str(tmp), os.path.realpath(tmp), tmp.as_posix(), Path(os.path.realpath(tmp)).as_posix()}
        # a Windows path inside JSON text has its backslashes doubled; and a drive letter may change case
        forms |= {f.replace("\\", "\\\\") for f in forms if "\\" in f}
        flags = re.IGNORECASE if is_windows() else 0
        for p in sorted(forms, key=len, reverse=True):
            s = re.sub(re.escape(p), "<TMP>", s, flags=flags)
    s = PORT.sub("127.0.0.1:<PORT>", s)
    s = UUID.sub("<ID>", s)
    s = ISO.sub("<TS>", s)
    s = EPOCH.sub(lambda m: m.group("pre") + "<TS>", s)
    s = PID_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}<PID>", s)
    s = SESSION.sub("<ID>", s)
    s = PORT.sub("<PORT>", s)
    s = JOB_ID.sub(lambda m: m.group("pre") + "<ID>", s)
    return s


def norm_json(v, tmp: Path | None = None, key: str = ""):
    """A parsed JSON value with its strings normalised and numbers under pid or time keys replaced.
    Key order does not matter: it is sorted when rendered."""
    if isinstance(v, dict):
        return {k: norm_json(x, tmp, k) for k, x in v.items()}
    if isinstance(v, list):
        return [norm_json(x, tmp, key) for x in v]
    if isinstance(v, str):
        if key in ID_KEYS and HEX8.fullmatch(v):
            return "<ID>"
        if TIME_KEY.search(key) and EPOCH_VALUE.fullmatch(v):
            return "<TS>"
        return norm_text(v, tmp)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if key in PID_KEYS:
            return "<PID>"
        if TIME_KEY.search(key) or key == "startedAt":
            return "<TS>"
    return v


def _settings_arg(row):
    """A call's `--settings <file>`: the file is a temp name each engine makes its own way, so it is replaced.
    A value that is JSON text, not a file, is kept."""
    args = row.get("args") if isinstance(row, dict) else None
    if isinstance(args, list):
        for i, a in enumerate(args[:-1]):
            if a == "--settings" and isinstance(args[i + 1], str) and not args[i + 1].lstrip().startswith("{"):
                args[i + 1] = "<SETTINGS-FILE>"
    return row


def render_json(v) -> str:
    return json.dumps(v, indent=2, sort_keys=True, ensure_ascii=False)


def norm_file(text: str, name: str, tmp: Path | None = None) -> str:
    """A file's content, normalised: a .json file or .jsonl file as sorted-key JSON, anything else as text."""
    try:
        if name.endswith(".jsonl"):
            rows = [_settings_arg(norm_json(json.loads(x), tmp)) for x in text.splitlines() if x.strip()]
            return "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows)
        if name.endswith(".json"):
            return render_json(norm_json(json.loads(text), tmp))
    except ValueError:
        pass
    return norm_text(text, tmp).rstrip("\n")


def dropped(text: str, patterns: tuple) -> str:
    """text without the lines that match one of the regexes (a step's `drop`)."""
    if not patterns:
        return text
    res = [re.compile(p) for p in patterns]
    return "\n".join(ln for ln in text.split("\n") if not any(r.search(ln) for r in res))


def norm_output(text: str, tmp: Path | None = None) -> str:
    """printed output: a whole-output JSON document gets sorted keys, any other text is normalised as is."""
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            return render_json(norm_json(json.loads(stripped), tmp))
        except ValueError:
            pass
    return norm_text(text, tmp).rstrip("\n")


# ---------------------------------------------------------------------------------------------
# Running one side

def command_for(engine: str, sc: "Scenario | None" = None) -> list:
    if sc is not None and sc.driver:
        return [sys.executable, str(HERE / sc.driver), engine]
    override = os.environ.get("PARITY_CMD_" + engine.upper())
    return shlex.split(override) if override else [sys.executable, "-m", "tares.crew"]


def is_windows() -> bool:
    """A function so a test can patch it."""
    return sys.platform == "win32"


def bash_available() -> str:
    """"" when the bash engine can run here, else the reason it cannot."""
    if is_windows():
        return "needs bash, which is not available on Windows"
    if not shutil.which("bash") or not shutil.which("jq"):
        return "needs bash and jq on PATH"
    return ""


def make_launcher(path: Path, extra: list, script: Path | None = None) -> Path:
    """An executable that runs the Python fake: a shell script, or a .cmd file on Windows (returns its path).

    Named `claude`, it is the Claude Code of the scenario: on PATH for a command that runs `claude` by
    name (the bash engine does for some commands, the Python engine will), and CLAUDE_BIN for the rest.
    The .cmd form passes the fake's exit code on, and is written with CRLF whatever the platform.
    The paths in it must be ASCII: cmd.exe reads a batch file in the console's code page."""
    if sys.platform == "win32":
        path = path.with_suffix(".cmd")
        lines = ['@"%s" "%s" %s %%*' % (sys.executable, script or FAKE_CLAUDE, " ".join(extra)), "@exit /b %ERRORLEVEL%"]
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    else:
        path.write_text('#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (sys.executable, script or FAKE_CLAUDE, " ".join(extra)), encoding="utf-8")
        path.chmod(0o755)
    return path


# What the person's own shell must not lend a scenario: the crew's and Tares' settings, Claude Code's
# config and job folders, the XDG folders and GitHub's CLI settings (all of them move state or a login).
STRIPPED_PREFIXES = ("FACTORY_", "TARES_", "CLAUDE_PLUGIN_OPTION_", "PARITY_", "FAKE_CLAUDE_",
                     "CLAUDE_CONFIG_DIR", "CLAUDE_JOB_DIR", "XDG_", "GH_")


def crew_env(tmp: Path, engine: str) -> dict:
    """The environment of a throwaway crew: everything under tmp, the Python fake for Claude Code, no
    station or Tares settings from the person's own shell."""
    home, crew, state, bindir = (tmp / n for n in ("home", "crew", "claude", "bin"))
    for d in (home, state, bindir):
        d.mkdir(parents=True, exist_ok=True)
    claude = make_launcher(bindir / "claude", [])  # claude.cmd on Windows, which is also what `claude` finds on PATH
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(STRIPPED_PREFIXES)
           and k not in ("TARESD_URL", "NAVFLOW_DB", "PWD", "OLDPWD")}
    env.update(
        HOME=str(home), USERPROFILE=str(home), FACTORY_CREW_HOME=str(crew), FAKE_CLAUDE_STATE=str(state),
        CLAUDE_BIN=str(claude), FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
        GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
        PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", TARES_FACTORY_ENGINE=engine,
        TARES_HOME=str(tmp / "tares-home"),
        PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""),  # `claude` by name finds the fake first
    )
    (tmp / "gitconfig").write_text("[user]\n\tname = Test Crew\n")
    return env


def _without_calls(text: str, prefixes: tuple) -> str:
    """calls.jsonl without the calls whose arguments start with one of the prefixes."""
    keep = []
    for line in text.splitlines():
        try:
            args = json.loads(line).get("args") or []
        except ValueError:
            args = []
        if not any(list(args[:len(pre)]) == list(pre) for pre in prefixes):
            keep.append(line)
    return "\n".join(keep) + ("\n" if keep else "")


def snapshot(run: Run) -> dict:
    """The files a scenario leaves: the crew home, and what the fake Claude Code recorded."""
    files = {}
    bases = [(run.crew, "crew/", None)]
    bases += [(d, d.name + "/", None) for d in sorted(run.tmp.glob("crew-*")) if d.is_dir()]   # more crews, side by side
    bases.append((run.state, "claude/", {"calls.jsonl", "agents.json"}))
    for base, prefix, only in bases:
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and (only is None or p.name in only):
                rel = prefix + p.relative_to(base).as_posix()
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    if p.name == "calls.jsonl" and run.ignore_calls:
                        text = _without_calls(text, run.ignore_calls)
                        if not text:
                            continue    # a run that made only left-out calls has no calls file, as one that made none
                    files[rel] = norm_file(text, p.name, run.tmp)
                except OSError:
                    files[rel] = "<unreadable>"
    t = run.tmp / "tares" / "writes.jsonl"   # what the stand-in Tares was asked to save
    if t.is_file():
        files["tares/writes.jsonl"] = norm_file(t.read_text(encoding="utf-8", errors="replace"), t.name, run.tmp)
    return files


@dataclass
class Side:
    engine: str
    skip: str = ""                  # why it did not run ("" when it did)
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    text: str = ""                  # the normalised record of the run, for the diff
    raw: str = ""                   # the first failing step's output, for the person reading


def run_side(sc: Scenario, engine: str) -> Side:
    side = Side(engine)
    if engine == "bash" and not os.environ.get("PARITY_CMD_BASH"):
        why = bash_available()
        if why:
            side.skip = why
            return side
    if engine == "python" and sc.not_windows and is_windows():
        side.skip = sc.not_windows
        return side
    tmp = Path(tempfile.mkdtemp(prefix=f"parity-{sc.name}-{engine}-")).resolve()
    stand_in = None
    fake = None
    try:
        env = crew_env(tmp, engine)
        if sc.stand_in is not None:
            stand_in = StandInTares(tmp / "tares", **sc.stand_in).start()
            env.update(TARESD_URL=stand_in.url, TARES_FACTORY_MCP_URL=stand_in.mcp_url)
            TARES_ADDR[:] = [f"127.0.0.1:{stand_in.port}"]
        results = []
        if sc.tares is not None:
            fake = FakeTares(routes_in(sc.tares, tmp))
            env["TARESD_URL"] = fake.url
        run = Run(tmp, results, fake, stand_in)
        run.ignore_calls = sc.ignore_calls
        if sc.prepare:
            sc.prepare(run)
        if sc.setup:
            sc.setup(tmp, env)
        here = lambda text: (text.replace("{tmp}", str(tmp)).replace("{tares}", fake.url if fake else "")
                             if isinstance(text, str) else text)
        for step in sc.steps:
            if step.run_before:
                step.run_before(run)
            if step.before:
                step.before(tmp)
            step_env = step.env(run) if callable(step.env) else step.env
            senv = {**env, **{k: here(v) for k, v in step_env.items()}}
            try:
                p = subprocess.run(command_for(engine, sc) + [here(a) for a in step.argv],
                                   cwd=here(step.cwd) or str(ROOT), env=senv,
                                   input=step.stdin, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=STEP_TIMEOUT)
                results.append({"code": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
            except subprocess.TimeoutExpired:
                results.append({"code": 124, "stdout": "", "stderr": f"parity: step timed out after {STEP_TIMEOUT}s\n"})
            if step.after:
                results[-1]["snap"] = step.after(tmp)
            if engine == "python" and results[-1]["code"] == 2 and NOT_PORTED in results[-1]["stderr"]:
                side.skip = "not ported"
                return side
        for label, fn in sc.checks:
            try:
                ok = bool(fn(run))
            except Exception as e:  # a check that cannot look is a check that fails
                ok = False
                label = f"{label} ({type(e).__name__}: {e})"
            (side.passed if ok else side.failed).append(label)
        if os.environ.get("PARITY_FAIL") in (engine, "both"):
            side.failed.append(f"deliberate failure (PARITY_FAIL={os.environ['PARITY_FAIL']})")
        blocks = []
        for step, r in zip(sc.steps, results):
            blocks.append(f"== step: {' '.join(step.argv)} ==\nexit: {r['code']}\n"
                          f"-- stdout --\n{dropped(norm_output(r['stdout'], tmp), step.drop)}\n"
                          f"-- stderr --\n{dropped(norm_output(r['stderr'], tmp), step.drop)}")
            if r.get("snap") is not None:  # what the step's `after` read (compared too)
                blocks.append(f"-- after --\n{render_json(norm_json(r['snap'], tmp))}")
        for rel, content in snapshot(run).items():
            blocks.append(f"== file {rel} ==\n{content}")
        if fake is not None:  # what each engine asked Tares, and whether it sent a key (never the key)
            # A read asked twice in a row counts once: bash asks again from a subshell that lost its cache.
            asked = []
            for q in fake.requests:
                if any(q["route"] == x or (x.endswith(" ") and q["route"].startswith(x)) for x in sc.ignore_asked):
                    continue
                line = f"{q['route']} key={'yes' if q['auth'] else 'no'} {norm_output(q['body'], tmp)}"
                if not (asked and asked[-1] == line and q["route"].startswith("GET ")):
                    asked.append(line)
            blocks.append("== asked of Tares ==\n" + "\n".join(asked))
        side.text = "\n".join(blocks) + "\n"
        if fake is not None:  # the fake's port is different on each side
            side.text = side.text.replace(fake.url, "<TARES>")
        side.raw = "\n".join(f"$ tares factory {' '.join(s.argv)}  (exit {r['code']})\n{r['stdout']}{r['stderr']}"
                             for s, r in zip(sc.steps, results))
        return side
    finally:
        TARES_ADDR[:] = []
        if stand_in is not None:
            stand_in.stop()
        if fake is not None:
            fake.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def run_scenario(sc: Scenario, out=print) -> int:
    """Run sc on both engines, print the result, return the exit code (0 or 1)."""
    return evaluate(sc, out)[0]


def evaluate(sc: Scenario, out=print) -> tuple:
    """Run sc on both engines and print the result: (exit code, whether it passes on the Python side)."""
    out(f"== {sc.name}: {sc.about or sc.suite} ==")
    sides = {e: run_side(sc, e) for e in ENGINES}
    code = 0
    for e in ENGINES:
        s = sides[e]
        if s.skip:
            out(f"SKIP {e}: {s.skip}")
        elif s.failed:
            code = 1
            out(f"FAIL {e}: {len(s.failed)} of {len(s.failed) + len(s.passed)} checks failed")
            for label in s.failed:
                out(f"  FAIL {label}")
            out(s.raw.rstrip("\n"))
        else:
            out(f"PASS {e}: {len(s.passed)} checks")
    b, p = sides["bash"], sides["python"]
    if b.skip and not is_windows():
        # Bash is the reference. Only Windows has none: elsewhere a missing bash or jq must not turn
        # "matches bash" into "the Python engine ran".
        code = 1
        out(f"ERROR bash: the bash side cannot run on this host ({b.skip}); there is nothing to compare "
            "the Python side with")
    if not b.skip and not p.skip and not b.failed and not p.failed and b.text != p.text:
        code = 1
        out("FAIL python: output differs from bash (normalised), bash first")
        out("".join(difflib.unified_diff(b.text.splitlines(True), p.text.splitlines(True), "bash", "python")).rstrip("\n"))
    elif not b.skip and not p.skip and not b.failed and not p.failed:
        out("MATCH: bash and python print and write the same")
    return code, (not p.skip and not p.failed and code == 0)


# ---------------------------------------------------------------------------------------------
# Coverage

CHECK_LINE = re.compile(r'^\s*check\s+"((?:[^"\\]|\\.)*)"')

# Suites that only run another suite on the Python engine. They have no `check "` line of their own, so
# --coverage leaves them out of the total and prints "wrapper of <suite>"; the suite they run counts their
# checks. suite -> (the suite it runs, why). Pinned in tests/test_parity.py: a suite with checks of its own
# cannot be listed here (coverage fails on it), so a real suite cannot drop out of the count.
# claude_update_mixed is renamed claude_update_python by TR-1157, so both names are listed.
WRAPPERS = {
    "crew_python": ("crew", "runs crew_test.sh on Python; its checks count under crew"),
    "claude_update_mixed": ("claude_update", "runs claude_update_test.sh on Python; its checks count under claude_update"),
    "claude_update_python": ("claude_update", "runs claude_update_test.sh on Python; its checks count under claude_update"),
}


def suites() -> list:
    return [ln.strip() for ln in CI_LIST.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def suite_scenarios() -> dict:
    """suite -> its scenarios: the labels of its `check "<label>"` lines (see the module docstring)."""
    out = {}
    for suite in suites():
        labels = []
        for line in (HERE / f"{suite}_test.sh").read_text(encoding="utf-8").splitlines():
            m = CHECK_LINE.match(line)
            if m:
                labels.append(m.group(1))
        out[suite] = labels or [f"(the whole {suite} suite)"]
    return out


def passes_one(name: str) -> bool:
    """Whether scenario `name` passes against Python now (a worker of passes_now)."""
    return evaluate(SCENARIOS[name], out=lambda _line: None)[1]


def passes_now() -> dict:
    """{scenario name: passes against Python}, every scenario run on both engines. Scenarios share nothing (each
    has its own folder and its own fake Tares on a free port), so they run in PARITY_JOBS processes at once
    (default: the CPUs, at most 4; 1 runs them one after the other in this process)."""
    try:
        jobs = int(os.environ.get("PARITY_JOBS") or min(os.cpu_count() or 1, 4))
    except ValueError:
        jobs = 1
    names = list(SCENARIOS)
    if jobs <= 1 or len(names) <= 1:
        return {n: passes_one(n) for n in names}
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        return dict(zip(names, pool.map(passes_one, names)))


def coverage(require_full: bool, out=print) -> int:
    found = suite_scenarios()
    passing: dict = {}  # (suite, label) -> scenario name, for scenarios that pass against Python now
    bad = 0
    for suite in found:
        if suite in WRAPPERS and not found[suite][0].startswith("(the whole"):
            out(f"ERROR {suite} is listed as a wrapper but has checks of its own: {len(found[suite])}")
            bad = 1
    results = passes_now()
    for sc in SCENARIOS.values():
        ok = results[sc.name]
        for label in sc.covers:
            if label not in found.get(sc.suite, []):
                out(f"ERROR scenario {sc.name} covers a check that is not in {sc.suite}_test.sh: {label}")
                bad = 1
            elif ok:
                passing[(sc.suite, label)] = sc.name
    total = done = 0
    counted = 0
    for suite, labels in found.items():
        if suite in WRAPPERS and labels[0].startswith("(the whole"):
            of, why = WRAPPERS[suite]
            out(f"{suite}: wrapper of {of} ({why})")
            continue
        counted += 1
        n = sum(1 for label in labels if (suite, label) in passing)
        total += len(labels)
        done += n
        out(f"{suite}: {n}/{len(labels)}")
        for label in labels:
            mark = f"PASS python  ({passing[(suite, label)]})" if (suite, label) in passing else "todo"
            out(f"  {mark:<28} {label}")
    pct = 100.0 * done / total if total else 100.0
    out(f"coverage: {done} of {total} scenarios pass against Python ({pct:.1f}%) in {counted} suites")
    if require_full and done < total:
        out(f"FAIL: --require-full wants every scenario ported, {total - done} are not")
        return 1
    return bad


# >>> TR-1159 crew_test.sh checks, one scenario per group of checks (every `check "<label>"` of tests/factory/crew_test.sh)
# group g1
# crew_test.sh: the shared MCP endpoint, the desk and status, down and finish, a hand-made crew.json, an
# unrecorded builder in a second repo, and the stops when no MCP server answers.

def g1_json(r: Run, i: int):
    return json.loads(r.results[i]["stdout"])


def g1_line(r: Run, pattern: str, i: int = -1) -> bool:
    """Whether a line of step i's output matches the pattern from its start (grep -q "^...")."""
    return re.search(pattern, r.out(i), re.M) is not None


# --- the shared MCP endpoint: `settings` and `_mcp-url` --------------------------------------------------------

scenario(Scenario(
    name="crew_g1_up_endpoints",
    ignore_calls=LSP_PROBE,
    suite="crew",
    about="crew up gives every crew station the same Tares address; `settings` shows the MCP endpoint too",
    stand_in={"settings": dict(SAVED)},
    prepare=trust_crew,
    steps=[Step(["crew", "up", "--yes"], stdin=""), Step(["settings", "crew-builder-x", "builder"])],
    checks=[
        ("crew-$r reaches the same Tares",
         lambda r: all(env_of(r, "crew-" + role, "TARES_FACTORY_URL") == r.stand_in.url for role in ROLES)),
        ("the settings command shows the MCP endpoint too",
         lambda r: g1_json(r, 1)["env"]["TARES_FACTORY_MCP_URL"] == r.stand_in.mcp_url),
    ],
    covers=["crew-$r reaches the same Tares", "the settings command shows the MCP endpoint too"],
))

scenario(Scenario(
    name="crew_g1_mcp_url",
    suite="crew",
    about="_mcp-url: a local Tares has its MCP on its own port, a remote one on its own host, TARES_FACTORY_MCP_URL wins",
    steps=[
        Step(["_mcp-url"], env={"TARESD_URL": "http://localhost:8787"}),
        Step(["_mcp-url"], env={"TARESD_URL": "http://127.0.0.1:8787", "TARES_MCP_PORT": "9911"}),
        Step(["_mcp-url"], env={"TARESD_URL": "https://cell.example.com/"}),
        Step(["_mcp-url"], env={"TARES_FACTORY_MCP_URL": "http://mcp.example:1/mcp/", "TARESD_URL": "https://cell.example.com"}),
    ],
    checks=[
        ("a local Tares: MCP on its own port", lambda r: r.out(0).strip() == "http://127.0.0.1:8788/mcp"),
        ("a local Tares: TARES_MCP_PORT moves it", lambda r: r.out(1).strip() == "http://127.0.0.1:9911/mcp"),
        ("a remote Tares: MCP on its own host", lambda r: r.out(2).strip() == "https://cell.example.com/mcp"),
        ("TARES_FACTORY_MCP_URL wins", lambda r: r.out(3).strip() == "http://mcp.example:1/mcp"),
    ],
    covers=["a local Tares: MCP on its own port", "a local Tares: TARES_MCP_PORT moves it",
            "a remote Tares: MCP on its own host", "TARES_FACTORY_MCP_URL wins"],
))


# --- the desk, read from Tares ---------------------------------------------------------------------------------

G1_NO_PROJECTS = {"GET /api/crew/projects": (200, {"projects": []})}
G1_D1 = {"id": "d1", "label": "D1", "question": "Keep the old endpoint?", "recommendation": "keep it until v2 ships",
         "why": "two callers still use it", "options": ["keep it until v2 ships", "drop it now"], "blocks": "T7",
         "blocking": True, "project": "p1", "project_name": "Links App"}
G1_D2 = {"id": "d2", "label": "D2", "question": "Rename the bucket?", "recommendation": "leave it",
         "why": "nothing reads the name", "options": ["rename now", "leave it"], "blocking": False}

scenario(Scenario(
    name="crew_g1_desk_empty",
    suite="crew",
    about="an empty desk: crew desk and status both say nothing waits",
    tares={**G1_NO_PROJECTS, "GET /api/desk?state=open": (200, {"desk": []})},
    setup=_setup_views,
    steps=[Step(["crew", "desk"]), Step(["crew", "status"])],
    checks=[
        ("empty desk: crew desk says nothing waits", lambda r: "Nothing waits for you" in r.out(0)),
        ("empty desk: status says nothing waits", lambda r: g1_line(r, r"^desk: nothing waits", 1)),
    ],
    covers=["empty desk: crew desk says nothing waits", "empty desk: status says nothing waits"],
))

scenario(Scenario(
    name="crew_g1_desk_two",
    suite="crew",
    about="two questions: status shows the blocking one first with its options, crew desk shows both, desk is short for it",
    tares={**G1_NO_PROJECTS, "GET /api/desk?state=open": (200, {"desk": [G1_D1, G1_D2]})},
    setup=_setup_views,
    steps=[Step(["crew", "status"]), Step(["crew", "desk"]), Step(["desk"])],
    checks=[
        ("status counts the questions", lambda r: g1_line(r, r"^desk: 2 waiting for you", 0)),
        ("status shows the blocking one first, with its project",
         lambda r: "D1 (blocks work) · Links App: Keep the old endpoint?" in r.out(0)),
        ("status shows its options and recommendation",
         lambda r: "A) keep it until v2 ships" in r.out(0) and "B) drop it now" in r.out(0)
         and "recommended: keep it until v2 ships, since two callers still use it" in r.out(0)),
        ("status leaves out the later question", lambda r: "Rename the bucket" not in r.out(0)),
        ("crew desk shows every question",
         lambda r: "Keep the old endpoint?" in r.out(1) and "D2: Rename the bucket?" in r.out(1) and "blocks: T7" in r.out(1)),
        ("crew desk says where to answer", lambda r: "tares factory crew attach orchestrator" in r.out(1)),
        ("factory desk is short for crew desk", lambda r: r.out(2).rstrip("\n") == r.out(1).rstrip("\n")),
    ],
    covers=["status counts the questions", "status shows the blocking one first, with its project",
            "status shows its options and recommendation", "status leaves out the later question",
            "crew desk shows every question", "crew desk says where to answer", "factory desk is short for crew desk"],
))


# --- a finished project's questions, crew down, and a stopped crew's status -------------------------------------

G1_F1 = {"id": "d1", "label": "D1", "question": "No project is left. Stop the crew?", "recommendation": "yes",
         "why": "nothing to build", "project": "p9", "project_name": "finishing", "project_finished": True,
         "blocking": True, "blocks": ""}
G1_F2 = {"id": "d2", "label": "D2", "recommendation": "yes", "why": "nothing to build",
         "question": "No project is left. Stop the crew? Run `tares factory crew down` in your terminal."}
G1_F3 = {"id": "d3", "label": "D3", "question": "Nothing left to build. Stop the crew?", "recommendation": "yes",
         "why": "shorter words"}
G1_F4 = {"id": "d4", "label": "D4", "question": "Keep nightly dependency bumps?", "recommendation": "yes", "why": "cheap"}
G1_F5 = {"id": "d5", "label": "D5", "question": "Stop the crew now?", "recommendation": "yes", "why": "late",
         "project": "p9", "project_name": "finishing", "project_finished": True, "blocking": True}
G1_DESK = "GET /api/desk?state=open"


def g1_later_desk(r: Run) -> None:
    """The desk after crew down: the stop questions are withdrawn, and a new one of the finished project is asked."""
    r.tares.routes[G1_DESK] = (200, {"desk": [G1_F4, G1_F5]})


def g1_withdrawn(r: Run, qid: str, reason: str = "") -> bool:
    asks = r.tares.asked(f"POST /api/desk/{qid}/withdraw")
    return bool(asks) and (not reason or any(json.loads(a["body"]) == {"reason": reason} for a in asks))


scenario(Scenario(
    name="crew_g1_down_desk",
    suite="crew",
    about="a running crew sees a finished project's question, crew down retires all and withdraws the stop questions, "
          "and a stopped crew's status leaves the finished project's question out",
    tares={**G1_NO_PROJECTS, G1_DESK: (200, {"desk": [G1_F1, G1_F2, G1_F3, G1_F4]}),
           **{f"POST /api/desk/d{n}/withdraw": (200, {"ok": True}) for n in (1, 2, 3, 4)}},
    setup=_setup_views,
    steps=[Step(["crew", "desk"]), Step(["crew", "down"]), Step(["crew", "status"], run_before=g1_later_desk)],
    checks=[
        ("a running crew sees a finished project's question", lambda r: "finishing: No project is left" in r.out(0)),
        ("a question with nothing in blocks prints no empty blocks line",
         lambda r: re.search(r"^  blocks: *$", r.out(0), re.M) is None),
        ("stations that never reported to Tares retire without a warning", lambda r: "could not tell Tares" not in r.out(1)),
        ("down retires all", lambda r: not [s for s in _stations_now(r).values() if s.get("status") == "active"]),
        ("crew down withdraws it", lambda r: g1_withdrawn(r, "d1", "the crew stopped and the project is finished")),
        ("and the stop-the-crew question asked with no project",
         lambda r: g1_withdrawn(r, "d2", "the person stopped the crew")),
        ("and one in other words that still asks to stop the crew",
         lambda r: g1_withdrawn(r, "d3", "the person stopped the crew")),
        ("a crew-wide question about something else stays open", lambda r: not r.tares.asked("POST /api/desk/d4/withdraw")),
        ("a stopped crew's status leaves it out",
         lambda r: g1_line(r, r"^desk: %d waiting" % (len(r.tares.routes[G1_DESK][1]["desk"]) - 1), 2)
         and "Stop the crew now" not in r.out(2)),
        ("and says no stations run instead of an empty table",
         lambda r: g1_line(r, r"^no stations running; tares factory crew up starts them", 2)
         and not g1_line(r, r"^STATION ", 2)),
    ],
    covers=["a running crew sees a finished project's question",
            "a question with nothing in blocks prints no empty blocks line",
            "stations that never reported to Tares retire without a warning", "down retires all",
            "crew down withdraws it", "and the stop-the-crew question asked with no project",
            "and one in other words that still asks to stop the crew",
            "a crew-wide question about something else stays open", "a stopped crew's status leaves it out",
            "and says no stations run instead of an empty table"],
))


# --- finish takes a project off the crew's list ----------------------------------------------------------------

def g1_after_finish(r: Run) -> None:
    r.tares.routes["GET /api/crew/projects"] = (200, {"projects": [p for p in PROJECTS if p["id"] != "p2"]})


scenario(Scenario(
    name="crew_g1_finish_list",
    suite="crew",
    about="a finished project is no longer on the crew's list",
    tares={"GET /api/crew/projects": (200, {"projects": PROJECTS}), "POST /api/projects/p2/finish": (200, {"open_projects": 1})},
    setup=_setup_projects,
    steps=[Step(["finish", "shop", "all shipped"]), Step(["projects"], run_before=g1_after_finish)],
    checks=[
        ("and it is off the crew's list",
         lambda r: r.code(0) == 0 and "✓ shop is done." in r.out(0) and r.code(1) == 0
         and re.search(r"^shop ", r.out(1), re.M) is None and "Links App" in r.out(1)),
    ],
    covers=["and it is off the crew's list"],
))


# --- a hand-made crew.json -------------------------------------------------------------------------------------

def g1_hand_made(r: Run) -> None:
    r.trust(r.crew)
    r.crew.mkdir(parents=True, exist_ok=True)
    (r.crew / "crew.json").write_text('{"stations": {"reviewer": {"model": "sonnet"}}}\n', encoding="utf-8")


scenario(Scenario(
    name="crew_g1_hand_made_crewjson",
    ignore_calls=LSP_PROBE,
    suite="crew",
    about="a crew.json written by hand keeps its choice, gains the template's defaults, and a crew without a releaser starts two stations",
    stand_in={"settings": {**SAVED, "releaser": False}},
    prepare=g1_hand_made,
    steps=[Step(["crew", "up", "--yes"], stdin="")],
    checks=[
        ("hand-made crew.json keeps its choice",
         lambda r: r.crew_json()["stations"]["reviewer"].get("model") == "sonnet"
         and r.crew_json()["stations"]["reviewer"].get("effort") == "high"),
        ("hand-made crew.json gains defaults",
         lambda r: r.crew_json()["stations"]["orchestrator"].get("model") == "opus"
         and r.crew_json()["limits"]["autocompact_tokens"] == 200000),
        ("no releaser unless asked for", lambda r: r.launches() == 2 and r.station("crew-releaser", "status") == ""),
    ],
    covers=["hand-made crew.json keeps its choice", "hand-made crew.json gains defaults", "no releaser unless asked for"],
))


# --- an unrecorded builder in the second repo of a project -------------------------------------------------------

def g1_setup_adopt(tmp: Path, env: dict) -> None:
    _setup_views(tmp, env)
    for repo in ("core", "charts"):
        (tmp / "argus" / repo).mkdir(parents=True, exist_ok=True)
    agents = tmp / "claude" / "agents.json"
    rows = json.loads(agents.read_text(encoding="utf-8"))
    rows.append(_row("working", "0000beef", "argus-build-m2-charts", cwd="%s/argus/charts" % tmp, sessionId="s-beef"))  # the route's spelling ("{tmp}/argus/charts"): the engines compare the two as text
    agents.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


scenario(Scenario(
    name="crew_g1_adopt_second_repo",
    suite="crew",
    about="roster records a live builder that stations.json lacks, with its project and the repo it runs in",
    tares={"GET /api/crew/projects": (200, {"projects": [
        {"id": "p_argus", "name": "argus", "tickets_done": 0, "tickets_total": 2, "milestone": None,
         "repos": [{"path": "{tmp}/argus/core"}, {"path": "{tmp}/argus/charts"}]}]})},
    setup=g1_setup_adopt,
    steps=[Step(["roster"])],
    checks=[
        ("multi: an unrecorded builder in the second repo is adopted with its project",
         lambda r: r.station("argus-build-m2-charts", "project") == "argus"
         and r.station("argus-build-m2-charts", "repo") == "%s/argus/charts" % r.tmp),
    ],
    covers=["multi: an unrecorded builder in the second repo is adopted with its project"],
))


# --- no MCP server at the endpoint -----------------------------------------------------------------------------

G1_NOMCP = "http://127.0.0.1:9/mcp"
G1_HEALTH = {"GET /health": (200, {"status": "ok", "capabilities": ["integrations"]})}

scenario(Scenario(
    name="crew_g1_no_mcp",
    ignore_calls=LSP_PROBE,
    suite="crew",
    about="crew up stops and names the URL and the command when no MCP server answers; taresd itself is not one",
    tares=G1_HEALTH,
    prepare=trust_crew,
    steps=[Step(["crew", "up", "--yes"], env={"TARES_FACTORY_MCP_URL": G1_NOMCP}, stdin=""),
           Step(["crew", "up", "--yes"], env={"TARES_FACTORY_MCP_URL": "{tares}/mcp"}, stdin="")],
    checks=[
        ("no MCP server: crew up stops", lambda r: r.code(0) != 0 and r.launches() == 0),
        ("no MCP server: it names the URL and the command",
         lambda r: "no MCP server answers at %s" % G1_NOMCP in r.out(0) and "tares mcp --transport streamable-http" in r.out(0)),
        ("no MCP server: no token, so no token placeholder", lambda r: "TARES_AUTH_TOKEN=" not in r.out(0)),
        ("taresd itself is not an MCP server",
         lambda r: "no MCP server answers at %s/mcp" % r.tares.url in r.out(1) and r.launches() == 0),
    ],
    covers=["no MCP server: crew up stops", "no MCP server: it names the URL and the command",
            "no MCP server: no token, so no token placeholder", "taresd itself is not an MCP server"],
))
# end group g1


# group g2
# crew_test.sh, the middle of its story: roster, retire, revive and the watch, halt and resume, the guard and the
# session start. Each scenario builds its own small crew (the stations of _cc_setup, a fake Tares for the routes the
# commands read) and runs the real commands on both engines.

G2_TARES = {"GET /api/crew/settings": (200, {"settings": {"autonomy": "L3-review", "releaser": True, "release_profile": "kind",
                                                           "challenger": False, "builders_max": 3}}),
            "GET /api/crew/projects": (200, {"projects": []}), "GET /api/grants": (200, {"all": ""})}
G2_QUIET = ("GET ",)                           # reads of Tares are made in a different number and order
G2_CALLS = (("agents",), ("--version",))       # so are the reads of the session list and the version
G2_SETUP = _cc_setup(have="1.0.0", installed="1.0.0")


def g2_agent(station: str, fn):
    """A `before`: change the station's row in the fake Claude Code's session list (fn edits the row)."""
    def before(tmp: Path) -> None:
        sid = _agent_of(tmp, station)
        p = tmp / "claude" / "agents.json"
        rows = json.loads(p.read_text(encoding="utf-8"))
        for row in rows:
            if row.get("id") == sid:
                fn(row)
        p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return before


def g2_stopped(station: str):
    def edit(row):
        row["state"] = "stopped"
        row.pop("pid", None)
    return g2_agent(station, edit)


def g2_done(station: str, pid=None):
    def edit(row):
        row["state"] = "done"
        row.pop("pid", None)
        if pid is not None:
            row["pid"] = pid
    return g2_agent(station, edit)


def g2_chain(*fns):
    def before(tmp: Path) -> None:
        for fn in fns:
            fn(tmp)
    return before


def g2_earlier(r: Run, station: str) -> list:
    return _stations_now(r)[station].get("earlier") or []


def g2_resumes(r: Run) -> int:
    return sum(1 for c in r.calls() if "--resume" in c["args"])


def g2_all_done(tmp: Path) -> None:
    p = tmp / "claude" / "agents.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    for row in rows:
        row["state"] = "done"
        row.pop("pid", None)
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def g2_sessions_resumed(tmp: Path) -> int:
    try:
        lines = (tmp / "claude" / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for x in lines if x.strip() and "--resume" in json.loads(x)["args"])


def g2_mark_launches(name: str):
    def run_before(r: Run) -> None:
        r.marks[name] = r.launches()
    return run_before


def g2_scenario(name: str, about: str, steps: list, checks: list, covers: list, setup=None, **kw) -> None:
    kw.setdefault("tares", G2_TARES)
    scenario(Scenario(name=name, suite="crew", about=about, setup=setup or G2_SETUP, ignore_asked=G2_QUIET,
                      ignore_calls=G2_CALLS, steps=steps, checks=checks, covers=covers, **kw))


def g2_sid(station: str, field: str = "id"):
    """An `after`: the station's field (its session id by default) in stations.json when the step ended."""
    def after(tmp: Path):
        return json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8")).get(station, {}).get(field)
    return after


def g2_after(*fns):
    def after(tmp: Path):
        return [fn(tmp) for fn in fns]
    return after


g2_scenario(
    "crew_g2_revive",
    "the watch revives a stopped station under a new id in its own conversation; a finished session that still runs is left, "
    "one with no process is revived, and with no process anywhere every finished station is revived",
    [Step(["crew", "watch", "--once"], before=g2_stopped("crew-reviewer"),
          after=g2_after(g2_sid("crew-reviewer"), lambda tmp: g2_sessions_resumed(tmp))),
     Step(["crew", "watch", "--once"], before=g2_done("crew-reviewer", pid=4242), after=g2_sid("crew-reviewer")),
     Step(["crew", "watch", "--once"], before=g2_done("crew-reviewer"),
          after=g2_after(g2_sid("crew-reviewer"), lambda tmp: len(g2_sid("crew-reviewer", "earlier")(tmp) or []))),
     Step(["crew", "watch", "--once"], before=g2_all_done, run_before=g2_mark_launches("before"))],
    [("reviewer revived with a new id", lambda r: r.snap(0)[0] not in (None, "", "bbbb0002")),
     ("revive resumed the conversation",
      lambda r: any(c["args"][:1] == ["--bg"] and c["args"][2] == "crew-reviewer" and arg_after(c["args"], "--resume") == "old-reviewer"
                    for c in r.calls())),
     ("earlier session kept in history", lambda r: g2_earlier(r, "crew-reviewer")[0]["id"] == "bbbb0002"),
     ("revived env unchanged", lambda r: env_of(r, "crew-reviewer", "FACTORY_STATION") == "crew-reviewer"),
     ("other stations untouched", lambda r: r.snap(0)[1] == 1),
     ("a done session still running is left alone", lambda r: r.snap(1) == r.snap(0)[0]),
     ("a done session with no process is revived",
      lambda r: r.snap(2)[0] != r.snap(1) and r.snap(2)[1] == 2),
     ("with no process left anywhere, every finished station is revived",
      lambda r: r.launches() - r.marks["before"] == 3)],
    ["reviewer revived with a new id", "revive resumed the conversation", "earlier session kept in history",
     "revived env unchanged", "other stations untouched", "a done session still running is left alone",
     "a done session with no process is revived", "with no process left anywhere, every finished station is revived"])

def g2_stops(r: Run) -> int:
    return sum(1 for c in r.calls() if c["args"][:1] == ["stop"])


def g2_mark(key: str):
    def run_before(r: Run) -> None:
        r.marks[key] = (r.launches(), g2_stops(r))
    return run_before


G2_FAIL = {"FAKE_CLAUDE_AGENTS_FAIL": "1"}

g2_scenario(
    "crew_g2_unreadable",
    "a session list that cannot be read is never taken for an empty one: the watch, revive, restart and status say so and change nothing; "
    "read again, the next round revives the station",
    [Step(["crew", "watch", "--once"], env=G2_FAIL, before=g2_stopped("crew-reviewer"), run_before=g2_mark("start")),
     Step(["revive", "crew-reviewer"], env=G2_FAIL),
     Step(["restart", "crew-orchestrator"], env=G2_FAIL),
     Step(["crew", "status"], env=G2_FAIL),
     Step(["crew", "watch", "--once"], run_before=g2_mark("failed"))],
    [("unreadable session list: the watch starts nothing and carries on",
      lambda r: r.code(0) == 0 and r.marks["failed"] == r.marks["start"] and "could not read" in r.out(0)),
     ("unreadable session list: revive refuses to guess",
      lambda r: r.code(1) != 0 and r.marks["failed"] == r.marks["start"] and "could not read" in r.out(1)),
     ("unreadable session list: restart stops nothing",
      lambda r: r.code(2) != 0 and r.marks["failed"] == r.marks["start"] and "could not read" in r.out(2)),
     ("unreadable session list: status says so instead of showing every station gone",
      lambda r: "could not read" in r.out(3) and " gone " not in r.out(3)),
     ("readable again: the next round revives it", lambda r: r.launches() - r.marks["failed"][0] == 1)],
    ["unreadable session list: the watch starts nothing and carries on", "unreadable session list: revive refuses to guess",
     "unreadable session list: restart stops nothing",
     "unreadable session list: status says so instead of showing every station gone",
     "readable again: the next round revives it"])

def g2_setup_roster(tmp: Path, env: dict) -> None:
    """The lifecycle seed plus a builder of the project "Links App" and a helper of it, both with a working session."""
    _setup_life(tmp, env)
    f = tmp / "crew" / "stations.json"
    stations = json.loads(f.read_text(encoding="utf-8"))
    base = {"status": "active", "repo": "", "cwd": "", "model": "opus", "effort": "high", "project": "Links App"}
    stations["links-app-build-m1"] = {**base, "role": "builder", "id": "ffff0006"}
    stations["links-app-build-m1-rebase"] = {**base, "role": "helper", "id": "ffff0007"}
    f.write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    a = tmp / "claude" / "agents.json"
    agents = json.loads(a.read_text(encoding="utf-8"))
    agents += [_row("working", "ffff0006", "links-app-build-m1", version="2.1.310"),
               _row("working", "ffff0007", "links-app-build-m1-rebase", version="2.1.310")]
    a.write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")


G2_HELPER = "links-app-build-m1-rebase"

g2_scenario(
    "crew_g2_roster_retire",
    "roster lists a station with its project; retire tells Tares which crew's station ended, and a retired station is not revived",
    [Step(["roster"]),
     Step(["retire", G2_HELPER], run_before=g2_mark("start")),
     Step(["revive", G2_HELPER])],
    [("roster lists project",
      lambda r: any(x.startswith("links-app-build-m1\tbuilder\tLinks App\tworking") for x in r.out(0).splitlines())),
     ("retire marks the station ended on Tares",
      lambda r: r.code(1) == 0 and r.tares.asked(f"POST /api/crew/{G2_HELPER}/end?crew=crew") != []
      and "could not tell Tares" not in r.out(1)),
     ("and says which crew's station it is",
      lambda r: len(r.tares.asked(f"POST /api/crew/{G2_HELPER}/end?crew=crew")) == 1),
     ("a retired station is not revived",
      lambda r: r.launches() == r.marks["start"][0] and "was retired" in r.out(2)
      and not (r.crew / "stations" / f"{G2_HELPER}.lock").exists())],
    ["roster lists project", "retire marks the station ended on Tares", "and says which crew's station it is",
     "a retired station is not revived"],
    setup=g2_setup_roster,
    tares={**_ended(G2_HELPER, 200)})

# A command held in the background (the bash suite's `cmd &`): it runs the same engine as the step, in its own
# process group so a kill takes its whole tree down, and the scenario waits until it has started a session.
G2_ENVS: dict = {}
G2_PROCS: dict = {}
G2_NOT_WINDOWS = "the held commands and their kill are POSIX process groups"


def g2_setup(tmp: Path, env: dict) -> None:
    G2_ENVS[str(tmp)] = env
    G2_SETUP(tmp, env)


def g2_bg_launches(tmp: Path, kind: str = "bg") -> int:
    """Calls to the fake Claude Code so far: sessions started (--bg) or stopped (stop)."""
    try:
        lines = (tmp / "claude" / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for x in lines if x.strip() and (("--bg" in json.loads(x)["args"]) if kind == "bg"
                                                  else json.loads(x)["args"][:1] == ["stop"]))


def g2_hold(name: str, argv: list, extra=None, hold: bool = False, wait_for: int = 1, kind: str = "bg"):
    """A before: start `tares factory <argv>` in the background and wait until it has asked Claude Code for
    `wait_for` sessions. hold: the fake keeps registering the session until g2_release removes the hold file."""
    def before(tmp: Path) -> None:
        env = {**G2_ENVS[str(tmp)], **{k: v.replace("{tmp}", str(tmp)) for k, v in (extra or {}).items()}}
        if hold:
            (tmp / f"{name}.hold").write_text("", encoding="utf-8")
            env["FAKE_CLAUDE_BG_HOLD"] = str(tmp / f"{name}.hold")
        base = g2_bg_launches(tmp, kind)
        out = open(tmp / f"{name}.out", "w", encoding="utf-8")
        G2_PROCS[(str(tmp), name)] = subprocess.Popen(
            [sys.executable, "-m", "tares.crew"] + argv, cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True)
        for _ in range(300):
            if g2_bg_launches(tmp, kind) - base >= wait_for:
                return
            time.sleep(0.05)
        raise RuntimeError(f"{name} did not start a session")
    return before


def g2_release(name: str):
    """A before: let the held command finish and wait for it."""
    def before(tmp: Path) -> None:
        (tmp / f"{name}.hold").unlink(missing_ok=True)
        G2_PROCS[(str(tmp), name)].wait(timeout=60)
    return before


def g2_kill(name: str):
    """A before: kill the held command and everything it started, as the bash suite's killtree does."""
    def before(tmp: Path) -> None:
        import signal
        p = G2_PROCS[(str(tmp), name)]
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait(timeout=60)
    return before


def g2_held_out(r: Run, name: str) -> str:
    return (r.tmp / f"{name}.out").read_text(encoding="utf-8", errors="replace")


def g2_cut_off_lock(station: str):
    """A before: a lock folder and its takeover folder left behind by a command that died."""
    def before(tmp: Path) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        lock = tmp / "crew" / "stations" / f"{station}.lock"
        lock.mkdir(parents=True)
        Path(str(lock) + ".takeover").mkdir()
        (lock / "pid").write_text(f"{dead.pid}\n", encoding="utf-8")
    return before


def g2_lock_left(r: Run, station: str) -> bool:
    lock = r.crew / "stations" / f"{station}.lock"
    return lock.exists() or Path(str(lock) + ".takeover").exists()


g2_scenario(
    "crew_g2_lock",
    "one command at a time starts a station: revive and the watch leave a station another command is starting alone; a killed holder "
    "or a cut-off takeover does not block the next command",
    [Step(["revive", "crew-reviewer"], run_before=g2_mark("s0"),
          before=g2_chain(g2_stopped("crew-reviewer"), g2_hold("holder", ["revive", "crew-reviewer"], hold=True))),
     Step(["crew", "watch", "--once"], run_before=g2_mark("after_revive")),
     Step(["roster"], run_before=g2_mark("held"), before=g2_release("holder")),
     Step(["crew", "watch", "--once"], run_before=g2_mark("s1"),
          before=g2_chain(g2_stopped("crew-reviewer"),
                          g2_hold("killed", ["revive", "crew-reviewer"], {"FAKE_CLAUDE_BG_DELAY": "60"}), g2_kill("killed"))),
     Step(["revive", "crew-reviewer"], run_before=g2_mark("killed_done"), before=g2_stopped("crew-reviewer")),
     Step(["revive", "crew-reviewer"], run_before=g2_mark("free"),
          before=g2_chain(g2_stopped("crew-reviewer"), g2_cut_off_lock("crew-reviewer")))],
    [("revive leaves a station another command is starting alone",
      lambda r: r.marks["after_revive"][0] - r.marks["s0"][0] == 1 and "another factory command" in r.out(0)),
     ("so does the watch", lambda r: r.marks["held"][0] - r.marks["s0"][0] == 1),
     ("the command that held the station started it, once",
      lambda r: r.marks["held"][0] - r.marks["s0"][0] == 1 and "started crew-reviewer" in g2_held_out(r, "holder")),
     ("a command killed while it held a station does not block the next one",
      lambda r: r.marks["killed_done"][0] - r.marks["s1"][0] == 2),
     ("and the station is free again afterwards",
      lambda r: r.marks["free"][0] - r.marks["killed_done"][0] == 1 and "another factory command" not in r.out(4)),
     ("so is one whose takeover was cut off by a kill",
      lambda r: r.launches() - r.marks["free"][0] == 1 and not g2_lock_left(r, "crew-reviewer"))],
    ["revive leaves a station another command is starting alone", "so does the watch",
     "the command that held the station started it, once",
     "a command killed while it held a station does not block the next one", "and the station is free again afterwards",
     "so is one whose takeover was cut off by a kill"],
    setup=g2_setup, not_windows=G2_NOT_WINDOWS)

def g2_race_agents(r: Run) -> int:
    rows = json.loads((r.state / "agents.json").read_text(encoding="utf-8"))
    return sum(1 for x in rows if x.get("name") == "crew-reviewer" and "pid" in x)


def g2_cron_armed(station: str):
    """A before: the station is listed as working with no process (a schedule is armed)."""
    def edit(row):
        row["state"] = "working"
        row.pop("pid", None)
        row.pop("status", None)
    return g2_agent(station, edit)


def g2_waiting(station: str):
    def edit(row):
        row["state"] = "blocked"
        row.pop("pid", None)
        row.pop("status", None)
    return g2_agent(station, edit)


G2_SLOW = {"FAKE_CLAUDE_BG_DELAY": "3", "FACTORY_STOP_WAIT": "2"}

g2_scenario(
    "crew_g2_race",
    "a restart and the watch's revive both find the station down: the restart keeps the station, the watch is turned away and one "
    "session runs under the name; a second restart is turned away while the first stops the station",
    [Step(["crew", "watch", "--once"], env={"FAKE_CLAUDE_BG_DELAY": "3"}, run_before=g2_mark("start"),
          before=g2_hold("racer", ["restart", "crew-reviewer"], G2_SLOW, kind="stop")),
     Step(["roster"], run_before=g2_mark("raced"), before=g2_release("racer")),
     Step(["restart", "crew-reviewer"], env={"FACTORY_STOP_WAIT": "4"},
          before=g2_hold("first", ["restart", "crew-reviewer"], {"FAKE_CLAUDE_STOP_DELAY": "2", "FACTORY_STOP_WAIT": "4"},
                         kind="stop")),
     Step(["roster"], before=g2_release("first"))],
    [("a restart racing the watch: one session under the name",
      lambda r: g2_race_agents(r) == 1 and r.marks["raced"][0] - r.marks["start"][0] == 1),
     ("restart and watch: the restart started it, the watch was turned away",
      lambda r: "started crew-reviewer" in g2_held_out(r, "racer") and "another factory command" in r.out(0)),
     ("restart holds the station while it stops it", lambda r: "another factory command" in r.out(2))],
    ["a restart racing the watch: one session under the name",
     "restart and watch: the restart started it, the watch was turned away",
     "restart holds the station while it stops it"],
    setup=g2_setup, not_windows=G2_NOT_WINDOWS)

def g2_twin(station: str, new_id: str):
    """A before: a second row in the session list with the station's id changed (a duplicate session under one name)."""
    def before(tmp: Path) -> None:
        sid = _agent_of(tmp, station)
        p = tmp / "claude" / "agents.json"
        rows = json.loads(p.read_text(encoding="utf-8"))
        rows.append({**next(x for x in rows if x.get("id") == sid), "id": new_id})
        p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return before


def g2_twin_gone(new_id: str):
    def before(tmp: Path) -> None:
        p = tmp / "claude" / "agents.json"
        rows = json.loads(p.read_text(encoding="utf-8"))
        for x in rows:
            if x.get("id") == new_id:
                x.pop("pid", None)
                x.pop("status", None)
                x["state"] = "working"
        p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return before


def g2_no_twin(new_id: str):
    def before(tmp: Path) -> None:
        p = tmp / "claude" / "agents.json"
        rows = [x for x in json.loads(p.read_text(encoding="utf-8")) if x.get("id") != new_id]
        p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return before


def g2_line_has(text: str, prefix: str) -> bool:
    return any(x.startswith(prefix) for x in text.splitlines())


g2_scenario(
    "crew_g2_gone",
    "a finished station with no process reads as gone; two live sessions under one name are said and the started one is kept, a gone row "
    "is not a second session; a schedule armed keeps a stopped station listed as working: it restarts, reads as gone and is revived, "
    "one waiting on a person is left alone",
    [Step(["roster"], before=g2_done("crew-reviewer")),
     Step(["revive", "crew-reviewer"], after=g2_sid("crew-reviewer")),
     Step(["crew", "status"], before=g2_twin("crew-reviewer", "0000dup1"), after=g2_sid("crew-reviewer")),
     Step(["crew", "status"], before=g2_twin_gone("0000dup1")),
     Step(["restart", "crew-orchestrator"], env={"FAKE_CLAUDE_STOP_KEEPS_STATE": "1", "FACTORY_STOP_WAIT": "2"},
          run_before=g2_mark("cron"), before=g2_no_twin("0000dup1")),
     Step(["roster"], before=g2_cron_armed("crew-orchestrator")),
     Step(["crew", "watch", "--once"], run_before=g2_mark("armed")),
     Step(["crew", "watch", "--once"], run_before=g2_mark("revived"), before=g2_waiting("crew-orchestrator"))],
    [("roster shows a finished station with no process as gone",
      lambda r: g2_line_has(r.out(0), "crew-reviewer\treviewer\t\tgone")),
     ("status shows two live sessions under one name",
      lambda r: re.search(r"TWO SESSIONS named crew-reviewer: .*0000dup1", r.out(2)) is not None),
     ("and keeps the one it started", lambda r: r.snap(2) == r.snap(1)),
     ("a gone row under the same name is not a second session", lambda r: "TWO SESSIONS" not in r.out(3)),
     ("restart of a station with a schedule armed goes through",
      lambda r: r.marks["armed"][0] - r.marks["cron"][0] == 1 and "did not stop" not in r.out(4)),
     ("listed as working with no process: roster says gone",
      lambda r: g2_line_has(r.out(5), "crew-orchestrator\torchestrator\t\tgone")),
     ("listed as working with no process: revived", lambda r: r.marks["revived"][0] - r.marks["armed"][0] == 1),
     ("waiting on a person with no process: left alone", lambda r: r.launches() == r.marks["revived"][0])],
    ["roster shows a finished station with no process as gone", "status shows two live sessions under one name",
     "and keeps the one it started", "a gone row under the same name is not a second session",
     "restart of a station with a schedule armed goes through",
     "listed as working with no process: roster says gone", "listed as working with no process: revived",
     "waiting on a person with no process: left alone"],
    )

def g2_earlier_n(r: Run, *stations: str) -> int:
    return sum(len(g2_earlier(r, x)) for x in stations)


def g2_mark_earlier(key: str, *stations: str):
    def run_before(r: Run) -> None:
        r.marks[key] = (r.launches(), g2_earlier_n(r, *stations), [g2_earlier_n(r, x) for x in stations])
    return run_before


def g2_runs(*fns):
    def run_before(r: Run) -> None:
        for fn in fns:
            fn(r)
    return run_before


def g2_stop_many(*stations: str):
    return g2_chain(*[g2_stopped(x) for x in stations])


def g2_finish(station: str):
    """A before: the builder has finished (it leaves stations/<name>.done)."""
    def before(tmp: Path) -> None:
        (tmp / "crew" / "stations").mkdir(parents=True, exist_ok=True)
        (tmp / "crew" / "stations" / f"{station}.done").write_text("", encoding="utf-8")
    return before


G2_B = "links-app-build-m1"
G2_EXTRA = (
    (G2_B, {"role": "builder", "project": "Links App", "id": "dddd0004", "sessionId": "old-b"}),
    ("shop-build-m1", {"role": "builder", "project": "shop", "id": "eeee0005", "sessionId": "old-shop"}),
    (G2_B + "-probe", {"role": "helper", "parent": G2_B, "project": "Links App", "id": "ffff0006", "sessionId": "old-probe"}),
)

g2_scenario(
    "crew_g2_revive_fails",
    "a revive that fails is reported and the watcher carries on, the next round revives them; only the unfinished builder is revived; "
    "restart resumes the reviewer",
    [Step(["crew", "watch", "--once"], env={"FAKE_CLAUDE_FAIL_RESUME": "1"},
          run_before=g2_mark_earlier("fail", "crew-orchestrator", "crew-releaser"),
          before=g2_stop_many("crew-orchestrator", "crew-releaser")),
     Step(["crew", "watch", "--once"]),
     Step(["crew", "watch", "--once"], run_before=g2_mark_earlier("round", G2_B, "shop-build-m1"),
          before=g2_chain(g2_stop_many(G2_B, "shop-build-m1", G2_B + "-probe"), g2_finish(G2_B))),
     Step(["restart", "reviewer"], env={"FACTORY_STOP_WAIT": "2"},
          run_before=g2_runs(g2_mark_earlier("built", G2_B, "shop-build-m1"), g2_mark_earlier("rev0", "crew-reviewer")))],
    [("watcher survives a failed revive",
      lambda r: r.code(0) == 0 and r.out(0).count("could not revive") == 2),
     ("next round revives them", lambda r: g2_earlier_n(r, "crew-orchestrator", "crew-releaser") == r.marks["fail"][1] + 2),
     ("only the unfinished builder is revived",
      lambda r: r.marks["built"][0] - r.marks["round"][0] == 1
      and r.marks["built"][2][1] == r.marks["round"][2][1] + 1 and r.marks["built"][2][0] == r.marks["round"][2][0]),
     ("restart resumes the reviewer", lambda r: g2_earlier_n(r, "crew-reviewer") == r.marks["rev0"][1] + 1)],
    ["watcher survives a failed revive", "next round revives them", "only the unfinished builder is revived",
     "restart resumes the reviewer"],
    setup=_cc_setup(have="1.0.0", installed="1.0.0", extra=G2_EXTRA))

G2_DOWN = {"TARESD_URL": "http://127.0.0.1:9"}
G2_MERGE = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr merge 3 --squash"}})


def g2_hook(tmp: Path, name: str, stdin: str, extra: dict) -> str:
    """`tares hook <name>` as Claude Code runs it: the payload on stdin, the station's environment, the plugin of the checkout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_"))}
    env.update(HOME=str(tmp / "home"), USERPROFILE=str(tmp / "home"), PYTHONPATH=str(ROOT), TARES_HOME=str(tmp / "tares-home"),
               PYTHONIOENCODING="utf-8", **extra)
    shim = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"
    p = subprocess.run([sys.executable, "-c", shim, "hook", name, "--plugin-root", str(ROOT / "claude-plugin")], input=stdin,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
    return p.stdout


def g2_guard(tmp: Path) -> str:
    return g2_hook(tmp, "guard", G2_MERGE, {"FACTORY_ROLE": "builder", "FACTORY_HOME": str(tmp / "crew")})


def g2_station_env(tmp: Path, station: str) -> dict:
    doc = json.loads((tmp / "crew" / "stations" / f"{station}.settings.json").read_text(encoding="utf-8"))
    return {k: str(v).replace(" ", "_") for k, v in doc["env"].items()}


g2_scenario(
    "crew_g2_halt_down",
    "halt, resume and the roster work while Tares is down; the halt file is written; the guard refuses a merge while halted and allows it "
    "after resume",
    [Step(["crew", "halt", "tares is down"], env=G2_DOWN),
     Step(["crew", "resume"], env=G2_DOWN),
     Step(["roster"], env=G2_DOWN),
     Step(["crew", "halt", "testing"], after=lambda tmp: [(tmp / "crew" / "HALT").is_file(), g2_guard(tmp)]),
     Step(["crew", "resume"], after=g2_guard)],
    [("halt works while Tares is down", lambda r: r.code(0) == 0 and "no Tares answers" not in r.out(0)),
     ("and resume", lambda r: r.code(1) == 0 and not (r.crew / "HALT").exists()),
     ("roster works while Tares is down", lambda r: g2_line_has(r.out(2), "crew-orchestrator\torchestrator")),
     ("halt file", lambda r: r.snap(3)[0] is True),
     ("guard denies merge while halted", lambda r: "halted" in r.snap(3)[1]),
     ("guard allows merge after resume", lambda r: r.snap(4) == "")],
    ["halt works while Tares is down", "and resume", "roster works while Tares is down", "halt file",
     "guard denies merge while halted", "guard allows merge after resume"])

def g2_session_start(tmp: Path) -> str:
    return g2_hook(tmp, "session-start", json.dumps({"source": "resume"}), g2_station_env(tmp, G2_B))


g2_scenario(
    "crew_g2_session_start",
    "a revived builder's session start names its station and its project and says the session was resumed",
    [Step(["revive", G2_B], before=g2_stopped(G2_B), after=g2_session_start)],
    [("session start names the station and project",
      lambda r: G2_B in r.snap(0) and "Links_App" in r.snap(0) and "resumed" in r.snap(0))],
    ["session start names the station and project"],
    setup=_cc_setup(have="1.0.0", installed="1.0.0", extra=G2_EXTRA))
# end group g2


# group g3
# crew_test.sh: stuck stations (autocompact thrash) in `status` and in the watch, the kit each station records, and the
# kit update that moves quiet stations one per round. A crew of three stations (and a helper where the case wants one)
# on the fake Claude Code; every station runs the current kit and Claude Code, so only what the case sets is due.

G3_COMPACT = '{"type":"system","subtype":"compact_boundary"}'
G3_THRASH_OLD = ('{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"Autocompact is '
                 'thrashing: x"}]}}')
G3_PROBE = ("crew-reviewer-probe", {"role": "helper", "parent": "crew-reviewer", "id": "dddd0004", "sessionId": "old-probe"})
G3_O, G3_R, G3_L = "crew-orchestrator", "crew-reviewer", "crew-releaser"


def g3_setup(transcripts: dict | None = None, extra=()):
    """The three stations idle on the current kit and Claude Code, each with a job file holding its session id, and
    these transcripts (station -> lines)."""
    base = _cc_setup(have="1.0.0", installed="1.0.0", extra=extra)

    def setup(tmp: Path, env: dict) -> None:
        base(tmp, env)
        rows = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))
        for name, row in rows.items():
            job = tmp / "home" / ".claude" / "jobs" / row["id"]
            job.mkdir(parents=True, exist_ok=True)
            (job / "state.json").write_text(json.dumps({"sessionId": row["sessionId"]}), encoding="utf-8")
        for name, lines in (transcripts or {}).items():
            _transcript(tmp, rows[name]["sessionId"], *lines)
    return setup


def g3_edit(path: Path, fn) -> None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    fn(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def g3_id(tmp: Path, name: str) -> str:
    return json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))[name]["id"]


def g3_agent(name: str, drop: tuple = (), **fields):
    """A step's before: set these fields on the station's row of the session list, and drop these."""
    def before(tmp: Path) -> None:
        sid = g3_id(tmp, name)

        def edit(rows):
            for row in rows:
                if row.get("id") == sid:
                    row.update(fields)
                    for k in drop:
                        row.pop(k, None)
        g3_edit(tmp / "claude" / "agents.json", edit)
    return before


def g3_job(name: str, drop: tuple = (), **fields):
    """A step's before: set these fields in the station's job file (state.json), and drop these."""
    def before(tmp: Path) -> None:
        def edit(doc):
            doc.update(fields)
            for k in drop:
                doc.pop(k, None)
        g3_edit(tmp / "home" / ".claude" / "jobs" / g3_id(tmp, name) / "state.json", edit)
    return before


def g3_row(name: str, **fields):
    """A step's before: set these fields on the station's row in stations.json."""
    def before(tmp: Path) -> None:
        g3_edit(tmp / "crew" / "stations.json", lambda doc: doc[name].update(fields))
    return before


def g3_all(*fns):
    def before(tmp: Path) -> None:
        for f in fns:
            f(tmp)
    return before


def g3_older(*names: str):
    return g3_all(*[g3_row(n, kit="older") for n in names])


def g3_transcript(name: str, session: str, *lines: str):
    def before(tmp: Path) -> None:
        _transcript(tmp, session, *lines)
    return before


def g3_moved(r: Run, name: str) -> bool:
    """The station has another session than the one it began with."""
    return _stn(r, name) != _CC_IDS.get(name, G3_PROBE[1]["id"])


def g3_last(r: Run, name: str) -> list:
    """The arguments of the station's last start with --bg."""
    return _bg_of(r, name)[-1] if _bg_of(r, name) else []


def g3_state_of(r: Run, i: int, name: str) -> str:
    """The STATE column of the station's row in a status."""
    for line in r.out(i).splitlines():
        cols = line.split()
        if cols and cols[0] == name and len(cols) > 3:
            return cols[3]
    return ""


def g3_row_has(r: Run, i: int, name: str, text: str) -> bool:
    return any(line.startswith(name + " ") and text in line for line in r.out(i).splitlines())


def g3_scn(name: str, about: str, setup, steps: list, checks: list, covers: list) -> Scenario:
    return scenario(Scenario(name=name, suite="crew", about=about, setup=setup, tares=_CC_TARES, steps=steps,
                             checks=checks, covers=covers, ignore_asked=_CC_NOT_YET, ignore_calls=_CC_READS))


def g3_snap(tmp: Path) -> dict:
    """After a step: each station's session id and how many sessions were started so far."""
    rows = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))
    try:
        calls = [json.loads(x) for x in (tmp / "claude" / "calls.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        calls = []
    try:
        agents = json.loads((tmp / "claude" / "agents.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        agents = []
    return {"ids": {n: v.get("id") for n, v in rows.items()}, "launches": sum(1 for c in calls if "--bg" in c["args"]),
            "up": sorted(x.get("name") for x in agents if x.get("state") != "stopped")}


def g3_watch(before=None, env: dict | None = None) -> Step:
    return _watch_once(before=before, env=env, after=g3_snap)


def g3_was_moved(r: Run, i: int, name: str) -> bool:
    """After step i the station had another session than the one it began with."""
    return r.snap(i)["ids"][name] != _CC_IDS.get(name, G3_PROBE[1]["id"])


def g3_now(ago: int = 0) -> str:
    return datetime.fromtimestamp(time.time() - ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# status: the stuck station is told in the row; a list that fails mid-way never reads as stuck; a stopped row that kept
# its status reads as gone; the state shown is where the turn is, not the list's working.
g3_scn(
    "crew_g3_stuck_status",
    "status shows a station Claude Code stopped for thrashing, and reads its state from the turn, not from the list's working",
    g3_setup({G3_O: [ORDINARY_LINE, G3_COMPACT, THRASH_LINE], G3_R: [G3_THRASH_OLD, ORDINARY_LINE]}),
    [Step(["crew", "status"]),
     Step(["crew", "status"], env={"FAKE_CLAUDE_AGENTS_FAIL_AFTER": "2"},
          before=g3_all(g3_agent(G3_O, status="busy"), lambda tmp: (tmp / "claude" / "agents-reads").unlink(missing_ok=True))),
     Step(["crew", "status"], before=g3_agent(G3_O, state="stopped", status="busy")),
     Step(["crew", "status"], before=g3_agent(G3_O, state="working", status="idle"))],
    [("status shows the stuck station",
      lambda r: g3_row_has(r, 0, G3_O, "STUCK on autocompact")),
     ("a list that fails mid-way never reads as stuck",
      lambda r: not g3_row_has(r, 1, G3_O, "STUCK")),
     ("a stopped row that kept its status reads as gone",
      lambda r: g3_state_of(r, 2, G3_O) == "gone"),
     ("status shows where its turn is, not the agent list's working",
      lambda r: g3_state_of(r, 3, G3_O) == "done"),
     ("status leaves a recovered station alone",
      lambda r: not g3_row_has(r, 3, G3_R, "STUCK"))],
    ["status shows the stuck station", "a list that fails mid-way never reads as stuck",
     "a stopped row that kept its status reads as gone", "status shows where its turn is, not the agent list's working",
     "status leaves a recovered station alone"])

# the watch: the stuck station is stopped and started in a new conversation, once; the others are left.
g3_scn(
    "crew_g3_stuck_watch",
    "the watch restarts a station stuck on autocompact in a new conversation, only that one, records it, and not again",
    g3_setup({G3_O: [ORDINARY_LINE, G3_COMPACT, THRASH_LINE], G3_R: [G3_THRASH_OLD, ORDINARY_LINE]}),
    [g3_watch(env={"FACTORY_STOP_WAIT": "2"}, before=g3_agent(G3_O, state="working", status="idle")), g3_watch()],
    [("thrashing station restarted",
      lambda r: g3_moved(r, G3_O) and "crew-orchestrator is stuck" in r.out(0)),
     ("restarted in a new conversation",
      lambda r: "--resume" not in g3_last(r, G3_O) and "in a new conversation" in json.dumps(g3_last(r, G3_O))),
     ("only the stuck station restarted",
      lambda r: r.launches() == 1 and not g3_moved(r, G3_R)),
     ("thrash recorded",
      lambda r: any('"event":"thrashing"' in x and G3_O in x for x in _events(r).replace(" ", "").splitlines())
      and "restarted-fresh" in _events(r)),
     ("the new conversation is not restarted again",
      lambda r: "is stuck" not in r.out(1))],
    ["thrashing station restarted", "restarted in a new conversation", "only the stuck station restarted",
     "thrash recorded", "the new conversation is not restarted again"])

# the watch leaves alone a station woken after the notice, one working on a new message, one busy by the list's
# status, and one whose session was cleared; a stopped stuck one comes back fresh; a stuck helper is left to its parent.
g3_scn(
    "crew_g3_stuck_left_alone",
    "a station woken after the thrash notice, working, busy by status or cleared is not restarted; a stopped one comes back fresh; a helper is left",
    g3_setup({G3_L: [G3_THRASH_OLD, WOKEN_LINE], "crew-reviewer-probe": [G3_THRASH_OLD]},
             extra=[("crew-reviewer-probe", {**G3_PROBE[1]})]),
    [g3_watch(env={"FACTORY_STOP_WAIT": "2"},
              before=g3_all(g3_agent(G3_L, drop=("status",), state="done"),
                            g3_agent("crew-reviewer-probe", drop=("status",), state="done"))),
     g3_watch(before=g3_all(g3_transcript(G3_L, "old-releaser", G3_THRASH_OLD), g3_agent(G3_L, state="working"))),
     g3_watch(before=g3_agent(G3_L, state="done", status="busy")),
     g3_watch(before=g3_all(g3_agent(G3_L, drop=("status",), state="done"), g3_job(G3_L, sessionId="session-cleared"))),
     g3_watch(before=g3_all(g3_job(G3_L, sessionId="old-releaser", state="working"),
                            g3_agent(G3_L, drop=("pid",), state="stopped")))],
    [("a station woken after the notice is left alone", lambda r: not g3_was_moved(r, 0, G3_L) and r.snap(0)["launches"] == 0),
     ("a station working on a new message is left alone", lambda r: not g3_was_moved(r, 1, G3_L) and r.snap(1)["launches"] == 0),
     ("busy by the list's status: left alone, whatever its state says",
      lambda r: not g3_was_moved(r, 2, G3_L) and r.snap(2)["launches"] == 0),
     ("a cleared session is not restarted", lambda r: not g3_was_moved(r, 3, G3_L) and r.snap(3)["launches"] == 0),
     ("a stopped stuck station comes back fresh",
      lambda r: g3_was_moved(r, 4, G3_L) and "--resume" not in g3_last(r, G3_L) and r.snap(4)["launches"] == 1),
     ("a stuck helper is not restarted", lambda r: not g3_was_moved(r, 4, "crew-reviewer-probe"))],
    ["a station woken after the notice is left alone", "a station working on a new message is left alone",
     "busy by the list's status: left alone, whatever its state says", "a cleared session is not restarted",
     "a stopped stuck station comes back fresh", "a stuck helper is not restarted"])

# the kit a station starts on is recorded in stations.json.
scenario(Scenario(
    name="crew_g3_kit_recorded",
    suite="crew",
    about="a station that is started has the kit it runs on in stations.json",
    driver=LAUNCH_DRIVER,
    steps=[_LIFE_LAUNCH],
    checks=[("stations record the kit they run on", lambda r: r.code(0) == 0 and r.station("crew-reviewer", "kit") != "")],
    covers=["stations record the kit they run on"],
))

# the kit update: status counts the stations on an older kit, and the watch moves one quiet station a round.
g3_scn(
    "crew_g3_kit_moves",
    "stations on an older kit: status counts them; the watch moves one quiet station a round, in its conversation, told why; busy ones wait",
    g3_setup(),
    [Step(["crew", "status"], before=g3_older(G3_O, G3_R, G3_L)),
     g3_watch(before=g3_all(
         g3_agent(G3_O, state="working", status="busy"),
         g3_agent(G3_R, state="working", status="idle"),
         g3_job(G3_R, inFlight={"tasks": 2, "queued": 0, "kinds": ["local_agent", "session_cron"]}),
         g3_agent(G3_L, state="working", status="waiting"),
         g3_job(G3_L, state="working", updatedAt=g3_now()))),
     g3_watch(before=g3_job(G3_R, inFlight={"tasks": 1, "queued": 0, "kinds": ["session_cron"],
                                           "wake": {"at": 1, "keepalive": True}})),
     g3_watch(before=lambda tmp: (g3_job(G3_R, inFlight={"tasks": 1, "queued": 0, "kinds": ["session_cron"]})(tmp),
                                  g3_job(G3_L, updatedAt=g3_now(3600))(tmp))),
     g3_watch()],
    [("status counts stations on an older kit",
      lambda r: any(x.startswith("plugin: 3 station(s) run an older version") for x in r.out(0).splitlines())),
     ("a working station waits", lambda r: r.code(1) == 0 and not g3_was_moved(r, 1, G3_O)),
     ("a station with a subagent running waits", lambda r: not g3_was_moved(r, 1, G3_R)),
     ("a station waiting on a person waits", lambda r: not g3_was_moved(r, 1, G3_L) and r.snap(1)["launches"] == 0),
     ("a station waiting for its own wake-up waits", lambda r: not g3_was_moved(r, 2, G3_R) and r.snap(2)["launches"] == 0),
     ("one quiet station moves per round (a cron doesn't count)",
      lambda r: int(g3_was_moved(r, 3, G3_R)) + int(g3_was_moved(r, 3, G3_L)) == 1),
     ("then the next, after half an hour of waiting on a person",
      lambda r: g3_was_moved(r, 4, G3_R) and g3_was_moved(r, 4, G3_L)),
     ("in the same conversation, told why",
      lambda r: arg_after(g3_last(r, G3_R), "--resume") == "old-reviewer"
      and "Tares plugin was updated" in " ".join(g3_last(r, G3_L))),
     ("the working station still waits", lambda r: not g3_was_moved(r, 4, G3_O))],
    ["status counts stations on an older kit", "a working station waits", "a station with a subagent running waits",
     "a station waiting on a person waits", "a station waiting for its own wake-up waits",
     "one quiet station moves per round (a cron doesn't count)", "then the next, after half an hour of waiting on a person",
     "in the same conversation, told why", "the working station still waits"])

# Nothing moves while the crew is halted, and a station with no conversation to resume is not moved.
g3_scn(
    "crew_g3_kit_holds",
    "a halted crew moves no station onto a newer kit; neither is one with no conversation to resume",
    g3_setup(),
    [Step(["crew", "halt", "testing"], before=g3_all(g3_older(G3_O), g3_agent(G3_O, state="working", status="idle"))),
     g3_watch(),
     Step(["crew", "resume"]),
     g3_watch(before=g3_row(G3_O, sessionId=""))],
    [("nothing moves while halted", lambda r: r.code(0) == 0 and not g3_was_moved(r, 1, G3_O) and r.snap(1)["launches"] == 0),
     ("a station with no conversation to resume isn't moved",
      lambda r: r.code(2) == 0 and not g3_was_moved(r, 3, G3_O) and r.snap(3)["launches"] == 0)],
    ["nothing moves while halted", "a station with no conversation to resume isn't moved"])

# A kit that cannot start a station takes down that one, not the crew; it comes back once the kit works.
g3_scn(
    "crew_g3_kit_failing",
    "a kit that cannot resume a station takes down one station at most, and they come back once it works",
    g3_setup(),
    [g3_watch(env={"FAKE_CLAUDE_FAIL_RESUME": "1"},
              before=g3_all(g3_older(G3_R, G3_L), g3_agent(G3_R, drop=("status",), state="done"),
                            g3_agent(G3_L, drop=("status",), state="done"))),
     g3_watch(env={"FAKE_CLAUDE_FAIL_RESUME": "1"}),
     g3_watch(env={"FAKE_CLAUDE_FAIL_RESUME": "1"}),
     g3_watch(),
     Step(["roster"])],
    [("a failing kit takes down one station at most",
      lambda r: sum(1 for n in (G3_R, G3_L) if n in r.snap(2)["up"]) >= 1),
     ("it comes back once the kit works",
      lambda r: any(x.startswith(G3_R) for x in r.out(4).splitlines()) and any(x.startswith(G3_L) for x in r.out(4).splitlines()))],
    ["a failing kit takes down one station at most", "it comes back once the kit works"])

# end group g3


# group g4
# crew_test.sh, "crew up starts the watch in the background, once; status says so; crew down stops it": real
# `crew up` runs on the stand-in Tares with the watch left on (FACTORY_NO_WATCH empty), a one second round and a short
# nap tick so the Python watch sees its stop file at once.

# FACTORY_PLAN_EVERY=0: the bash watch reads the plan's limits (/usage) between rounds and answers the console's /context asks;
# the Python watch does not do that yet (no serve_readings in tares/crew/watch.py), so the scenarios here leave it out on both sides.
G4_WATCH_ENV = {"FACTORY_NO_WATCH": "", "FACTORY_WATCH_INTERVAL": "1", "FACTORY_ASK_POLL": "0.2", "FACTORY_PLAN_EVERY": "0",
                "TARES_FACTORY_MCP_URL": "{tares}/mcp"}


def g4_alive(pid) -> bool:
    try:
        import psutil
        return bool(pid) and psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def g4_pid(tmp: Path) -> int:
    try:
        return int((tmp / "crew" / "watch.pid").read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return 0


def g4_wait(seconds: float, ok) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ok():
            return True
        time.sleep(0.1)
    return ok()


def g4_up_after(tmp: Path) -> dict:
    """After a crew up: the watch's pid, whether it runs, and whether its log has the banner (given up to 6 seconds)."""
    pid = g4_pid(tmp)
    log = tmp / "crew" / "watch.log"
    banner = g4_wait(6, lambda: log.is_file() and "watching the crew" in log.read_text(encoding="utf-8", errors="replace"))
    return {"pid": pid, "alive": g4_alive(pid), "log": banner}


G4_UP_TARES = {"GET /health": (200, {"status": "ok", "capabilities": ["integrations"]}),
               "GET /api/crew/settings": (200, {**SAVED, "releaser": False}),
               "GET /api/people": (200, {"people": [{"id": "local", "name": "Local"}]}),
               "GET /api/grants": (200, {"all": ""}),
               "GET /api/crew/projects": (200, {"projects": []}), "GET /api/desk": (200, {"desk": []}),
               "GET /api/desk?state=open": (200, {"desk": []}),
               "POST /mcp": (200, {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "fake-tares"}}})}


def g4_quiet(tmp: Path) -> None:
    """After the last step, the watch is gone: its log is by design not the same (the Python watch is asked to stop with a
    file and says so, the bash watch is killed), so the log leaves the comparison; what it said was read by an earlier step."""
    try:
        (tmp / "crew" / "watch.log").unlink()
    except OSError:
        pass


scenario(Scenario(
    name="crew_g4_up_watch",
    suite="crew",
    about="crew up starts the watch in the background and logs; a second crew up keeps it; crew down stops it; --no-watch starts none",
    ignore_calls=LSP_PROBE + _CC_READS,
    tares=G4_UP_TARES,
    # the live watch polls Tares every round, so the reads and the claude-retry write it makes depend on timing
    ignore_asked=("GET ", "PUT /api/crew/claude-retry?crew=crew"),
    prepare=trust_crew,
    steps=[Step(["crew", "up", "--yes"], env=G4_WATCH_ENV, after=g4_up_after),
           Step(["crew", "up", "--yes"], env=G4_WATCH_ENV, after=lambda tmp: {"pid": g4_pid(tmp)}),
           Step(["crew", "status"], env=G4_WATCH_ENV),
           Step(["crew", "down"], env=G4_WATCH_ENV),
           Step(["crew", "up", "--yes", "--no-watch"], env=G4_WATCH_ENV),
           Step(["crew", "down"], env=G4_WATCH_ENV, after=g4_quiet)],
    checks=[
        ("crew up starts the watch in the background",
         lambda r: "started the watch" in r.out(0) and bool(r.snap(0)["pid"]) and r.snap(0)["alive"]),
        ("it writes to its log", lambda r: r.snap(0)["log"]),
        ("a second crew up keeps the one watch",
         lambda r: "already running, pid %s" % r.snap(0)["pid"] in r.out(1) and r.snap(1)["pid"] == r.snap(0)["pid"]),
        # crew_test.sh puts this check after a `;` on its line, so the harness's line count (which wants `check "` first) skips it
        ("status says the watch runs", lambda r: any(ln.startswith("watch: running (pid %s)" % r.snap(0)["pid"]) for ln in r.out(2).splitlines())),
        ("crew down stops the watch",
         lambda r: not g4_alive(r.snap(0)["pid"]) and not (r.crew / "watch.pid").exists() and "stopped the watch" in r.out(3)),
        ("--no-watch starts none", lambda r: "the watch" not in r.out(4) and not (r.crew / "watch.pid").exists()),
    ],
    covers=["crew up starts the watch in the background", "it writes to its log", "a second crew up keeps the one watch",
            "crew down stops the watch", "--no-watch starts none"],  # "status says the watch runs" is asserted too (not a counted line)
))

# crew_test.sh, "A permission the person allows in the console is a grant on Tares": one story on the launch driver,
# a reviewer, an orchestrator and two builders of the project shop, each launched with the grants of the world, then
# the world gets a project grant, a grant for a helper and a grant for every project.
G4_MERGE = "shop-build-m1 may run gh pr merge 12 --squash in shop"
G4_E2E = "shop-build-m1-e2e may run npm run e2e in shop"
G4_PROJECT = '- "%s" (2026-02-04)\n' % G4_MERGE
G4_HELPER = '- "%s" (2026-02-05)\n' % G4_E2E
G4_BUSY = ("crew-orchestrator", "crew-reviewer", "shop-build-m1", "shop-build-m2")
G4_W0 = lworld("L3-review", grants=LAUNCH_GRANT)
G4_W1 = lworld("L3-review", grants=LAUNCH_GRANT, project_grants={"shop": G4_PROJECT})
G4_W2 = lworld("L3-review", grants=LAUNCH_GRANT, project_grants={"shop": G4_PROJECT + G4_HELPER})
G4_W3 = lworld("L3-review", grants=LAUNCH_GRANT + REGRANT_GRANT, project_grants={"shop": G4_PROJECT + G4_HELPER})


def g4_recorded(tmp: Path) -> dict:
    """What a station launched with is kept: the row's grants and the grants file."""
    row = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))["shop-build-m1"]
    kept = (tmp / "crew" / "stations" / "shop-build-m1.grants").read_text(encoding="utf-8")
    return {"row": bool(row.get("grants")), "file": "staging database" in kept, "session": row.get("sessionId")}


def g4_recording(step: Step) -> Step:
    step.after = g4_recorded
    return step


def g4_prompt(r: "Run", station: str) -> str:
    """The prompt of a station's first restart (its second start)."""
    return _bg_of(r, station)[1][-1]


def g4_started_and_differs(tmp: Path) -> dict:
    """The sessions started so far, and whether the busy orchestrator's grants differ from the reviewer's."""
    rows = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))
    return {"started": _sessions_started(tmp), "differs": rows["crew-orchestrator"]["grants"] != rows["crew-reviewer"]["grants"]}


scenario(Scenario(
    name="crew_g4_regrant",
    suite="crew",
    about="a permission allowed in Tares reaches a station between turns, in its conversation, once; busy and attached stations wait; helpers and blocked stations get it",
    driver=LAUNCH_DRIVER,
    not_windows=WIN_NO_MULTILINE_REGRANT,
    steps=[_launch("crew-orchestrator", "orchestrator", G4_W0),                                            # 0
           _launch("crew-reviewer", "reviewer", G4_W0),                                                    # 1
           g4_recording(_launch("shop-build-m1", "builder", G4_W0, parent="crew-orchestrator", project="shop")),   # 2
           _launch("shop-build-m2", "builder", G4_W0, parent="crew-orchestrator", project="shop"),         # 3
           wstep(G4_W1, before=_busy(*G4_BUSY)),                                                           # 4: busy: waits
           wstep(G4_W1, before=_both(_idle("shop-build-m1", "crew-reviewer"), _busy("shop-build-m2")),
                 after=_sessions_and("shop-build-m1")),                                                    # 5: m1 restarted
           wstep(G4_W1, before=_idle("shop-build-m1")),                                                    # 6: once per change
           _launch("shop-build-m1-e2e", "helper", G4_W1, parent="shop-build-m1", project="shop"),          # 7
           wstep(G4_W2, before=_both(_idle("shop-build-m1-e2e", "shop-build-m2"), _attach("shop-build-m2"))),   # 8
           wstep(G4_W2, before=_leave("shop-build-m2")),                                                   # 9: they left
           wstep({**G4_W3, "grants_status": 500}, before=_idle("crew-reviewer")),                          # 10: unreadable
           wstep(G4_W3, before=_both(_idle("crew-reviewer"), _halted)),                                    # 11: halted
           wstep(G4_W3, before=_both(_resumed, _waiting("crew-reviewer")), after=g4_started_and_differs)],  # 12: blocked on a person
    checks=[
        ("stations record the grants they started with", lambda r: r.snap(2)["row"] is True and r.snap(2)["file"] is True),
        ("a busy station waits for its new grant", lambda r: r.code(4) == 0 and r.snap(4) == 4 and "new permissions" not in r.out(4)),
        ("between turns it is restarted with the grant",
         lambda r: r.code(5) == 0 and r.snap(5)["started"] == 5 and "shop-build-m1 has new permissions from Tares" in r.out(5)
         and '"event":"regranted"' in _events(r).replace(" ", "")),
        ("in the same conversation", lambda r: bool(r.snap(2)["session"]) and arg_after(_bg_of(r, "shop-build-m1")[1], "--resume") == r.snap(2)["session"]),
        ("its fresh settings have the grant",
         lambda r: any(('"%s"' % G4_MERGE) in a and "for project shop, in their own words" in a for a in r.snap(5)["allow"])),
        ("its prompt quotes the new grant and says to run the refused command again",
         lambda r: "allowed new permissions in Tares" in g4_prompt(r, "shop-build-m1") and ('"%s"' % G4_MERGE) in g4_prompt(r, "shop-build-m1")
         and "run it again now" in g4_prompt(r, "shop-build-m1")),
        ("and only the new one", lambda r: "staging database" not in g4_prompt(r, "shop-build-m1")),
        ("a busy builder of the same project still waits", lambda r: "shop-build-m2 has new permissions" not in r.out(5)),
        ("a station whose grants did not change is left alone",
         lambda r: "crew-reviewer has new permissions" not in r.out(5) and r.snap(5)["started"] == 5),
        ("once per change", lambda r: r.code(6) == 0 and r.snap(6) == 5),
        ("a live helper is restarted with its grant",
         lambda r: "shop-build-m1-e2e has new permissions" in r.out(8) and any(G4_E2E in a for a in allow_of(r, "shop-build-m1-e2e"))),
        ("a station open in a terminal waits", lambda r: "shop-build-m2 has new permissions" not in r.out(8)),
        ("and gets it once they leave",
         lambda r: "shop-build-m2 has new permissions" in r.out(9) and any(G4_E2E in a for a in allow_of(r, "shop-build-m2"))),
        ("grants that can't be read restart nothing", lambda r: r.code(10) == 0 and r.snap(10) == r.snap(9) and "new permissions" not in r.out(10)),
        ("nothing restarts while halted", lambda r: r.code(11) == 0 and r.snap(11) == r.snap(9) and "new permissions" not in r.out(11)),
        ("a station blocked on a person gets the grant at once",
         lambda r: "crew-reviewer has new permissions" in r.out(12)
         and any('for all projects, in their own words: "the reviewer may label pull requests"' in a for a in allow_of(r, "crew-reviewer"))),
        ("busy crew stations still wait", lambda r: r.snap(12)["differs"] and "crew-orchestrator has new permissions" not in r.out(12)),
    ],
    covers=["stations record the grants they started with", "a busy station waits for its new grant",
            "between turns it is restarted with the grant", "in the same conversation", "its fresh settings have the grant",
            "its prompt quotes the new grant and says to run the refused command again", "and only the new one",
            "a busy builder of the same project still waits", "a station whose grants did not change is left alone",
            "once per change", "a live helper is restarted with its grant", "a station open in a terminal waits",
            "and gets it once they leave", "grants that can't be read restart nothing", "nothing restarts while halted",
            "a station blocked on a person gets the grant at once", "busy crew stations still wait"],
))

# crew_test.sh, the same story seen from the person's side: `crew desk` shows a refused command and where to allow it,
# and `crew status` says a station has new permissions waiting, or waits for the person to leave its session.
G4_ITEM = {"label": "D1", "blocking": False, "project_name": "shop", "station": "shop-build-m1", "command": "gh pr merge 12 --squash",
           "question": "shop-build-m1 was refused a command. Allow it?", "recommendation": G4_MERGE, "why": "it merges its own pull request"}
G4_VIEWS_TARES = {"GET /api/desk?state=open": (200, {"desk": [G4_ITEM]}),
                  "GET /api/grants?project=shop": (200, {"all": "", "project": G4_PROJECT}),
                  "GET /api/grants": (200, {"all": ""}),
                  "GET /api/crew/projects": (200, {"projects": []})}


def g4_setup_views(tmp: Path, env: dict) -> None:
    """The views' crew with two shop builders, both idle, each with a conversation and the grants it started with."""
    _setup_views(tmp, env)
    p = tmp / "crew" / "stations.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    for name in ("shop-build-m1", "shop-build-m2"):
        rows[name].update(sessionId="sess-" + name[-2:], grants="1")
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    a = tmp / "claude" / "agents.json"
    agents = [x for x in json.loads(a.read_text(encoding="utf-8")) if x["id"] != "cccc0003"]
    agents += [_row("idle", "cccc0003", "shop-build-m1", version="2.1.310"), _row("idle", "dddd0004", "shop-build-m2", version="2.1.310")]
    a.write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")


def g4_line(r: "Run", i: int, station: str) -> str:
    return next((x for x in r.out(i).splitlines() if x.startswith(station + " ")), "")


scenario(Scenario(
    name="crew_g4_views",
    suite="crew",
    about="crew desk shows the refused command and where to allow it; crew status says a station has new permissions waiting, or waits until someone leaves its session",
    setup=g4_setup_views,
    tares=G4_VIEWS_TARES,
    steps=[Step(["crew", "desk"]),
           Step(["crew", "status"], before=_attach("shop-build-m2")),
           Step(["crew", "status"], before=_leave("shop-build-m2"))],
    checks=[
        ("crew desk shows the refused command and where to allow it",
         lambda r: "shop-build-m1 was refused: gh pr merge 12 --squash" in r.out(0) and "allow it in Tares" in r.out(0)),
        ("status says it has new permissions waiting",
         lambda r: "NEW PERMISSIONS: the watch restarts it" in g4_line(r, 1, "shop-build-m1")
         and "NEW PERMISSIONS" in g4_line(r, 2, "shop-build-m2")),
        ("status says it waits for you to leave it", lambda r: "once you leave its session" in g4_line(r, 1, "shop-build-m2")),
    ],
    covers=["crew desk shows the refused command and where to allow it", "status says it has new permissions waiting",
            "status says it waits for you to leave it"],
))

# crew_test.sh: a running watch whose plugin is updated starts again on the new version, once the update has settled.
# The bash suite runs `crew watch` in the foreground from a copy of the kit; a step here cannot be left running, so the
# watch is the one crew up starts, from a copy of the kit, and the update is a line added to a skill of the copy.
def g4_setup_kit(tmp: Path, env: dict) -> None:
    _cc_plugin(tmp, env)


def g4_banners(tmp: Path) -> int:
    try:
        return (tmp / "crew" / "watch.log").read_text(encoding="utf-8", errors="replace").count("watching the crew")
    except OSError:
        return 0


def g4_first_banner(tmp: Path) -> dict:
    g4_wait(6, lambda: g4_banners(tmp) >= 1)
    time.sleep(1.5)   # the first round has read the kit, so the line below is a change of it
    return {"banners": g4_banners(tmp)}


def g4_update_kit(tmp: Path) -> None:
    with open(tmp / "kit" / "claude-plugin" / "skills" / "factory-protocol" / "SKILL.md", "a", encoding="utf-8") as f:
        f.write("# updated\n")


def g4_restarted(tmp: Path) -> dict:
    g4_wait(25, lambda: g4_banners(tmp) >= 2)
    log = (tmp / "crew" / "watch.log").read_text(encoding="utf-8", errors="replace")
    return {"banners": g4_banners(tmp), "said": "the plugin was updated" in log, "pid": g4_pid(tmp), "alive": g4_alive(g4_pid(tmp))}


scenario(Scenario(
    name="crew_g4_watch_update",
    suite="crew",
    about="a watch that crew up started restarts itself on the new version when the plugin is updated",
    ignore_calls=LSP_PROBE + _CC_READS,
    tares=G4_UP_TARES,
    ignore_asked=("GET ", "PUT /api/crew/claude-retry?crew=crew"),
    prepare=trust_crew,
    setup=g4_setup_kit,
    steps=[Step(["crew", "up", "--yes"], env=G4_WATCH_ENV, after=g4_first_banner),
           Step(["crew", "status"], env=G4_WATCH_ENV, before=g4_update_kit, after=g4_restarted),
           Step(["crew", "down"], env=G4_WATCH_ENV, after=g4_quiet)],
    checks=[
        ("the watch restarts itself on an updated plugin",
         lambda r: r.snap(0)["banners"] == 1 and r.snap(1)["said"] and r.snap(1)["banners"] >= 2 and r.snap(1)["alive"]),
    ],
    covers=["the watch restarts itself on an updated plugin"],
))
# end group g4
# <<< TR-1159 crew_test.sh checks


# settings_*: `settings <station> [role]` prints the settings file a station launches with (cmd_settings, TR-1122).
# The file itself is checked by launch_settings_*; here the command: which role it takes and what it prints.

SETTINGS_TARES = {"GET /api/crew/settings": (200, {"settings": {"autonomy": "L4-ship", "releaser": True, "release_profile": "kind",
                                                                 "challenger": False, "builders_max": 3}}),
                  "GET /api/crew/projects": (200, {"projects": []}), "GET /api/grants": (200, {"all": ""})}


SETTINGS_ENV = {"TARESD_URL": "{tares}"}


def _printed(r: "Run", i: int) -> dict:
    return json.loads(r.results[i]["stdout"])


scenario(Scenario(
    name="settings_station",
    suite="tidy",
    about="settings prints the file a station launches with: the role of a role word, of stations.json, or the one given",
    setup=_setup_views,
    tares=SETTINGS_TARES,
    steps=[Step(["settings", "crew-reviewer"], env=SETTINGS_ENV), Step(["settings", "orchestrator"], env=SETTINGS_ENV),
           Step(["settings", "crew-reviewer", "releaser"], env=SETTINGS_ENV), Step(["settings", "crew-releaser"], env=SETTINGS_ENV),
           Step(["settings", "old-helper"], env=SETTINGS_ENV)],
    checks=[
        ("settings prints the file a station launches with",
         lambda r: r.code(0) == 0 and _printed(r, 0)["env"]["FACTORY_STATION"] == "crew-reviewer"
         and _printed(r, 0)["env"]["FACTORY_ROLE"] == "reviewer"
         and _printed(r, 3) == station_json(r, "crew-releaser")),
        ("a role word names this crew's station", lambda r: r.code(1) == 0 and _printed(r, 1)["env"]["FACTORY_STATION"] == "crew-orchestrator"),
        ("a second word is the role", lambda r: r.code(2) == 0 and _printed(r, 2)["env"]["FACTORY_ROLE"] == "releaser"),
        ("a station with no record takes its role from its name", lambda r: r.code(3) == 0 and _printed(r, 3)["env"]["FACTORY_ROLE"] == "releaser"),
        ("a recorded station takes the role stations.json holds", lambda r: r.code(4) == 0 and _printed(r, 4)["env"]["FACTORY_ROLE"] == "helper"),
    ],
    covers=[],
))

scenario(Scenario(
    name="settings_tidy",
    suite="tidy",
    about="every crew station may run factory tidy: the orchestrator, reviewer and releaser settings allow it",
    setup=_setup_views,
    tares=SETTINGS_TARES,
    steps=[Step(["settings", role], env=SETTINGS_ENV) for role in ("orchestrator", "reviewer", "releaser")],
    checks=[("$s may run factory tidy",
             lambda r: all(r.code(i) == 0 and "Bash(factory tidy *)" in _printed(r, i)["permissions"]["allow"] for i in range(3)))],
    covers=["$s may run factory tidy"],
))


# ---------------------------------------------------------------------------------------------
# crew_update_* and cc_retry_* (TR-964): `crew update` and `crew restart` as commands, and the retry back-off of a
# Claude Code move that lands on an old spare. The source is claude_update_test.sh. The stations are in stations.json
# and the sessions in the fake Claude Code's agents.json; a session the command starts gets its version from a one-shot
# hook of the fake (on-bg), which sets it in agents.json and arms itself again, since the Python engine reads the
# version from the session list and has no process list to look at. The versions come in turn from spawn-seq (the last
# one stays). Tares is a fake with the routes a launch reads, and the PUT of the retry list.

CU_OLD, CU_NEW = "2.1.100", "2.1.200"
CU_RETRY = "PUT /api/crew/claude-retry?crew=crew"
CU_SETTINGS = {"autonomy": "L4-ship", "releaser": True, "release_profile": "kind", "challenger": False, "builders_max": 3}
CU_TARES = {"GET /api/crew/settings": (200, {"settings": CU_SETTINGS}), "GET /api/crew/projects": (200, {"projects": []}),
            "GET /api/grants": (200, {"all": ""}), CU_RETRY: (200, {"ok": True})}
CU_ENV = {"TARESD_URL": "{tares}", "FACTORY_UPDATE_POLL": "1", "FACTORY_STOP_WAIT": "2"}
# bash reads GET /api/crew/pace before it starts a session; the Python pace is a stub until TR-962, so the
# read is left out of the list of what was asked (the pace itself is not what these scenarios test).
# Reads of Tares are left out, as in the cc_move scenarios: the engines make them in a different number and order
# (the pace is real in Python since TR-962). What is written to Tares, started and stopped is compared.
CU_NO_PACE = ("GET ",)
CU_NO_HOOK = "the new session's version is set by the fake Claude Code's on-bg hook, which runs through bash"

CU_HOOK = '''import json, os
S = os.environ["FAKE_CLAUDE_STATE"]
me = os.environ["FAKE_ID"]
seq = open(S + "/spawn-seq").read().split()
v = seq.pop(0) if len(seq) > 1 else seq[0]
if seq:
    open(S + "/spawn-seq", "w").write(" ".join(seq) + "\\n")
rows = json.load(open(S + "/agents.json"))
for r in rows:
    if r["id"] == me:
        r["version"] = v
        r["status"] = "idle"
json.dump(rows, open(S + "/agents.json", "w"), indent=2)
if os.path.exists(S + "/halt-after-first"):
    os.unlink(S + "/halt-after-first")
    open(os.environ["FACTORY_CREW_HOME"] + "/HALT", "w").write('{"reason": "mid"}')
open(S + "/on-bg", "w").write(open(S + "/on-bg-cmd").read())
'''

CU_STATIONS = {
    "crew-orchestrator": {"role": "orchestrator", "id": "aaaa0001", "sessionId": "sess-o"},
    "crew-reviewer": {"role": "reviewer", "id": "bbbb0002", "sessionId": "sess-r"},
}


def _cu_station(tmp: Path, **over) -> dict:
    row = {"status": "active", "parent": "", "project": "", "repo": "", "cwd": str(tmp / "work"), "model": "opus", "effort": "high"}
    row.update(over)
    return row


def _cu_world(stations=None, installed: str = CU_NEW, runs: str = CU_OLD, spawn: str = CU_NEW, states=None):
    """A setup hook: the crew's stations (each with its session `runs`, idle unless `states` says otherwise), the
    installed Claude Code, and the versions the sessions it starts get (spawn: words in turn)."""
    def setup(tmp: Path, env: dict) -> None:
        crew, state = tmp / "crew", tmp / "claude"
        (crew / "stations").mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        (tmp / "work").mkdir(exist_ok=True)
        (crew / "crew.json").write_text('{"name": "crew"}\n', encoding="utf-8")
        recorded = {n: _cu_station(tmp, **over) for n, over in (stations or CU_STATIONS).items()}
        (crew / "stations.json").write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
        rows = []
        for n, s in recorded.items():
            if s["status"] == "active":
                rows.append(_row((states or {}).get(n, "idle"), s["id"], n, version=runs, cwd=str(tmp / "work")))
        (state / "agents.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        (state / "version").write_text(installed + "\n", encoding="utf-8")
        (state / "spawn-seq").write_text(spawn + "\n", encoding="utf-8")
        hook = state / "hook.py"
        hook.write_text(CU_HOOK, encoding="utf-8")
        cmd = f'"{sys.executable}" "{hook}"'
        (state / "on-bg-cmd").write_text(cmd, encoding="utf-8")
        (state / "on-bg").write_text(cmd, encoding="utf-8")
    return setup


def _cu_seq(*versions: str):
    """A `before` hook: the sessions started from now on get these versions in turn."""
    def before(tmp: Path) -> None:
        (tmp / "claude" / "spawn-seq").write_text(" ".join(versions) + "\n", encoding="utf-8")
    return before


def _cu_names(r: Run) -> list:
    return [c["args"][2] for c in bg_calls(r)]


def _cu_marks(r: Run, station: str) -> list:
    s = stations_json(r)[station]
    return [s.get("cc_failed"), s.get("cc_fails"), s.get("cc_fresh")]


def _cu_unmarked(r: Run, station: str) -> bool:
    s = stations_json(r)[station]
    return all(s.get(k) is None for k in ("cc_failed", "cc_fails", "cc_retry_at", "cc_fresh"))


def _cu_puts(r: Run) -> list:
    return [json.loads(q["body"]) for q in r.tares.asked(CU_RETRY)]


def _cu_resumed(r: Run, station: str) -> list:
    return ["--resume" in c["args"] for c in bg_calls(r) if c["args"][2] == station]


def _cu_with_key(setup):
    """The same world with the crew's key in its token file, which is what a Tares call sends."""
    def keyed(tmp: Path, env: dict) -> None:
        setup(tmp, env)
        (tmp / "crew" / ".tares-token").write_text("cu-secret\n", encoding="utf-8")
    return keyed


def _cu_is(got, want) -> bool:
    """got == want; when it is not, the check fails with both in its message."""
    if got != want:
        raise AssertionError(f"got {got}, wanted {want}")
    return True


def _cu_wait():
    """An `after` hook: seconds from now to the station's retry time, to the nearest ten (the two engines run a moment
    apart), and its failed moves."""
    def after(tmp: Path):
        import time
        s = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))["crew-reviewer"]
        at = s.get("cc_retry_at")
        return {"in": int(round(int(at) - time.time(), -1)) if at else None, "fails": s.get("cc_fails")}
    return after


def _cu_halt_file(tmp: Path) -> None:
    (tmp / "claude" / "halt-after-first").write_text("", encoding="utf-8")


def _cu_busy_reviewer(tmp: Path) -> None:
    p = tmp / "claude" / "agents.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    for row in rows:
        if row["name"] == "crew-reviewer":
            row["status"] = "busy"
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _cu_orchestrator_current(tmp: Path) -> None:
    p = tmp / "claude" / "agents.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    for row in rows:
        if row["name"] == "crew-orchestrator":
            row["version"] = CU_NEW
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _cu_drop_stations(tmp: Path) -> None:
    (tmp / "crew" / "stations.json").unlink()


def _cu_no_claude(tmp: Path) -> None:
    f = tmp / "noclaude"
    f.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    f.chmod(0o755)


scenario(Scenario(
    name="crew_update_args",
    suite="claude_update",
    about="a --wait that is not a number of seconds, or an argument it does not know, is refused with nothing restarted",
    setup=_cu_world(),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    steps=[Step(["crew", "update", "--wait", "abc"], env=CU_ENV), Step(["crew", "update", "--wait", "1e3"], env=CU_ENV),
           Step(["crew", "update", "--wait", "-5"], env=CU_ENV), Step(["crew", "update", "--wait", ""], env=CU_ENV),
           Step(["crew", "update", "--wait", "99999999999999999999"], env=CU_ENV), Step(["crew", "update", "--wait"], env=CU_ENV),
           Step(["crew", "update", "--bogus"], env=CU_ENV), Step(["crew", "restart", "--now"], env=CU_ENV)],
    checks=[
        ("--wait with a word, 1e3, a minus, nothing or 20 digits is refused",
         lambda r: all(r.code(i) != 0 and "takes a number of seconds" in r.out(i) for i in range(5))),
        ("--wait with no value is refused with the usage line, nothing restarted",
         lambda r: r.code(5) != 0 and "takes a number of seconds" in r.out(5) and r.launches() == 0),
        ("an argument it does not know gets the usage line",
         lambda r: r.code(6) != 0 and "usage: " in r.out(6) and "crew update [--force] [--wait <seconds>]" in r.out(6)
         and r.code(7) != 0 and "crew update [--force] [--wait <seconds>]" in r.out(7)),
    ],
    covers=["--wait with no value is refused with the usage line, nothing restarted"],
))

scenario(Scenario(
    name="crew_update_gates",
    suite="claude_update",
    about="halted, no readable Claude Code version and no stations recorded each stop the command, restarting nothing",
    setup=_cu_world(),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    steps=[Step(["crew", "halt", "maintenance"], env=CU_ENV), Step(["crew", "update"], env=CU_ENV),
           Step(["crew", "restart"], env=CU_ENV), Step(["crew", "resume"], env=CU_ENV),
           Step(["crew", "update"], env={**CU_ENV, "CLAUDE_BIN": "{tmp}/noclaude"}, before=_cu_no_claude),
           Step(["crew", "update"], env=CU_ENV, before=_cu_drop_stations)],
    checks=[
        ("halted: the command stops and says why, nothing moves",
         lambda r: r.code(1) != 0 and "the crew is halted (maintenance)" in r.out(1) and r.code(2) != 0
         and "the crew is halted (maintenance)" in r.out(2) and r.launches() == 0),
        ("no readable version: crew update stops plainly",
         lambda r: r.code(4) != 0 and "could not read the Claude Code version" in r.out(4) and r.launches() == 0),
        ("no stations recorded: it says so and succeeds", lambda r: r.code(5) == 0 and "no stations recorded" in r.out(5)),
    ],
    covers=["no readable version: crew update stops plainly"],
))

scenario(Scenario(
    name="crew_update_current",
    suite="claude_update",
    about="stations already on the installed Claude Code are left; crew restart and --force restart them anyway, the orchestrator last",
    setup=_cu_world(runs=CU_NEW),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env=CU_ENV), Step(["crew", "restart"], env=CU_ENV), Step(["crew", "update", "--force"], env=CU_ENV)],
    checks=[
        ("already current: nothing restarted",
         lambda r: r.code(0) == 0 and f"crew-reviewer already runs Claude Code {CU_NEW}" in r.out(0)
         and f"every station runs Claude Code {CU_NEW}, one session each" in r.out(0)),
        ("crew restart restarts every station anyway, the orchestrator last",
         lambda r: r.code(1) == 0 and _cu_names(r)[:2] == ["crew-reviewer", "crew-orchestrator"]
         and f"restarting crew-reviewer on Claude Code {CU_NEW}, in the same conversation" in r.out(1)),
        ("--force is crew restart", lambda r: r.code(2) == 0 and _cu_names(r) == ["crew-reviewer", "crew-orchestrator"] * 2),
    ],
    covers=["already current: nothing restarted"],
))

scenario(Scenario(
    name="crew_update_moves",
    suite="claude_update",
    about="stations behind move in the same conversation, the others first and the orchestrator last; a second run finds none behind",
    setup=_cu_world(),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env=CU_ENV), Step(["crew", "update"], env=CU_ENV)],
    checks=[
        ("every station runs the new version, the orchestrator last",
         lambda r: r.code(0) == 0 and _cu_names(r) == ["crew-reviewer", "crew-orchestrator"]
         and f"every station runs Claude Code {CU_NEW}, one session each" in r.out(0)),
        ("each is resumed in its own conversation",
         lambda r: [c["args"][c["args"].index("--resume") + 1] for c in bg_calls(r)] == ["sess-r", "sess-o"]),
        ("no mark is left on a station that got there", lambda r: _cu_unmarked(r, "crew-reviewer") and _cu_unmarked(r, "crew-orchestrator")),
        ("a second run finds every station on it", lambda r: r.code(1) == 0 and len(_cu_names(r)) == 2),
        ("Tares is told the retry list, empty", lambda r: _cu_puts(r)[0] == {"stations": {}}),
    ],
    covers=[],
))

scenario(Scenario(
    name="crew_update_classify",
    suite="claude_update",
    about="a stopped station and one with no conversation are named, not restarted; the one that has no conversation fails the run",
    setup=_cu_world(stations={**CU_STATIONS, "crew-releaser": {"role": "releaser", "id": "cccc0003"},
                              "crew-builder": {"role": "builder", "id": "dddd0004", "sessionId": "sess-b", "project": "shop"}},
                    states={"crew-builder": "stopped"}),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env=CU_ENV)],
    checks=[
        ("a stopped station waits for the watch",
         lambda r: "crew-builder is stopped; it starts on Claude Code" in r.out(0) and "crew-builder" not in _cu_names(r)),
        ("a station with no conversation is named and fails the run",
         lambda r: r.code(0) != 0 and "crew-releaser has no conversation to resume" in r.out(0)
         and f"not every station runs Claude Code {CU_NEW}: crew-releaser" in r.out(0)),
        ("the others still move, the orchestrator last", lambda r: _cu_names(r) == ["crew-reviewer", "crew-orchestrator"]),
    ],
    covers=[],
))

scenario(Scenario(
    name="crew_update_wait",
    suite="claude_update",
    about="a busy station is waited for, then given up on at --wait; the others still move and the run fails naming it",
    setup=_cu_world(),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update", "--wait", "2"], env=CU_ENV, before=_cu_busy_reviewer)],
    checks=[
        ("it says it waits, once", lambda r: r.out(0).count("waiting for crew-reviewer to be idle (it is working)") == 1),
        ("past --wait it gives up and fails naming the station",
         lambda r: r.code(0) != 0 and "gave up waiting for crew-reviewer to be idle" in r.out(0)
         and f"not every station runs Claude Code {CU_NEW}: crew-reviewer" in r.out(0)),
        ("the busy station is not restarted, the orchestrator is", lambda r: _cu_names(r) == ["crew-orchestrator"]),
    ],
    covers=[],
))

scenario(Scenario(
    name="crew_update_self",
    suite="claude_update",
    about="run from the orchestrator it leaves that station out and says how it moves; --force says it needs another session",
    setup=_cu_world(),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env={**CU_ENV, "FACTORY_STATION": "crew-orchestrator"}),
           Step(["crew", "update"], env={**CU_ENV, "FACTORY_STATION": "crew-orchestrator"}, before=_cu_orchestrator_current),
           Step(["crew", "update", "--force"], env={**CU_ENV, "FACTORY_STATION": "crew-orchestrator"})],
    checks=[
        ("from the orchestrator: the others move, the orchestrator is left, and the way it moves is said",
         lambda r: r.code(0) == 0 and _cu_names(r)[:1] == ["crew-reviewer"] and "crew-orchestrator" not in _cu_names(r)[:1]
         and "crew-orchestrator, which ran this command, moves when" in r.out(0)),
        ("a caller that is already on the version is not restarted and not told anything about its move",
         lambda r: r.code(1) == 0 and "crew-orchestrator already runs Claude Code" in r.out(1)
         and "every station runs Claude Code" in r.out(1) and "which ran this command" not in r.out(1)
         and "restarting" not in r.out(1)),
        ("--force restarts the others, leaves the caller and says it needs another session",
         lambda r: r.code(2) == 0 and _cu_names(r) == ["crew-reviewer", "crew-reviewer"]
         and "can only be restarted from another session" in r.out(2)),
    ],
    covers=[],
))

scenario(Scenario(
    name="crew_update_dup",
    suite="claude_update",
    about="two sessions under one station name are reported and fail the run",
    setup=_cu_world(runs=CU_NEW),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    steps=[Step(["crew", "update"], env=CU_ENV, before=lambda tmp: _cu_add_dup(tmp)), Step(["crew", "update"], env=CU_ENV, before=lambda tmp: _cu_drop_dup(tmp))],
    checks=[
        ("two sessions under one name: reported, the update fails",
         lambda r: r.code(0) != 0 and "TWO SESSIONS named crew-reviewer" in r.out(0)
         and f"not every station runs Claude Code {CU_NEW}: two-sessions" in r.out(0)),
        ("one session each: it passes", lambda r: r.code(1) == 0 and "one session each" in r.out(1)),
    ],
    covers=["two sessions under one name: reported, the update fails", "one session each: it passes"],
))


def _cu_add_dup(tmp: Path) -> None:
    p = tmp / "claude" / "agents.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    rows.append({"id": "dup00001", "name": "crew-reviewer", "state": "working", "status": "idle", "sessionId": "other", "pid": 49999,
                 "cwd": "/", "version": CU_NEW})
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _cu_drop_dup(tmp: Path) -> None:
    p = tmp / "claude" / "agents.json"
    rows = [x for x in json.loads(p.read_text(encoding="utf-8")) if x["id"] != "dup00001"]
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


scenario(Scenario(
    name="crew_update_halt_mid",
    suite="claude_update",
    about="a halt that comes after the first restart stops the run before the orchestrator, and says so",
    setup=_cu_world(),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env=CU_ENV, before=_cu_halt_file)],
    checks=[("halted in the middle: crew update stops and says so",
             lambda r: r.code(0) != 0 and "the crew was halted; nothing more was restarted" in r.out(0)
             and _cu_names(r) == ["crew-reviewer"])],
    covers=[],
))

# cc_retry_*: a station whose restarts come back on an old spare is marked, retried after a back-off that doubles up to a
# cap, and the list of them goes to Tares (PUT /api/crew/claude-retry). crew update tries at once whatever the back-off says.

# A start in bash reads the grants and the projects again on every try; the Python side reads them once for the
# run. Neither read changes what is done, so the retry scenarios leave them out of the list of what was asked.
CU_RETRY_ENV = CU_ENV

scenario(Scenario(
    name="cc_retry_marks",
    suite="claude_update",
    about="every restart on an old spare: three tries in the conversation, one new conversation, then the station is marked and "
          "Tares gets the list; a second run tries at once and doubles the wait; a run that gets there clears the marks",
    setup=_cu_with_key(_cu_world(spawn=CU_OLD)),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env=CU_RETRY_ENV, after=_cu_wait()), Step(["crew", "update"], env=CU_RETRY_ENV, after=_cu_wait()),
           Step(["crew", "update"], env=CU_RETRY_ENV, before=_cu_seq(CU_NEW), after=_cu_wait())],
    checks=[
        ("old spares every time: three resumed tries then a new conversation, for each station",
         lambda r: _cu_names(r)[:8] == ["crew-reviewer"] * 4 + ["crew-orchestrator"] * 4
         and _cu_resumed(r, "crew-reviewer")[:4] == [True, True, True, False]),
        ("the run says the watch tries again, and fails naming both",
         lambda r: r.code(0) != 0 and "the watch tries again in 10 min" in r.out(0)
         and f"not every station runs Claude Code {CU_NEW}: crew-reviewer crew-orchestrator" in r.out(0)),
        ("it is marked: the version, one failed move, the new conversation done, a retry time ten minutes on",
         lambda r: r.snap(0)["fails"] == 1 and 540 < r.snap(0)["in"] <= 600),
        ("Tares has the list, with a key",
         lambda r: any(sorted(p["stations"]) == ["crew-orchestrator", "crew-reviewer"] for p in _cu_puts(r))
         and _cu_is(_cu_puts(r)[0]["stations"]["crew-reviewer"], {"target": CU_NEW, "fails": 1, "next_at": _cu_puts(r)[0]["stations"]["crew-reviewer"]["next_at"]})
         and _cu_is([q["auth"] for q in r.tares.asked(CU_RETRY)], [True] * len(r.tares.asked(CU_RETRY)))),
        ("crew update ignores the back-off: it tries again at once and fails again",
         lambda r: r.code(1) != 0 and "the watch tries again in 20 min" in r.out(1)),
        ("no second new conversation: three more tries in the old one",
         lambda r: _cu_is(_cu_resumed(r, "crew-reviewer"), [True, True, True, False, True, True, True, True])),
        ("the wait doubles: two failed moves, a retry time twenty minutes on",
         lambda r: r.snap(1)["fails"] == 2 and 1140 < r.snap(1)["in"] <= 1200),
        ("the second list says two failures",
         lambda r: any(p["stations"].get("crew-reviewer", {}).get("fails") == 2 for p in _cu_puts(r))),
        ("once a spare is not in the way every mark is cleared, here and on Tares",
         lambda r: r.code(2) == 0 and _cu_unmarked(r, "crew-reviewer") and _cu_unmarked(r, "crew-orchestrator")
         and _cu_is(_cu_puts(r)[-1], {"stations": {}})),
    ],
    covers=["crew update ignores the backoff, tries the orchestrator again and says the watch retries",
            "the failure marks are cleared, here and on Tares"],
))

scenario(Scenario(
    name="cc_retry_cap",
    suite="claude_update",
    about="FACTORY_CC_BACKOFF sets the first wait and FACTORY_CC_BACKOFF_MAX the cap it doubles up to",
    setup=_cu_world(stations={"crew-reviewer": CU_STATIONS["crew-reviewer"]}, spawn=CU_OLD),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env={**CU_ENV, "FACTORY_CC_BACKOFF": "100", "FACTORY_CC_BACKOFF_MAX": "150"}, after=_cu_wait())
           for _ in range(3)],
    checks=[
        ("the first wait is FACTORY_CC_BACKOFF", lambda r: r.snap(0)["fails"] == 1 and 90 < r.snap(0)["in"] <= 100),
        ("the second doubles, up to the cap", lambda r: r.snap(1)["fails"] == 2 and 140 < r.snap(1)["in"] <= 150),
        ("the third stays at the cap", lambda r: r.snap(2)["fails"] == 3 and 140 < r.snap(2)["in"] <= 150),
        ("the Tares list follows", lambda r: [p["stations"]["crew-reviewer"]["fails"] for p in _cu_puts(r)] == [1, 2, 3]),
    ],
    covers=[],
))

scenario(Scenario(
    name="cc_retry_tries",
    suite="claude_update",
    about="FACTORY_CC_TRIES is the number of tries in the conversation before the new one",
    setup=_cu_world(stations={"crew-reviewer": CU_STATIONS["crew-reviewer"]}, spawn=CU_OLD),
    tares=CU_TARES,
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env={**CU_ENV, "FACTORY_CC_TRIES": "1"}),
           Step(["crew", "update"], env={**CU_ENV, "FACTORY_CC_TRIES": "5"})],
    checks=[
        ("one try, then the new conversation", lambda r: _cu_resumed(r, "crew-reviewer")[:2] == [True, False]),
        ("five tries in the conversation, and the new conversation is not made again",
         lambda r: _cu_resumed(r, "crew-reviewer")[2:] == [True] * 5),
    ],
    covers=[],
))

scenario(Scenario(
    name="cc_retry_put_fails",
    suite="claude_update",
    about="a Tares that refuses the retry list does not change what the command says or does",
    setup=_cu_world(stations={"crew-reviewer": CU_STATIONS["crew-reviewer"]}, spawn=CU_OLD),
    tares={**CU_TARES, CU_RETRY: (500, "boom")},
    ignore_asked=CU_NO_PACE,
    not_windows=CU_NO_HOOK,
    steps=[Step(["crew", "update"], env=CU_ENV)],
    checks=[("the run fails as it would, naming the station and the retry",
             lambda r: r.code(0) != 0 and f"not every station runs Claude Code {CU_NEW}: crew-reviewer" in r.out(0)
             and "the watch tries again in 10 min" in r.out(0) and len(r.tares.asked(CU_RETRY)) == 1)],
    covers=[],
))


# usage_test.sh, the watch's readings (TR-1163)
# The watch answers the Crew page's asks for a /context reading and reads the plan's limits between its rounds
# (readings_support.py runs it and stops it once the fake Tares has served the asks route a number of times).
# The fake Tares below does what Tares does: an ask goes away once the reading comes in.

class _UsageWatchRoutes(dict):
    """The routes of a fake Tares that lists the Crew page's asks; the reading that is posted answers the ask of the
    orchestrator's own session, and /ticks says how many times the asks were listed."""

    def __init__(self, routes, asks):
        super().__init__(routes)
        self.asks = list(asks)
        self.listed = 0

    def get(self, key, default=None):
        if key == "GET /api/crew/context/asks":
            self.listed += 1
            return (200, {"asks": self.asks})
        if key == "GET /ticks":
            return (200, {"n": self.listed})
        if key == "POST /ingest/claude_code":
            self.asks = [a for a in self.asks if a["session"] != "sess-orch-1"]
        return super().get(key, default)


# What the rounds of the watch do around the readings (both engines do their own: pace, retry, settings) is left out
# of the comparison; the asks listed and the readings posted are in it.
_UW_ROUND = (("agents",), ("--version",))
_UW_ASKED = ("GET /ticks", "GET /health", "GET /api/crew/settings", "GET /api/crew/pace", "PUT ")


def _usage_watch_asks(*asks):
    def prepare(r):
        r.tares.routes = _UsageWatchRoutes(r.tares.routes, [{"station": s, "session": x} for s, x in asks])
    return prepare


def _usage_asks_left(r) -> list:
    return [(a["station"], a["session"]) for a in r.tares.routes.asks]


def _usage_calls_with(r, needle: str) -> list:
    return [c for c in r.calls() if needle in c["args"]]


def _usage_posted(r, kind: str) -> list:
    return [b for b in (json.loads(q["body"]) for q in r.tares.asked("POST /ingest/claude_code")) if b.get("type") == kind]


scenario(Scenario(
    name="usage_watch_readings",
    suite="usage",
    about="the watch reads a station's /context when the Crew page asks, leaves an ask for another station alone, and reads no plan when told not to",
    tares={"POST /ingest/claude_code": (200, {"ok": True})},
    setup=_setup_usage,
    prepare=_usage_watch_asks(("crew-orchestrator", "sess-orch-1"), ("foreign-station", "s-foreign")),
    driver="readings_support.py",
    ignore_asked=_UW_ASKED,
    ignore_calls=_UW_ROUND,
    steps=[Step(["watch", "3"], env={"FACTORY_PLAN_EVERY": "0"})],
    checks=[
        ("the watch reads what the Crew page asked for",
         lambda r: r.code(0) == 0 and ("crew-orchestrator", "sess-orch-1") not in _usage_asks_left(r)
         and len(_usage_calls_with(r, "/context")) == 1 and len(_usage_posted(r, "session_context")) == 1),
        ("and leaves another crew's ask alone",
         lambda r: _usage_asks_left(r) == [("foreign-station", "s-foreign")]
         and not any("s-foreign" in c["args"] for c in r.calls())),
        ("and reads no plan when told not to",
         lambda r: not _usage_calls_with(r, "/usage") and not _usage_posted(r, "claude_plan")),
    ],
    covers=["the watch reads what the Crew page asked for", "and leaves another crew's ask alone",
            "and reads no plan when told not to"],
))

scenario(Scenario(
    name="usage_watch_other_crew",
    suite="usage",
    about="an ask for a station of the same name that another crew plays (another session) is not read",
    tares={"POST /ingest/claude_code": (200, {"ok": True})},
    setup=_setup_usage,
    prepare=_usage_watch_asks(("crew-orchestrator", "s-other-crew")),
    driver="readings_support.py",
    ignore_asked=_UW_ASKED,
    ignore_calls=_UW_ROUND,
    steps=[Step(["watch", "3"], env={"FACTORY_PLAN_EVERY": "0"})],
    checks=[
        ("an ask for the same name in another crew is left alone",
         lambda r: r.code(0) == 0 and _usage_asks_left(r) == [("crew-orchestrator", "s-other-crew")]
         and not _usage_calls_with(r, "/context")),
    ],
    covers=["an ask for the same name in another crew is left alone"],
))

scenario(Scenario(
    name="usage_watch_plan",
    suite="usage",
    about="the watch reads the plan's limits at its first tick and sends them; with no plan to read it says so once",
    tares={"POST /ingest/claude_code": (200, {"ok": True})},
    setup=_setup_usage,
    prepare=_usage_watch_asks(),
    driver="readings_support.py",
    ignore_asked=_UW_ASKED,
    ignore_calls=_UW_ROUND,
    steps=[Step(["watch", "3"]), Step(["watch", "6"], env={"FAKE_CLAUDE_NO_PLAN": "1"})],
    checks=[
        ("the first tick reads the plan and sends it once",
         lambda r: r.code(0) == 0 and len(_usage_posted(r, "claude_plan")) == 1 and "could not read" not in r.out(0)),
        ("with no plan to read it is said once, and said plainly",
         lambda r: r.code(1) == 0 and r.out(1).count("could not read the plan's limits (") == 1
         and "plan says why); trying again every 600s" in r.out(1)),
        ("and each watch reads the plan once",
         lambda r: len(_usage_calls_with(r, "/usage")) == 2),
    ],
    covers=[],
))


# crewname_test.sh (TR-1162)
# The crew's name: refused names, the roster's first line, spawn and adoption under each crew's names, the hooks
# that follow the crew. Steps that need a Tares ask a fake one for the projects, and `crew up` the stand-in.

CN_ENV = {"FACTORY_CREW_HOME": "{tmp}/empty"}


def _cn_bad_names() -> list:
    return ["Bad_Name", "-x", "UPPER", "a b", "x-build-y", "build", "a" * 32]


scenario(Scenario(
    name="crewname_names",
    suite="crewname",
    about="the name is read before anything else: a roster of an empty home says which crew it is; a bad name is refused by any command",
    steps=[
        Step(["roster"], env={**CN_ENV, **NO_TARES, "FACTORY_CREW_NAME": "a"}),
        *[Step(["roster"], env={**CN_ENV, **NO_TARES, "FACTORY_CREW_NAME": bad}) for bad in _cn_bad_names()],
        Step(["roster"], env={**CN_ENV, **NO_TARES, "FACTORY_CREW_NAME": "a" * 31}),
        Step(["status"], env={**CN_ENV, **NO_TARES, "FACTORY_CREW_NAME": "Bad_Name"}),
    ],
    checks=[
        ("FACTORY_CREW_NAME=a: roster names a-orchestrator",
         lambda r: r.out(0).splitlines()[0] == "crew: a (orchestrator: a-orchestrator)"),
        ("crew name '$bad' is refused",
         lambda r: all(r.code(i + 1) != 0 and "is not valid: it is 1 to 31 characters" in r.out(i + 1)
                       for i in range(len(_cn_bad_names())))),
        ("a 31 character name is accepted",
         lambda r: r.out(-2).splitlines()[0] == "crew: %s (orchestrator: %s-orchestrator)" % ("a" * 31, "a" * 31)),
        ("a command other than roster refuses an invalid crew name",
         lambda r: r.code(-1) != 0 and "is not valid: it is 1 to 31 characters" in r.out(-1)),
    ],
    covers=["FACTORY_CREW_NAME=a: roster names a-orchestrator", "crew name '$bad' is refused",
            "a 31 character name is accepted", "a command other than roster refuses an invalid crew name"],
))


def _cn_url(extra, up: bool):
    """A step's env: the crew `extra` names, and either the stand-in Tares (`crew up`) or the fake one that holds the projects."""
    def env(r):
        base = extra(r) if callable(extra) else dict(extra)
        if up:
            return {**base, "TARESD_URL": r.stand_in.url, "TARES_FACTORY_MCP_URL": r.stand_in.mcp_url}
        return {**base, "TARESD_URL": r.tares.url, "TARES_FACTORY_MCP_URL": r.tares.url + "/mcp"}
    return env


def _cn_default(r):
    return {}


def _cn_setup(tmp: Path, env: dict) -> None:
    _mkrepo(tmp, "shop")


def _cn_stand_in_releaser(r):
    """The releaser is only started when the crew asks for one: record a stand-in so the helper path can run."""
    home = r.tmp / "crew-a"
    f = home / "stations.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["a-releaser"] = {"role": "releaser", "status": "active", "project": "", "repo": "", "cwd": str(home), "model": "", "effort": ""}
    f.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _cn_add_sessions(*rows):
    """A session the crew did not record, listed by the fake Claude Code."""
    def run_before(r):
        f = r.state / "agents.json"
        cur = json.loads(f.read_text(encoding="utf-8"))
        for row in rows:
            cur.append({**row, "cwd": str(r.tmp / "shop")})
        f.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
    return run_before


def _cn_agents(r) -> list:
    try:
        return json.loads((r.state / "agents.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _cn_sent(r, station: str, text: str) -> bool:
    return any(c["args"][2:3] == [station] and any(text in a for a in c["args"]) for c in r.calls() if "--bg" in c["args"])


_CN_TARES = {
    "GET /api/crew/settings": (200, {"settings": {**SAVED, "releaser": False, "builders_max": 5}}),
    "GET /api/crew/projects": (200, {"projects": [
        {"id": "uc_shop", "name": "shop", "repos": [{"path": "{tmp}/shop"}]},
        {"id": "uc_atools", "name": "a-tools", "repos": [{"path": "{tmp}/shop"}]}]}),
    "GET /api/grants": (200, {"all": ""}),
    "POST /mcp": (200, {"result": {"serverInfo": {"name": "tares"}}}),
}

_CN_REPO = "{tmp}/shop"
_CN_A = crew_at_unnamed("a")
_CN_B = crew_at_unnamed("b")
CN_ORCH_LINE = "a-orchestrator\torchestrator\t\tworking\t"

scenario(Scenario(
    name="crewname_stations",
    suite="crewname",
    about="the default crew and two named crews build the same project side by side, each under its own names; the roster adopts only its own",
    stand_in={"settings": {**SAVED, "releaser": False}},
    tares=_CN_TARES,
    setup=_cn_setup,
    prepare=trust_all,
    ignore_calls=LSP_PROBE,
    ignore_asked=("GET ",),
    steps=[
        Step(["crew", "up", "--yes"], env=_cn_url(_cn_default, True), stdin=""),                                  # 0
        Step(["spawn", "builder", "shop", "m1", _CN_REPO, "work T1"], env=_cn_url(_cn_default, False)),            # 1
        Step(["spawn", "helper", "crew-reviewer", "probe", "look"], env=_cn_url(_cn_default, False)),              # 2
        Step(["crew", "up", "--yes"], env=_cn_url(crew_at("a"), True), stdin=""),                                  # 3
        Step(["crew", "up", "--yes"], env=_cn_url(crew_at("b"), True), stdin=""),                                  # 4
        Step(["roster"], env=_cn_url(_CN_A, False)),                                                               # 5
        Step(["roster"], env=_cn_url(_CN_B, False)),                                                               # 6
        Step(["settings", "a-reviewer", "reviewer"], env=_cn_url(_CN_A, False)),                                   # 7
        Step(["spawn", "builder", "shop", "m1", _CN_REPO, "work T1"], env=_cn_url(_cn_default, False)),            # 8
        Step(["spawn", "builder", "shop", "m1", _CN_REPO, "work T1"], env=_cn_url(_CN_A, False)),                  # 9
        Step(["spawn", "builder", "shop", "m1", _CN_REPO, "work T1"], env=_cn_url(_CN_B, False)),                  # 10
        Step(["roster"], env=_cn_url(_CN_A, False)),                                                               # 11
        Step(["spawn", "helper", "a-reviewer", "probe", "look"], env=_cn_url(_CN_A, False)),                       # 12
        Step(["spawn", "builder", "a-tools", "m1", _CN_REPO, "x"], env=_cn_url(_CN_A, False)),                     # 13
        Step(["spawn", "releaser-helper", "probe", "look"], env=_cn_url(_CN_A, False), run_before=_cn_stand_in_releaser),   # 14
        Step(["roster"], env=_cn_url(_CN_A, False), run_before=_cn_add_sessions(
            {"id": "0000a002", "name": "a-shop-build-m2", "state": "working", "sessionId": "s-a002", "startedAt": 98, "pid": 4242},
            {"id": "0000b009", "name": "b-shop-build-m9", "state": "working", "sessionId": "s-b009", "startedAt": 99, "pid": 4243})),  # 15
        Step(["roster"], env=_cn_url(_cn_default, False), run_before=_cn_add_sessions(
            {"id": "0000d005", "name": "shop-build-m5", "state": "working", "sessionId": "s-d005", "startedAt": 100, "pid": 4244})),  # 16
    ],
    checks=[
        ("default crew: a builder is <project>-build-<slug>", lambda r: r.station("shop-build-m1", "role") == "builder"),
        ("default crew: its parent is crew-orchestrator",
         lambda r: env_of(r, "shop-build-m1", "FACTORY_PARENT") == "crew-orchestrator"),
        ("default crew: its env carries the crew",
         lambda r: env_of(r, "shop-build-m1", "FACTORY_CREW") == "crew"
         and env_of(r, "shop-build-m1", "FACTORY_ORCHESTRATOR") == "crew-orchestrator"),
        ("default crew: a helper keeps its suffix rule", lambda r: r.station("crew-reviewer-probe", "parent") == "crew-reviewer"),
        ("default crew: the boot prompt sends READY to crew-orchestrator",
         lambda r: _cn_sent(r, "shop-build-m1", "send crew-orchestrator READY")),
        ("crew $c: roster names the crew (a)", lambda r: r.out(5).splitlines()[0] == "crew: a (orchestrator: a-orchestrator)"),
        ("crew $c: roster names the crew (b)", lambda r: r.out(6).splitlines()[0] == "crew: b (orchestrator: b-orchestrator)"),
        ("crew $c: roster lists its orchestrator working (a)", lambda r: CN_ORCH_LINE in r.out(5).replace("\r", "")),
        ("crew $c: roster lists its orchestrator working (b)", lambda r: "b-orchestrator\torchestrator\t\tworking\t" in r.out(6)),
        ("crew.json names the crew with no variable",
         lambda r: json.loads(r.results[7]["stdout"]).get("env", {}).get("FACTORY_CREW") == "a"),
        ("crew a: the builder is a-shop-build-m1",
         lambda r: r.station("a-shop-build-m1", "role", home_of(r, "a")) == "builder"
         and r.station("shop-build-m1", "role", home_of(r, "a")) == ""),
        ("crew a: its parent is a-orchestrator",
         lambda r: r.station("a-shop-build-m1", "parent", home_of(r, "a")) == "a-orchestrator"
         and env_of_in(r, home_of(r, "a"), "a-shop-build-m1", "FACTORY_PARENT") == "a-orchestrator"),
        ("crew a: its env carries the crew and the orchestrator",
         lambda r: env_of_in(r, home_of(r, "a"), "a-shop-build-m1", "FACTORY_CREW") == "a"
         and env_of_in(r, home_of(r, "a"), "a-shop-build-m1", "FACTORY_ORCHESTRATOR") == "a-orchestrator"
         and env_of_in(r, home_of(r, "a"), "a-shop-build-m1", "FACTORY_STATION") == "a-shop-build-m1"),
        ("crew a: its boot prompt sends READY to a-orchestrator",
         lambda r: _cn_sent(r, "a-shop-build-m1", "send a-orchestrator READY")),
        ("crew b: the same project builds as b-shop-build-m1, beside a's",
         lambda r: r.station("b-shop-build-m1", "role", home_of(r, "b")) == "builder"
         and sum(1 for n in agent_names(r) if n in ("a-shop-build-m1", "b-shop-build-m1")) == 2),
        ("the default crew keeps its builder name beside them",
         lambda r: r.station("shop-build-m1", "role") == "builder" and agent_names(r).count("shop-build-m1") == 1),
        ("crew a: roster lists its builder and not b's",
         lambda r: "a-shop-build-m1\tbuilder\tshop\tworking\t" in r.out(11) and "b-shop-build-m1" not in r.out(11)),
        ("crew a: a helper of a-reviewer keeps its suffix rule and parent",
         lambda r: r.station("a-reviewer-probe", "parent", home_of(r, "a")) == "a-reviewer"
         and r.station("a-reviewer-probe", "role", home_of(r, "a")) == "helper"),
        ("crew a: a helper of a-reviewer is told to load station-reviewer",
         lambda r: any(c["args"][2:3] == ["a-reviewer-probe"]
                       and any("station-reviewer" in x and "send a-reviewer DONE" in x for x in c["args"])
                       for c in r.calls() if "--bg" in c["args"])),
        ("crew a: a project can not be named like the crew",
         lambda r: "can't start with \"a\"" in r.out(13) and r.station("a-tools-build-m1", "role", home_of(r, "a")) == ""),
        ("crew a: a releaser helper hangs under a-releaser",
         lambda r: r.station("a-releaser-probe", "parent", home_of(r, "a")) == "a-releaser"
         and r.station("a-releaser-probe", "role", home_of(r, "a")) == "helper"),
        ("crew a: an unrecorded a-shop-build-m2 is adopted, under a-orchestrator",
         lambda r: r.station("a-shop-build-m2", "role", home_of(r, "a")) == "builder"
         and r.station("a-shop-build-m2", "parent", home_of(r, "a")) == "a-orchestrator"
         and r.station("a-shop-build-m2", "project", home_of(r, "a")) == "shop"),
        ("crew a: b's unrecorded builder is not adopted", lambda r: r.station("b-shop-build-m9", "role", home_of(r, "a")) == ""),
        ("default crew: an unrecorded shop-build-m5 is adopted",
         lambda r: r.station("shop-build-m5", "role") == "builder" and r.station("shop-build-m5", "parent") == "crew-orchestrator"),
        ("default crew: a- and b- builders on the same repo are not adopted",
         lambda r: r.station("a-shop-build-m2", "role") == "" and r.station("b-shop-build-m9", "role") == ""
         and r.station("a-shop-build-m1", "role") == ""),
    ],
    covers=["crew $c: roster names the crew", "crew $c: roster lists its orchestrator working",
            "default crew: a builder is <project>-build-<slug>", "default crew: its parent is crew-orchestrator",
            "default crew: its env carries the crew", "default crew: a helper keeps its suffix rule",
            "default crew: the boot prompt sends READY to crew-orchestrator", "crew.json names the crew with no variable",
            "crew a: the builder is a-shop-build-m1", "crew a: its parent is a-orchestrator",
            "crew a: its env carries the crew and the orchestrator", "crew a: its boot prompt sends READY to a-orchestrator",
            "crew b: the same project builds as b-shop-build-m1, beside a's", "the default crew keeps its builder name beside them",
            "crew a: roster lists its builder and not b's", "crew a: a helper of a-reviewer keeps its suffix rule and parent",
            "crew a: a helper of a-reviewer is told to load station-reviewer", "crew a: a project can not be named like the crew",
            "crew a: a releaser helper hangs under a-releaser",
            "crew a: an unrecorded a-shop-build-m2 is adopted, under a-orchestrator", "crew a: b's unrecorded builder is not adopted",
            "default crew: an unrecorded shop-build-m5 is adopted",
            "default crew: a- and b- builders on the same repo are not adopted"],
))


CN_HOOKS = "crewname_support.py"
_CN_MERGE = "gh pr merge 3"
_CN_UNDO = "kubectl --context kind-factory rollout " + "undo deploy/app"


def _cn_guard(parent: str, command: str, crew: str) -> Step:
    return Step(["guard", parent, command, crew])


def _cn_start(parent: str, crew: str) -> Step:
    return Step(["start", parent, crew])


scenario(Scenario(
    name="crewname_hooks",
    suite="crewname",
    about="the hooks follow the crew: a helper of a-reviewer is a reviewer, a helper of a builder is a builder; the plugin sends the crew's name",
    driver=CN_HOOKS,
    steps=[
        _cn_guard("a-releaser", _CN_MERGE, "a"),                                                     # 0
        _cn_guard("a-releaser", _CN_UNDO, "a"),                                                      # 1
        _cn_guard("a-shop-build-m1", "factory restart a-reviewer", "a"),                             # 2
        _cn_guard("crew-releaser", _CN_MERGE, ""),                                                   # 3
        _cn_guard("crew-releaser-build-x", _CN_MERGE, ""),                                           # 4
        _cn_guard("a-releaser-build-x", _CN_MERGE, "a"),                                             # 5
        _cn_start("a-reviewer", "a"),                                                                # 6
        _cn_start("a-shop-build-m1", "a"),                                                           # 7
        _cn_start("crew-releaser", ""),                                                              # 8
        _cn_start("crew-reviewer-build-x", ""),                                                      # 9
        _cn_start("a-reviewer-build-x", "a"),                                                        # 10
    ],
    checks=[
        ("guard: a helper of a-releaser acts as a releaser (named crew)",
         lambda r: r.out(0).strip() == "deny" and r.out(1).strip() == "allow"),
        ("guard: a helper of a-shop-build-m1 acts as a builder", lambda r: r.out(2).strip() == "deny"),
        ("guard: the default crew still works with no FACTORY_CREW", lambda r: r.out(3).strip() == "deny"),
        ("guard: a helper whose name has -build- under crew-releaser is still a releaser's",
         lambda r: r.out(4).strip() == "deny" and r.out(5).strip() == "deny"),
        ("session start: a helper of a-reviewer loads station-reviewer (named crew)",
         lambda r: r.out(6).strip() == "station-reviewer" and r.out(7).strip() == "station-builder"
         and r.out(8).strip() == "station-releaser"),
        ("session start: a helper whose name has -build- under crew-reviewer loads station-reviewer",
         lambda r: r.out(9).strip() == "station-reviewer" and r.out(10).strip() == "station-reviewer"),
        ("the plugin sends X-Tares-Crew from FACTORY_CREW",
         lambda r: json.loads((ROOT / "claude-plugin" / ".mcp.json").read_text(encoding="utf-8"))
         ["mcpServers"]["tares"]["headers"]["X-Tares-Crew"] == "${FACTORY_CREW:-}"),
    ],
    covers=["guard: a helper of a-releaser acts as a releaser (named crew)", "guard: a helper of a-shop-build-m1 acts as a builder",
            "guard: the default crew still works with no FACTORY_CREW",
            "guard: a helper whose name has -build- under crew-releaser is still a releaser's",
            "session start: a helper of a-reviewer loads station-reviewer (named crew)",
            "session start: a helper whose name has -build- under crew-reviewer loads station-reviewer",
            "the plugin sends X-Tares-Crew from FACTORY_CREW"],
))


def main(argv: list) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    if flags - {"--coverage", "--require-full", "--list"}:
        print(__doc__)
        return 2
    if "--list" in flags:
        print("\n".join(SCENARIOS))
        return 0
    if "--coverage" in flags:
        return coverage("--require-full" in flags)
    if "--require-full" in flags:
        print("parity: --require-full goes with --coverage", file=sys.stderr)
        return 2
    if not args:
        print("usage: parity.py <scenario>... | --list | --coverage [--require-full]", file=sys.stderr)
        return 2
    names: list = []
    for pattern in args:  # `tidy_*` runs every tidy scenario; several patterns run the union, once each
        found = [n for n in SCENARIOS if fnmatch.fnmatchcase(n, pattern)]
        if not found:
            print(f"parity: no scenario {pattern!r}; known: {', '.join(SCENARIOS)}", file=sys.stderr)
            return 2
        names += [n for n in found if n not in names]
    return max(run_scenario(SCENARIOS[n]) for n in names)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
