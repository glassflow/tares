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
    """A ticket nobody works on any more: done or canceled, or (once the factory's stages exist)
    merged or shipped."""
    return t.get("status") in DONE_STATUSES or t.get("stage") in ("merged", "shipped")


def delivered(t: dict) -> bool:
    """A ticket whose work exists, so the ones depending on it may start: done, merged or
    shipped. A canceled one did not deliver, so its dependents stay blocked until someone drops
    the dependency or the plan changes."""
    return t.get("status") == "done" or t.get("stage") in ("merged", "shipped")


def readiness(tickets: list[dict]) -> dict[str, dict]:
    """{ticket id: {ready, blocked_by: [labels]}} for every ticket. A ticket is ready when it is
    not finished and everything it depends on is delivered; a dependency that no longer exists
    does not block. A canceled dependency shows as "T2 (canceled)"."""
    by_id = {t["id"]: t for t in tickets}
    out = {}
    for t in tickets:
        waits = [by_id[d] for d in t.get("depends_on") or [] if d in by_id and not delivered(by_id[d])]
        # ready means "can be started": a ticket already in the work is not
        out[t["id"]] = {"ready": t.get("status") == "todo" and t.get("stage") in (None, "todo")
                                 and not waits,
                        "blocked_by": [label(w) + (" (canceled)" if w.get("status") == "canceled"
                                                   else "") for w in waits]}
    return out


# ── the ledger (M4): who holds a ticket and where it stands ───────────────────

STAGES = ("todo", "doing", "review", "changes", "merged", "shipped", "blocked")
# a Tares-owned ticket's status follows its stage; a Linear one keeps Linear's
STATUS_OF_STAGE = {"todo": "todo", "doing": "in_progress", "review": "in_progress",
                   "changes": "in_progress", "blocked": "in_progress", "merged": "done",
                   "shipped": "done"}


def stage_ok(stage, reason) -> tuple[str, str]:
    s = str(stage or "").strip().lower()
    if s not in STAGES:
        raise DocError(f"unknown stage {stage!r}; one of: " + ", ".join(STAGES))
    r = " ".join(str(reason or "").split())[:500]
    if s == "blocked" and not r:
        raise DocError("say why it is blocked (reason)")
    return s, r


def may_move(caller: dict, ticket: dict, stage: str) -> str | None:
    """None when `caller` may move `ticket` to `stage`, else the reason (TR-428): the station
    holding the ticket (or a helper it started) moves its stage, only the releaser marks it
    shipped, and an unlabeled session may do anything."""
    if not caller.get("station"):
        return None
    if stage == "shipped":
        return may(caller, "shipped")
    holder = ticket.get("holder")
    if holder and holder in (caller["station"], caller.get("parent")):
        return None
    name = label(ticket)
    if not holder:
        return (f"{name} has no holder yet: the orchestrator assigns it (assign_ticket) before "
                "anyone moves it")
    return f"{name} is held by {holder}; ask the orchestrator to reassign it"


# ── the build, recorded (M5) ──────────────────────────────────────────────────

ASSUMPTION_STATES = ("open", "kept", "overturned")


def _line(v, what: str, n: int = 1000, required: bool = True) -> str:
    s = " ".join(str(v or "").split())
    if required and not s:
        raise DocError(f"say {what}")
    if len(s) > n:
        raise DocError(f"{what} is {len(s)} characters; keep it to {n}")
    return s


def assumption_ok(question, choice, why, affects) -> tuple[str, str, str, str]:
    return (_line(question, "the question"), _line(choice, "what you chose"),
            _line(why, "why you chose it"), _line(affects, "who it affects", 300, False))


def check_ok(command, result, commit, broke_test) -> tuple[str, str, str, str]:
    """A check a builder ran: the command, what it showed, the commit it ran on, and for a test
    it added, the test proven to fail against broken code."""
    cmd = str(command or "").strip()
    if not cmd:
        raise DocError("say the command you ran")
    if len(cmd) > 1000:
        raise DocError("keep the command to 1000 characters")
    commit = str(commit or "").strip()[:64]
    if commit and not re.match(r"^[0-9a-fA-F]{4,64}$", commit):
        raise DocError("commit is the commit's hash")
    return cmd, _line(result, "what it showed", 1000), commit, _line(broke_test, "the test", 300, False)


def check_line(cmd: str, result: str, commit: str, broke_test: str) -> str:
    """One check as it reads in the working doc."""
    at = f"[{commit[:7]}] " if commit else ""
    broke = f" (broke the code under `{broke_test}`: it failed, then passed again)" if broke_test else ""
    return f"- {at}`{cmd}` -> {result}{broke}"


def add_check_to_doc(body: str, line: str) -> str:
    """The working doc with `line` appended to the Checks part of its Progress section; both are
    made when missing. The rest of the doc is left as it is."""
    lines = body.rstrip("\n").split("\n") if body.strip() else []
    prog = next((i for i, l in enumerate(lines) if re.match(r"^##\s+Progress\b", l, re.I)), None)
    if prog is None:
        return "\n".join(lines + ["", "## Progress", "", "### Checks", "", line]) + "\n"
    end = next((i for i in range(prog + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[i])), len(lines))
    chk = next((i for i in range(prog + 1, end) if re.match(r"^###\s+Checks\b", lines[i], re.I)), None)
    if chk is None:
        at = end
        while at > prog + 1 and not lines[at - 1].strip():
            at -= 1
        block = ["", "### Checks", "", line] + ([""] if end < len(lines) else [])
        return "\n".join(lines[:at] + block + lines[end:]) + "\n"
    stop = next((i for i in range(chk + 1, end) if re.match(r"^#{1,3}\s", lines[i])), end)
    at = stop
    while at > chk + 1 and not lines[at - 1].strip():
        at -= 1
    return "\n".join(lines[:at] + [line] + lines[at:]) + "\n"


# ── the crew (M3): one always-on crew serves every project ────────────────────

ROLES = ("orchestrator", "reviewer", "releaser", "builder", "helper", "comms")
AUTONOMY = {
    "L3-review": "you approve each merge",
    "L4-ship": "a builder merges after the reviewer's pass and green CI",
    "L5-dark": "the releaser may deploy to prod after verifying, you are told",
}
MAX_NAME = 120


def station_ok(name, role, parent) -> tuple[str, str, str]:
    """(station, role, parent), cleaned; DocError when the station has no name or an unknown
    role."""
    name = str(name or "").strip()[:MAX_NAME]
    role = str(role or "").strip().lower()
    parent = str(parent or "").strip()[:MAX_NAME]
    if not name:
        raise DocError("a station needs its name")
    if role and role not in ROLES:
        raise DocError(f"unknown role {role!r}; one of: " + ", ".join(ROLES))
    return name, role, parent


CREW_DEFAULTS = {"autonomy": None, "release_profile": None, "prod_pattern": None,
                 "challenger": None, "builders_max": None}


def crew_settings_ok(body: dict, cur: dict) -> dict:
    """The crew's settings with the fields `body` sets changed; DocError on a bad value."""
    out = {**CREW_DEFAULTS, **cur}
    if body.get("autonomy") not in (None, ""):
        a = str(body["autonomy"]).strip()
        if a not in AUTONOMY:
            raise DocError(f"unknown autonomy {a!r}; one of: " + ", ".join(AUTONOMY))
        out["autonomy"] = a
    for k in ("release_profile", "prod_pattern"):
        if body.get(k) is not None:
            v = " ".join(str(body[k]).split())
            if len(v) > 300:
                raise DocError(f"{k} is {len(v)} characters; keep it to 300")
            out[k] = v or None
    if body.get("challenger") is not None:
        out["challenger"] = bool(body["challenger"])
    if body.get("builders_max") is not None:
        try:
            n = int(body["builders_max"])
        except (TypeError, ValueError):
            raise DocError("builders_max is a whole number")
        if not 1 <= n <= 20:
            raise DocError("builders_max is between 1 and 20")
        out["builders_max"] = n
    return out


def grant_line(words, when: str) -> str:
    """One grant as it is kept: the person's words, quoted, and the day they said them."""
    w = " ".join(str(words or "").split()).strip().strip('"')
    if not w:
        raise DocError("a grant quotes the person's words; there are none")
    if len(w) > 400:
        raise DocError(f"the grant is {len(w)} characters; keep it to 400")
    return f'"{w}" ({when})'


# Who may write what (the contract's table). A labeled station is any caller that says which
# station it is (the MCP proxy forwards FACTORY_STATION and FACTORY_ROLE); an unlabeled one (a
# person's own session, a single build session, the factory CLI) may do everything, as in M1.
# Cooperative, not a security boundary.
ONLY = {
    "grants": ("orchestrator",),
    "global_docs": ("orchestrator",),
    "assign": ("orchestrator",),
    "desk": ("orchestrator",),
    "assumption_state": ("orchestrator",),
    "review": ("reviewer",),
    "shipped": ("releaser",),
}
WHO = {"grants": "the orchestrator", "global_docs": "the orchestrator",
       "assign": "the orchestrator", "desk": "the orchestrator",
       "assumption_state": "the orchestrator", "review": "the reviewer",
       "shipped": "the releaser"}


def may(caller: dict, action: str) -> str | None:
    """None when `caller` ({station, role, parent}) may do `action`, else the reason to tell it."""
    if not caller.get("station"):
        return None
    allowed = ONLY.get(action)
    if allowed is None or caller.get("role") in allowed:
        return None
    return (f"{caller['station']} is a {caller.get('role') or 'station'}: only {WHO[action]} "
            f"may do this. Send it to {WHO[action]}.")
