"""Station skills move tickets only with Tares (TR-850).

The builder, orchestrator, reviewer and releaser skills change a ticket's status with set_stage
and nothing else: Tares writes it through to Linear or GitHub Issues. A line that tells a station
to use Linear's tools or `gh issue` for a status would move a ticket behind Tares's back. The
session and spec skills are out of scope: a person's own session still moves its own tickets.

Every mention of `linear` (any case) or `gh issue` in the four skills must sit inside one of the
allowed phrases below. The phrases are stripped from each line first, so a new instruction on a
line that already holds an allowed phrase is still caught.

Run: python tests/test_skills_ticket_moves.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = ["station-builder", "station-orchestrator", "station-reviewer", "station-releaser"]

# Phrases that only name a ticket system or show what an identifier looks like.
ALLOWED = [
    "Linear identifier (`ENG-12`)",
    "Linear or GitHub",
]
FLAG = re.compile(r"linear|gh issue", re.I)


def offenders(text):
    """(line number, line) for each line with a flagged mention outside the allowed phrases."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        rest = line
        for phrase in ALLOWED:
            rest = rest.replace(phrase, "")
        if FLAG.search(rest):
            out.append((n, line.strip()))
    return out


def main():
    bad = 0
    # the checker itself: flags what it must, passes what it may
    must_flag = [
        "For a Linear-owned ticket, also move its Linear status with Linear's tools.",
        "`gh issue close 12 --repo a/b`",
        "run `gh issue edit 3` (Linear or GitHub)",
        "Use the LINEAR MCP to set the status.",
    ]
    must_pass = [
        "Ref = the Linear identifier (`ENG-12`) or GitHub identifier.",
        "Tares moves the status in Linear or GitHub too; never move it there yourself.",
        "no mention at all",
    ]
    for line in must_flag:
        if not offenders(line):
            print("FAIL checker lets through:", line)
            bad += 1
    for line in must_pass:
        if offenders(line):
            print("FAIL checker flags:", line)
            bad += 1

    for name in SKILLS:
        path = os.path.join(ROOT, "claude-plugin", "skills", name, "SKILL.md")
        if not os.path.exists(path):
            print("FAIL missing skill:", path)
            bad += 1
            continue
        with open(path, encoding="utf-8") as f:
            found = offenders(f.read())
        for n, line in found:
            print(f"FAIL {name}/SKILL.md:{n}: {line[:160]}")
            bad += 1
        if not found:
            print(f"ok {name}")
    if bad:
        print(f"{bad} failed")
        sys.exit(1)
    print("all ok")


if __name__ == "__main__":
    main()
