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
# repo files a skill may name in backticks; they are not pages of the skill
NOT_PAGES = {"AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "README.md", "MEMORY.md"}
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def named_pages(text):
    return set(re.findall(r"`([A-Za-z0-9_.-]+\.md)`", text)) - NOT_PAGES


for skill_md in sorted(glob.glob(os.path.join(SKILLS, "*", "SKILL.md"))):
    folder = os.path.dirname(skill_md)
    name = os.path.basename(folder)
    text = open(skill_md, encoding="utf-8").read()
    pages = sorted(f for f in os.listdir(folder) if f.endswith(".md") and f != "SKILL.md")
    named = named_pages(text)
    for page in pages:
        ck(f"{name}: SKILL.md names {page}", page in named, sorted(named))
        body = open(os.path.join(folder, page), encoding="utf-8").read()
        ck(f"{name}: {page} says when to read it", re.search(r"^Part of the .+ (manual|skill)\. Read (when|on) ", body, re.M) is not None, body[:120])
    for page in sorted(named):
        ck(f"{name}: the page {page} that SKILL.md names exists", os.path.exists(os.path.join(folder, page)), page)

print("the checker")
ck("AGENTS.md and CHANGELOG.md in backticks are not pages, a missing page still is",
   named_pages("see `AGENTS.md`, `CHANGELOG.md`, `gone.md`") == {"gone.md"})

print("spec-project pages keep the rules the split moved")
write_out = open(os.path.join(SKILLS, "spec-project", "write-out.md"), encoding="utf-8").read()
ck("write-out.md item 7: a working doc per ticket, attached, never in Linear or GitHub",
   "7. A working doc per ticket: `write_doc` kind `working`" in write_out and "`attach_working_doc`" in write_out
   and "Working docs never go to Linear or GitHub" in write_out)
ck("write-out.md item 7: the working doc's sections, in order",
   [h for h in re.findall(r"^   ## (.+)$", write_out, re.M)]
   == ["Goal", "Context and files", "Steps", "Definition of done", "Out of scope", "How to check", "Progress"],
   re.findall(r"^   ## (.+)$", write_out, re.M))
contributor = open(os.path.join(SKILLS, "spec-project", "contributor.md"), encoding="utf-8").read()
ck("contributor.md: answers go through answer_question with the person's words",
   "call `answer_question(id, answer, project)` with their words" in contributor and "Say which" in contributor)
ck("contributor.md: questions are asked one at a time and numbers restart per project",
   "restart in each project" in contributor and "one at a time" in contributor)

print("factory-protocol pages keep the rules the split moved")
linear = open(os.path.join(SKILLS, "factory-protocol", "linear.md"), encoding="utf-8").read()
ck("linear.md: list_issues with fields and a limit of 25 or less",
   '`list_issues` always with `fields=["id","title","status","assignee","updatedAt"]`' in linear
   and "`limit` of 25 or less" in linear)
ck("linear.md: get_issue with fields, one updatedAt look per tick, one save_issue",
   '`get_issue` with `fields=["status","updatedAt"]`' in linear and 'updatedAt="<your last look' in linear
   and "One `save_issue` carrying every field" in linear)
ck("linear.md: a Tares ticket is read with Tares's tools, and a refused fields is called without",
   "read with the Tares tools" in linear and "If Linear refuses `fields` or `limit`" in linear)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
