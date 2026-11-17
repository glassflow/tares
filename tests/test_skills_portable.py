"""The station skills and plugin commands tell Claude to run only what Git Bash on Windows has (TR-974).

On Windows the crew's Claude Code runs shell commands in Git Bash, which ships bash, coreutils (touch,
sort, head, cat), git and curl, but not jq, ps, pgrep, perl, nohup and the like. A skill that tells a
station to pipe to `jq` or read `ps -` output breaks there.

Inventory of the shell a skill or command tells Claude to run (checked by hand, 2026-11-09):
  OK in Git Bash   git, gh (also `-q` filters, which gh evaluates itself), docker, kind, npm, node, cd, &&,
                   tares factory commands (heavy too), rm named only as something never to run,
                   `touch "$FACTORY_HOME/stations/$FACTORY_STATION.done"` (coreutils touch, mixed paths ok),
                   `git log --format=%at ... | sort -n | head -1` (coreutils sort and head)
  not OK           jq, ps, pgrep, pkill, nohup, perl, setsid, flock, lsof, osascript, pbcopy, `date -`,
                   `sed -i`, `readlink -f`, `stat -c`, a bare python3 (missing, or the Store alias), /tmp
None of the "not OK" ones is in a skill today; this test keeps it so. A new need goes through a
`tares factory` subcommand or a `--json` output the skill reads whole, not through one of these.

Run: python tests/test_skills_portable.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "claude-plugin"

P = F = 0


def ck(label, cond, detail=None):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# (label, pattern). A pattern matches the unix-only tool as a word, not as part of another word.
BANNED = [
    ("jq", r"(?<![\w-])jq(?![\w-])"),
    ("ps -", r"(?<![\w-])ps\s+-"),
    ("ps aux", r"(?<![\w-])ps\s+aux"),
    ("pgrep", r"(?<![\w-])pgrep(?![\w-])"),
    ("pkill", r"(?<![\w-])pkill(?![\w-])"),
    ("nohup", r"(?<![\w-])nohup(?![\w-])"),
    ("perl", r"(?<![\w-])perl(?![\w-])"),
    ("setsid", r"(?<![\w-])setsid(?![\w-])"),
    ("flock", r"(?<![\w-])flock(?![\w-])"),
    ("lsof", r"(?<![\w-])lsof(?![\w-])"),
    ("osascript", r"(?<![\w-])osascript(?![\w-])"),
    ("pbcopy", r"(?<![\w-])pb(copy|paste)(?![\w-])"),
    ("date -", r"(?<![\w-])date\s+-"),
    ("sed -i", r"(?<![\w-])sed\s+(-\w*i|--in-place)"),
    ("readlink -f", r"(?<![\w-])readlink\s+-f"),
    ("stat -c/-f", r"(?<![\w-])stat\s+-[cf]"),
    ("/tmp", r"(?<![\w.-])/tmp(/|\b)"),
]
# python3 is banned in the skills only. The command file challenger-waive.md still runs a bare python3 until
# TR-1103 switches the plugin's commands to `tares hook`. When that lands, move python3 into BANNED and flip the
# "not yet banned in a command" check below.
BANNED_SKILLS_ONLY = [("python3", r"(?<![\w-])python3(?![\w-])")]


def scan(text, extra=()):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for label, pat in list(BANNED) + list(extra):
            if re.search(pat, line):
                hits.append((n, label, line.strip()[:120]))
    return hits


skills = sorted((PLUGIN / "skills").glob("*/*.md"))
commands = sorted((PLUGIN / "commands").glob("*.md"))

print("the files")
ck("there are skills to scan", len(skills) >= 9, [s.parent.name for s in skills])
ck("there are commands to scan", len(commands) >= 5, [c.name for c in commands])
for f in skills:
    hits = scan(f.read_text(encoding="utf-8"), BANNED_SKILLS_ONLY)
    ck(f"{f.parent.name}/{f.name} names no unix-only tool", not hits, hits)
scanned_commands = []
for f in commands:
    hits = scan(f.read_text(encoding="utf-8"))
    scanned_commands.append(f.name)
    ck(f"commands/{f.name} names no unix-only tool", not hits, hits)
ck("every command file went through the scan", scanned_commands == [c.name for c in commands], scanned_commands)

print("the scan itself")
samples = {
    "jq": "curl x | jq -r '.a'",
    "ps -": "ps -p $PID -o etime=",
    "ps aux": "ps aux | head",
    "pgrep": "pgrep -f claude",
    "pkill": "pkill claude",
    "nohup": "nohup factory watch &",
    "perl": "perl -pe 's/a/b/'",
    "setsid": "setsid factory watch",
    "flock": "flock /tmp/x cmd",
    "lsof": "lsof -i :8787",
    "osascript": "osascript -e 'beep'",
    "pbcopy": "echo hi | pbcopy",
    "date -": "date -u +%s",
    "sed -i": "sed -i 's/a/b/' f",
    "readlink -f": "readlink -f \"$0\"",
    "stat -c/-f": "stat -c %Y file",
    "/tmp": "cat /tmp/out.txt",
}
for label, line in samples.items():
    ck(f"a line with `{label}` is caught", any(h[1] == label for h in scan(line)), scan(line))
ck("a command line with a unix-only tool is caught by the command scan", scan("run: jq -r .a f.json") != [])
ck("a bare python3 is not yet banned in a command (TR-1103)", scan("python3 x.py") == [])
ck("python3 is caught in a skill", scan("python3 x.py", BANNED_SKILLS_ONLY) != [])
clean = ["touch \"$FACTORY_HOME/stations/$FACTORY_STATION.done\"", "git log --format=%at origin/main..HEAD | sort -n | head -1",
         "gh issue view 3 --repo R --json id -q .id", "update - the ticket", "the perlman rule", "jqueryish", "a spare-ps note"]
ck("the commands a skill really uses are not flagged", all(scan(c, BANNED_SKILLS_ONLY) == [] for c in clean),
   [(c, scan(c, BANNED_SKILLS_ONLY)) for c in clean])

print("factory-protocol keeps its reload and permission rules")
fp = (PLUGIN / "skills" / "factory-protocol" / "SKILL.md").read_text(encoding="utf-8")
fp_desc = re.search(r"^description: (.*)$", fp, re.M).group(1)
ck("the description does not say to load it after any compaction", "after any compaction" not in fp_desc, fp_desc[-120:])
ck("the description still says to load it at boot and on reload", "at boot" in fp_desc and "reload" in fp_desc, fp_desc[-120:])
ck("rule 13 (a denied permission) is still there", "13. **A permission check denies an action**" in fp)
ck("the compaction bullet is still there and names Claude Code's cut note",
   "**After a compaction**" in fp and "skill content truncated for compaction" in fp)
bullet = next(l for l in fp.splitlines() if l.startswith("- **After a compaction**"))
ck("after a compaction a skill is loaded again only when truncated or missing a rule, and pages are read on need",
   "re-attached" in bullet and "only if" in bullet and "skill content truncated for compaction" in bullet
   and "a rule is missing" in bullet and "Pages are not re-attached" in bullet, bullet[:200])
ck("the compaction bullet does not say to load both skills again", "load this skill and your station skill again" not in fp
   and "load both skills again" not in fp, bullet[:200])
ck("the older Tares page is named and present", "`older-tares.md`" in fp and (PLUGIN / "skills" / "factory-protocol" / "older-tares.md").is_file())
ck("the skill stays under its size limit (14,000 characters)", len(fp) <= 14000, len(fp))

print("the report is a small state file")
skills_dir = PLUGIN / "skills"
rp = (skills_dir / "factory-protocol" / "report.md").read_text(encoding="utf-8")
ck("factory-protocol names the report page", "`report.md`" in fp)
for section in ("## Now", "## Waiting on", "## Desk", "## Undelivered", "## Bouncing", "## Taken"):
    ck(f"the report page names {section}", f"`{section}`" in rp)
ck("the report page says to keep Undelivered, which the watch reads", "Always keep this section" in rp)
ck("the report page sends history to report log and search to report grep",
   "tares factory report log" in rp and "tares factory report grep" in rp)
ck("the report page gives the 4 KB and 8 KB limits", "4 KB" in rp and "8 KB" in rp)
bad = []
for md in sorted(skills_dir.glob("*/*.md")):
    for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        low = line.lower()
        hits = [m.start() for m in (
            re.search(r"(>>|\btail\b|\bcat\b|\bless\b|\bgrep\b|\btee\b|\bsed\b|\bhead\b)[^`\n]*reports/", line),
            re.search(r"reports/[^`\n]*(>>|\|\s*(tail|grep|tee))", line),
            re.search(r"\bappend\b[^.]*\b(report|log)\b", line, re.I)) if m]
        if hits and not any(w in low[:min(hits)] for w in ("never", "no skill")):
            bad.append(f"{md.relative_to(skills_dir)}:{n}")
ck("no skill tells a station to cat, tail or append to a report", not bad, bad)
orch = (skills_dir / "station-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
ck("the orchestrator finds undelivered peer messages with report grep",
   "tares factory report grep --days 1 '\\[TF:'" in orch)
for station in ("station-orchestrator", "station-reviewer", "station-releaser"):
    body = (skills_dir / station / "SKILL.md").read_text(encoding="utf-8")
    ck(f"{station} logs history with report log", "tares factory report log" in body)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
