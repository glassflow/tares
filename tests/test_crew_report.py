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
from tares.crew.errors import SessionListUnreadableError  # noqa: E402

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

print("grep --section")
SEC = TMP / "section-home"
SENV = {"FACTORY_CREW_HOME": str(SEC), "HOME": str(TMP)}
(SEC / "reports" / "log").mkdir(parents=True)
for i in range(30):  # many stations whose logs and reports all mention [TF:, so a plain grep hits its cap
    st = f"crew-b{i:02d}"
    (SEC / "reports" / "log" / st).mkdir()
    (SEC / "reports" / "log" / st / "2026-11-14.md").write_text(
        "".join(f"- 10:{n:02d}Z [TF:DONE] sent {n}\n" for n in range(5)), newline="\n")
    (SEC / "reports" / f"{st}.md").write_text(
        f"## Now\n[TF:REVIEW] waiting on {st}\n## Undelivered\nnone\n## Taken\nx\n", newline="\n")
(SEC / "reports" / "crew-zz-builder.md").write_text(
    "## Now\n[TF:ASK] not parked, only in Now\n## Undelivered\n[TF:ASK] parked question for the orchestrator\n"
    "\n[TF:DONE] parked done\n## Taken\n[TF:ASK] under Taken\n", newline="\n")
(SEC / "reports" / "log" / "crew-b00" / "final.md").write_text(
    "## Undelivered\n[TF:ASK] old parked line of an archived report\n", newline="\n")
code, out, err = run("grep", "--days", "36500", "\\[TF:", env=SENV)
ck("a plain grep for [TF: is cut by its cap and misses the parked line (the gap)",
   code == 0 and "parked question" not in out and "more matching lines not shown" in out, out[-200:])
code, out, err = run("grep", "--section", "Undelivered", env=SENV)
ck("--section Undelivered shows the parked lines of a crowded home",
   code == 0 and "crew-zz-builder report: [TF:ASK] parked question for the orchestrator" in out
   and "crew-zz-builder report: [TF:DONE] parked done" in out, out)
ck("...and nothing else: not Now, not Taken, not logs, not an archived report, not `none`, not a blank line",
   len(out.splitlines()) == 2 and "only in Now" not in out and "under Taken" not in out and "sent" not in out
   and "archived report" not in out, out)
code, out, err = run("grep", "--section", "undelivered", "done", env=SENV)
ck("the section name is case-insensitive and the regex still narrows", code == 0 and out.splitlines() == [
   "crew-zz-builder report: [TF:DONE] parked done"], out)
code, out, err = run("grep", "--section", "Undelivered", "--station", "crew-b03", env=SENV)
ck("--station narrows --section; a station with only `none` is no match", code == 0 and out.strip() == "no match", out)
code, out, err = run("grep", "--section", env=SENV)
ck("--section with no name is the usage", code == 1 and "usage:" in err, err)
code, out, err = run("grep", "--section", "--days", "1", env=SENV)
ck("a flag-looking section name is the usage, not a name", code == 1 and "usage:" in err, (code, err))
code, out, err = run("grep", "--section", "Undelivered", "--days", "1", env=SENV)
ck("--days with --section is refused, not ignored", code == 1 and "--days does not apply" in err, (code, err))
(SEC / "reports" / "crew-fence.md").write_text(
    "## Undelivered (2)\n[TF:ASK] counted heading\n- none\n```\n## Taken\n[TF:ASK] inside a fence\n```\n## Now\nx\n",
    newline="\n")
code, out, err = run("grep", "--section", "Undelivered", "--station", "crew-fence", env=SENV)
ck("`## Undelivered (2)` is the section, `- none` is empty, a `## ` in a code fence is no heading",
   code == 0 and "counted heading" in out and "crew-fence report: ## Taken" in out and "inside a fence" in out and "- none" not in out, out)

print("grep --section on a home that was never archived")
OLD = TMP / "unarchived-home"
OENV = {"FACTORY_CREW_HOME": str(OLD), "HOME": str(TMP)}
(OLD / "reports").mkdir(parents=True)
rows = {"crew-orchestrator": {"status": "running"}, "crew-zz-builder": {"status": "running"}, "dead-old": {"status": "retired"}}
(OLD / "stations.json").write_text(json.dumps(rows), newline="\n")
for i in range(30):  # dead stations, sorted before the live ones, each with a long Undelivered section
    (OLD / "reports" / f"a-dead-{i:02d}.md").write_text(
        "## Undelivered\n" + "".join(f"[TF:ASK] dead {i} line {n}\n" for n in range(5)), newline="\n")
(OLD / "reports" / "dead-old.md").write_text("## Undelivered\n[TF:ASK] retired station line\n", newline="\n")
(OLD / "reports" / "crew-zz-builder.md").write_text("## Undelivered\n[TF:ASK] live peer parked T99\n", newline="\n")
code, out, err = run("grep", "--section", "Undelivered", env=OENV)
ck("only live stations are read: a live peer's parked ASK is shown among 150 dead lines",
   code == 0 and out.splitlines() == ["crew-zz-builder report: [TF:ASK] live peer parked T99"], out)
code, out, err = run("grep", "--section", "Undelivered", "--station", "dead-old", env=OENV)
ck("...a retired station is skipped even when named", code == 0 and out.strip() == "no match", out)
(OLD / "stations.json").unlink()
code, out, err = run("grep", "--section", "Undelivered", "dead 29", env=OENV)
ck("without a stations.json every report counts", code == 0 and "a-dead-29 report" in out, out)

rows = {"crew-orchestrator": {"status": "retired"}, "crew-zz-builder": {"status": "retired"}}
(OLD / "stations.json").write_text(json.dumps(rows), newline="\n")
code, out, err = run("grep", "--section", "Undelivered", env=OENV)
ck("when every station is retired nothing is live: no report is read, not all of them", code == 0 and out.strip() == "no match", out)
code, out, err = run("grep", "--section", "Undelivered", "--station", "crew-zz-builder", env=OENV)
ck("...even a named one", code == 0 and out.strip() == "no match", out)

print("grep --section: (none) is empty, an unclosed fence hides nothing")
(SEC / "reports" / "crew-paren.md").write_text(
    "## Undelivered\n(none)\n- (none)\n* (none)\n(None)\n## Taken\nreal taken line\n", newline="\n")
code, out, err = run("grep", "--section", "Undelivered", "--station", "crew-paren", env=SENV)
ck("`(none)`, `- (none)` and `(None)` count as empty", code == 0 and out.strip() == "no match", out)
code, out, err = run("grep", "--section", "Taken", "--station", "crew-paren", env=SENV)
ck("...and the next section is still read", code == 0 and "real taken line" in out, out)
(SEC / "reports" / "crew-open.md").write_text(
    "## Now\n```\nhalf typed\n## Undelivered\n[TF:ASK] after the open fence\n## Taken\ntaken after it\n", newline="\n")
code, out, err = run("grep", "--section", "Undelivered", "--station", "crew-open", env=SENV)
ck("a fence that is never closed does not hide the headings after it",
   code == 0 and "after the open fence" in out and "taken after it" not in out, out)
(SEC / "reports" / "crew-two.md").write_text(
    "## Now\n```\n## Undelivered\nfenced, not a section\n```\n```\n## Undelivered\n[TF:ASK] after the second\n", newline="\n")
code, out, err = run("grep", "--section", "Undelivered", "--station", "crew-two", env=SENV)
ck("a closed fence still hides its `## ` lines when a later fence is left open",
   code == 0 and "after the second" in out and "fenced, not a section" not in out, out)

code, out, err = run("log", "--section", "Undelivered", "x", env=ENV)
ck("--section is not an option of log: it stays in the entry", code == 0 and
   any("--section Undelivered x" in f.read_text() for f in LOG.glob("*.md")), (code, err))


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
REAL_IDLE = report._idle
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

# a copy that fails or comes out short leaves the source and names both files; so does a source that cannot go
real_copy = shutil.copyfileobj
def fail_copy(a, b, *x):
    raise OSError(28, "No space left on device")
def short_copy(a, b, *x):
    b.write(a.read(3))
for label, fake, want in (("a failed copy", fail_copy, "cannot copy"), ("a short copy", short_copy, "is short")):
    put(TMP / "from.md", "new text\n")
    shutil.copyfileobj = fake
    try:
        report._move(TMP / "from.md", TMP / "fresh.md")
        got = ""
    except report.CrewError as e:
        got = e.message
    finally:
        shutil.copyfileobj = real_copy
    ck(f"_move: {label} is a CrewError naming both files, the source stays",
       want in got and "from.md" in got and "fresh.md" in got and (TMP / "from.md").read_bytes() == b"new text\n", got)
    (TMP / "fresh.md").unlink()
real_unlink = Path.unlink
def stuck_unlink(self, *x, **y):
    raise PermissionError(13, "Permission denied")
put(TMP / "from.md", "new text\n")
Path.unlink = stuck_unlink
try:
    report._move(TMP / "from.md", TMP / "fresh.md")
    got = ""
except report.CrewError as e:
    got = e.message
finally:
    Path.unlink = real_unlink
ck("_move: a source that cannot be taken away says both files now exist", "both files exist" in got and (TMP / "from.md").is_file()
   and (TMP / "fresh.md").read_bytes() == b"new text\n", got)

# a station is checked again right before its own files move: one that starts a turn meanwhile stops the run
put(AREP / "crew-orchestrator.next.md", "# orch\n\n## Undelivered\nnone\n")
put(AREP / "crew-reviewer.next.md", "# reviewer\n\n## Undelivered\nnone\n")
put(AREP / "crew-reviewer.md", "# reviewer\n\n## Undelivered\nnone\n")
stations(**{"crew-orchestrator": "running", "crew-reviewer": "running", "old-builder": "retired"})
calls = {}
def turn_starts(station, paths):
    calls[station] = calls.get(station, 0) + 1
    return not (station == "crew-reviewer" and calls[station] > 1)
report._idle = turn_starts
code, out, err = arun("--apply")
orch_done = list((AREP / "log" / "crew-orchestrator").glob("archive-until-2026-11-15*.md"))
ck("a station that starts a turn after the first check stops the run before its files move",
   code == 1 and "crew-reviewer started a turn" in err and (AREP / "crew-reviewer.next.md").is_file()
   and (AREP / "crew-reviewer.md").read_text().startswith("# reviewer") and not (AREP / "crew-orchestrator.next.md").exists()
   and len(orch_done) >= 1, (code, err, out))
report._idle = REAL_IDLE

# the real _idle, with the session answers faked
put(ARC / "stations.json", json.dumps({"crew-reviewer": {"status": "running", "id": "abcd1234"},
                                        "crew-orchestrator": {"status": "running"}}))
real = (report.claude.attached, report.claude.turn_state, report.claude.between_turns)
PATHS = report.CrewPaths.at(ARC)
def idle(att=False, ts="", bt=True, ts_raises=False, station="crew-reviewer"):
    report.claude.attached = lambda s, p: att
    def turn(sid, c=None):
        if ts_raises:
            raise SessionListUnreadableError("cannot read")
        return ts
    report.claude.turn_state = turn
    report.claude.between_turns = lambda s, c=None, p=None, now=None: bt
    return REAL_IDLE(station, PATHS)
try:
    ck("a stopped live station (no session listed) is idle, though between_turns says no for it", idle(ts="", bt=False) is True)
    ck("...a station with no id is idle too", idle(ts="working", bt=False, station="crew-orchestrator") is True)
    ck("a working session is not idle", idle(ts="working", bt=False) is False)
    ck("a session between turns is idle", idle(ts="done", bt=True) is True)
    ck("a session open in a terminal is not idle, even when done", idle(att=True, ts="done", bt=True) is False)
    ck("a session list that cannot be read is not idle", idle(ts_raises=True) is False)
finally:
    report.claude.attached, report.claude.turn_state, report.claude.between_turns = real
report._idle = lambda station, paths: station not in BUSY

# the guards of the plan
put(AREP / "Bad Name.md", "not a station\n")
stations(**{"crew-orchestrator": "running"})
code, out, err = arun()
ck("a file whose name is no station name is skipped with a note, never planned", "skip Bad Name.md: not a station name" in out
   and "Bad Name" not in out.replace("skip Bad Name.md: not a station name", ""), out)
(ARC / "stations.json").unlink()
code, out, err = arun()
ck("without stations.json nothing can tell a live report from an old one: refused", code == 1 and "no stations.json in" in err, err)
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
