"""The ticket count groups, pinned in one file both sides read: factory.count_of and
status_words.count_line here, their console mirror (ui/src/components/ProgressRing.tsx) in
ui/tests/count-parity.check.cjs. Either side drifting from the file fails its own CI job.

Run: python tests/test_ticket_counts_golden.py
     python tests/test_ticket_counts_golden.py --write   (after a deliberate change to the rule)
"""
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares import factory
from tares import status_words as sw

GOLDEN = ROOT / "ui" / "tests" / "count-cases.json"


def cases() -> dict:
    """Every stage (and none), every status, with and without a wait and a merged pull request."""
    tickets = []
    for stage, status, waits, merged in itertools.product(
            [None, *factory.STAGES], ["todo", "in_progress", "in_review", "done", "canceled"],
            [[], ["T1"]], [False, True]):
        t = {"stage": stage, "status": status, "blocked_by": waits, "pr": {"merged": True} if merged else None}
        tickets.append({**t, "count": factory.count_of(t, waits)})
    lines = []
    for c in [{"merged": 0, "total": 0}, {"merged": 2, "total": 3},
              {"merged": 5, "closed": 1, "queued": 2, "stuck": 1, "total": 9},
              {"merged": 0, "stuck": 2, "total": 7}, {"merged": 1, "closed": 1, "total": 2}]:
        lines.append({"counts": c, "line": sw.count_line(c)})
    return {"tickets": tickets, "lines": lines}


now = cases()
if "--write" in sys.argv:
    GOLDEN.write_text(json.dumps(now, indent=1) + "\n")
    print(f"wrote {GOLDEN.relative_to(ROOT)}")
    sys.exit(0)
pinned = json.loads(GOLDEN.read_text())
bad = [(a, b) for a, b in zip(now["tickets"], pinned["tickets"]) if a != b]
bad += [(a, b) for a, b in zip(now["lines"], pinned["lines"]) if a != b]
same_shape = len(now["tickets"]) == len(pinned["tickets"]) and len(now["lines"]) == len(pinned["lines"])
ok = same_shape and not bad
print(("  ok   " if ok else "  FAIL ") + "count_of and count_line agree with ui/tests/count-cases.json"
      + ("" if ok else f"  {bad[:3] or 'case lists differ in length'}; rerun with --write if the change is meant"))
print(f"\n{int(ok)} passed, {int(not ok)} failed")
sys.exit(0 if ok else 1)
