"""`tares factory report log|grep`: a station's log, kept apart from its report.

A station's report, `reports/<station>.md`, is its current state: small, rewritten in place, read whole
after a compaction. What happened goes to the log instead, one dated file per station and day under
`reports/log/<station>/<YYYY-MM-DD>.md`, which no station reads back whole.

  log "<text>"     appends `- HH:MMZ <text>` to today's log file (UTC); warns on stderr when the report
                   itself has grown past REPORT_CAP, since a report that big is history, not state
  grep <regex>     the matching lines of the logs and reports, newest day first, at most MAX_LINES lines
                   of at most MAX_LINE characters and MAX_BYTES in all, across every station; `--station <s>`
                   narrows it to one station, `--days <n>` to the last n days (default 14)

log writes for FACTORY_STATION unless `--station` names another. Standard library only.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

from . import contract
from .errors import CrewError
from .paths import CrewPaths, default_home

USAGE = ('usage: tares factory report log [--station <s>] "<text>"\n'
         '       tares factory report grep [--station <s>] [--days <n>] <regex>')
REPORT_CAP = 8 * 1024
MAX_LINES = 40
MAX_LINE = 300
MAX_BYTES = 4000
DEFAULT_DAYS = 14
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_STATION = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def _now() -> dt.datetime:
    """A function so a test can set the clock."""
    return dt.datetime.now(dt.timezone.utc)


def _paths(env: dict) -> CrewPaths:
    return CrewPaths(env.get("FACTORY_CREW_HOME") or env.get("FACTORY_HOME") or default_home(env))


def log_dir(paths: CrewPaths, station: str) -> Path:
    return paths.reports_dir / "log" / station


def _options(args: list, station: str) -> tuple[str, int, list]:
    days, rest = DEFAULT_DAYS, []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--station", "--days") and i + 1 < len(args):
            if a == "--station":
                station = args[i + 1]
            elif not args[i + 1].isdigit() or int(args[i + 1]) < 1:
                raise CrewError(f"--days takes a whole number of days, not {args[i + 1]!r}")
            else:
                days = int(args[i + 1])
            i += 2
            continue
        rest.append(a)
        i += 1
    if station and not _STATION.match(station):
        raise CrewError(f"{station!r} is not a station name")
    return station, days, rest


def append(paths: CrewPaths, station: str, text: str) -> Path:
    now = _now()
    folder = log_dir(paths, station)
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / f"{now:%Y-%m-%d}.md"
    line = " ".join(text.split())  # one entry is one line, so grep and the day files stay line-shaped
    with open(f, "a", encoding="utf-8", newline="\n") as out:
        out.write(f"- {now:%H:%MZ} {line}\n")
    return f


def _files(paths: CrewPaths, station: str, days: int) -> list[tuple[str, Path]]:
    """(label, file) to search, newest day first, then the reports themselves."""
    since = (_now() - dt.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    root = paths.reports_dir / "log"
    stations = [station] if station else sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    found = []
    for s in stations:
        folder = root / s
        if folder.is_dir():
            found += [(f"{s} {p.stem}", p) for p in folder.iterdir() if _DAY.match(p.name) and p.stem >= since]
    found.sort(key=lambda x: x[0].split(" ")[1], reverse=True)
    reports = [paths.reports_dir / f"{station}.md"] if station else sorted(paths.reports_dir.glob("*.md"))
    return found + [(f"{p.stem} report", p) for p in reports if p.is_file()]


def search(paths: CrewPaths, station: str, days: int, pattern: str) -> tuple[list[str], int]:
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise CrewError(f"{pattern!r} is not a regular expression: {e}") from None
    shown, size, more = [], 0, 0
    for label, f in _files(paths, station, days):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits = [ln for ln in lines if rx.search(ln)]
        for ln in reversed(hits):  # newest first within a file too
            ln = ln.strip()
            out = f"{label}: {ln[:MAX_LINE]}{'...' if len(ln) > MAX_LINE else ''}"
            if len(shown) >= MAX_LINES or size + len(out.encode()) + 1 > MAX_BYTES:
                more += 1
                continue
            shown.append(out)
            size += len(out.encode()) + 1
    return shown, more


def cmd_report(argv: list, env: dict) -> int:
    """`report log|grep ...`. argv starts with the word report."""
    try:
        return _report([a for a in argv[1:] if a != "--json"], env)
    except CrewError as e:
        if not contract.refuse(e, argv):
            print(e.line, file=sys.stderr)
        return 1


def _report(args: list, env: dict) -> int:
    if not args or args[0] not in ("log", "grep"):
        raise CrewError(USAGE)
    # log writes your own station's file; grep searches every station unless --station names one
    station, days, rest = _options(args[1:], env.get("FACTORY_STATION", "") if args[0] == "log" else "")
    paths = _paths(env)
    if args[0] == "log":
        if not station:
            raise CrewError("no station: FACTORY_STATION is unset, so name one with --station")
        text = " ".join(rest).strip()
        if not text:
            raise CrewError(USAGE)
        append(paths, station, text)
        report = paths.reports_dir / f"{station}.md"
        if report.is_file() and report.stat().st_size > REPORT_CAP:
            print(f"{report} is {report.stat().st_size // 1024} KB. It holds your current state only: rewrite it "
                  "without what is done (the log keeps the history).", file=sys.stderr)
        return 0
    if len(rest) != 1:
        raise CrewError(USAGE)
    shown, more = search(paths, station, days, rest[0])
    for ln in shown:
        print(ln)
    if more:
        print(f"{more} more matching lines not shown: narrow the pattern, --station or --days.")
    if not shown:
        print("no match")
    return 0
