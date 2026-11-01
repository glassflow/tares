"""The orchestrator skill handles a builder's mock ASK (TR-779).

A screen ticket's mock ASK carries a file path. The orchestrator reads it, keeps it as the project
doc "Mock: <screen>", and puts the desk item on the project, so the person's approval shows in
list_decisions(project), which the reviewer's design check reads. The plugin version is bumped.

Run: .venv/bin/python tests/test_factory_orchestrator_mock.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


skill = open(os.path.join(ROOT, "claude-plugin/skills/station-orchestrator/SKILL.md")).read()
ask = skill.split("## Answering questions", 1)[1].split("## The desk", 1)[0]
mock = ask.split("A builder's mock `ASK`", 1)[1] if "A builder's mock `ASK`" in ask else ""

ck("the orchestrator skill has a section for a builder's mock ASK", bool(mock), ask[:200])
ck("it reads the mock file from the path in the ASK", "Read the mock file from the path in the `ASK`" in mock, mock[:300])
ck("it keeps the mock as the project doc \"Mock: <screen>\" with write_doc",
   'project doc "Mock: <screen>"' in mock and "`write_doc`" in mock, mock[:500])
ck("it passes project to the mock's desk_add so list_decisions(project) shows the approval",
   re.search(r'`desk_add\([^)]*project="<the project>"\)`', mock) is not None and "`list_decisions(project)`" in mock, mock[:700])
ck("it names the PR line the builder writes with the D<n>", 'Mock: "Mock: <screen>" (desk D<n>)' in mock, mock[-300:])

plugin = json.load(open(os.path.join(ROOT, "claude-plugin/.claude-plugin/plugin.json")))
ck("the plugin version is bumped past 0.6.1", tuple(int(x) for x in plugin["version"].split(".")) > (0, 6, 1), plugin["version"])
ck("the changelog names the plugin version", f"Plugin {plugin['version']}" in open(os.path.join(ROOT, "CHANGELOG.md")).read(), plugin["version"])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
