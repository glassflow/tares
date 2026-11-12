#!/usr/bin/env python3
"""Check the crew porting ledger (scripts/crew_ledger.json): a finished record of the bash crew.

The ledger lists everything the bash crew (claude-plugin/bin/factory, removed in TR-975) did, one row per
function, helper, command, environment variable, state file, test file, cmux touchpoint and hook entry, so
nothing was dropped without anyone saying so. The bash is gone, so this script no longer reads it: it checks
the ledger itself. A row is finished when it is either dropped with a reason, or ported to Python code that
exists today. Nothing is compared with any other file, except the Python a ported row points at.

Usage (from any folder):

    python scripts/check_ledger.py                      check the ledger: every row dropped or ported, and the Python exists
    python scripts/check_ledger.py --export-md [PATH]   write the readable copy (stdout without PATH)
    python scripts/check_ledger.py --json               print the problems as JSON instead of text
    --ledger PATH uses another ledger file (tests).

Exit status: 0 when clean, 1 when there are problems, 2 for a bad command line or an unreadable file.

Ledger format (version 1). One JSON object:

    {"version": 1, "source": "claude-plugin/bin/factory (removed in TR-975)", "functions": [...],
     "helpers": [...], "dispatcher_cases": [...], "env_vars": [...], "state_files": [...],
     "test_suites": [...], "cmux_touchpoints": [...], "hook_entries": [...]}

Every row of every list is an object with these fields, all present:

    name     the identity of the row, unique in its list (helpers: unique with its file)
    kind     what sort of thing it is (function, script, python, case, env, internal, file, suite, touchpoint, hook)
    ticket   the Linear ticket that ported it, a string like "TR-955"; null only when dropped
    dropped  false, or a non-empty string saying why this has no Python counterpart
    python   where it lives in Python, see below; "" when dropped
    ported   true when the Python exists; false only for a dropped row
    note     free text, may be empty

Extra fields by list are informational and never checked: what, file, scope, path, reads, writes, where, match,
needle, arm, local_only.

Every row must be finished, one of:

    dropped   a reason string that is not empty (ticket may be null), ported false
    ported    ported true and a python value that resolves, in one of two forms:
              "module.path:symbol"  the module is a file under the repo (tares.crew.state is tares/crew/state.py,
                                    a package is its __init__.py) and the symbol is a top-level function, class or
                                    constant of it, or Class.method; found with ast, nothing is imported
              "path/to/file"        a plain repo path that must exist as a file (an environment variable, a state
                                    file or a test helper whose code is a whole file)
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
SECTIONS = ["functions", "helpers", "dispatcher_cases", "env_vars", "state_files", "test_suites",
            "cmux_touchpoints", "hook_entries"]
FIELDS = ["name", "kind", "ticket", "dropped", "python", "ported", "note"]
TICKET_RE = re.compile(r"^TR-\d+$")


class Problems:
    def __init__(self):
        self.items = []

    def add(self, section, name, kind, message):
        self.items.append({"section": section, "name": name, "problem": kind, "message": message})


# ---------------------------------------------------------------- the ledger

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def rows(data, section):
    r = data.get(section, [])
    return r if isinstance(r, list) else []


def row_id(section, row):
    name = row.get("name")
    if section == "helpers":
        return "%s::%s" % (row.get("file", ""), name)
    return name


# ---------------------------------------------------------------- the Python a ported row points at

def module_file(module: str):
    base = ROOT.joinpath(*module.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def top_level_names(body):
    names = {}
    for node in body:
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


def resolve_path(spec: str):
    """None when spec is a repo path of an existing file, else the reason."""
    p = Path(spec)
    if p.is_absolute() or ".." in p.parts:
        return "python path %r must be relative to the repo, without .." % spec
    if not (ROOT / p).is_file():
        return "python path %s does not exist as a file in the repo" % spec
    return None


def resolve_python(spec: str):
    """None when the python value resolves, else the reason. Parses the module with ast, imports nothing."""
    if not isinstance(spec, str) or not spec.strip():
        return "python is empty"
    if ":" not in spec:
        return resolve_path(spec)
    if spec.count(":") != 1 or not all(spec.split(":")):
        return "python must look like module.path:symbol or a repo path, got %r" % (spec,)
    module, symbol = spec.split(":")
    path = module_file(module)
    if path is None:
        return "module %s has no file under the repo (looked for %s.py and %s/__init__.py)" % (
            module, module.replace(".", "/"), module.replace(".", "/"))
    rel = path.relative_to(ROOT).as_posix()
    try:
        tree = ast.parse(read_text(path))
    except SyntaxError as e:
        return "%s does not parse: %s" % (rel, e)
    names = top_level_names(tree.body)
    head, _, rest = symbol.partition(".")
    if head not in names:
        return "%s has no top-level %s" % (rel, head)
    if rest:
        node = names[head]
        if not isinstance(node, ast.ClassDef):
            return "%s in %s is not a class, so %s cannot be looked up in it" % (head, rel, rest)
        if rest not in top_level_names(node.body):
            return "class %s in %s has no %s" % (head, rel, rest)
    return None


# ---------------------------------------------------------------- the rules

def check_rules(problems, data):
    for section in SECTIONS:
        seen = set()
        lst = data.get(section, [])
        if not isinstance(lst, list):
            problems.add(section, section, "bad-section", "%s must be a list of rows" % section)
            continue
        for k, row in enumerate(lst):
            if not isinstance(row, dict):
                problems.add(section, str(k), "bad-row", "%s row %d is not an object" % (section, k))
                continue
            if not isinstance(row.get("name"), str) or not row.get("name"):
                problems.add(section, str(k), "bad-row", "%s row %d has no name" % (section, k))
                continue
            name = str(row_id(section, row))
            missing = [f for f in FIELDS if f not in row]
            if missing:
                problems.add(section, name, "missing-field", "%s '%s' lacks the field(s) %s" % (section, name, ", ".join(missing)))
                continue
            if name in seen:
                problems.add(section, name, "duplicate", "%s: the ledger lists '%s' twice" % (section, name))
            seen.add(name)
            if not isinstance(row["kind"], str) or not row["kind"]:
                problems.add(section, name, "bad-kind", "%s '%s': kind must be a non-empty string" % (section, name))
            if not isinstance(row["note"], str):
                problems.add(section, name, "bad-note", "%s '%s': note must be a string" % (section, name))
            dropped, ported, python, ticket = row["dropped"], row["ported"], row["python"], row["ticket"]
            if not isinstance(ported, bool):
                problems.add(section, name, "bad-ported", "%s '%s': ported must be true or false" % (section, name))
                continue
            if ticket is not None and not (isinstance(ticket, str) and TICKET_RE.match(ticket)):
                problems.add(section, name, "bad-ticket", "%s '%s': ticket must be like TR-123 or null, got %r" % (section, name, ticket))
            if dropped is not False and not isinstance(dropped, str):
                problems.add(section, name, "bad-dropped", "%s '%s': dropped must be false or a reason string" % (section, name))
            elif isinstance(dropped, str):
                if not dropped.strip():
                    problems.add(section, name, "dropped-no-reason", "%s '%s' is dropped without a reason" % (section, name))
                if ported:
                    problems.add(section, name, "dropped-and-ported", "%s '%s' is both dropped and ported" % (section, name))
            else:
                if not isinstance(ticket, str):
                    problems.add(section, name, "no-owner", "%s '%s' has no owning ticket like TR-123 (ticket=%r) and is not dropped" % (section, name, ticket))
                if not ported:
                    problems.add(section, name, "not-finished", "%s '%s' is neither ported nor dropped: port it (ported true with its python), or set dropped to the reason" % (section, name))
                else:
                    why = resolve_python(python)
                    if why:
                        problems.add(section, name, "python-unresolved", "%s '%s' is ported to %r but %s" % (section, name, python, why))


# ---------------------------------------------------------------- markdown

TITLES = {
    "functions": "Functions of claude-plugin/bin/factory (removed in TR-975)",
    "helpers": "Helper scripts and glue",
    "dispatcher_cases": "Dispatcher cases",
    "env_vars": "Environment variables",
    "state_files": "State files",
    "test_suites": "Test files (tests/factory)",
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
        head = ["name"] + (["file"] if with_file else []) + ["what", "ticket", "python", "ported", "note"]
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "|".join("---" for _ in head) + "|")
        for r in rs:
            who = ("dropped: " + r["dropped"]) if isinstance(r.get("dropped"), str) else r.get("ticket")
            vals = [r.get("name")] + ([r.get("file")] if with_file else []) + [
                r.get("what", r.get("path", "")), who, r.get("python"), "yes" if r.get("ported") else "no", r.get("note")]
            out.append("| " + " | ".join(cell(v) for v in vals) + " |")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# ---------------------------------------------------------------- main

def write_lf(path, text):
    with open(str(path), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def load_ledger(path: Path, problems):
    """The ledger as a dict, or None (with the reason in problems). OSError is left to the caller."""
    try:
        data = json.loads(read_text(path))
    except ValueError as e:
        problems.add("ledger", str(path), "bad-json", "%s is not valid JSON: %s" % (path, e))
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        problems.add("ledger", str(path), "bad-version", "%s is not a version 1 ledger" % path)
        return None
    return data


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Check scripts/crew_ledger.json: every row dropped with a reason, or ported to Python that exists.")
    ap.add_argument("--export-md", nargs="?", const="-", metavar="PATH", help="write the readable Markdown copy to PATH or stdout")
    ap.add_argument("--json", action="store_true", help="print the problems as JSON")
    ap.add_argument("--ledger", metavar="PATH", help="use this ledger file instead of scripts/crew_ledger.json")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0

    ledger_path = Path(args.ledger) if args.ledger else LEDGER
    problems = Problems()
    try:
        data = load_ledger(ledger_path, problems)
    except OSError as e:
        print("check_ledger: cannot read %s: %s" % (ledger_path, e), file=sys.stderr)
        return 2

    if args.export_md is not None:
        if data is None:
            print("check_ledger: " + problems.items[0]["message"], file=sys.stderr)
            return 1
        text = export_md(data)
        if args.export_md == "-":
            sys.stdout.write(text)
        else:
            try:
                write_lf(Path(args.export_md), text)
            except OSError as e:
                print("check_ledger: cannot write %s: %s" % (args.export_md, e), file=sys.stderr)
                return 2
        return 0

    if data is not None:
        check_rules(problems, data)

    if args.json:
        print(json.dumps({"ok": not problems.items, "problems": problems.items}, indent=2, ensure_ascii=False))
    elif problems.items:
        print("check_ledger: %d problem(s)" % len(problems.items))
        for p in problems.items:
            print("  - " + p["message"])
    else:
        n = sum(len(rows(data, s)) for s in SECTIONS)
        print("check_ledger: ok, %d rows in %d sections, every one ported or dropped" % (n, len(SECTIONS)))
    return 1 if problems.items else 0


if __name__ == "__main__":
    sys.exit(main())
