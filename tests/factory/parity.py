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
suite. A scenario here lists the labels it covers (`covers`), and counts as passing against Python
when its run passes on the Python side (a SKIP does not count). Percentage = passing / all scenarios.

Test hooks (for tests/test_parity.py): PARITY_CMD_BASH and PARITY_CMD_PYTHON replace the command a side
runs (default `python -m tares.crew`); PARITY_FAIL=bash|python|both adds a failing check to that side.

Standard library only.
"""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FAKE_CLAUDE = HERE / "fake_claude.py"
sys.path.insert(0, str(HERE))
from fake_tares import FakeTares as StandInTares  # noqa: E402  (the stand-in daemon `crew up` talks to; FakeTares below is the route table of the read commands)
CI_LIST = HERE / "ci_list.txt"
ENGINES = ("bash", "python")
STEP_TIMEOUT = 180
NOT_PORTED = "not yet ported"


# ---------------------------------------------------------------------------------------------
# Scenarios

@dataclass
class Step:
    argv: list           # everything after `tares factory`; "{tmp}" in an argument or env value is the scenario's folder
    env: object = field(default_factory=dict)   # a dict, or fn(Run) -> dict when it names the throwaway folders
    stdin: str = ""
    before: object = None   # fn(tmp) run before the step: change files between two commands
    after: object = None    # fn(tmp) -> anything, run after the step; Run.snap(i) gives it back to a check
    run_before: object = None   # fn(Run) run just before the step: files the suite writes between its commands


@dataclass
class Scenario:
    name: str
    suite: str           # the bash suite it comes from (tests/factory/<suite>_test.sh)
    steps: list
    checks: list         # (label, fn(Run) -> bool), the assertions of the bash suite, kept as they are
    covers: list         # labels of the suite's `check` calls this scenario stands for
    about: str = ""
    stand_in: object = None  # None: no stand-in. A dict (settings, people, grants): a stand-in Tares (fake_tares.py)
                             # on a free port, TARESD_URL and TARES_FACTORY_MCP_URL point at it
    prepare: object = None   # fn(Run) run once before the first step: ~/.claude.json, crew.json, ...
    skip_calls: tuple = ()   # leading `claude` arguments of calls left out of the compared record: what bash
                             # asks Claude Code for a part the Python engine has not ported yet (see crew up)
    setup: object = None    # fn(tmp, env) run once before the first step: build files, a repo, a link
    tares: dict | None = None   # routes of a fake Tares to run for the scenario (see FakeTares); None: no Tares


class Run:
    """What a scenario's checks can look at: the steps' output and the crew's state on disk."""

    def __init__(self, tmp: Path, results: list, tares=None, stand_in=None):
        self.tmp = tmp
        self.tares = tares      # the route table (FakeTares) of the read commands, or None
        self.crew = tmp / "crew"
        self.state = tmp / "claude"
        self.home = tmp / "home"
        self.results = results
        self.stand_in = stand_in    # the stand-in Tares (fake_tares.py) of `crew up`, or None
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
# Same assertions as the bash checks, on a stand-in Tares (fake_tares.py) instead of a daemon. The labels in
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
    skip_calls=LSP_PROBE,
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
    skip_calls=LSP_PROBE,
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
    skip_calls=LSP_PROBE,
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


def env_of_in(r, home, station, key):
    v = r.station_settings(station, home).get("env", {}).get(key)
    return "" if v is None or v is False else v


scenario(Scenario(
    name="crewname_two_crews",
    suite="crewname",
    about="two named crews and the default crew side by side; the name comes from FACTORY_CREW_NAME, then crew.json",
    stand_in={"settings": {**SAVED, "releaser": False}},
    prepare=trust_all,
    skip_calls=LSP_PROBE,
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
    skip_calls=LSP_PROBE,
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
# Normalising

ISO = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?")
# An epoch time is replaced only where it is a time: after a word that says so (`at`, `since`, `until`,
# `ts`, `time`, `epoch`, or a name ending in _at, _ts, _time, At), with a `=`, `:` or space between. A
# plain ten-digit number elsewhere (a port, a count, an id) is kept.
EPOCH = re.compile(r"(?P<pre>(?<![\w.])(?:at|since|until|epoch|ts|time|\w*_at|\w*_ts|\w*_time|\w*At)[\"']?\s*[=:]?\s*[\"']?)"
                   r"1[5-9]\d{8}(?:\d{3})?(?![\d.])")
EPOCH_VALUE = re.compile(r"1[5-9]\d{8}(?:\d{3})?")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
SESSION = re.compile(r"\bsession-\d+\b")
# A job id (eight hex digits) is replaced only where a job id is printed: after `backgrounded ·`, or after
# job, session, id, stop or rm. In JSON, a string under an id key is replaced whole (see ID_KEYS).
JOB_ID = re.compile(r"(?P<pre>\bbackgrounded[^0-9a-f\n]+|\b(?:job|session|id|stop|rm)[ =:]+[\"']?)[0-9a-f]{8}\b")
HEX8 = re.compile(r"[0-9a-f]{8}")
PID_TEXT = re.compile(r"\b(pid|PID)([ =:]+)\d+")
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
    s = UUID.sub("<ID>", s)
    s = ISO.sub("<TS>", s)
    s = EPOCH.sub(lambda m: m.group("pre") + "<TS>", s)
    s = PID_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}<PID>", s)
    s = SESSION.sub("<ID>", s)
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

def command_for(engine: str) -> list:
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


def make_launcher(path: Path, extra: list) -> Path:
    """An executable that runs the Python fake: a shell script, or a .cmd file on Windows (returns its path).

    Named `claude`, it is the Claude Code of the scenario: on PATH for a command that runs `claude` by
    name (the bash engine does for some commands, the Python engine will), and CLAUDE_BIN for the rest.
    The .cmd form passes the fake's exit code on, and is written with CRLF whatever the platform.
    The paths in it must be ASCII: cmd.exe reads a batch file in the console's code page."""
    if sys.platform == "win32":
        path = path.with_suffix(".cmd")
        lines = ['@"%s" "%s" %s %%*' % (sys.executable, FAKE_CLAUDE, " ".join(extra)), "@exit /b %ERRORLEVEL%"]
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    else:
        path.write_text('#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (sys.executable, FAKE_CLAUDE, " ".join(extra)), encoding="utf-8")
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


def _skipped_call(line: str, skip_calls: tuple) -> bool:
    try:
        args = json.loads(line).get("args", [])
    except ValueError:
        return False
    return any(args[:len(pre)] == list(pre) for pre in skip_calls)


def snapshot(run: Run, skip_calls: tuple = ()) -> dict:
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
                    if p.name == "calls.jsonl" and skip_calls:
                        text = "".join(ln for ln in text.splitlines(True) if not _skipped_call(ln, skip_calls))
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
            fake = FakeTares(sc.tares)
            env["TARESD_URL"] = fake.url
        run = Run(tmp, results, fake, stand_in)
        if sc.prepare:
            sc.prepare(run)
        if sc.setup:
            sc.setup(tmp, env)
        here = lambda text: text.replace("{tmp}", str(tmp)) if isinstance(text, str) else text
        for step in sc.steps:
            if step.run_before:
                step.run_before(run)
            if step.before:
                step.before(tmp)
            step_env = step.env(run) if callable(step.env) else step.env
            senv = {**env, **{k: here(v) for k, v in step_env.items()}}
            try:
                p = subprocess.run(command_for(engine) + [here(a) for a in step.argv], cwd=str(ROOT), env=senv,
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
                          f"-- stdout --\n{norm_output(r['stdout'], tmp)}\n-- stderr --\n{norm_output(r['stderr'], tmp)}")
        for rel, content in snapshot(run, sc.skip_calls).items():
            blocks.append(f"== file {rel} ==\n{content}")
        if fake is not None:  # what each engine asked Tares, and whether it sent a key (never the key)
            # A read asked twice in a row counts once: bash asks again from a subshell that lost its cache.
            asked = []
            for q in fake.requests:
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


def coverage(require_full: bool, out=print) -> int:
    found = suite_scenarios()
    passing: dict = {}  # (suite, label) -> scenario name, for scenarios that pass against Python now
    bad = 0
    for sc in SCENARIOS.values():
        quiet: list = []
        ok = evaluate(sc, out=quiet.append)[1]
        for label in sc.covers:
            if label not in found.get(sc.suite, []):
                out(f"ERROR scenario {sc.name} covers a check that is not in {sc.suite}_test.sh: {label}")
                bad = 1
            elif ok:
                passing[(sc.suite, label)] = sc.name
    total = done = 0
    for suite, labels in found.items():
        n = sum(1 for label in labels if (suite, label) in passing)
        total += len(labels)
        done += n
        out(f"{suite}: {n}/{len(labels)}")
        for label in labels:
            mark = f"PASS python  ({passing[(suite, label)]})" if (suite, label) in passing else "todo"
            out(f"  {mark:<28} {label}")
    pct = 100.0 * done / total if total else 100.0
    out(f"coverage: {done} of {total} scenarios pass against Python ({pct:.1f}%) in {len(found)} suites")
    if require_full and done < total:
        out(f"FAIL: --require-full wants every scenario ported, {total - done} are not")
        return 1
    return bad


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
    if len(args) != 1:
        print("usage: parity.py <scenario> | --list | --coverage [--require-full]", file=sys.stderr)
        return 2
    names = [n for n in SCENARIOS if fnmatch.fnmatchcase(n, args[0])]  # `tidy_*` runs every tidy scenario
    if not names:
        print(f"parity: no scenario {args[0]!r}; known: {', '.join(SCENARIOS)}", file=sys.stderr)
        return 2
    return max(run_scenario(SCENARIOS[n]) for n in names)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
