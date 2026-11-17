"""The plugin's skills fit what Claude Code keeps after a compaction (T7).

Claude Code re-attaches each invoked skill after a compaction but cuts it at 20,000 characters, and a
station told its copy was truncated loads the skill again (about 22,000 characters each time).
So every SKILL.md stays under CAP with room to grow, a station's two always-loaded skills stay under
PAIR_CAP, and every page a skill points to exists.
Plain script: exits non-zero on failure.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "claude-plugin", "skills")
CUT = 20000      # what Claude Code keeps of a re-attached skill
CAP = 16000      # per SKILL.md: room to grow before the cut
PAIR_CAP = 30000  # factory-protocol plus one station skill, re-attached after every compaction
STATIONS = ["station-orchestrator", "station-reviewer", "station-releaser", "station-builder"]
NOT_PAGES = {"AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "README.md", "MEMORY.md"}
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def text(skill, name="SKILL.md"):
    with open(os.path.join(SKILLS, skill, name), encoding="utf-8") as f:
        return f.read()


for skill in sorted(os.listdir(SKILLS)):
    if not os.path.isfile(os.path.join(SKILLS, skill, "SKILL.md")):
        continue
    body = text(skill)
    ck(f"{skill}/SKILL.md is under {CAP} characters", len(body) <= CAP, len(body))
    named = set(re.findall(r"`([\w-]+\.md)`", body)) - NOT_PAGES - {"SKILL.md"}
    for page in sorted(named):
        ck(f"{skill}/SKILL.md names {page}, which is in its folder", os.path.isfile(os.path.join(SKILLS, skill, page)))

protocol = len(text("factory-protocol"))
for station in STATIONS:
    pair = protocol + len(text(station))
    ck(f"factory-protocol + {station} is under {PAIR_CAP} characters", pair <= PAIR_CAP, pair)

ck("the cap leaves room under Claude Code's cut", CAP < CUT)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
