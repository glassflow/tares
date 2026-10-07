"""The software factory on Tares (P-TR-176, M2 onward): the rules the API and the MCP tools
apply to a factory project's plan, before the store sees anything.

A factory project has milestones (each ships something a person can check: its acceptance
checks) and tickets that belong to a milestone and may depend on other tickets. A ticket is
ready when everything it depends on is done; the orchestrator (or a single build session) works
the ready tickets in plan order.

Tares runs nothing here. It keeps the plan and answers "what is ready", "what is blocked by
what", so every session reads the same plan.
"""
from __future__ import annotations

import re

from .docs import DocError, title_ok

MAX_GOAL = 500
MAX_CHECKS = 20
MAX_CHECK = 500
MAX_DEPENDS = 30

# a ticket status that counts as finished for the tickets that depend on it
DONE_STATUSES = ("done", "canceled")


def goal_ok(goal) -> str:
    g = " ".join(str(goal or "").split())
    if len(g) > MAX_GOAL:
        raise DocError(f"the goal is {len(g)} characters; keep it to {MAX_GOAL}")
    return g


def checks_ok(checks) -> list[dict]:
    """[{check, expect}], cleaned. A check is a command or a short manual step; `expect` is what
    it should show. A plain string is a check with no expectation."""
    if checks is None:
        return []
    if not isinstance(checks, list):
        raise DocError("checks is a list of {check, expect}")
    if len(checks) > MAX_CHECKS:
        raise DocError(f"{len(checks)} checks; keep a milestone to {MAX_CHECKS}")
    out = []
    for c in checks:
        if isinstance(c, str):
            c = {"check": c}
        if not isinstance(c, dict):
            raise DocError("each check is {check, expect}")
        check = " ".join(str(c.get("check") or "").split())
        expect = " ".join(str(c.get("expect") or "").split())
        if not check:
            raise DocError("a check needs its `check`: the command or the step")
        if len(check) > MAX_CHECK or len(expect) > MAX_CHECK:
            raise DocError(f"keep a check and its expectation to {MAX_CHECK} characters each")
        out.append({"check": check, "expect": expect})
    return out


def milestone_ok(name, goal, checks) -> tuple[str, str, list[dict]]:
    return title_ok(name), goal_ok(goal), checks_ok(checks)


_NUM = re.compile(r"^[Tt]?(\d{1,6})$")


def ticket_number(ref: str) -> int | None:
    """3 for "T3", "t3" or "3"; None for any other ref (an id or a Linear identifier)."""
    m = _NUM.match(str(ref or "").strip())
    return int(m.group(1)) if m else None


def label(t: dict) -> str:
    """How a ticket is named to people and sessions: its Linear identifier, else T<n>."""
    return t.get("identifier") or (f"T{t['number']}" if t.get("number") else t["id"])


def depends_ok(depends_on) -> list[str]:
    """The refs a ticket depends on, as given (resolved by the caller), each once."""
    if depends_on is None:
        return []
    if isinstance(depends_on, str):
        depends_on = [d for d in re.split(r"[,\s]+", depends_on) if d]
    if not isinstance(depends_on, list):
        raise DocError("depends_on is a list of tickets")
    out: list[str] = []
    for d in depends_on:
        d = str(d or "").strip()
        if d and d not in out:
            out.append(d)
    if len(out) > MAX_DEPENDS:
        raise DocError(f"{len(out)} dependencies; a ticket that needs that many is too big")
    return out


def find_loop(deps: dict[str, list[str]], start: str) -> list[str] | None:
    """A dependency loop through `start` in {ticket id: [ids it depends on]}, as the path that
    closes it, or None."""
    path: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def walk(t: str) -> list[str] | None:
        if t in on_path:
            return path[path.index(t):] + [t]
        if t in done:
            return None
        on_path.add(t)
        path.append(t)
        for d in deps.get(t, []):
            hit = walk(d)
            if hit:
                return hit
        path.pop()
        on_path.discard(t)
        done.add(t)
        return None

    return walk(start)


def finished(t: dict) -> bool:
    """A ticket the ones depending on it no longer wait for: done or canceled, or (once the
    factory's stages exist) merged or shipped."""
    return t.get("status") in DONE_STATUSES or t.get("stage") in ("merged", "shipped")


def readiness(tickets: list[dict]) -> dict[str, dict]:
    """{ticket id: {ready, blocked_by: [labels]}} for every ticket. A ticket is ready when it is
    not finished and everything it depends on is finished; a dependency that no longer exists
    does not block."""
    by_id = {t["id"]: t for t in tickets}
    out = {}
    for t in tickets:
        waits = [by_id[d] for d in t.get("depends_on") or [] if d in by_id and not finished(by_id[d])]
        out[t["id"]] = {"ready": not finished(t) and not waits,
                        "blocked_by": [label(w) for w in waits]}
    return out
