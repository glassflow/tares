"""`tares factory report log|grep|archive` (tares/crew/report.py): a station's dated log, apart from its report.

log appends one dated line to reports/log/<station>/<day>.md, flattens newlines, needs a station, and
warns when the report itself is past the cap; grep is newest first, case-insensitive, bounded in lines,
line length and bytes, says how many lines it left out, honours --station and --days, and refuses a bad
regex or bad usage with exit 1; the command is in the dispatch table.

archive is a dry run unless --apply, never deletes or overwrites (a name that exists gets -2, -3), moves a
retired or unknown station's report to final.md and a live one's to archive-until-<day>.md with its
<station>.next.md in its place, keeps the Undelivered section, refuses while a station is in a turn, and
every run here is in a scratch crew home.

Run: .venv/bin/python tests/test_crew_report.py
"""
import contextlib
import datetime as dt
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares.crew import dispatch, report  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


TMP = Path(tempfile.mkdtemp(prefix="crew_report_"))
HOME = TMP / "factory"
ENV = {"FACTORY_CREW_HOME": str(HOME), "FACTORY_STATION": "crew-orchestrator", "HOME": str(TMP)}
CLOCK = [dt.datetime(2026, 11, 14, 15, 30, tzinfo=dt.timezone.utc)]
report._now = lambda: CLOCK[0]


def run(*args, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = report.cmd_report(["report", *args], ENV if env is None else env)
    return code, out.getvalue(), err.getvalue()


LOG = HOME / "reports" / "log" / "crew-orchestrator"

print("log")
code, out, err = run("log", "TR-1363 filed;", "assign next tick")
day = LOG / "2026-11-14.md"
ck("log exits 0 and writes today's file", code == 0 and day.is_file(), (code, err))
ck("...one dated line, the words joined", day.read_text() == "- 15:30Z TR-1363 filed; assign next tick\n", day.read_text())
run("log", "two\nlines\n  here")
ck("a newline in the text stays one line", day.read_text().splitlines()[-1] == "- 15:30Z two lines here", day.read_text())
CLOCK[0] = dt.datetime(2026, 11, 15, 0, 5, tzinfo=dt.timezone.utc)
run("log", "after midnight")
ck("a new UTC day is a new file", (LOG / "2026-11-15.md").read_text() == "- 00:05Z after midnight\n")
code, out, err = run("log", "x", env={k: v for k, v in ENV.items() if k != "FACTORY_STATION"})
ck("no FACTORY_STATION and no --station: exit 1, says so", code == 1 and "FACTORY_STATION" in err, err)
code, out, err = run("log", "--station", "crew-reviewer", "for the reviewer")
ck("--station names another station's log", code == 0 and (HOME / "reports/log/crew-reviewer/2026-11-15.md").is_file())
code, out, err = run("log", "--station", "../etc", "x")
ck("a station name with a path in it is refused", code == 1 and "not a station name" in err, err)
code, out, err = run("log", "--station", "my_station", "underscore name")
ck("a station name with an underscore is accepted, as station launch accepts it",
   code == 0 and (HOME / "reports/log/my_station/2026-11-15.md").is_file(), err)
code, out, err = run("log", "moved files --station crew-reviewer --days 3")
ck("--station and --days inside the text stay in the entry",
   code == 0 and (LOG / "2026-11-15.md").read_text().splitlines()[-1] == "- 00:05Z moved files --station crew-reviewer --days 3"
   and "moved files" not in (HOME / "reports/log/crew-reviewer/2026-11-15.md").read_text(), err)
code, out, err = run("log", "moved", "files", "--station", "crew-reviewer")
ck("--station after the first word of the text stays in the entry when the words come unquoted",
   code == 0 and (LOG / "2026-11-15.md").read_text().splitlines()[-1] == "- 00:05Z moved files --station crew-reviewer"
   and "moved files" not in (HOME / "reports/log/crew-reviewer/2026-11-15.md").read_text(), err)
code, out, err = run("log", "--days", "3", "x")
ck("--days before the text is refused for log", code == 1 and "--days" in err, err)
code, out, err = run("log")
ck("log with no text is the usage, exit 1", code == 1 and "usage:" in err, err)
(HOME / "reports" / "crew-orchestrator.md").write_bytes(b"x" * (report.REPORT_CAP + 1))
code, out, err = run("log", "big report")
ck("a report past the cap still logs, and warns on stderr", code == 0 and "rewrite it" in err and out == "", err)
(HOME / "reports" / "crew-orchestrator.md").write_bytes(b"# crew-orchestrator\n\n## Waiting on\n- TR-1363 assign\n")
code, out, err = run("log", "small report")
ck("a report under the cap logs without a warning", code == 0 and err == "", err)

print("grep")
code, out, err = run("grep", "tr-1363")
lines = out.splitlines()
ck("grep is case-insensitive and finds the log and the report", code == 0 and len(lines) == 2, out)
ck("...the log line first, labelled with station and day", lines[0].startswith("crew-orchestrator 2026-11-14: - 15:30Z TR-1363"), out)
ck("...then the report", lines[1].startswith("crew-orchestrator report: - TR-1363"), out)
code, out, err = run("grep", "after midnight|two lines")
ck("newest day first", out.splitlines()[0].startswith("crew-orchestrator 2026-11-15"), out)
code, out, err = run("grep", "nothing-like-this")
ck("no match says so, exit 0", code == 0 and out.strip() == "no match", out)
code, out, err = run("grep", "for the reviewer")
ck("without --station every station's log is searched", "crew-reviewer 2026-11-15" in out, out)
code, out, err = run("grep", "--station", "crew-orchestrator", "for the reviewer")
ck("--station keeps to that station", out.strip() == "no match", out)
old = LOG / "2026-10-01.md"
old.write_bytes(b"- 10:00Z ancient TR-1\n")
code, out, err = run("grep", "ancient")
ck("a day older than --days (default 14) is not searched", out.strip() == "no match", out)
code, out, err = run("grep", "--days", "60", "ancient")
ck("...and --days 60 reaches it", "ancient" in out, out)
with open(LOG / "2026-11-15.md", "a", newline="\n") as f:
    for i in range(200):
        f.write(f"- 01:{i % 60:02d}Z bulk entry {i} " + "y" * 500 + "\n")
code, out, err = run("grep", "bulk entry")
shown = [ln for ln in out.splitlines() if "bulk entry" in ln]
ck("grep shows at most MAX_LINES lines", len(shown) <= report.MAX_LINES, len(shown))
ck("...each cut to MAX_LINE characters plus a mark", all(ln.endswith("...") and len(ln) < report.MAX_LINE + 60 for ln in shown))
ck("...within MAX_BYTES in all", len(out.encode()) <= 4096, len(out.encode()))
ck("...and says how many it left out", out.splitlines()[-1].endswith("narrow the pattern, --station or --days."), out[-200:])
ck("...the newest entry first", "bulk entry 199" in shown[0], shown[0][:80])
with open(LOG / "2026-11-15.md", "a", newline="\n") as f:
    f.writelines(f"- 02:00Z short {i}\n" for i in range(100))
code, out, err = run("grep", "short [0-9]")
ck("short lines stop at MAX_LINES, not only at MAX_BYTES",
   len([ln for ln in out.splitlines() if "short" in ln]) == report.MAX_LINES and out.splitlines()[-1].startswith("60 more"),
   out[-120:])
code, out, err = run("grep", "([")
ck("a bad regex is refused, exit 1", code == 1 and "regular expression" in err, err)
code, out, err = run("grep", "--days", "zero", "x")
ck("--days must be a number", code == 1 and "--days" in err, err)
code, out, err = run("grep", "--days", "\u00b2", "x")
ck("--days takes ASCII digits only: a superscript two is refused, not a ValueError", code == 1 and "--days" in err, err)
code, out, err = run("grep", "--days", "\u0663", "x")
ck("...and so is an Arabic-Indic digit", code == 1 and "--days" in err, err)
code, out, err = run("grep", "--days", "0", "x")
ck("--days 0 is refused", code == 1 and "--days" in err, err)
code, out, err = run("grep", "--days", "999999999", "ancient")
ck("--days 999999999 is capped, not an OverflowError: it reaches every day", code == 0 and "ancient" in out, (code, err, out))
code, out, err = run("grep", "--days", "9" * 5000, "ancient")
ck("a number of thousands of digits is capped too", code == 0 and "ancient" in out, (code, err[-120:]))
code, out, err = run("grep")
ck("grep with no pattern is the usage", code == 1 and "usage:" in err, err)
code, out, err = run()
ck("no subcommand is the usage", code == 1 and "usage:" in err, err)


print("archive")
ARC = TMP / "archive-home"
AENV = {"FACTORY_CREW_HOME": str(ARC), "HOME": str(TMP)}
AREP = ARC / "reports"


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def stations(**rows):
    put(ARC / "stations.json", json.dumps({n: {"status": st} for n, st in rows.items()}))


def arun(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = report.cmd_report(["report", "archive", *args], AENV)
    return code, out.getvalue(), err.getvalue()


BUSY = set()
report._idle = lambda station, paths: station not in BUSY
put(ARC / "crew.json", "{}")
stations(**{"crew-orchestrator": "running", "crew-reviewer": "running", "old-builder": "retired"})
put(AREP / "crew-orchestrator.md", "# orch\n## Undelivered\n- RELEASE for x\n" + "history\n" * 50)
put(AREP / "crew-orchestrator.next.md", "# orch\n\n## Now\nticking\n\n## Undelivered\n- RELEASE for x\n")
put(AREP / "crew-reviewer.md", "# reviewer old\n" + "reviewed TR-9\n" * 20)
put(AREP / "old-builder.md", "# old builder\nTR-77 done\n")
put(AREP / "ci-paused-runs.md", "run 1 paused\n")
put(AREP / "cell-switch.log", "not a report\n")
put(AREP / "log" / "crew-orchestrator" / "2026-11-14.md", "- 10:00Z a day file\n")
before = tree(ARC)

code, out, err = arun()
ck("a dry run exits 0 and changes nothing under the crew home", code == 0 and tree(ARC) == before, (code, err))
ck("...one line per move, says it would", out.count("would move") == 4 and "dry run" in out, out)
ck("...the reviewer has no next file: skipped, with the reason", "skip crew-reviewer: no crew-reviewer.next.md yet" in out, out)
ck("...a retired station's report goes to final.md", "would move old-builder.md -> log/old-builder/final.md" in out, out)
ck("...a live one's goes to archive-until-<day>.md and its next file takes its place",
   "crew-orchestrator.md -> log/crew-orchestrator/archive-until-2026-11-15.md" in out
   and "crew-orchestrator.next.md -> crew-orchestrator.md" in out, out)
ck("...a side note that is no station is history too", "ci-paused-runs.md -> log/ci-paused-runs/final.md" in out, out)
code, out, err = arun("--apply", "--bogus")
ck("an unknown option is the usage", code == 1 and "usage:" in err and tree(ARC) == before, err)

BUSY.add("crew-orchestrator")
code, out, err = arun("--apply")
ck("--apply refuses while a station it would touch is in a turn, and moves nothing",
   code == 1 and "crew-orchestrator" in err and tree(ARC) == before, (code, err))
code, out, err = arun()
ck("a dry run still exits 0 and names the busy station", code == 0 and "busy: crew-orchestrator" in out and tree(ARC) == before, out)
BUSY.clear()

sizes = sum(len(v) for v in before.values())
code, out, err = arun("--apply")
after = tree(ARC)
ck("--apply exits 0", code == 0, (code, err))
ck("...the retired report is in final.md, unchanged", after["reports/log/old-builder/final.md"] == before["reports/old-builder.md"])
ck("...the old orchestrator report is the archive, and the next file is now the report",
   after["reports/log/crew-orchestrator/archive-until-2026-11-15.md"] == before["reports/crew-orchestrator.md"]
   and after["reports/crew-orchestrator.md"] == before["reports/crew-orchestrator.next.md"]
   and "reports/crew-orchestrator.next.md" not in after, sorted(after))
ck("...the reviewer's report and the other files stay",
   after["reports/crew-reviewer.md"] == before["reports/crew-reviewer.md"] and after["reports/cell-switch.log"] == before["reports/cell-switch.log"]
   and after["reports/log/crew-orchestrator/2026-11-14.md"] == before["reports/log/crew-orchestrator/2026-11-14.md"])
ck("...no byte is lost", sum(len(v) for v in after.values()) == sizes, (sizes, sum(len(v) for v in after.values())))
code, out, err = run("grep", "--station", "crew-orchestrator", "--days", "1", "history", env=AENV)
ck("grep finds a line of an archived report, whatever --days says", "crew-orchestrator archive-until-2026-11-15" in out, out)

put(AREP / "crew-orchestrator.next.md", "# orch again\n\n## Undelivered\nnone\n")
code, out, err = arun("--apply")
after2 = tree(ARC)
ck("a second apply the same day keeps both archives",
   code == 0 and after2["reports/log/crew-orchestrator/archive-until-2026-11-15.md"] == before["reports/crew-orchestrator.md"]
   and after2["reports/log/crew-orchestrator/archive-until-2026-11-15-2.md"] == before["reports/crew-orchestrator.next.md"], (code, err, sorted(after2)))
put(AREP / "old-builder.md", "# the builder came back and died again\n")
code, out, err = arun("--apply")
ck("a second final.md does not replace the first: it is final-2.md",
   code == 0 and tree(ARC)["reports/log/old-builder/final.md"] == before["reports/old-builder.md"]
   and tree(ARC)["reports/log/old-builder/final-2.md"] == b"# the builder came back and died again\n", sorted(tree(ARC)))

put(AREP / "crew-reviewer.next.md", "# reviewer\n\n## Now\nidle\n")
put(AREP / "crew-reviewer.md", "# reviewer old\n\n## Undelivered\n- VERDICT for x\n")
snap = tree(ARC)
code, out, err = arun("--apply")
ck("a report with Undelivered is not archived when the new one lacks the section: STOP, exit 1, files kept",
   code == 1 and "STOP crew-reviewer" in out and tree(ARC) == snap, (code, out))
put(AREP / "crew-reviewer.next.md", "# reviewer\n\n## Undelivered\n- VERDICT for x\n")
code, out, err = arun("--apply")
ck("...with the section carried over it moves", code == 0 and "STOP" not in out
   and (AREP / "log/crew-reviewer/archive-until-2026-11-15.md").is_file(), (code, out, err))

put(TMP / "from.md", "new text\n")
put(TMP / "to.md", "what is there\n")
try:
    report._move(TMP / "from.md", TMP / "to.md")
    refused = False
except report.CrewError:
    refused = True
ck("_move never replaces a file that is there", refused and (TMP / "from.md").is_file()
   and (TMP / "to.md").read_bytes() == b"what is there\n")

(ARC / "stations.json").unlink()
code, out, err = arun()
ck("without stations.json nothing can tell a live report from an old one: refused", code == 1 and "stations.json" in err, err)
stations(**{"old-builder": "retired"})
code, out, err = arun()
ck("with no live station it is refused too, even as a dry run", code == 1 and "no live station" in err, err)
ck("a log folder and a file that is no .md are never moved", (AREP / "cell-switch.log").is_file() and (AREP / "log").is_dir())

print("permissions")
from tares.crew import launch  # noqa: E402
for role in ("orchestrator", "builder", "reviewer", "releaser", "helper"):
    ck(f"a {role} may run tares factory report", "Bash(tares factory report *)" in launch.permissions(role)["allow"], role)

print("dispatch")
ck("`report` is in the table", dispatch.find(["report", "log"]) is not None)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
