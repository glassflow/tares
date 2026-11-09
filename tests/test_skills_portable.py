"""The station skills and plugin commands tell Claude to run only what Git Bash on Windows has (TR-974).

On Windows the crew's Claude Code runs shell commands in Git Bash, which ships bash, coreutils (touch,
sort, head, cat), git and curl, but not jq, ps, pgrep, perl, nohup and the like. A skill that tells a
station to pipe to `jq` or read `ps -` output breaks there.

Inventory of the shell a skill or command tells Claude to run (checked by hand, 2026-11-09):
  OK in Git Bash   git, gh (also `-q` filters, which gh evaluates itself), docker, kind, npm, node, cd, &&,
                   factory / tares factory commands, factory-heavy, rm named only as something never to run,
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
# only the skills: a command file is run through `tares hook` since TR-1068, and its own test holds that
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
for f in commands:
    hits = scan(f.read_text(encoding="utf-8"))
    ck(f"commands/{f.name} names no unix-only tool", not hits, hits)

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
ck("python3 is caught in a skill", scan("python3 x.py", BANNED_SKILLS_ONLY) != [])
clean = ["touch \"$FACTORY_HOME/stations/$FACTORY_STATION.done\"", "git log --format=%at origin/main..HEAD | sort -n | head -1",
         "gh issue view 3 --repo R --json id -q .id", "update - the ticket", "the perlman rule", "jqueryish", "a spare-ps note"]
ck("the commands a skill really uses are not flagged", all(scan(c, BANNED_SKILLS_ONLY) == [] for c in clean),
   [(c, scan(c, BANNED_SKILLS_ONLY)) for c in clean])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
