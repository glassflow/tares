#!/usr/bin/env python3
"""Fail a pull request that changes a ported bash function without changing its Python twin (TR-1065).

While the bash crew (claude-plugin/bin/factory) is ported to Python (tares/crew/), both engines have to
keep doing the same thing. The ledger (scripts/crew_ledger.json, read by scripts/check_ledger.py) says
which bash function is ported (`ported: true`) and where its Python lives (`python`, "module:symbol").
This script compares the bash between two git revisions and, for every ported function whose body changed,
requires the same pull request to change that row's Python file too.

Usage:

    python scripts/check_port_drift.py <base> <head>             e.g. origin/main HEAD
    python scripts/check_port_drift.py <base> <head> --labels a,b,c      the PR's labels
    --ledger PATH   use another ledger (tests); default scripts/crew_ledger.json of the repo
    --repo DIR      the git repo to read (tests); default the repo this script lives in

What counts as the PR's change: the bash is read at the MERGE BASE of <base> and <head> and at <head>,
and the changed-file list is `git diff --name-only <merge-base> <head>`. A newer commit on <base> that
edits the bash is therefore not counted as this PR's change, which is what keeps a PR from failing for a
commit it did not make. In CI <head> is the PR's merge commit and <base> is origin/<target branch>.

Which functions: those of claude-plugin/bin/factory and of the helper scripts the ledger's `helpers`
section lists (claude-plugin/bin/factory-heavy, since TR-1103 the only one). A
function starts at a line `name() {` (at the start of the line; a comment or a command may follow the
brace) and runs to the next line that is `}` (a trailing comment is fine), the style of these files. A line
like `name() { cmd; }` is a one-line function, even when its command goes on over a backslash. Braces are never counted otherwise, because `${x}`, heredocs
and `case` would break that. Functions defined inside another function (indented) are part of the outer one. A function whose body
differs, that was removed or that was renamed counts as changed; a renamed one is found by its old name.

Escape hatch: `--labels` containing `port-drift-ok` prints a note and exits 0 before any work.

A ported row whose function is found in neither revision, or whose `file` is not one of the watched scripts,
is an error (exit 2), never a pass: a check that cannot see a function must not say it is unchanged.

What "changed its Python" means: the row's Python module file is in the PR's diff. Any edit to that file
counts, so a module several rows share (today tares/crew/client.py) is satisfied by any edit to it. The check
does not compare the Python symbol itself.

Exit status: 0 when clean, 1 when a ported function changed without its Python file, 2 for a bad command
line, an unreadable ledger, an unknown revision or a ported row it cannot find.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_ledger  # noqa: E402  (the same file lists as the ledger check, no heavy imports)

ROOT = HERE.parent
SKIP_LABEL = "port-drift-ok"
DEF_RE = re.compile(r"^([A-Za-z_][\w.:+-]*)\(\)\s*\{(.*)$")
ONE_LINE_END_RE = re.compile(r"[;&]\s*\}\s*(#.*)?$")  # `name() { cmd; }` and `name() { cmd; }  # note`: bash needs the ; (or &) before the closing brace
CLOSE_RE = re.compile(r"^\}\s*(#.*)?$")
SCRIPTS = [check_ledger.FACTORY_REL] + list(check_ledger.HELPER_RELS)


class Bad(Exception):
    """A bad command line, ledger or revision: exit 2."""


def git(repo, *args, check=True):
    try:
        r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True)
    except OSError as e:
        raise Bad("cannot run git: %s" % e)
    out = r.stdout.decode("utf-8", "replace")
    if check and r.returncode != 0:
        raise Bad("git %s failed: %s" % (" ".join(args), r.stderr.decode("utf-8", "replace").strip()))
    return r.returncode, out


def resolve(repo, rev):
    code, out = git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}", check=False)
    if code != 0 or not out.strip():
        raise Bad("unknown revision %s" % rev)
    return out.strip()


def functions(text):
    """{name: body text} for each `name() {` ... `}` block at the start of a line."""
    out = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = DEF_RE.match(lines[i])
        if not m:
            i += 1
            continue
        rest, k = m.group(2).strip(), i
        while lines[k].rstrip().endswith("\\") and k + 1 < len(lines):  # the command goes on over a backslash
            k += 1
            rest += " " + lines[k].strip()
        if not rest.startswith("#") and ONE_LINE_END_RE.search(rest):
            j = k  # a one-line function (its command may run over several lines)
        else:
            j = i + 1
            while j < len(lines) and not CLOSE_RE.match(lines[j]):
                j += 1
        body = "\n".join(lines[i:j + 1])
        name = m.group(1)
        out[name] = out[name] + "\n" + body if name in out else body
        i = j + 1
    return out


def functions_at(repo, rev, rel):
    """The functions of a file at a revision; {} when the file does not exist there."""
    code, out = git(repo, "show", "%s:%s" % (rev, rel), check=False)
    return functions(out) if code == 0 else {}


def python_file(repo, head, spec):
    """The repo path of the Python file a ledger `python` value lives in.

    "tares.crew.state:slugify" and "tares.crew.state" both give tares/crew/state.py; a package gives its
    __init__.py when that is what exists at head.
    """
    module = str(spec).split(":", 1)[0].strip()
    base = module.replace(".", "/")
    flat, pkg = base + ".py", base + "/__init__.py"
    if git(repo, "cat-file", "-e", "%s:%s" % (head, flat), check=False)[0] != 0 \
            and git(repo, "cat-file", "-e", "%s:%s" % (head, pkg), check=False)[0] == 0:
        return pkg
    return flat


def ported_rows(data):
    """[(script path, function name, python spec)] for each ported, not dropped function row."""
    out = []
    for section in ("functions", "helpers"):
        rows = data.get(section, [])
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("ported") is not True or isinstance(row.get("dropped"), str):
                continue
            if row.get("kind") not in (None, "function") or not isinstance(row.get("name"), str):
                continue
            spec = row.get("python")
            if not isinstance(spec, str) or not spec.strip():
                continue
            rel = check_ledger.FACTORY_REL if section == "functions" else row.get("file")
            if rel not in SCRIPTS:
                raise Bad("ported row %s() names the file %r, which is not a watched script" % (row["name"], rel))
            out.append((rel, row["name"], spec))
    return out


def load_ledger(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        raise Bad("cannot read the ledger %s: %s" % (path, e))
    except ValueError as e:
        raise Bad("the ledger %s is not valid JSON: %s" % (path, e))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise Bad("%s is not a version 1 ledger" % path)
    return data


def check(repo, base, head, ledger):
    """Returns (drifts, checked) where each drift is (script, function, python file, spec)."""
    base_sha, head_sha = resolve(repo, base), resolve(repo, head)
    code, out = git(repo, "merge-base", base_sha, head_sha, check=False)
    if code != 0 or not out.strip():
        raise Bad("%s and %s have no common ancestor" % (base, head))
    mb = out.strip()
    changed_files = set(git(repo, "diff", "--name-only", mb, head_sha)[1].splitlines())
    data = load_ledger(ledger)

    rows = ported_rows(data)
    cache = {}

    def funcs(rev, rel):
        if (rev, rel) not in cache:
            cache[(rev, rel)] = functions_at(repo, rev, rel)
        return cache[(rev, rel)]

    drifts, seen = [], set()
    for rel, name, spec in rows:
        before, after = funcs(mb, rel).get(name), funcs(head_sha, rel).get(name)
        if before is None and after is None:
            raise Bad("the ported function %s() of %s is in neither %s nor %s; the ledger or this parser is wrong"
                      % (name, rel, mb[:9], head_sha[:9]))
        if before == after:
            continue
        pyfile = python_file(repo, head_sha, spec)
        pkg_dir = pyfile.rsplit("/", 1)[0] + "/" if pyfile.endswith("/__init__.py") else None
        touched = pyfile in changed_files or (pkg_dir is not None and any(f.startswith(pkg_dir) for f in changed_files))
        if not touched and (rel, name) not in seen:
            seen.add((rel, name))
            drifts.append((rel, name, pyfile, spec))
    return drifts, len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fail when a ported bash function changes without its Python file.")
    ap.add_argument("base", help="the base revision, e.g. origin/main")
    ap.add_argument("head", help="the head revision, e.g. HEAD")
    ap.add_argument("--labels", default="", help="the PR's labels, comma separated; port-drift-ok skips the check")
    ap.add_argument("--ledger", metavar="PATH", help="use this ledger instead of scripts/crew_ledger.json of the repo")
    ap.add_argument("--repo", metavar="DIR", help="the git repo to read (default: the repo this script lives in)")
    args = ap.parse_args(argv)

    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    if SKIP_LABEL in labels:
        print("%s label set: port drift check skipped" % SKIP_LABEL)
        return 0

    repo = Path(args.repo) if args.repo else ROOT
    ledger = Path(args.ledger) if args.ledger else repo / "scripts" / "crew_ledger.json"
    try:
        drifts, checked = check(repo, args.base, args.head, ledger)
    except Bad as e:
        print("check_port_drift: %s" % e, file=sys.stderr)
        return 2

    if not drifts:
        print("check_port_drift: ok, %d ported functions checked, none changed without their Python" % checked)
        return 0
    print("check_port_drift: %d ported bash function(s) changed without their Python module" % len(drifts))
    for rel, name, pyfile, spec in drifts:
        print("  - %s() in %s changed. It is ported to %s, so change %s in the same pull request."
              % (name, rel, spec, pyfile))
    print("Change both engines together. If the bash change really needs no Python change, "
          "add the %s label to the pull request." % SKIP_LABEL)
    return 1


if __name__ == "__main__":
    sys.exit(main())
