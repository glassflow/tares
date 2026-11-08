#!/usr/bin/env python3
"""Check the crew porting ledger (scripts/crew_ledger.json) against the bash crew.

The ledger is the list of everything the bash crew does that the Python engine has to take over, so
nothing is dropped without anyone saying so. This script is the proof: it reads the bash files and the
ledger, and fails when they disagree. TR-1065 (a bash change must carry the Python change) reads the same
file, so keep the format below stable.

Usage (from any folder):

    python scripts/check_ledger.py --list-only      every bash thing is in the ledger, and no row is stale
    python scripts/check_ledger.py                  the same, plus the ported and owner rules below
    python scripts/check_ledger.py --export-md [PATH]   write the readable copy (stdout without PATH)
    python scripts/check_ledger.py --refresh-lines  rewrite the "line" fields from the current bash
    python scripts/check_ledger.py --json           print the problems as JSON instead of text
    --ledger PATH and --bash PATH use another ledger or another copy of claude-plugin/bin/factory (tests).

Exit status: 0 when clean, 1 when there are problems, 2 for a bad command line or an unreadable file.

Ledger format (version 1). One JSON object:

    {"version": 1, "source": "claude-plugin/bin/factory", "functions": [...], "helpers": [...],
     "dispatcher_cases": [...], "env_vars": [...], "state_files": [...], "test_suites": [...],
     "cmux_touchpoints": [...], "hook_entries": [...]}

Every row of every list is an object with these fields:

    name     the identity of the row (what the check compares with the bash, see below)
    kind     what sort of thing it is (function, script, python, case, env, file, suite, touchpoint, hook)
    ticket   the Linear ticket that ports it, a string like "TR-955"; null only when dropped
    dropped  false, or a string saying why this is not ported (then ticket may be null)
    python   where it lives in Python once ported, "module:symbol" (tares.crew.state:slugify), "" until then
    ported   true once the Python exists; the default mode then checks that python resolves
    note     free text, may be empty

Extra fields by list, all informational unless said:

    functions         line (current line, refreshed by --refresh-lines), what. name = the bash function name.
    helpers           file, line, what. name = function name; kind "script" rows are a whole helper file
                      (every file the check knows must have one) and kind "python" rows are `def name` in
                      a Python file (tares/cli.py).
    dispatcher_cases  line, scope (early, main, crew, spawn), arm (the case label), local_only (works while Tares
                      is down), what. name = the command words: "status", "crew up", "spawn builder".
    env_vars          reads, writes (booleans), where (who touches it), what. name = the variable. kind is "env" or
                      "internal" (a shell variable that only looks like one).
    state_files       scope ("crew_home" or "external"), path (what a person would type), needle (external rows: text
                      that must appear in the bash), reads, writes, what. name = for crew_home rows the key the
                      check derives from the bash (crew.json, HALT, stations/*.lock, reports); for external
                      rows any unique label.
    test_suites       name = a file in tests/factory.
    cmux_touchpoints  file, match (a regular expression), line, what. Every line of the scanned files that
                      mentions cmux must match one row, and every row must match at least one line.
    hook_entries      name = "<event>: <matcher or *>: <command and args>" of each entry in
                      claude-plugin/hooks/hooks.json that runs python3 or bash.

What the check compares:
    functions         every `name() {` or `function name {` in claude-plugin/bin/factory, nested ones too
    helpers           the same in the other scripts, plus the script rows and the `def` rows
    dispatcher_cases  the arms of the main case, `crew` case, `spawn` case, and the early help case
    env_vars          every FACTORY_*, TARES_*, CLAUDE_*, CMUX_* name, TARESD_URL, NO_COLOR, FORCE_COLOR, HOME, TMPDIR, PATH
                      in the bash files, hooks.json and tares/cli.py:_factory
    state_files       every $CREW_HOME/<file> the bash names, the $HOME dotfiles it reads, and the per-repo
                      $repo/.factory/<name> paths and info/exclude (each needs an external row with a needle)
    test_suites       every file in tests/factory
Line numbers move with every edit; they are not compared. --refresh-lines brings them up to date.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "scripts" / "crew_ledger.json"
FACTORY_REL = "claude-plugin/bin/factory"
HELPER_RELS = [
    "claude-plugin/bin/factory-heavy",
    "claude-plugin/bin/mcp-headers",
    "claude-plugin/hooks/guard.sh",
    "claude-plugin/hooks/session-start.sh",
]
HOOKS_REL = "claude-plugin/hooks/hooks.json"
CLI_REL = "tares/cli.py"
TESTS_REL = "tests/factory"
SECTIONS = ["functions", "helpers", "dispatcher_cases", "env_vars", "state_files", "test_suites",
            "cmux_touchpoints", "hook_entries"]
TICKET_RE = re.compile(r"^TR-\d+$")

FUNC_RE = re.compile(
    r"^\s*(?:function\s+([A-Za-z_][\w.:+-]*)\s*(?:\(\s*\))?\s*(?:\{|$)|([A-Za-z_][\w.:+-]*)\s*\(\s*\)\s*(?:\{|$))")
ENV_RE = re.compile(r"\b(?:(?:FACTORY|TARES|CLAUDE|CMUX)_[A-Z0-9_]*[A-Z0-9]|TARESD_URL|NO_COLOR|FORCE_COLOR|HOME|TMPDIR|PATH)\b")
HOME_FILE_RE = re.compile(r"\$HOME\"?/(\.[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.*$-]+)*)")
CREW_FILE_RE = re.compile(r"(?:CREW_HOME|FACTORY_HOME)\}?\"?/(\{[a-z,]+\}|[A-Za-z0-9_.-]+)")
REPO_FILE_RE = re.compile(r"\$\{?(?:repo|FACTORY_REPO)\}?\"?/(\.factory/[A-Za-z0-9_.-]+)|\b(info/exclude)\b")
STATION_FILE_RE = re.compile(r"/(?:\$\{?\w+\}?|\$\([^)]*\))\.(settings\.json|lock|done|pace|wake|grants)")


class Problems:
    def __init__(self):
        self.items = []

    def add(self, section, name, kind, message):
        self.items.append({"section": section, "name": name, "problem": kind, "message": message})


# ---------------------------------------------------------------- reading the bash

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def bash_functions(path: Path):
    """[(name, line)] for each shell function defined in the file, nested ones too, in file order."""
    out = []
    for n, line in enumerate(read_text(path).splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = FUNC_RE.match(line)
        if m:
            out.append((m.group(1) or m.group(2), n))
    return out


def case_arms(lines, start):
    """[(label, line)] for the arms of the `case` on lines[start] (0-based), up to its esac."""
    m = re.match(r"^(\s*)case\b", lines[start])
    indent = len(m.group(1))
    arm_re = re.compile(r"^ {%d}([^\s()#][^()]*?)\)(?:\s|$)" % (indent + 2))
    out = []
    for j in range(start + 1, len(lines)):
        if re.match(r"^ {%d}esac\b" % indent, lines[j]):
            break
        a = arm_re.match(lines[j])
        if a:
            out.append((a.group(1).strip(), j + 1))
    return out


def dispatcher_arms(path: Path):
    """[(name, scope, arm, line)] for the factory script's command words."""
    lines = read_text(path).splitlines()
    out = []
    for i, ln in enumerate(lines):
        if re.match(r"^case \"\$\{1:-help\}\" in help\|-h\|--help\)", ln):
            out.append(("help|-h|--help", "early", "help|-h|--help", i + 1))
    mains = [i for i, ln in enumerate(lines) if re.match(r'^case "\$cmd" in\s*$', ln)]
    if mains:
        for arm, n in case_arms(lines, mains[-1]):
            out.append((arm, "main", arm, n))
    for scope, fn, var in (("crew", "cmd_crew", "sub"), ("spawn", "cmd_spawn", "kind")):
        f = next((i for i, ln in enumerate(lines) if re.match(r"^%s\(\)" % fn, ln)), None)
        if f is None:
            continue
        c = next((i for i in range(f, len(lines)) if re.match(r'^\s*case "\$%s" in\s*$' % var, lines[i])), None)
        if c is None:
            continue
        for arm, n in case_arms(lines, c):
            out.append((scope + " " + arm, scope, arm, n))
    return out


def cli_factory_text(path: Path) -> str:
    """The source of tares/cli.py:_factory, the part that execs the bash."""
    text = read_text(path)
    m = re.search(r"^def _factory\(.*?(?=^def |\Z)", text, re.S | re.M)
    return m.group(0) if m else ""


def corpus(files: dict) -> str:
    """The text the env and state-file scans read: the bash files, hooks.json and the cli glue."""
    parts = []
    for rel, path in files.items():
        if rel == CLI_REL:
            parts.append(cli_factory_text(path))
        elif path.is_file():
            parts.append(read_text(path))
    return "\n".join(parts)


def env_names(text: str):
    return set(ENV_RE.findall(text))


def crew_keys(text: str):
    keys = set()
    for m in CREW_FILE_RE.finditer(text):
        comp = m.group(1)
        if comp.startswith("{"):
            keys.update(comp[1:-1].split(","))
        elif comp == "stations":
            s = STATION_FILE_RE.match(text, m.end())
            keys.add("stations/*." + s.group(1) if s else "stations")
        else:
            comp = comp.rstrip(".-,")
            if comp:
                keys.add(comp)
    return keys


def hook_keys(path: Path):
    keys = []
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        return keys
    for event, groups in (data.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                cmd = str(h.get("command", ""))
                args = " ".join(str(a) for a in h.get("args", []))
                if re.search(r"\b(python3|bash)\b", cmd):
                    keys.append("%s: %s: %s" % (event, g.get("matcher") or "*", (cmd + " " + args).strip()))
    return keys


# ---------------------------------------------------------------- the ledger

def load_ledger(path: Path):
    try:
        data = json.loads(read_text(path))
    except OSError as e:
        raise SystemExit("check_ledger: cannot read %s: %s" % (path, e))
    except ValueError as e:
        raise SystemExit("check_ledger: %s is not valid JSON: %s" % (path, e))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SystemExit("check_ledger: %s is not a version 1 ledger" % path)
    return data


def rows(data, section):
    r = data.get(section, [])
    return r if isinstance(r, list) else []


def row_id(section, row):
    name = row.get("name")
    if section == "helpers":
        return "%s::%s" % (row.get("file", ""), name)
    return name


# ---------------------------------------------------------------- the comparisons

def compare(problems, section, found, ledger_rows, label, where):
    """found: {id: where-it-is}. Report what is missing from the ledger and what is stale in it."""
    seen = {}
    for row in ledger_rows:
        i = row_id(section, row)
        if not isinstance(row.get("name"), str) or not row.get("name"):
            problems.add(section, str(i), "bad-row", "%s has a row without a name" % section)
            continue
        if i in seen:
            problems.add(section, i, "duplicate", "%s: the ledger lists %s '%s' twice" % (section, label, i))
        seen[i] = row
    for i in sorted(found):
        if i not in seen:
            problems.add(section, i, "missing", "missing from the ledger: %s '%s' (%s). Add a row to %s in scripts/crew_ledger.json with an owner or a reason it is dropped"
                         % (label, i, found[i], section))
    for i in sorted(seen):
        if i not in found:
            problems.add(section, i, "stale", "stale ledger row: %s '%s' in %s no longer exists in %s. Remove the row or rename it"
                         % (label, i, section, where))


def check_lists(problems, data, files, factory_path, ledger_path):
    fac_rel = FACTORY_REL

    # functions
    found = {}
    for name, n in bash_functions(factory_path):
        found.setdefault(name, "%s:%d" % (fac_rel, n))
    compare(problems, "functions", found, rows(data, "functions"), "function", fac_rel)

    # helpers: functions, whole scripts, python glue
    found = {}
    for rel in HELPER_RELS:
        p = files[rel]
        if p.is_file():
            found["%s::%s" % (rel, rel.rsplit("/", 1)[-1])] = rel
            for name, n in bash_functions(p):
                found.setdefault("%s::%s" % (rel, name), "%s:%d" % (rel, n))
    helper_rows = rows(data, "helpers")
    for row in helper_rows:
        if row.get("kind") == "python":
            p = files.get(row.get("file"))
            ok = p is not None and p.is_file() and re.search(r"^def %s\(" % re.escape(str(row.get("name"))), read_text(p), re.M)
            if ok:
                found[row_id("helpers", row)] = row.get("file")
    compare(problems, "helpers", found, helper_rows, "helper", "the helper scripts")

    # dispatcher cases
    found = {}
    for name, scope, arm, n in dispatcher_arms(factory_path):
        found.setdefault(name, "%s:%d" % (fac_rel, n))
    compare(problems, "dispatcher_cases", found, rows(data, "dispatcher_cases"), "command", fac_rel)

    # environment variables
    text = corpus(files)
    found = {n: "named in the crew's bash, hooks or cli glue" for n in env_names(text)}
    compare(problems, "env_vars", found, rows(data, "env_vars"), "environment variable", "the crew's files")

    # state files
    found = {k: "named as $CREW_HOME/%s" % k for k in crew_keys(text)}
    state_rows = rows(data, "state_files")
    crew_rows = [r for r in state_rows if r.get("scope") == "crew_home"]
    ext_rows = [r for r in state_rows if r.get("scope") != "crew_home"]
    for r in ext_rows:
        needle = r.get("needle")
        if isinstance(needle, str) and needle and needle in text:
            found[r["name"]] = "found in the files"
    for m in HOME_FILE_RE.finditer(text):
        got = m.group(1)
        if not any(isinstance(r.get("needle"), str) and r["needle"] and (r["needle"] in got or got in r["needle"]) for r in ext_rows):
            found["$HOME/" + got] = "read as $HOME/%s" % got
    for m in REPO_FILE_RE.finditer(text):
        got = m.group(1) or m.group(2)
        if not any(isinstance(r.get("needle"), str) and r["needle"] and (r["needle"] in got or got in r["needle"]) for r in ext_rows):
            key = ("<repo>/.git/" if got.startswith("info/") else "<repo>/") + got
            found[key] = "named as %s" % key
    compare(problems, "state_files", found, state_rows, "state file", "the crew's files")

    # test suites
    tdir = ROOT / TESTS_REL
    found = {p.name: TESTS_REL for p in sorted(tdir.iterdir()) if p.is_file()} if tdir.is_dir() else {}
    compare(problems, "test_suites", found, rows(data, "test_suites"), "test file", TESTS_REL)

    # cmux
    scanned = [fac_rel] + HELPER_RELS + [HOOKS_REL]
    lines_by_file = {}
    for rel in scanned:
        p = files.get(rel) or ROOT / rel
        if p.is_file():
            lines_by_file[rel] = read_text(p).splitlines()
    if tdir.is_dir():
        for p in sorted(tdir.iterdir()):
            if p.is_file():
                lines_by_file["%s/%s" % (TESTS_REL, p.name)] = read_text(p).splitlines()
    touch = rows(data, "cmux_touchpoints")
    used = set()
    for rel, lines in sorted(lines_by_file.items()):
        for n, ln in enumerate(lines, 1):
            if "cmux" not in ln.lower():
                continue
            hit = False
            for k, r in enumerate(touch):
                if r.get("file") == rel and _search(r.get("match"), ln):
                    used.add(k)
                    hit = True
            if not hit:
                problems.add("cmux_touchpoints", "%s:%d" % (rel, n), "missing",
                             "missing from the ledger: cmux mention at %s:%d: %s. Add or widen a row in cmux_touchpoints"
                             % (rel, n, ln.strip()[:90]))
    for k, r in enumerate(touch):
        if not isinstance(r.get("name"), str) or not r.get("name"):
            problems.add("cmux_touchpoints", str(k), "bad-row", "cmux_touchpoints has a row without a name")
        elif k not in used:
            problems.add("cmux_touchpoints", r["name"], "stale",
                         "stale ledger row: cmux touchpoint '%s' matches no line of %s any more. Remove or fix the row"
                         % (r["name"], r.get("file")))

    # hook entries
    found = {k: HOOKS_REL for k in hook_keys(files[HOOKS_REL])}
    compare(problems, "hook_entries", found, rows(data, "hook_entries"), "hook entry", HOOKS_REL)


def _search(pattern, line):
    try:
        return bool(pattern) and re.search(pattern, line) is not None
    except re.error:
        return False


# ---------------------------------------------------------------- the rules for ported and owners

def module_file(module: str):
    base = ROOT.joinpath(*module.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def top_level_names(tree):
    names = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names[n.id] = node
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names[node.target.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names[(a.asname or a.name).split(".")[0]] = node
    return names


def resolve_python(spec: str):
    """None when module:symbol resolves, else the reason. Parses the module with ast, imports nothing."""
    if not isinstance(spec, str) or spec.count(":") != 1 or not all(spec.split(":")):
        return "python must look like module.path:symbol, got %r" % (spec,)
    module, symbol = spec.split(":")
    path = module_file(module)
    if path is None:
        return "module %s has no file under the repo (looked for %s.py and %s/__init__.py)" % (
            module, module.replace(".", "/"), module.replace(".", "/"))
    try:
        tree = ast.parse(read_text(path))
    except SyntaxError as e:
        return "%s does not parse: %s" % (path.relative_to(ROOT), e)
    names = top_level_names(tree)
    head, _, rest = symbol.partition(".")
    if head not in names:
        return "%s has no top-level %s" % (path.relative_to(ROOT).as_posix(), head)
    if rest:
        node = names[head]
        if not isinstance(node, ast.ClassDef):
            return "%s in %s is not a class, so %s cannot be looked up in it" % (head, path.relative_to(ROOT).as_posix(), rest)
        inner = top_level_names(ast.Module(body=node.body, type_ignores=[]))
        if rest not in inner:
            return "class %s in %s has no %s" % (head, path.relative_to(ROOT).as_posix(), rest)
    return None


def check_rules(problems, data):
    for section in SECTIONS:
        for row in rows(data, section):
            name = str(row_id(section, row))
            if not isinstance(row.get("name"), str) or not row.get("name"):
                continue
            dropped = row.get("dropped")
            ported = row.get("ported")
            python = row.get("python", "")
            ticket = row.get("ticket")
            if dropped is not False and not isinstance(dropped, str):
                problems.add(section, name, "bad-dropped", "%s '%s': dropped must be false or a reason string" % (section, name))
                continue
            if isinstance(dropped, str):
                if not dropped.strip():
                    problems.add(section, name, "dropped-no-reason", "%s '%s' is dropped without a reason" % (section, name))
                if ported:
                    problems.add(section, name, "dropped-and-ported", "%s '%s' is both dropped and ported" % (section, name))
            else:
                if not isinstance(ticket, str) or not TICKET_RE.match(ticket):
                    problems.add(section, name, "no-owner", "%s '%s' has no owning ticket like TR-123 (ticket=%r) and is not dropped" % (section, name, ticket))
            if not isinstance(ported, bool):
                problems.add(section, name, "bad-ported", "%s '%s': ported must be true or false" % (section, name))
                continue
            if ported:
                if not python:
                    problems.add(section, name, "ported-no-python", "%s '%s' is ported but its python field is empty" % (section, name))
                elif not isinstance(dropped, str):
                    why = resolve_python(python)
                    if why:
                        problems.add(section, name, "python-unresolved", "%s '%s' is ported to %s but %s" % (section, name, python, why))


# ---------------------------------------------------------------- lines and markdown

def refresh_lines(data, files, factory_path):
    """Update the line fields from the current bash. Returns how many changed."""
    changed = 0

    def put(row, n):
        nonlocal changed
        if row.get("line") != n:
            row["line"] = n
            changed += 1

    funcs = {}
    for name, n in bash_functions(factory_path):
        funcs.setdefault(name, n)
    for row in rows(data, "functions"):
        if row.get("name") in funcs:
            put(row, funcs[row["name"]])
    hf = {}
    for rel in HELPER_RELS:
        for name, n in bash_functions(files[rel]) if files[rel].is_file() else []:
            hf.setdefault((rel, name), n)
    for row in rows(data, "helpers"):
        if (row.get("file"), row.get("name")) in hf:
            put(row, hf[(row["file"], row["name"])])
    arms = {name: n for name, _, _, n in dispatcher_arms(factory_path)}
    for row in rows(data, "dispatcher_cases"):
        if row.get("name") in arms:
            put(row, arms[row["name"]])
    for row in rows(data, "cmux_touchpoints"):
        p = files.get(row.get("file")) or ROOT / str(row.get("file"))
        if p.is_file():
            for n, ln in enumerate(read_text(p).splitlines(), 1):
                if "cmux" in ln.lower() and _search(row.get("match"), ln):
                    put(row, n)
                    break
    return changed


def count_stale_lines(data, files, factory_path):
    import copy
    return refresh_lines(copy.deepcopy(data), files, factory_path)


TITLES = {
    "functions": "Functions of claude-plugin/bin/factory",
    "helpers": "Helper scripts and glue",
    "dispatcher_cases": "Dispatcher cases",
    "env_vars": "Environment variables",
    "state_files": "State files",
    "test_suites": "Test suites (tests/factory)",
    "cmux_touchpoints": "cmux touchpoints",
    "hook_entries": "Hook entries (hooks.json)",
}


def cell(v):
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ")


def export_md(data) -> str:
    out = ["# Crew porting ledger", "",
           "Generated from scripts/crew_ledger.json by `python scripts/check_ledger.py --export-md`. Edit the JSON, not this copy.", ""]
    total = ported = dropped = 0
    for section in SECTIONS:
        for r in rows(data, section):
            total += 1
            ported += 1 if r.get("ported") is True else 0
            dropped += 1 if isinstance(r.get("dropped"), str) else 0
    out += ["%d rows, %d ported, %d dropped." % (total, ported, dropped), ""]
    for section in SECTIONS:
        rs = rows(data, section)
        out += ["## %s (%d)" % (TITLES[section], len(rs)), ""]
        with_file = any("file" in r for r in rs)
        head = ["name"] + (["file"] if with_file else []) + ["line", "what", "ticket", "python", "ported", "note"]
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "|".join("---" for _ in head) + "|")
        for r in rs:
            who = ("dropped: " + r["dropped"]) if isinstance(r.get("dropped"), str) else r.get("ticket")
            vals = [r.get("name")] + ([r.get("file")] if with_file else []) + [
                r.get("line"), r.get("what", r.get("path", "")), who, r.get("python"), "yes" if r.get("ported") else "no", r.get("note")]
            out.append("| " + " | ".join(cell(v) for v in vals) + " |")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# ---------------------------------------------------------------- main

def write_lf(path, text):
    with open(str(path), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Check scripts/crew_ledger.json against the bash crew.")
    ap.add_argument("--list-only", action="store_true", help="only check that the ledger and the bash list the same things")
    ap.add_argument("--export-md", nargs="?", const="-", metavar="PATH", help="write the readable Markdown copy to PATH or stdout")
    ap.add_argument("--refresh-lines", action="store_true", help="rewrite the line fields of the ledger from the current bash")
    ap.add_argument("--json", action="store_true", help="print the problems as JSON")
    ap.add_argument("--ledger", metavar="PATH", help="use this ledger file instead of scripts/crew_ledger.json")
    ap.add_argument("--bash", metavar="PATH", help="use this file instead of claude-plugin/bin/factory")
    args = ap.parse_args(argv)

    ledger_path = Path(args.ledger) if args.ledger else LEDGER
    factory_path = Path(args.bash) if args.bash else ROOT / FACTORY_REL
    if not factory_path.is_file():
        print("check_ledger: cannot find the bash script %s" % factory_path, file=sys.stderr)
        return 2
    data = load_ledger(ledger_path)
    files = {FACTORY_REL: factory_path, HOOKS_REL: ROOT / HOOKS_REL, CLI_REL: ROOT / CLI_REL}
    for rel in HELPER_RELS:
        files[rel] = ROOT / rel

    if args.export_md is not None:
        text = export_md(data)
        if args.export_md == "-":
            sys.stdout.write(text)
        else:
            write_lf(Path(args.export_md), text)
        return 0

    if args.refresh_lines:
        n = refresh_lines(data, files, factory_path)
        write_lf(ledger_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("check_ledger: %d line numbers updated in %s" % (n, ledger_path))
        return 0

    problems = Problems()
    check_lists(problems, data, files, factory_path, ledger_path)
    if not args.list_only:
        check_rules(problems, data)

    stale_lines = count_stale_lines(data, files, factory_path)
    if args.json:
        print(json.dumps({"ok": not problems.items, "problems": problems.items, "stale_lines": stale_lines}, indent=2, ensure_ascii=False))
    elif problems.items:
        print("check_ledger: %d problem(s)" % len(problems.items))
        for p in problems.items:
            print("  - " + p["message"])
    else:
        total = sum(len(rows(data, s)) for s in SECTIONS)
        print("check_ledger: ok, %d rows in %d sections%s" % (total, len(SECTIONS), " (list only)" if args.list_only else ""))
        if stale_lines:
            print("  note: %d line numbers are out of date; run: python scripts/check_ledger.py --refresh-lines" % stale_lines)
    return 1 if problems.items else 0


if __name__ == "__main__":
    sys.exit(main())
