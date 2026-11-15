"""The rules the plugin ships match the ones Tares ships (TR-864).

A station that starts while Tares is down, with no copy of its rules, runs on
`claude-plugin/templates/default-rules.json`. That file is a copy of `DEFAULTS` in
`tares/integrations/builtin/guardrails_tares.py`; this test fails when the two differ, so a change to a
default is made in both.

Run: python tests/test_default_rules_in_sync.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares.integrations.builtin import guardrails_tares as G  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


file = ROOT / "claude-plugin" / "templates" / "default-rules.json"
doc = json.loads(file.read_text(encoding="utf-8"))
shipped = doc.get("defaults")
ck("the file holds a list of defaults", isinstance(shipped, list) and len(shipped) > 0)
ck("it has the same ids, in the same order, as the code", [d.get("id") for d in shipped] == [d["id"] for d in G.DEFAULTS],
   f"{[d.get('id') for d in shipped]}")
for want in G.DEFAULTS:
    got = next((d for d in shipped if d.get("id") == want["id"]), {})
    ck(f"{want['id']}: kind, role, text and reason are the code's", got == want, f"{got!r}")
ck("every default has a kind of allow or deny and a role", all(d["kind"] in ("allow", "deny") and d["role"] for d in shipped))

# the crew engine renders the shipped file, so each role gets rules that are not empty
from tares.crew import rules  # noqa: E402

for role in G.ROLES:
    out = rules.shipped(str(ROOT / "claude-plugin"), role)
    ck(f"shipped rules for {role} give allow and soft_deny sentences", bool(out["allow"]) and bool(out["soft_deny"]))
    want = G.render(role, allow_grants=[], deny_rules=[], values={})
    ck(f"...and equal what Tares renders for {role} with no grants", out["allow"] == want["allow"] and out["soft_deny"] == want["soft_deny"])

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
