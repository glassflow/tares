"""A skill's pages and the skill agree (factory-context-diet M1).

A skill that moved a rare procedure to a page next to its SKILL.md names the page in backticks, and
every page in the folder is named there. A page that is deleted or misnamed, or a page nobody points
to, is a rule the station never reads.

Run: python tests/test_skill_pages.py
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "claude-plugin", "skills")
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


for skill_md in sorted(glob.glob(os.path.join(SKILLS, "*", "SKILL.md"))):
    folder = os.path.dirname(skill_md)
    name = os.path.basename(folder)
    text = open(skill_md, encoding="utf-8").read()
    pages = sorted(f for f in os.listdir(folder) if f.endswith(".md") and f != "SKILL.md")
    named = set(re.findall(r"`([A-Za-z0-9_.-]+\.md)`", text))
    for page in pages:
        ck(f"{name}: SKILL.md names {page}", page in named, sorted(named))
        body = open(os.path.join(folder, page), encoding="utf-8").read()
        ck(f"{name}: {page} says when to read it", re.search(r"^Part of the .+ (manual|skill)\. Read (when|on) ", body, re.M) is not None, body[:120])
    for page in sorted(named):
        ck(f"{name}: the page {page} that SKILL.md names exists", os.path.exists(os.path.join(folder, page)), page)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
