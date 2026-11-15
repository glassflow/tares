#!/usr/bin/env python3
"""Collect the files in changelog.d/ into CHANGELOG.md as a new release section (TR-1249).

    python scripts/changelog_collect.py <version> <date> [summary] [--root DIR]

The new `## [<version>] - <date>` section goes under `## [Unreleased]`: the summary paragraph, then Added,
Changed and Fixed. Bullets already under Unreleased come first in their category, then the fragments in file
name order. Unreleased is left in place, empty. The fragments are deleted, README.md never. A fragment with an
unknown category or no text stops everything: exit 1, the file named on stderr, nothing written or deleted.
"""
import sys
from pathlib import Path

CATEGORIES = ("Added", "Changed", "Fixed")


def fragment_bullet(name, text):
    """(category, bullet) from one fragment; ValueError naming the file when it is not well formed."""
    head, _, rest = text.strip().partition("\n")
    if head.strip() not in CATEGORIES:
        raise ValueError(f"{name}: the first line must be exactly Added, Changed or Fixed, not {head.strip()!r}")
    if not rest.strip():
        raise ValueError(f"{name}: no text after the category line")
    return head.strip(), "- " + "\n  ".join(ln.strip() for ln in rest.strip().splitlines() if ln.strip())


def parse_unreleased(body):
    """{category: [bullet, ...]} from the lines under `## [Unreleased]`; a bullet keeps its continuation lines."""
    found, cat = {}, None
    for ln in body.splitlines():
        if ln.startswith("### "):
            cat = ln[4:].strip()
            found.setdefault(cat, [])
        elif ln.startswith("- ") and cat:
            found[cat].append(ln.rstrip())
        elif ln.strip() and cat and found[cat]:
            found[cat][-1] += "\n" + ln.rstrip()
    return {c: b for c, b in found.items() if b}


def collect(changelog_text, fragments, version, date, summary=""):
    """The changelog text with a new release section, or the text unchanged when there is nothing to collect."""
    bullets = {}
    for name in sorted(fragments):
        cat, bullet = fragment_bullet(name, fragments[name])
        bullets.setdefault(cat, []).append(bullet)
    lines = changelog_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "## [Unreleased]"), None)
    if start is None:
        start = next((i for i, ln in enumerate(lines) if ln.startswith("## [")), len(lines))
        lines[start:start] = ["## [Unreleased]", ""]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
    carried = parse_unreleased("\n".join(lines[start + 1:end]))
    if not bullets and not carried:
        return changelog_text
    merged = {c: carried.get(c, []) + bullets.get(c, []) for c in set(carried) | set(bullets)}
    order = list(CATEGORIES) + sorted(set(merged) - set(CATEGORIES))
    section = [f"## [{version}] - {date}", ""]
    if (summary or "").strip():
        section += [summary.strip(), ""]
    for cat in order:
        if merged.get(cat):
            section += [f"### {cat}"] + "\n".join(merged[cat]).split("\n") + [""]
    out = lines[:start + 1] + [""] + section + lines[end:]
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv):
    args, root = list(argv), Path(__file__).resolve().parent.parent
    if "--root" in args:
        i = args.index("--root")
        root = Path(args[i + 1])
        del args[i:i + 2]
    if len(args) < 2:
        print("usage: changelog_collect.py <version> <date> [summary] [--root DIR]", file=sys.stderr)
        return 2
    version, date, summary = args[0], args[1], (args[2] if len(args) > 2 else "")
    path = root / "CHANGELOG.md"
    files = sorted(f for f in (root / "changelog.d").glob("*.md") if f.name != "README.md")
    old = path.read_text()
    try:
        new = collect(old, {f.name: f.read_text() for f in files}, version, date, summary)
    except ValueError as e:
        print(f"changelog_collect: {e}", file=sys.stderr)
        return 1
    if new == old:
        print("changelog_collect: no fragments in changelog.d/ and nothing under Unreleased, CHANGELOG.md is unchanged")
        return 0
    path.write_text(new)
    for f in files:
        f.unlink()
    print(f"changelog_collect: {version} written from {len(files)} fragment(s) and the Unreleased bullets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
