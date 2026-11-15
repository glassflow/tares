#!/usr/bin/env python3
"""The CI check that a pull request leaves a changelog fragment (TR-1249).

    git diff --name-status origin/main...HEAD | python scripts/check_changelog.py --labels "bug,no changelog"

Input on stdin, one change per line as `<status>\\t<path>` (A, M, D, or R<score>\\t<old>\\t<new>, as
`git diff --name-status` prints it). A change under tares/ or ui/src/ needs a new or changed
changelog.d/<ticket>.md; a deleted one does not count, nor does README.md. The label `no changelog` skips it.
"""
import argparse
import os
import sys

SKIP_LABEL = "no changelog"
WATCHED = ("tares/", "ui/src/")


def parse(lines):
    """(all changed paths, paths added, modified or renamed to) from `git diff --name-status` lines."""
    changed, added = [], []
    for ln in lines:
        parts = ln.rstrip("\n").split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        status, paths = parts[0][0], parts[1:]
        changed += paths
        if status in "AMRC":
            added.append(paths[-1])
    return changed, added


def is_fragment(path):
    return path.startswith("changelog.d/") and path.endswith(".md") and "/" not in path[len("changelog.d/"):] \
        and path != "changelog.d/README.md"


def check(changed, labels, added=None):
    """An error message for the author, or None. `added` is the paths added or modified (default: `changed`)."""
    if SKIP_LABEL in [lb.strip() for lb in labels]:
        return None
    if not any(p.startswith(WATCHED) for p in changed):
        return None
    if any(is_fragment(p) for p in (changed if added is None else added)):
        return None
    return ("This pull request changes tares/ or ui/src/ but adds no changelog fragment. Add a file "
            "changelog.d/<ticket>.md (for example changelog.d/TR-123.md): the first line is Added, Changed or "
            "Fixed, the rest is the entry. See changelog.d/README.md. For a change nobody needs to read about, "
            f"add the label `{SKIP_LABEL}` instead.")


def main(argv, stdin):
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="", help="the pull request's labels, comma separated")
    ns = ap.parse_args(argv)
    changed, added = parse(stdin.read().splitlines())
    msg = check(changed, ns.labels.split(","), added)
    if msg:
        print(f"::error::{msg}" if "GITHUB_ACTIONS" in os.environ else msg, file=sys.stderr)
        return 1
    print("changelog check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], sys.stdin))
