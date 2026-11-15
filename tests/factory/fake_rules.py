"""The rules route of a fake Tares (GET /api/factory/guardrails/rules, TR-862), for the crew tests.

A fake Tares keeps its grants as the text the old crew engine parsed (`- "words" (2026-01-02)` lines, the
`all` text and a project's own). Tares itself keeps them as rows and renders the rules with
`tares.integrations.builtin.guardrails_tares.render`; this module does the same from that text, so a test
world written as grant text still gives a station the sentences it always got.

`rules_answer(...)` is the body Tares sends; `RULES_PATH` is the route; `CAPABILITY` is what /health lists.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tares.integrations.builtin import guardrails_tares as G  # noqa: E402

RULES_PATH = "/api/factory/guardrails/rules"
CAPABILITY = "guardrail_rules"
PROD = "(^|[-_.])(prod|production)([-_.]|$)"

_SP = r"[ \t\n\r\f\v]"
_QUOTED = re.compile(r'^"(.*)"' + _SP + r"*\(([0-9]{4}-[0-9]{2}-[0-9]{2})\)" + _SP + r"*$", re.S)
_DATED = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})" + _SP + r"*[:,-]?" + _SP + r"*(.*)$", re.S)
_DATE_START = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_grants(text: str, scope: str) -> list:
    """`{words, given_at, scope}` for each grant line of the text (a last line without a newline is left out,
    as the old engine's read loop left it out)."""
    out = []
    for line in (text or "").split("\n")[:-1]:
        mine = scope
        if line.startswith("@project "):
            line = line[len("@project "):]
        if line.startswith(("- ", "* ")):
            line = line[2:]
        elif not _DATE_START.match(line):
            continue
        date, words = "", line
        m = _QUOTED.match(line)
        if m:
            words, date = m.group(1), m.group(2)
        else:
            m = _DATED.match(line)
            if m:
                date, words = m.group(1), m.group(2)
        if words:
            out.append({"words": words, "given_at": date, "scope": mine})
    return out


def rules_answer(station: str, role: str, project_name: str, all_text: str, project_text: str,
                 prod: str = PROD, deny: list | None = None) -> dict:
    """The body of the rules route for one station: the defaults of its role, then the team's grants, then the
    project's, then the deny rules of your own (each `{text}`)."""
    grants = parse_grants(all_text if str(all_text or "").endswith("\n") else (all_text or "") + "\n", "all projects")
    if project_name and project_text:
        text = project_text if project_text.endswith("\n") else project_text + "\n"
        grants += parse_grants(text, f"project {project_name}")
    out = G.render(role, allow_grants=grants, deny_rules=list(deny or []), values={"prod_pattern": prod})
    return {"station": station, "role": role, "project": project_name or None, **out}
