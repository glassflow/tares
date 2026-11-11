"""`tares factory crew up` in Python (TR-956), and the setup contract the desktop app asks its questions through.

    crew up [--yes] [--no-watch] [role...]
    crew setup --questions [--json]        every outstanding setup question, as one document (schemas/setup_questions.json)
    crew setup --answers <file.json> [--json]  validate the answers and save them, as the prompts would; --json says what was saved (schemas/setup_answers.json)

Python is the default engine (TR-1069): the dispatcher sends `crew up` here unless TARES_FACTORY_ENGINE=bash
(or "engine": "bash" in crew.json) asks for bash. Wherever it differs from claude-plugin/bin/factory, bash is right, except for the
lines below.

What is ported here (bash name -> Python): installed_tares, clash_text, home_trusted, ensure_home_trusted,
store_crew_name, people_count, suggest_crew_name, setup_crew_name, ask, choose, ask_settings, ask_grants,
ensure_settings, cmd_crew_up; prime is TR-949's (tares.crew.client) and is used as it is. crew_name_ok,
crew_name and crew_orchestrator are TR-948's and are used from tares.crew.state.

The setup questions are one table (QUESTIONS below). The prompts ask them one at a time; the contract
lists them as data and takes the answers as data. Both end in the same two functions, save_settings
and add_grant, so the same settings are saved on Tares either way. In a terminal, `tares factory` runs
the box of tares/factory_setup.py before it calls this; the plain prompts here are what
`python -m tares.crew` and a pipe get, as in bash.

Differences from bash (each one a decision, none an accident):

 0. `crew up` runs here by default (TR-1069), as every ported command does; TARES_FACTORY_ENGINE=bash runs
    bash's. It was gated behind TARES_FACTORY_ENGINE=python until the watch (TR-958) and the language
    server offer (TR-970) were wired in. `crew setup` has no bash twin and runs here unless the engine
    is "bash".

 1. `crew setup` is new: bash has no such command (it answers "crew takes up, down, ..."). It runs on
    Python whatever TARES_FACTORY_ENGINE says unless that is "bash" (then bash's own error answers).
 2. The watch is started here (watch.start, TR-958, wired in TR-1155): the Python watch, as
    `crew watch` is routed (TARES_FACTORY_WATCH=bash keeps the bash one, TR-1069). The language server offer (lsp_offer,
    TR-970) is Up.lsp_offer, which calls tares.crew.doctor_lsp.offer.
 3. Starting the stations (launch, boot_prompt, write_settings, is_alive: TR-950 and TR-953, not ported
    yet) is the one step that still runs bash: `start_stations` hands the roles to the bash internal
    command `_start-stations`, which is the second half of bash's own cmd_crew_up, cut out unchanged
    (the roles loop and its "Starting the crew" line; no watch, no language servers). When TR-953 lands, replace
    start_stations here and delete the bash function and its case arm. Without bash (Windows) crew up
    stops at that step with exit 2 and "not yet ported" in its message, after the settings and the crew
    name are saved.
 4. The claude binary: CLAUDE_BIN, else the first `claude` on PATH that is not a cmux wrapper script
    (bash's claude_bin, TR-950, in the part this ticket needs). It is looked up when crew up starts, as
    bash does at its own start.
 5. The terminal box (tares/factory_setup.py) is not imported here: it needs termios, which Windows lacks.
    `tares factory` runs it before the dispatcher, so a terminal user still sees the box.
 6. prod_pattern is saved with the release profile as the default pattern, as bash does; the box keeps a
    pattern Tares already has. The contract follows bash.
 7. The contract refuses an answer for a question that is not outstanding, an unknown answer, and any
    invalid answer, and saves nothing in that case. The prompts re-ask instead.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import contract, doctor_lsp, state
from . import watch as watch_mod
from .client import TaresClient, TaresHTTPError, settings_defaults, settings_missing
from .errors import CrewError
from .paths import CREW_NAME_RULE, ME, CrewPaths

NAME_PATTERN = "^[a-z0-9][a-z0-9-]{0,30}$"

# ---------------------------------------------------------------------------------------------
# The questions. One table: the prompts and the contract both read it.
#
# kind: "choice" (one of choices), "bool" (choices are the two answers, true and false) or "text".
# choices: [{"value": ..., "label": ...}], the first is the one the prompt numbers 1.
# recommended: the value Enter (and --yes) takes. why: the reason given with it.

_SHIPPING = [("kind", "To a local kind cluster first"), ("github-release", "Tags and GitHub releases only"),
             ("custom", "Each project's own steps")]

QUESTIONS = {
    "autonomy": {
        "text": "Who merges pull requests?", "kind": "choice",
        "choices": [("L4-ship", "The crew, once its reviewer approves and tests pass"), ("L3-review", "You, each one")],
        "recommended": "L4-ship", "why": "Builders merge on their own; you answer only what the crew can't decide.",
    },
    "releaser": {
        "text": "Should the crew release and deploy finished work?", "kind": "bool",
        "choices": [(False, "No, it stops when work is merged to main"),
                    (True, "Yes, a releaser tags and deploys each finished milestone")],
        "recommended": False, "why": "You release yourself, the way you do today.",
    },
    "release_profile": {
        "text": "How does the releaser ship?", "kind": "choice", "choices": _SHIPPING,
        "recommended": "kind", "why": "Rehearses every release on your machine.",
    },
    "challenger": {
        "text": "Should a second AI (Codex) check every commit before review?", "kind": "bool",
        "choices": [(False, "No"), (True, "Yes, needs the Codex CLI")],
        "recommended": False, "why": "The crew's reviewer checks every pull request either way.",
    },
    "builders_max": {
        "text": "How many builders may work at the same time?", "kind": "choice",
        "choices": [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4")],
        "recommended": "2", "why": "Each builder is a Claude Code session working on one repo.",
    },
}
GRANT_TEXT = "Anything you always allow the crew, in your own words?"
GRANT_WHY = "Your answers above already cover routine work."


def _choices_json(spec) -> list:
    return [{"value": v, "label": label} for v, label in spec["choices"]]


def question_json(qid: str, *, when=None, text=None, default=None, why=None, pattern="") -> dict:
    """One question as the contract prints it: {id, text, kind, choices, default, recommendation, why, pattern},
    and `when` (null, or {"releaser": true}: ask it only after that answer)."""
    if qid == "grant":
        return {"id": "grant", "text": GRANT_TEXT, "kind": "text", "choices": [], "default": "",
                "recommendation": "", "why": GRANT_WHY, "pattern": "", "when": None}
    if qid == "crew_name":
        return {"id": "crew_name", "text": text, "kind": "text", "choices": [], "default": default,
                "recommendation": default, "why": why, "pattern": NAME_PATTERN, "when": None}
    spec = QUESTIONS[qid]
    return {"id": qid, "text": spec["text"], "kind": spec["kind"], "choices": _choices_json(spec),
            "default": spec["recommended"], "recommendation": spec["recommended"], "why": spec["why"],
            "pattern": "", "when": when}


# ---------------------------------------------------------------------------------------------
# Small helpers

def _terminal() -> bool:
    """[ -t 0 ] && [ -t 1 ]"""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _read_line():
    """`IFS= read -r`: one line without its newline; None at the end of the input."""
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError, AttributeError):
        return None
    if line == "":
        return None
    return line[:-1] if line.endswith("\n") else line


def _err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def _home_of(env: dict) -> str:
    return env.get("HOME") or os.path.expanduser("~")


def paths_from(env: dict) -> CrewPaths:
    return CrewPaths(env.get("FACTORY_CREW_HOME") or f"{_home_of(env)}/factory")


def claude_bin(env: dict) -> str:
    """CLAUDE_BIN as typed, else the first `claude` on PATH that is not a cmux wrapper script
    (bash's claude_bin; TR-950 owns the full one)."""
    if env.get("CLAUDE_BIN"):
        return env["CLAUDE_BIN"]
    seen = set()
    for d in (env.get("PATH") or "").split(os.pathsep):
        for name in (("claude.exe", "claude.cmd", "claude.bat", "claude") if os.name == "nt" else ("claude",)):
            c = os.path.join(d, name) if d else name
            if c in seen or not (os.path.isfile(c) and os.access(c, os.X_OK)):
                continue
            seen.add(c)
            try:
                with open(c, "rb") as f:
                    body = f.read()
            except OSError:
                body = b""
            if body[:2] == b"#!" and b"cmux" in body:
                continue
            return c
    raise CrewError("claude not found on PATH (set CLAUDE_BIN to its path)")


def kit_plugin(paths: CrewPaths) -> str:
    """The kit this run belongs to (bash's KIT_PLUGIN): the plugin folder, physical; a run from the frozen
    copy means the checkout the copy was taken from."""
    from ..cli import plugin_dir  # not at import: tares.cli is heavy and the dispatcher must stay light
    kit = os.path.realpath(plugin_dir())
    if paths.checkout_file.is_file():
        home = os.path.realpath(paths.raw) if paths.home.is_dir() else paths.raw
        if (kit + "/").startswith(home + "/frozen-kit/"):
            c = state.read_text(paths.checkout_file).rstrip("\n")
            if os.path.isdir(c):
                kit = c
    return kit


def kit_version(kit: str) -> str:
    return state.jq_get(state.read_json(Path(kit) / ".claude-plugin" / "plugin.json", None), ["version"])


# ---------------------------------------------------------------------------------------------
# The plugin installed from a marketplace

def installed_tares(env: dict, my_version: str) -> list:
    """'name version scope' for each installed tares plugin of another version (a project install adds its path)."""
    doc = state.read_json(Path(_home_of(env)) / ".claude" / "plugins" / "installed_plugins.json", None)
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    out = []
    if not isinstance(plugins, dict):
        return out
    for key, rows in plugins.items():
        if not key.startswith("tares@") or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("version") == my_version:
                continue
            line = f"{key} {state.jq_raw(row.get('version'))} {state.jq_raw(row.get('scope'))}"
            path = row.get("projectPath")
            if path is not None and path is not False:
                line += f" ({state.jq_raw(path)})"
            out.append(line)
    return out


def clash_text(lines: list, kit: str, my_version: str) -> str:
    first = lines[0]
    key = first.split(" ", 1)[0]
    joined = ";".join(lines).replace(";", "; ")
    extra = ""
    if " project (" in first:
        where = re.sub(r".*[(](.*)[)].*", r"\1", first)
        extra = f" --scope project (run it in {where})"
    return (f"another version of the tares plugin is installed from a marketplace ({joined}). Where Claude Code "
            f"loads it, it takes the place of this one ({kit}, version {my_version}) and stations run without the "
            f"factory skills. If they report missing skills, remove it: claude plugin uninstall {key}{extra}")


# ---------------------------------------------------------------------------------------------
# Trust

def home_trusted(paths: CrewPaths, env: dict) -> bool:
    """Has Claude Code trusted the crew home, by the path as typed or the physical one?"""
    if not paths.home.is_dir():
        return False
    real = os.path.realpath(paths.raw)
    doc = state.read_json(Path(_home_of(env)) / ".claude.json", None)
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        return False
    return any(isinstance(projects.get(k), dict) and projects[k].get("hasTrustDialogAccepted") is True
               for k in (paths.raw, real))


# ---------------------------------------------------------------------------------------------
# crew up

class Up:
    """One `crew up` or `crew setup`: the crew's files, Tares, and the person's answers."""

    def __init__(self, env: dict, paths: CrewPaths | None = None, client: TaresClient | None = None):
        self.env = env
        self.paths = paths or paths_from(env)
        self.client = client or TaresClient(self.paths, env)
        self.assume_yes = False
        self._kit = None

    def say(self, msg: str) -> None:
        _err(f"{ME}: {msg}\n")

    def lsp_offer(self, claude_path: str) -> list:
        """Recommend the missing language servers of the handed-over repos; install only on a yes (bash lsp_offer).
        Claude Code is asked which plugins are on first, as bash does, even when no project has a repo."""
        plugins = doctor_lsp.read_plugins(claude_path)
        try:
            rows = self.client.project_repo_rows()
        except CrewError:
            rows = []
        return doctor_lsp.offer(doctor_lsp.missing(rows, plugins, self.env), self.assume_yes, claude_path, say=self.say)

    @property
    def kit(self) -> str:
        if self._kit is None:
            self._kit = kit_plugin(self.paths)
        return self._kit

    # -- prompts (bash: ask, choose) --

    def ask(self, question: str, rec: str, why: str, pattern: str) -> str:
        """One line of text; Enter takes the recommendation; an answer that does not match is asked again."""
        while True:
            ans = ""
            if not self.assume_yes:
                _err(f"\n{question}\n  recommended: {rec} ({why})\n  press enter to take it, or type your answer\n> ")
                got = _read_line()
                if got is None:
                    _err("\n")
                    got = ""
                ans = got
            ans = ans or rec
            if re.search(pattern, ans):
                return ans
            self.say(f'"{ans}" is not one of the choices')

    def choose(self, question: str, default: int, options: list) -> str:
        """One question with numbered choices; Enter takes the default. options: [(value, label)]. The number
        or the value picks one. Returns the value as text."""
        values = [str(v) for v, _ in options]
        if self.assume_yes:
            return values[default - 1]
        _err(f"\n{question}\n")
        for i, (_, label) in enumerate(options, 1):
            _err(f"  {i}) {label}{'   (recommended)' if i == default else ''}\n")
        while True:
            _err(f"  Choice [{default}]: ")
            got = _read_line()
            if got is None:
                _err("\n")
                got = ""
            ans = got or str(default)
            if re.fullmatch(r"[0-9]+", ans) and 1 <= int(ans) <= len(values):
                return values[int(ans) - 1]
            if ans in values:
                return ans
            _err(f"  Pick a number from 1 to {len(values)}.\n")

    def _choose_q(self, qid: str, prefix: str):
        spec = QUESTIONS[qid]
        options = [(("yes" if v else "no") if spec["kind"] == "bool" else v, label) for v, label in spec["choices"]]
        rec = spec["recommended"]
        rec = ("yes" if rec else "no") if spec["kind"] == "bool" else rec
        default = [v for v, _ in options].index(rec) + 1
        return self.choose(f"{prefix}{spec['text']}", default, options)

    # -- the settings --

    def ask_settings(self, missing: list) -> dict:
        """Ask for each missing setting and save the answers on Tares. Returns what was saved."""
        answers: dict = {}
        total = len(missing)
        n = 0
        _err("\nSetting up your crew. Asked once, saved on Tares.\n")
        for k in missing:
            if k == "autonomy":
                n += 1
                answers["autonomy"] = self._choose_q("autonomy", f"{n}/{total}  ")
            elif k == "releaser":
                n += 1
                answers["releaser"] = self._choose_q("releaser", f"{n}/{total}  ") == "yes"
                if answers["releaser"]:
                    answers["release_profile"] = self._choose_q("release_profile", "     ")
                    answers["prod_pattern"] = settings_defaults()["prod_pattern"]
            elif k == "challenger":
                n += 1
                answers["challenger"] = self._choose_q("challenger", f"{n}/{total}  ") == "yes"
            elif k == "builders_max":
                n += 1
                answers["builders_max"] = int(self._choose_q("builders_max", f"{n}/{total}  "))
            elif k in ("release_profile", "prod_pattern"):  # a releaser chosen earlier, its shipping not yet
                if "release_profile" in answers:
                    continue
                answers["release_profile"] = self._choose_q("release_profile", "")
                answers["prod_pattern"] = settings_defaults()["prod_pattern"]
        self.save_settings(answers)
        return answers

    def save_settings(self, body: dict) -> None:
        """Both paths save the settings here: PUT /api/crew/settings."""
        try:
            self.client.tares_http("PUT", "/api/crew/settings", state.dumps_compact(body))
        except TaresHTTPError:
            raise CrewError(f"could not save the crew settings on Tares at {self.client.tares}") from None
        self.client._settings = None

    def add_grant(self, words: str) -> None:
        """Both paths save a grant here: POST /api/grants for all projects."""
        try:
            self.client.tares_http("POST", "/api/grants", state.dumps_compact({"words": words, "scope": "all"}))
        except TaresHTTPError:
            raise CrewError("could not save the grant on Tares") from None

    def ask_grants(self) -> None:
        """Standing grants for all projects, one per question, until an empty answer."""
        if self.assume_yes:
            return
        while True:
            _err("\nAnything you always allow the crew, in your own words? Press Enter to skip.\n"
                 "  (for example: builders may bump patch versions of dependencies)\n> ")
            words = _read_line() or ""
            if not words:
                _err("\n")
                return
            self.add_grant(words)
            _err("  saved\n")

    def ensure_settings(self) -> None:
        raw = self.client.settings_raw()
        missing = settings_missing(raw)
        if not missing:
            return
        self.ask_settings(missing)
        self.ask_grants()

    # -- the crew's name --

    def store_crew_name(self, name: str) -> None:
        cfg = state.read_json(self.paths.crew_cfg, None)
        try:
            if not isinstance(cfg, dict):
                raise ValueError("not an object")
            cfg["crew_name"] = name
            state.write_json(self.paths.crew_cfg, cfg)
        except (OSError, ValueError):
            raise CrewError(f"could not save the crew name in {self.paths.shown('crew.json')}") from None

    def people_count(self) -> int:
        """People on this Tares other than the built-in local one; 0 when Tares can't say (an older Tares has
        no /api/people)."""
        try:
            doc = state.loads_json(self.client.tares_http("GET", "/api/people"))
            people = doc.get("people") if isinstance(doc, dict) else None
            return len([p for p in people if not (isinstance(p, dict) and p.get("id") == "local")]) if isinstance(people, list) else 0
        except (TaresHTTPError, ValueError, CrewError):
            return 0

    def suggest_crew_name(self, hostname: str | None = None) -> str:
        """<first name>-<short hostname>, lowercased."""
        full = (state.ccfg("human", "name", paths=self.paths) or "").strip()
        first = state.slugify(full.split(" ", 1)[0]) if full else ""
        if hostname is None:
            try:
                hostname = socket.gethostname().split(".")[0]
            except OSError:
                hostname = ""
        host = state.slugify(hostname)
        s = f"{first}{'-' if first and host else ''}{host}"[:31]
        s = re.sub(r"-$", "", s.replace("build", "bld"))
        return s if state.crew_name_ok(s) else "crew"

    def crew_name_state(self) -> tuple:
        """What setup_crew_name would do: ("keep", None), ("store", name) for FACTORY_CREW_NAME, or
        ("ask", suggestion). Raises the refusals bash dies with."""
        p = self.paths
        stored = state.ccfg("crew_name", paths=p)
        wanted = self.env.get("FACTORY_CREW_NAME") or ""
        if stored:
            if wanted and wanted != stored:
                raise CrewError(f"this crew is named {stored} (crew_name in {p.shown('crew.json')}), but "
                                f"FACTORY_CREW_NAME says {wanted}. Its stations carry the stored name: change "
                                f"crew_name in {p.shown('crew.json')} after `{ME} crew down`, or unset FACTORY_CREW_NAME.")
            return "keep", None
        # A crew already running with no stored name is the default crew: renaming it would start a second
        # orchestrator and reviewer beside the running ones, so it keeps "crew" until it is taken down.
        stations = state.read_json(p.stations, None)
        if isinstance(stations, dict) and any(isinstance(s, dict) and s.get("status") == "active" for s in stations.values()):
            if wanted and wanted != "crew":
                raise CrewError(f"this crew is already running under the name crew, but FACTORY_CREW_NAME says "
                                f"{wanted}. Take it down first (`{ME} crew down`) to give it a new name.")
            return "keep", None
        if wanted:
            return "store", wanted
        if self.people_count() <= 1:
            return "keep", None
        return "ask", self.suggest_crew_name()

    def setup_crew_name(self) -> None:
        """A crew shared with other people (TR-541) is named once at its first start, so its sessions are told
        apart from the same person's crew on another machine. One person keeps "crew"."""
        what, value = self.crew_name_state()
        if what == "store":
            self.store_crew_name(value)
        elif what == "ask":
            while True:
                n = self.ask("More than one person works on this Tares. What should this crew be called, so it is "
                             "told apart from the crews of the others (and from yours on another machine)?",
                             value, f"used in the station names: {value}-orchestrator, {value}-reviewer", NAME_PATTERN)
                if state.crew_name_ok(n):
                    break
                self.say(f'"{n}" is not a valid crew name: it is {CREW_NAME_RULE}')
            self.store_crew_name(n)

    # -- trust --

    def ensure_home_trusted(self, claude: str) -> None:
        if home_trusted(self.paths, self.env):
            return
        home = self.paths.raw
        how = f'cd {home} && claude, pick "Yes, I trust this folder", then /exit'
        if self.assume_yes or not _terminal():
            raise CrewError(f"Claude Code has not trusted the crew home {home} yet, and the stations run there. "
                            f"Trust it once ({how}), then run crew up again.")
        _err(f"\nThe crew keeps its own folder, {home}, whichever folder you run this from (FACTORY_CREW_HOME moves it).\n"
             "Its stations run there, and Claude Code starts no session in a folder you have not trusted.\n"
             'Open Claude Code there now to trust it? Pick "Yes, I trust this folder", then type /exit.\n'
             "  recommended: yes\n  press enter to take it, or type no\n> ")
        ans = _read_line()
        if ans is None:
            ans = "no"
        if (ans or "yes") not in ("y", "Y", "yes", "Yes"):
            raise CrewError(f"not starting the crew: its home {home} is not trusted. Trust it once ({how}), then run crew up again.")
        try:
            subprocess.run([claude], cwd=home, env=self.env)
        except OSError:
            pass
        if not home_trusted(self.paths, self.env):
            raise CrewError(f"Claude Code still has not trusted {home} (did the trust prompt get a no?). "
                            f"Trust it once ({how}), then run crew up again.")
        self.say("the crew home is trusted")

    # -- the contract --

    def outstanding(self) -> list:
        """Every question crew up would ask now, in the order it asks them (the crew's name, the settings, a grant)."""
        out = []
        what, value = self.crew_name_state()
        if what == "ask":
            out.append(question_json("crew_name", default=value, why=f"used in the station names: {value}-orchestrator, {value}-reviewer",
                                     text="More than one person works on this Tares. What should this crew be called, so it is "
                                          "told apart from the crews of the others (and from yours on another machine)?"))
        missing = settings_missing(self.client.settings_raw())
        shipping_done = False
        for k in missing:
            if k == "releaser":
                out.append(question_json("releaser"))
                out.append(question_json("release_profile", when={"releaser": True}))
                shipping_done = True
            elif k in ("release_profile", "prod_pattern"):
                if not shipping_done:
                    out.append(question_json("release_profile"))
                    shipping_done = True
            else:
                out.append(question_json(k))
        if missing:
            out.append(question_json("grant"))
        return out

    def check_answers(self, questions: list, answers) -> tuple:
        """(crew_name or None, settings body, grants, problems). Nothing is saved here."""
        problems = []
        if not isinstance(answers, dict):
            return None, {}, [], ["the answers must be a JSON object: {\"<question id>\": <answer>, ...}"]
        by_id = {q["id"]: q for q in questions}
        for k in answers:
            if k not in by_id:
                problems.append(f'"{k}" is not an outstanding question (outstanding: {", ".join(by_id) or "none"})')
        values: dict = {}
        for q in questions:
            qid = q["id"]
            if qid == "grant":
                continue
            if qid not in answers:
                if q["when"] is None or answers.get("releaser") is True or str(answers.get("releaser")).lower() in ("yes", "true"):
                    problems.append(f'no answer for "{qid}"')
                continue
            ans = answers[qid]
            if q["kind"] == "text":
                if not isinstance(ans, str) or not re.search(q["pattern"], ans) or not state.crew_name_ok(ans):
                    problems.append(f'"{ans}" is not a valid crew name: it is {CREW_NAME_RULE}')
                else:
                    values[qid] = ans
            elif q["kind"] == "bool":
                yes = ans is True or (isinstance(ans, str) and ans.lower() in ("yes", "true"))
                no = ans is False or (isinstance(ans, str) and ans.lower() in ("no", "false"))
                if yes or no:
                    values[qid] = yes
                else:
                    problems.append(f'"{ans}" is not one of the choices for "{qid}" (true, false)')
            else:
                allowed = [str(c["value"]) for c in q["choices"]]
                if isinstance(ans, bool) or str(ans) not in allowed:
                    problems.append(f'"{ans}" is not one of the choices for "{qid}" ({", ".join(allowed)})')
                else:
                    values[qid] = str(ans)
        if "release_profile" in answers and values.get("releaser") is False:
            problems.append('"release_profile" only applies when the crew has a releaser')
        grants = []
        if "grant" in answers:
            g = answers["grant"]
            items = g if isinstance(g, list) else [g]
            if "grant" not in by_id:
                pass  # already reported as not outstanding
            elif not all(isinstance(x, str) for x in items):
                problems.append('"grant" must be text, or a list of text')
            else:
                grants = [x for x in items if x]
        body: dict = {}
        for k in ("autonomy", "releaser", "release_profile", "challenger", "builders_max"):
            if k not in values:
                continue
            if k == "release_profile":
                if values.get("releaser") is False:
                    continue
                body["release_profile"] = values[k]
                body["prod_pattern"] = settings_defaults()["prod_pattern"]
            elif k == "builders_max":
                body[k] = int(values[k])
            else:
                body[k] = values[k]
        return values.get("crew_name"), body, grants, problems

    def save_answers(self, name, body: dict, grants: list) -> None:
        """What the prompts do after the last answer, for answers that came as data."""
        if name:
            self.store_crew_name(name)
        if body:
            self.save_settings(body)
        for words in grants:
            self.add_grant(words)


def _run(fn) -> int:
    try:
        return fn() or 0
    except CrewError as e:
        _err(e.line + "\n")
        return 1


def start_stations(env: dict, roles: list) -> int:
    """Start the crew's stations (bash's second half of cmd_crew_up). Until launch is ported (TR-953) this
    runs the bash internal command `_start-stations`; see the module docstring."""
    from ..cli import plugin_dir
    script = plugin_dir() / "bin" / "factory"
    if os.name == "nt" or not shutil.which("bash", path=env.get("PATH")):
        _err(f'{ME}: "crew up" cannot start the stations here: that step is not yet ported to the Python engine '
             "(TR-953) and runs in bash, which is not available. The settings and the crew name are saved.\n")
        return 2
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run(["bash", str(script), "_start-stations", *roles], env=env, stdin=subprocess.DEVNULL).returncode


def cmd_crew_up(argv: list, env: dict) -> int:
    return _run(lambda: _crew_up(argv, env))


def entry_crew_up(argv: list, env: dict) -> int:
    """What the dispatcher calls for `crew up`. The watch (TR-958) and the language server offer (TR-970) are
    wired in, so it runs here whatever the engine; `engine=bash` never reaches this (the dispatcher hands
    every command to bash then)."""
    return cmd_crew_up(argv, env)


def _crew_up(argv: list, env: dict) -> int:
    claude = claude_bin(env)
    up = Up(env)
    state.crew_name(up.paths)                      # a name that is not valid stops here, not halfway
    c = up.client
    c.prime()
    roles: list = []
    watch = True
    for a in argv[2:]:
        if a in ("--yes", "-y"):
            up.assume_yes = True
        elif a == "--no-watch":
            watch = False
        else:
            roles.append(a)
    c.need_capability("integrations")
    c.require_mcp()
    my_version = kit_version(up.kit)
    clash = installed_tares(env, my_version)
    if clash:
        up.say(f"warning: {clash_text(clash, up.kit, my_version)}")
    state.ensure_crew_home(up.paths)
    up.ensure_home_trusted(claude)
    c.keep_token()
    up.setup_crew_name()
    up.ensure_settings()
    c.prime()
    up.lsp_offer(claude)
    if not roles:
        roles = c.crew_roles()
    code = start_stations(env, roles)
    if code:
        return code
    if watch:
        watch_mod.start(up.paths, env)   # FACTORY_NO_WATCH is read there, as in bash
    _err(f"\nThe crew takes a few minutes to check in. Next:\n"
         f"  Watch it              {c.tares}/crew\n"
         f"  Answer its questions  {ME} crew attach orchestrator   (← to leave it running)\n"
         f"  Give it a project     {ME} spec \"what to build\"   (in the project's repo)\n"
         f"  Stop it               {ME} crew down\n")
    return 0


def cmd_crew_setup(argv: list, env: dict) -> int:
    """With `--json` a failure is the error document on stdout (schemas/error.json); otherwise a line on stderr."""
    try:
        return _crew_setup(argv, env) or 0
    except CrewError as e:
        if not contract.refuse(e, argv):
            _err(e.line + "\n")
        return 1


def entry_crew_setup(argv: list, env: dict) -> int:
    return cmd_crew_setup(argv, env)


def _setup_usage(args: list) -> int:
    text = f"usage: {ME} crew setup --questions [--json] | {ME} crew setup --answers <file.json> [--json]"
    if "--json" in args:
        contract.fail("usage", text, contract.EXIT_USAGE)
    raise CrewError(text)


def _crew_setup(argv: list, env: dict) -> int:
    args = argv[2:]
    flags = [a for a in args if a != "--json"]
    answers_file = None
    mode = ""
    i = 0
    while i < len(flags):
        if flags[i] == "--questions":
            mode, i = "questions", i + 1
        elif flags[i] == "--answers" and i + 1 < len(flags):
            mode, answers_file, i = "answers", flags[i + 1], i + 2
        else:
            return _setup_usage(args)
    if not mode:
        return _setup_usage(args)
    up = Up(env)
    state.crew_name(up.paths)
    up.client.prime()
    questions = up.outstanding()
    if mode == "questions":
        contract.emit({"questions": questions}, "setup_questions")
        return 0
    try:
        answers = state.loads_json(state.read_text(Path(answers_file)))
    except (OSError, ValueError) as e:
        raise CrewError(f"could not read the answers from {answers_file}: {e}") from None
    name, body, grants, problems = up.check_answers(questions, answers)
    if problems:
        if "--json" in args:  # the reader gets them in the error document, not on stderr
            raise CrewError("the answers were not saved: " + "; ".join(problems))
        for p in problems:
            _err(f"{ME}: {p}\n")
        raise CrewError("the answers were not saved: fix the ones listed above")
    state.ensure_crew_home(up.paths)
    up.save_answers(name, body, grants)
    saved = ([] if not name else ["crew_name"]) + list(body) + (["grant"] if grants else [])
    if "--json" in args:
        contract.emit({"saved": saved, "grants": len(grants)}, "setup_answers")
    else:
        sys.stdout.write(f"saved on Tares: {', '.join(saved) or 'nothing'}\n")
    return 0
