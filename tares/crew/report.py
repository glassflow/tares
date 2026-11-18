"""`tares factory report log|grep|archive`: a station's log, kept apart from its report.

A station's report, `reports/<station>.md`, is its current state: small, rewritten in place, read whole
after a compaction. What happened goes to the log instead, one dated file per station and day under
`reports/log/<station>/<YYYY-MM-DD>.md`, which no station reads back whole.

  log "<text>"     appends `- HH:MMZ <text>` to today's log file (UTC); warns on stderr when the report
                   itself has grown past REPORT_CAP, since a report that big is history, not state
  grep <regex>     the matching lines of the logs and reports, newest day first, at most MAX_LINES lines
                   of at most MAX_LINE characters and MAX_BYTES in all, across every station; `--station <s>`
                   narrows it to one station, `--days <n>` to the last n days (default 14); the archived
                   files (final.md, archive-until-*.md) are searched too, whatever --days says;
                   `--section <name>` looks only at that `## <name>` section of each live report (no logs, no
                   archives; with a stations.json, only the stations it holds and has not retired) and the regex is then optional: `grep --section Undelivered` lists what each
                   station parked there
  archive          moves the old append-only reports into `reports/log/<station>/`: a report of a station
                   that is not live goes to `final.md`; a live station's report goes to
                   `archive-until-<day>.md` once the station has written its new state file as
                   `reports/<station>.next.md`, which then takes its place. A dry run unless `--apply`; it
                   never deletes a file and never overwrites one (a name that exists gets -2, -3, ...); it
                   refuses while a station it would touch is in a turn, and skips a station whose old
                   report has `## Undelivered` where the new one has not

log writes for FACTORY_STATION unless `--station` names another. Standard library only.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import sys
from pathlib import Path

from . import claude, contract, state
from .errors import CrewError, NotBetweenTurnsError
from .paths import CrewPaths, default_home
from .state import station_name_ok

USAGE = ('usage: tares factory report log [--station <s>] "<text>"\n'
         '       tares factory report grep [--station <s>] [--days <n>] [--section <name>] [<regex>]\n'
         '       tares factory report archive [--apply]')
REPORT_CAP = 8 * 1024
MAX_LINES = 40
MAX_LINE = 300
MAX_BYTES = 4000
DEFAULT_DAYS = 14
MAX_DAYS = 36500  # a hundred years: every file there is, and a date the calendar can still hold
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_ARCHIVED = re.compile(r"^(final|archive-until-[0-9-]+)(-\d+)?\.md$")
_ASCII_DIGITS = re.compile(r"[0-9]+")


def _now() -> dt.datetime:
    """A function so a test can set the clock."""
    return dt.datetime.now(dt.timezone.utc)


def _paths(env: dict) -> CrewPaths:
    return CrewPaths(env.get("FACTORY_CREW_HOME") or env.get("FACTORY_HOME") or default_home(env))


def log_dir(paths: CrewPaths, station: str) -> Path:
    return paths.reports_dir / "log" / station


def _days(text: str) -> int:
    """--days: ASCII digits only (`isdigit` also takes `²`, which `int` then rejects), at least 1, and no more
    than MAX_DAYS, so a huge number means all of it instead of a date that cannot exist."""
    if not _ASCII_DIGITS.fullmatch(text):
        raise CrewError(f"--days takes a whole number of days, not {text!r}")
    digits = text.lstrip("0")
    if not digits:
        raise CrewError(f"--days takes a whole number of days, not {text!r}")
    return MAX_DAYS if len(digits) > 6 else min(int(digits), MAX_DAYS)


def _options(args: list, station: str, leading_only: bool = False) -> tuple[str, int, list]:
    """Read --station and --days. With leading_only (log), only options before the text count, so the words
    of an entry that happen to be `--station x` stay in the entry."""
    days, rest = DEFAULT_DAYS, []
    i = 0
    while i < len(args):
        a = args[i]
        if leading_only and rest:
            rest += args[i:]
            break
        if leading_only and a == "--days":
            raise CrewError("--days is for grep, not log")
        if a in ("--station", "--days") and i + 1 < len(args):
            if a == "--station":
                station = args[i + 1]
            else:
                days = _days(args[i + 1])
            i += 2
            continue
        rest.append(a)
        i += 1
    if station and not station_name_ok(station):
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
    archived = []
    for s in stations:
        folder = root / s
        if folder.is_dir():
            archived += [(f"{s} {p.stem}", p) for p in sorted(folder.iterdir(), reverse=True) if _ARCHIVED.match(p.name)]
    reports = [paths.reports_dir / f"{station}.md"] if station else sorted(paths.reports_dir.glob("*.md"))
    return found + [(f"{p.stem} report", p) for p in reports if p.is_file()] + archived


_EMPTY = ("none", "- none", "* none")
_FENCE = ("```", "~~~")


def _section(lines: list, name: str) -> list:
    """The lines under `## <name>` (or `## <name> (2)`) up to the next `## ` heading, without blanks and a bare
    `none` or `- none`. A `## ` line inside a code fence is text, not a heading."""
    want = name.lower()
    keep, inside, fenced = [], False, False
    for ln in lines:
        if ln.lstrip().startswith(_FENCE):
            fenced = not fenced
        elif ln.startswith("## ") and not fenced:
            head = ln[3:].strip().lower()
            inside = head == want or bool(re.fullmatch(re.escape(want) + r"\s*\(.*\)", head))
            continue
        if inside and ln.strip() and ln.strip().lower() not in _EMPTY:
            keep.append(ln)
    return keep


def _live(paths: CrewPaths) -> set | None:
    """The names of the stations stations.json holds and has not retired; None when there is no such list
    (a home that is not a crew home), and then every report counts."""
    try:
        rows = state.stations_load(paths)
    except (CrewError, OSError, ValueError):
        return None
    live = {n for n, v in rows.items() if isinstance(v, dict) and v.get("status") != "retired"}
    return live or None


def search(paths: CrewPaths, station: str, days: int, pattern: str, section: str = "") -> tuple[list[str], int]:
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise CrewError(f"{pattern!r} is not a regular expression: {e}") from None
    shown, size, more = [], 0, 0
    live = _live(paths) if section else None
    for label, f in _files(paths, station, days):
        if section and not label.endswith(" report"):
            continue
        if live is not None and f.stem not in live:  # a dead station's parked lines would crowd out a live one's
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if section:
            lines = _section(lines, section)
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


# ---------- archive ----------


class Move:
    """One planned move: `src` goes to `dst` (a name nothing holds yet)."""

    def __init__(self, src: Path, dst: Path, station: str = ""):
        self.src, self.dst, self.station = src, dst, station


def _free(path: Path, taken: set) -> Path:
    """`path`, or `name-2.md`, `name-3.md`, ... when a file is there or an earlier move of this plan takes it."""
    cand, n = path, 1
    while cand.exists() or cand in taken:
        n += 1
        cand = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return cand


def plan_archive(paths: CrewPaths) -> tuple[list[Move], list[str], list[str]]:
    """(moves, notes, stops) for the reports folder. A station is live when stations.json holds it and it is
    not retired; every other `<name>.md` is history. Plain moves only: nothing here deletes."""
    state.require_crew(paths)
    if not paths.stations.is_file():
        raise CrewError(f"no stations.json in {paths.raw}: cannot tell a live station from a retired one")
    rows = state.stations_load(paths)
    live = {n for n, v in rows.items() if isinstance(v, dict) and v.get("status") != "retired"}
    if not live:
        raise CrewError("stations.json holds no live station: nothing to tell a live report from an old one")
    root = paths.reports_dir
    stamp = f"{_now():%Y-%m-%d}"
    moves, notes, stops, taken = [], [], [], set()
    for f in sorted(root.glob("*.md")) if root.is_dir() else []:
        name = f.stem
        if not f.is_file() or name.endswith(".next"):
            continue
        if not station_name_ok(name):
            notes.append(f"skip {f.name}: not a station name")
            continue
        folder = root / "log" / name
        if name not in live:
            dst = _free(folder / "final.md", taken)
            taken.add(dst)
            moves.append(Move(f, dst))
            continue
        new = root / f"{name}.next.md"
        if not new.is_file():
            notes.append(f"skip {name}: no {name}.next.md yet")
            continue
        old_text = f.read_text(encoding="utf-8", errors="replace")
        new_text = new.read_text(encoding="utf-8", errors="replace")
        if "## Undelivered" in old_text and "## Undelivered" not in new_text:
            stops.append(f"STOP {name}: the old report has an Undelivered section the new one lacks")
            continue
        dst = _free(folder / f"archive-until-{stamp}.md", taken)
        taken.add(dst)
        moves.append(Move(f, dst, name))
        moves.append(Move(new, f, name))
    return moves, notes, stops


def _idle(station: str, paths: CrewPaths) -> bool:
    """Nothing is writing the station's files: nobody has it open in a terminal, and it either has no live
    session (stopped) or is between turns. A session list that cannot be read is not idle."""
    try:
        if claude.attached(station, paths):
            return False
        sid = state.stations_get(station, "id", paths=paths)
        if not sid or claude.turn_state(sid) == "":
            return True
        return claude.between_turns(station, None, paths)
    except CrewError:
        return False


def _move(src: Path, dst: Path) -> None:
    """Copy `src` to a file that must not exist yet (x mode), check the copy, then take `src` away: a move that
    cannot replace a file, which `rename` and `replace` do. Any failure is a CrewError naming both files; a
    half-written `dst` is left (the next run picks another name) and `src` stays where it was."""
    try:
        with open(src, "rb") as a, open(dst, "xb") as b:
            shutil.copyfileobj(a, b)
    except FileExistsError:
        raise CrewError(f"{dst} exists: not overwriting it") from None
    except OSError as e:
        raise CrewError(f"cannot copy {src} to {dst}: {e.strerror or e}; {src} is left where it was") from None
    try:
        short = dst.stat().st_size != src.stat().st_size
    except OSError as e:
        raise CrewError(f"cannot check the copy {dst} of {src}: {e.strerror or e}; {src} is left where it was") from None
    if short:
        raise CrewError(f"copy of {src} to {dst} is short: {src} left where it was")
    try:
        src.unlink()
    except OSError as e:
        raise CrewError(f"copied {src} to {dst} but cannot take {src} away: {e.strerror or e} "
                        f"(a station may have it open); both files exist now") from None


def archive(paths: CrewPaths, apply: bool) -> int:
    moves, notes, stops = plan_archive(paths)
    root = paths.reports_dir

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    for n in notes + stops:
        print(n)
    busy = [n for n in sorted({m.station for m in moves if m.station}) if not _idle(n, paths)]
    if apply and busy:
        raise NotBetweenTurnsError(f"{', '.join(busy)} in a turn or open in a terminal: not moving anything; "
                                   "run it again between turns")
    rechecked = set()
    for m in moves:
        if apply:
            if m.station and m.station not in rechecked:
                rechecked.add(m.station)
                if not _idle(m.station, paths):
                    raise NotBetweenTurnsError(f"{m.station} started a turn: stopped before touching its files; "
                                               "run it again between turns")
            try:
                m.dst.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise CrewError(f"cannot make {m.dst.parent}: {e.strerror or e}") from None
            _move(m.src, m.dst)
        print(f"{'moved' if apply else 'would move'} {rel(m.src)} -> {rel(m.dst)}")
    if busy:
        print(f"busy: {', '.join(busy)} (a run with --apply would refuse until they are between turns)")
    print(f"{len(moves)} {'file' if len(moves) == 1 else 'files'} {'moved' if apply else 'to move; this was a dry run, add --apply to move them'}")
    return 1 if stops and apply else 0


def cmd_report(argv: list, env: dict) -> int:
    """`report log|grep|archive ...`. argv starts with the word report."""
    try:
        return _report([a for a in argv[1:] if a != "--json"], env)
    except CrewError as e:
        if not contract.refuse(e, argv):
            print(e.line, file=sys.stderr)
        return 1


def _report(args: list, env: dict) -> int:
    if not args or args[0] not in ("log", "grep", "archive"):
        raise CrewError(USAGE)
    if args[0] == "archive":
        if args[1:] not in ([], ["--apply"]):
            raise CrewError(USAGE)
        return archive(_paths(env), bool(args[1:]))
    # log writes your own station's file; grep searches every station unless --station names one
    args, section = list(args), ""
    if args[0] == "grep" and "--section" in args[1:]:
        i = args.index("--section", 1)
        if i + 1 >= len(args) or not args[i + 1].strip() or args[i + 1].startswith("-"):
            raise CrewError(USAGE)
        if "--days" in args:
            raise CrewError("--days does not apply to --section: it reads the reports, not the logs")
        section = args[i + 1].strip()
        del args[i:i + 2]
    station, days, rest = _options(args[1:], env.get("FACTORY_STATION", "") if args[0] == "log" else "", args[0] == "log")
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
    if len(rest) != (0 if section and not rest else 1):
        raise CrewError(USAGE)
    shown, more = search(paths, station, days, rest[0] if rest else ".", section)
    for ln in shown:
        print(ln)
    if more:
        print(f"{more} more matching lines not shown: narrow the pattern or --station." if section
              else f"{more} more matching lines not shown: narrow the pattern, --station or --days.")
    if not shown:
        print("no match")
    return 0
