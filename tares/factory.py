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

import hashlib
import os
import re
from datetime import datetime, timezone

from .docs import DocError, title_ok

MAX_GOAL = 500
MAX_CHECKS = 20
MAX_CHECK = 500
MAX_DEPENDS = 30

# a ticket status that counts as finished for the tickets that depend on it
DONE_STATUSES = ("done", "canceled")
# the stages of a ticket still in the work; a done or canceled status clears them (TR-1008)
OPEN_STAGES = ("todo", "doing", "review", "changes", "blocked")
STAGE_WORD = {"todo": "to do", "doing": "in progress", "review": "in review",
              "changes": "changes asked", "blocked": "blocked", "merged": "merged",
              "shipped": "merged", "canceled": "canceled"}


def state_of(t: dict, blocked_by: list[str] | None = None) -> str:
    """THE answer to "where is this ticket" (TR-1008), in words: merged, canceled, blocked, in
    progress, in review, changes asked or to do. A ticket keeps two fields: `status`, what its
    home (Tares, Linear, GitHub Issues) says, and `stage`, where the crew's workflow has it. A done
    or canceled status closes the ticket whatever the stage says; otherwise the stage decides.
    `blocked_by` (factory.readiness) turns a ticket to do into blocked. Every screen, the project's
    ring and the review queue read this; factory_store.review_tickets mirrors it in SQL."""
    stage, status = t.get("stage"), t.get("status")
    if stage in ("merged", "shipped") or status == "done":
        return "merged"
    if status == "canceled":
        return "canceled"
    if stage == "blocked":
        return "blocked"
    if stage in ("doing", "review", "changes"):
        return STAGE_WORD[stage]
    if status == "in_progress":
        return "in progress"
    return "blocked" if blocked_by else "to do"


def goal_ok(goal) -> str:
    g = " ".join(str(goal or "").split())
    if len(g) > MAX_GOAL:
        raise DocError(f"the goal is {len(g)} characters; keep it to {MAX_GOAL}")
    return g


def checks_ok(checks) -> list[dict]:
    """[{check, expect, for?}], cleaned. A check is a command or a short manual step; `expect` is
    what it should show. A plain string is a check with no expectation. `for: "person"` marks a
    check only a person can do (look at a screen, try a flow); every other check is the crew's,
    which it runs and records when the milestone's last ticket merges (TR-1017)."""
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
        who = str(c.get("for") or "crew").strip().lower()
        if who not in ("crew", "person"):
            raise DocError('a check is "for" the crew (the default) or a "person"')
        out.append({"check": check, "expect": expect, **({"for": "person"} if who == "person" else {})})
    return out


def milestone_state(m: dict, tickets: list[dict], results: dict | None = None,
                    accepted: dict | None = None, crew_has_it: bool = False) -> dict:
    """Where a milestone stands for acceptance (TR-1017), from its checks, the latest result of
    each (`results` {index: {result, note, by, at}}) and a whole-milestone acceptance. A crew
    check is the crew's to run and record; a person check (`for: "person"`) is the person's.

    delivered: every live ticket merged. accepted: accepted as a whole, or delivered with every
    crew check passed and every person check passed (a milestone without checks is accepted once
    delivered). crew_failed / person_wrong: work for the crew, never for the person. needs_person:
    delivered, not accepted, nothing failed, and a person check still open, or crew checks the
    crew never recorded (a milestone from before results were kept), which the person may accept
    as it is. With `crew_has_it` (the project is with a crew, TR-1021) unrecorded crew checks stay
    the crew's: only a person check makes it need the person."""
    results = results or {}
    live = [t for t in tickets if t.get("status") != "canceled"]
    done = bool(live) and all(delivered(t) for t in live)
    crew, person = [], []
    for i, c in enumerate(m.get("checks") or []):
        (person if c.get("for") == "person" else crew).append(i)
    res = lambda i: (results.get(i) or {}).get("result")
    crew_failed = [i for i in crew if res(i) == "fail"]
    crew_unrun = [i for i in crew if res(i) is None]
    person_wrong = [i for i in person if res(i) == "fail"]
    person_open = [i for i in person if res(i) is None]
    passed = not (crew_failed or crew_unrun or person_wrong or person_open)
    is_accepted = bool(accepted) or (done and passed)
    needs = (done and not is_accepted and not crew_failed and not person_wrong
             and bool(person_open or (crew_unrun and not crew_has_it)))
    return {"delivered": done, "accepted": is_accepted,
            "accepted_by": (accepted or {}).get("by") if accepted else ("checks" if is_accepted else None),
            "crew": crew, "person": person, "crew_failed": crew_failed, "crew_unrun": crew_unrun,
            "person_wrong": person_wrong, "person_open": person_open, "needs_person": needs}


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


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_RE = re.compile(r"^(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                        r"([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def repo_ok(repo) -> str | None:
    """A GitHub repo as `owner/name`, from that, a github.com URL or a git remote; None for empty.
    DocError for anything else."""
    r = str(repo or "").strip()
    if not r:
        return None
    m = _GITHUB_RE.match(r)
    if m:
        r = m.group(1)
    if r.endswith(".git"):
        r = r[:-4]
    if not _REPO_RE.match(r):
        raise DocError(f"repo {repo!r} is not a GitHub repo; give it as owner/name")
    return r


_NORM_REPO = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9._-]+$")
# a pasted GitHub address: https://github.com/o/n(.git)(/...), github.com/o/n, git@github.com:o/n.git
_NORM_GITHUB = re.compile(r"^(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)([^/]+)/([^/?#]+)")


def norm_repo(repo) -> str:
    """The repo as stored and matched: trimmed and lowercase, a pasted GitHub address turned
    into `owner/name`. ValueError unless it comes out as `owner/name`. Grants, sign-offs and
    the area owners (ee) all match repos this way."""
    r = str(repo or "").strip().lower()
    m = _NORM_GITHUB.match(r)
    if m:
        name = m.group(2)
        r = f"{m.group(1)}/{name[:-4] if name.endswith('.git') else name}"
    if not _NORM_REPO.match(r):
        raise ValueError(f"{repo!r} is not a repo: write it as owner/name, like glassflow/tares")
    return r


def handover_repos(repos) -> list[dict]:
    """The repos a project is handed over with, each {path, name}: the folder it is checked out
    in on the crew's machine and its GitHub name (owner/name, may be empty). Takes strings (a
    path, or owner/name) or {path, name} objects. DocError for a bad one."""
    if repos is None:
        return []
    if isinstance(repos, (str, dict)):
        repos = [repos]
    if not isinstance(repos, list):
        raise DocError("repos is a list of {path, name}")
    out: list[dict] = []
    for x in repos:
        if isinstance(x, str):
            x = x.strip()
            x = ({"path": x, "name": None} if x.startswith(("/", "~", "."))
                 else {"path": None, "name": x})
        if not isinstance(x, dict):
            raise DocError("each repo is {path, name}")
        path = str(x.get("path") or "").strip()
        if path.startswith("~"):
            path = os.path.expanduser(path)
        if path and not path.startswith("/"):
            raise DocError(f"repo path {path!r} is not absolute; give the full folder the repo "
                           "is cloned in on the crew's machine")
        path = os.path.normpath(path)[:500] if path else None
        name = repo_ok(x.get("name"))
        if not path and not name:
            continue
        same = next((o for o in out if (path and o["path"] == path)
                     or (name and (o["name"] or "").lower() == name.lower())), None)
        if same:
            same["path"] = same["path"] or path
            same["name"] = same["name"] or name
        else:
            out.append({"path": path, "name": name})
    if len(out) > 20:
        raise DocError(f"{len(out)} repos; a project handed to the crew takes up to 20")
    return out


MAP_MAX_CHARS = 8000   # about 2,000 tokens
MAP_NOTE_MAX = 200


def map_version(body: str | None) -> str:
    """What a writer must name to change the map: a short hash of the map it read ("" for none)."""
    return hashlib.sha1(body.encode()).hexdigest()[:10] if body else ""


def map_ok(body, note) -> tuple[str, str]:
    """A repo map and the line that says what changed in it, or DocError. The cap keeps every
    brief that carries the map small: a longer map has to lose something first."""
    text, why = str(body or "").strip(), " ".join(str(note or "").split())
    if not text:
        raise DocError("the map is empty: write the layout, entry points, how to install, build, "
                       "test and lint, conventions, the files tickets touch most and known traps")
    if len(text) > MAP_MAX_CHARS:
        raise DocError(f"the map is {len(text)} characters; the most is {MAP_MAX_CHARS} (about "
                       f"2,000 tokens). Cut {len(text) - MAP_MAX_CHARS}: keep what a builder needs "
                       "on its first day, and point to files for the rest")
    if not why:
        raise DocError("say what changed in the map (note): it becomes a dated line in its history")
    if len(why) > MAP_NOTE_MAX:
        raise DocError(f"the note is {len(why)} characters; the most is {MAP_NOTE_MAX}")
    return text, why


def merge_repos(old: list[dict], new: list[dict]) -> list[dict]:
    """A later hand-over adds its repos to the ones already handed over, never replaces them."""
    return handover_repos([*(old or []), *(new or [])])


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


def ready_to_close(project: dict, tickets: list[dict]) -> bool:
    """A project still open whose every ticket is finished and at least one was delivered: the
    console offers to close it. A project whose tickets were all canceled is not ready; it needs
    a person's look."""
    return (project.get("status") != "done"
            and all(finished(t) for t in tickets) and any(delivered(t) for t in tickets))


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


def progress(tickets: list[dict]) -> dict:
    """{done, doing, blocked, todo, canceled, total}: a project's tickets as its progress ring
    shows them. Done goes by the stage as much as the status: a Linear or GitHub ticket keeps
    its home's status, which can still say in progress after the crew merged it. Blocked is a
    station's "blocked", or a ticket to do that waits on one not delivered. Canceled ones stay
    out of the total. The project page counts its ring the same way
    (ui/src/components/ProgressRing.tsx)."""
    waits = readiness(tickets)
    n = {"done": 0, "doing": 0, "blocked": 0, "todo": 0, "canceled": 0}
    ring = {"merged": "done", "canceled": "canceled", "blocked": "blocked", "in progress": "doing",
            "in review": "doing", "changes asked": "doing", "to do": "todo"}
    for t in tickets:
        n[ring[state_of(t, waits[t["id"]]["blocked_by"])]] += 1
    return {**n, "total": n["done"] + n["doing"] + n["blocked"] + n["todo"]}


def activity_at(updated_at, tickets: list[dict]):
    """When a project last moved: its own update or its newest ticket's (a stage change, a new
    PR and a sync all touch the ticket), whichever is later. The Projects list shows this as
    Updated, so a project the crew is working on reads as fresh without anyone editing it."""
    stamps = [t for t in [updated_at, *(x.get("updated_at") for x in tickets)] if t]
    return max(stamps) if stamps else None


# ── the ledger (M4): who holds a ticket and where it stands ───────────────────

STAGES = ("todo", "doing", "review", "changes", "merged", "shipped", "blocked")
# the status each stage asks of the ticket system (a `tares.integrations.types.Status` value): every
# ticket's status follows its stage, whichever system holds the ticket (TR-848)
STATUS_OF_STAGE = {"todo": "todo", "doing": "in_progress", "review": "in_review",
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
    # one line, no backticks: it goes into the working doc inside a code span
    cmd = " ".join(str(command or "").split()).replace("`", "'")
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


# ── review and merge (M6) ─────────────────────────────────────────────────────

VERDICTS = ("pass", "changes", "block")
SEVERITIES = ("P1", "P2", "P3")


def review_ok(verdict, verified, not_verified, findings, head) -> tuple:
    """(verdict, verified, not_verified, findings, head), cleaned; DocError on a bad value."""
    v = str(verdict or "").strip().lower()
    if v not in VERDICTS:
        raise DocError("verdict is one of: " + ", ".join(VERDICTS))
    head = str(head or "").strip().lower()
    if not re.match(r"^[0-9a-f]{7,64}$", head):
        raise DocError("head is the commit the review was made on (its hash)")
    ver = str(verified or "").strip()[:4000]
    if not ver:
        raise DocError("say what you verified (the commands you ran and what they showed)")
    out = []
    for f in findings or []:
        if not isinstance(f, dict):
            raise DocError("each finding is {severity, file, line, blocking, text}")
        sev = str(f.get("severity") or "P2").strip().upper()
        if sev not in SEVERITIES:
            raise DocError("a finding's severity is P1, P2 or P3")
        text = _line(f.get("text"), "what the finding is", 2000)
        try:
            ln = int(f.get("line") or 0)
        except (TypeError, ValueError):
            ln = 0
        out.append({"severity": sev, "file": str(f.get("file") or "").strip()[:300], "line": ln,
                    "blocking": bool(f.get("blocking", sev in ("P1", "P2"))), "text": text})
    if v == "pass" and any(f["blocking"] for f in out):
        raise DocError("a pass cannot carry a blocking finding")
    return v, ver, str(not_verified or "").strip()[:2000], out, head


# what a finding is about, to see the same mistake come back (TR-443). A plain word bucket:
# cheap, stable, and readable by a person deciding whether it is a rule
FINDING_KINDS = [
    ("migration", (r"migrations?", r"migrate", r"schema change", r"alter table")),
    ("missing test", (r"no tests?", r"untested", r"missing tests?", r"not covered by",
                      r"without a test", r"test passes against")),
    ("error handling", (r"error handling", r"unhandled", r"swallow(s|ed)?", r"uncaught")),
    ("config key", (r"config keys?", r"env vars?", r"environment variables?")),
    ("null or empty", (r"null", r"empty (list|string|value|input)", r"undefined",
                       r"nonetype")),
    ("concurrency", (r"race conditions?", r"concurren\w*", r"deadlocks?", r"data race")),
    ("security", (r"secrets?", r"(sql|command) injection", r"xss", r"csrf",
                  r"auth(entication|orization|z)?\b(?!or)")),
    ("docs out of date", (r"readme", r"out of date doc\w*", r"comment says")),
]


def finding_kind(text: str) -> str:
    """What a finding is about, as a word bucket (word boundaries, no generic words)."""
    t = (text or "").lower()
    for kind, pats in FINDING_KINDS:
        if any(re.search(rf"\b{p}\b", t) for p in pats):
            return kind
    words = [w for w in re.findall(r"[a-z]{4,}", t)][:4]
    return " ".join(words) or "other"


RECUR_MIN = 3          # the same kind in this many tickets of a repo is a pattern
RECUR_DAYS = 30


def _norm(s: str) -> str:
    return " ".join(str(s or "").split()).lower()


# a failure in a command's output: a non-zero failure count, a FAILED line, a traceback
_FAIL_COUNT = re.compile(r"\b([1-9]\d*)\s+(failed|errors?|failures?)\b"
                         r"|\b(failures|errors|failed)\s*[=:]\s*([1-9]\d*)\b", re.I)
_FAIL_LINE = re.compile(r"^\s*(FAILED\b|FAIL\b|Traceback \(most recent call last\))", re.M)
# the counts a claim and an output are compared on
_COUNT = re.compile(r"\b(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b", re.I)


def _says_failed(text: str) -> bool:
    t = str(text or "")
    return bool(_FAIL_COUNT.search(t) or _FAIL_LINE.search(t))


def _same_command(claimed: str, ran: str) -> bool:
    """The claimed command is what ran: the same command, or the last step of a chain
    (`cd app && pytest -q` ran `pytest -q`). A short recorded command never stands for a longer
    claim."""
    c, r = _norm(claimed), _norm(ran)
    if len(r) < 3 or not c:
        return False
    if c == r:
        return True
    last = re.split(r"\s*(?:&&|;|\|\|)\s*", r)[-1].strip()
    return last == c


def match_check(check: dict, commands: list[dict], since=None) -> tuple[str, str | None]:
    """("matched" | "not_found" | "differs", what the recording shows) for a check a builder
    claimed (TR-442): the command must be in the recording between `since` (when the ticket
    went into the work) and the claim, and what it printed must agree with the claimed result
    (each pass, fail, error and skip count the claim states, and no failure where the claim
    says it passed). A break check also needs the named test failing, then passing."""
    when = check.get("at")
    ran = [c for c in commands if _same_command(check.get("command"), c["command"])
           and (when is None or c["at"] <= when) and (since is None or c["at"] >= since)]
    if not ran:
        return "not_found", None
    run = ran[-1]
    out = run["output"] or ""
    claim = str(check.get("result") or "")
    counts = [(n, w.lower().rstrip("s")) for n, w in _COUNT.findall(claim) if int(n) > 0]
    missing = [f"{n} {w}" for n, w in counts
               if not re.search(rf"\b{n}\s+{w}s?\b", out, re.I)]
    claims_pass = not _says_failed(claim)
    if missing or (claims_pass and (run["is_error"] or _says_failed(out))):
        tail = out.strip().splitlines()[-3:] if out.strip() else ["(no output recorded)"]
        return "differs", " / ".join(tail)[:400]
    test = str(check.get("broke_test") or "").strip()
    if test:
        about = [c for c in commands if test.lower() in (c["command"] + " " + c["output"]).lower()
                 and (when is None or c["at"] <= when) and (since is None or c["at"] >= since)]
        failed_at = next((i for i, c in enumerate(about) if c["is_error"] or _says_failed(c["output"])), None)
        passed_after = failed_at is not None and any(
            not (c["is_error"] or _says_failed(c["output"])) for c in about[failed_at + 1:])
        if not passed_after:
            return "differs", f"the recording does not show {test} failing and then passing"
    return "matched", None


def challenger_layer(events: list[dict]) -> dict:
    """The Codex challenger's say on a ticket (TR-440), from its recorded reviews and waivers:
    rounds, the findings still open (the latest review's), the ones fixed since (in an earlier
    review, not in the latest), the waivers with their reasons."""
    commits = [e for e in events if e["type"] == "challenge_commit"]
    waivers = [f for e in events if e["type"] == "challenge_waived"
               for f in (e["challenge"].get("findings") or [])]
    if not commits:
        return {"rounds": 0, "open": [], "fixed": [], "waived": waivers, "verdict": None,
                "sha": None, "clean": True}
    last = commits[-1]["challenge"]
    verdict = str(last.get("verdict") or "").upper()
    waived_titles = {w.get("title") for w in waivers}
    # a waiver recorded after the review it answers takes the finding out of the open ones
    open_ = ([f for f in (last.get("findings") or [])
              if not f.get("waived") and f.get("title") not in waived_titles]
             if verdict == "FAIL" else [])
    open_titles = {f.get("title") for f in open_}
    seen: dict[str, dict] = {}
    for e in commits[:-1]:
        for f in e["challenge"].get("findings") or []:
            if not f.get("waived"):
                seen.setdefault(f.get("title"), f)
    fixed = [f for t, f in seen.items() if t not in open_titles and t not in waived_titles]
    blocking = [f for f in open_ if f.get("priority") in ("P1", "P2")]
    # a run that gave no verdict (error, timeout) is not a clean layer
    ran = verdict in ("PASS", "FAIL")
    return {"rounds": len(commits), "open": open_, "fixed": fixed, "waived": waivers,
            "verdict": "pass" if verdict == "PASS" else "findings" if verdict == "FAIL"
            else (verdict.lower() or "no verdict"),
            "sha": last.get("sha"), "clean": ran and not blocking}


# ── you in the loop (M7) ──────────────────────────────────────────────────────

DESK_STATES = ("open", "answered", "withdrawn")
MATCH_HOURS = 48       # an answer is looked for in what the person typed this recently
MATCH_SKEW_SECONDS = 60  # a reply typed this long before the item's cell timestamp still counts: machines' clocks differ


MAX_COMMAND = 2000     # a refused command, kept as the station ran it


def desk_ok(question, recommendation, why, options, context, station=None,
            command=None) -> tuple:
    """The desk item's fields, cleaned. A permission request also names the station that was
    refused and the exact command: kept verbatim (only trimmed), since the person allows that."""
    q = _line(question, "the question", 1000)
    rec = _line(recommendation, "your recommendation", 500)
    w = _line(why, "why you recommend it", 1000)
    if options is None:
        options = []
    if not isinstance(options, list) or len(options) > 8:
        raise DocError("options is a list of up to 8 choices")
    opts = [_line(o, "an option", 300) for o in options]
    st = _line(station, "the station", MAX_NAME, False)
    cmd = str(command or "").strip()
    if len(cmd) > MAX_COMMAND:
        raise DocError(f"the command is {len(cmd)} characters; keep it to {MAX_COMMAND}")
    if cmd and not st:
        raise DocError("a permission request names the station that was refused")
    return q, rec, w, opts, _line(context, "the context", 1500, False), st, cmd


def _plain(s: str) -> str:
    t = " ".join(str(s or "").lower().split())
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    return t.strip(" .!\"'")


_PASTE_TAG = re.compile(r"</?pasted_content\b[^>]*>")


def _pieces(text: str) -> list[str]:
    """A turn, each of its lines, and each sentence of each line, in plain form. Claude Code wraps
    a paste in <pasted_content> tags; the person typed what is inside them (TR-697)."""
    text = _PASTE_TAG.sub("\n", str(text or ""))
    out = [_plain(text)]
    for line in text.splitlines():
        if line.strip():
            out.append(_plain(line))
            out += [_plain(s) for s in re.split(r"(?<=[.!?;])\s+", line) if s.strip()]
    return [p for p in out if p]


def words_match(words: str, turns: list[dict]) -> dict | None:
    """The person turn whose whole text, or one whole line or sentence of it, is `words` (case,
    spacing and quote marks aside), or None. Not a fragment: "ok" is not found in "token", nor
    "ship it" in "I don't want to ship it". An answer recorded as the person's must be what
    they typed (TR-450)."""
    w = _plain(_PASTE_TAG.sub("\n", str(words or "")))
    if len(w) < 2:
        return None
    return next((t for t in turns if w in _pieces(t["text"])), None)


OPTION_LETTERS = "ABCDEFGH"   # desk options are lettered by position; desk_ok allows 8
_LETTER = re.compile(r"^\(?([a-h])\)?[.)]?$")


def option_letter(words: str) -> int | None:
    """The option a reply of one letter names (B, b, B), (B), B.): its index, or None when the
    words are not a single letter (TR-475)."""
    m = _LETTER.match(_plain(words))
    return OPTION_LETTERS.lower().index(m.group(1)) if m else None


# a reply that names an option by its letter inside a little more: "D85 A", "B - next week",
# "A) the first one", "b: yes". The letter must stand alone after an optional desk label.
_PICKED = re.compile(r"^(?:d\d+\s*[:.)\-]?\s+)?\(?([a-h])\)?(?:\s*[.):]|\s+-|\s*$)")
_LABEL = re.compile(r"^d\d+\s*[:.)\-]?\s+")


def picked_option(words: str, choice: str, options: list[str]) -> str | None:
    """The option a desk answer picks, or None (TR-1018). A letter, alone or after the desk label
    and before a separator ("D85 A", "B - later", "A) yes"), picks by position; otherwise the
    choice the orchestrator passed, or the words without the label, equal to an option or
    starting with it (either way round) pick that option."""
    opts = [o for o in options or [] if str(o or "").strip()]
    if not opts:
        return None
    for text in (choice, words):
        m = _PICKED.match(_plain(text))
        if m:
            i = OPTION_LETTERS.lower().index(m.group(1))
            if i < len(opts):
                return opts[i]
    for text in (choice, words):
        t = _LABEL.sub("", _plain(text))
        if not t:
            continue
        for o in opts:
            po = _plain(o)
            if po and (t == po or t.startswith(po) or (len(t) >= 3 and po.startswith(t))):
                return o
    return None


def decision_words(d: dict) -> dict:
    """How a decision reads (TR-1018): {answer, said}. `answer` is what was chosen (the option's
    text, or the person's own words when the question had no options); `said` is their words as
    typed, only when they add something to the answer: a bare letter, "D85 A", or more words."""
    words = " ".join(str(d.get("words") or "").split())
    choice = " ".join(str(d.get("choice") or "").split())
    answer = choice or words
    bare = _LABEL.sub("", _plain(words))
    said = None if not words or bare == _plain(answer) else words
    return {"answer": answer, "said": said}


def letter_match(index: int, turns: list[dict]) -> dict | None:
    """The person turn that is that option's letter and nothing else. A letter inside a longer
    message never counts: "B but later" and "maybe B" are not B."""
    return next((t for t in turns if option_letter(t["text"]) == index), None)


QUIET_MIN = 10   # a working session that sent nothing for this long shows as quiet
# an idle or waiting session silent this long is most likely gone: Claude Code retires idle background
# sessions after about an hour, and one killed, or whose end never reached Tares, never says so
WAITING_QUIET_MIN = 90


def shown_state(state: str | None, last_at, now) -> str | None:
    """The state to show for a session: "quiet" once a working, idle or waiting one has been
    silent past its window (TR-473), else the state it reported."""
    if state in ("working", "idle", "waiting") and hasattr(last_at, "timestamp"):
        quiet = (now - last_at).total_seconds() // 60
        if quiet >= (QUIET_MIN if state == "working" else WAITING_QUIET_MIN):
            return "quiet"
    return state


# Tares reads a session a few seconds behind what the person typed, so a reply not found yet is
# looked for again for this long before the answer is refused (TR-475).
MATCH_WAIT_SECONDS = 10.0


def says_every_project(words: str) -> bool:
    """The person's words say a standing answer holds for every project."""
    return bool(re.search(r"\b(all|every|each) projects?\b|\beverywhere\b", str(words or ""),
                          re.I))


# ── the crew (M3): one always-on crew serves every project ────────────────────

ROLES = ("orchestrator", "reviewer", "releaser", "builder", "helper")
# the always-on stations, named <crew>-<role> (crew-<role> for the default crew, TR-541); builders
# and helpers come and go
CREW_ROLES = ("orchestrator", "reviewer", "releaser")
AUTONOMY = {
    "L3-review": "you approve each merge",
    "L4-ship": "a builder merges after the reviewer's pass and green CI",
}
MAX_NAME = 120

# a model call whose tool only coordinates the crew: Tares and Linear MCP calls, messages, the
# agent list, skill loads and the schedule (TR-683)
_COORDINATION_PREFIXES = ("mcp__tares__", "mcp__plugin_tares_tares__", "mcp__linear")
_COORDINATION_TOOLS = ("SendMessage", "ListAgents", "Skill", "CronCreate", "CronList", "CronDelete")


def is_coordination(tool: str) -> bool:
    tool = str(tool or "")
    return tool in _COORDINATION_TOOLS or tool.startswith(_COORDINATION_PREFIXES)


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


CREW_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def crew_station_role(station: str, role: str = "") -> str:
    """The crew role (orchestrator, reviewer, releaser) of a station named `<crew>-<role>`, for any
    crew name; "" for a builder, a helper or anything else. `role` is the role the station
    reports, when known: a builder or helper is never a crew role, whatever its name ends in. A
    name with `-build-` in it is a builder's, never a crew station's (a crew name cannot hold that
    word), so a project named crew-build-tools is not mangled."""
    if role and role not in CREW_ROLES:
        return ""
    if "-build-" in f"-{station}-":
        return ""
    for r in ((role,) if role else CREW_ROLES):
        crew = station[:-len(r) - 1] if station.endswith("-" + r) else ""
        if not CREW_NAME_RE.match(crew):
            continue
        if not role and any(crew.endswith("-" + x) or crew == x for x in CREW_ROLES):
            return ""  # `crew-reviewer-releaser`: a helper of crew-reviewer, not a releaser
        return r
    return ""


def _crew_side_by_name(name: str) -> bool:
    """A crew station (`<crew>-<role>`) or a helper of one (`<crew>-<role>-<suffix>`), by its name
    alone: no `-build-` in it, and a role word set between hyphens."""
    if "-build-" in f"-{name}-":
        return False
    return bool(crew_station_role(name)) or any(f"-{r}-" in name for r in CREW_ROLES)


def on_crew_side(name: str, rows: list[dict]) -> bool:
    """Whether a station is one of the crew's own or a helper under one (they serve every project
    and hold no tickets), by its role and its parent chain in `rows`, the stations as /api/crew
    lists them. A parent no row reports is read by its name, for any crew name."""
    cur = next((r for r in rows if r.get("name") == name), None)
    if cur is None:
        return _crew_side_by_name(name)
    for _ in range(10):
        if cur is None or cur.get("role") != "helper" or not cur.get("parent"):
            break
        up = next((r for r in rows if r.get("name") == cur["parent"]), None)
        if up is None:
            return _crew_side_by_name(cur["parent"])
        cur = up
    return cur is not None and cur.get("role") in CREW_ROLES


def attach_command(station: str, role: str = "") -> str:
    """The command that opens a station's session in a terminal. The crew's own stations go by
    their role (`crew-reviewer` and `ana-mbp-reviewer` are both `reviewer`); any other, a helper
    of one among them (`crew-reviewer-lint`), by its full name, as `factory crew attach` takes
    them."""
    return f"tares factory crew attach {crew_station_role(station, role) or station}"


CREW_DEFAULTS = {"autonomy": None, "releaser": None, "release_profile": None,
                 "prod_pattern": None, "challenger": None, "builders_max": None,
                 "kit": {"frozen": None}, "idle_wake": None, "pace": None,
                 "claude_update": None, "docker_cleanup": None, "docker_keep": None}


def kit_ok(v) -> dict:
    """The kit setting: `frozen` is null (the crew follows the checkout) or when, by whom and
    which kit version `factory crew freeze` froze it at; DocError on anything else."""
    if not isinstance(v, dict) or set(v) != {"frozen"}:
        raise DocError("kit is an object with one field, frozen")
    f = v.get("frozen")
    if f is None:
        return {"frozen": None}
    if not isinstance(f, dict):
        raise DocError("kit.frozen is null or an object with at, by and version")
    out = {}
    for k in ("at", "by", "version"):
        x = f.get(k)
        if not isinstance(x, str) or not x.strip():
            raise DocError(f"kit.frozen.{k} is a non-empty string")
        x = " ".join(x.split())
        if len(x) > 100:
            raise DocError(f"kit.frozen.{k} is {len(x)} characters; keep it to 100")
        out[k] = x
    try:
        datetime.fromisoformat(out["at"].replace("Z", "+00:00"))
    except ValueError:
        raise DocError("kit.frozen.at is a time such as 2026-10-25T10:00:00Z") from None
    return {"frozen": out}


def _stricter_autonomy(a, b):
    """The stricter of two autonomy levels: the one listed first in AUTONOMY (the person
    approves more). An unset or unknown one gives way to the other."""
    levels = list(AUTONOMY)
    ranked = [x for x in (a, b) if x in levels]
    return min(ranked, key=levels.index) if ranked else (a or b)


def stricter_than_team(team: dict, mine: dict) -> dict:
    """`mine` with autonomy and challenger held to the team's value, which is a ceiling: a
    person can ask for more care than the team does, never for less."""
    out = dict(mine)
    if team.get("autonomy") in AUTONOMY:
        out["autonomy"] = _stricter_autonomy(team["autonomy"], mine.get("autonomy"))
    if team.get("challenger"):
        out["challenger"] = True
    return out


def personal_ceiling_ok(team: dict, body: dict) -> None:
    """DocError when `body` sets a personal autonomy or challenger looser than the team's value,
    naming the team's value. Ingest scope and the other settings have no ceiling."""
    a = str(body.get("autonomy") or "").strip()
    t = team.get("autonomy")
    if a in AUTONOMY and t in AUTONOMY and _stricter_autonomy(a, t) != a:
        raise DocError(f"autonomy {a} is looser than the team's {t}; a personal value can only "
                       f"be as strict or stricter. Clear yours to use the team's.")
    if body.get("challenger") is not None and not body["challenger"] and team.get("challenger"):
        raise DocError("challenger off is looser than the team's value, on; a personal value "
                       "can only be as strict or stricter. Clear yours to use the team's.")


def crew_settings_ok(body: dict, cur: dict) -> dict:
    """The crew's settings with the fields `body` sets changed; DocError on a bad value.
    `clear: [field, ...]` unsets those fields (a person's own fall back to the team's)."""
    out = {**CREW_DEFAULTS, **cur}
    clear = body.get("clear")
    if clear is not None:
        if not isinstance(clear, list) or any(k not in CREW_DEFAULTS for k in clear):
            raise DocError("clear is a list of settings, from: " + ", ".join(CREW_DEFAULTS))
        for k in clear:
            out[k] = None
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
    for k in ("challenger", "releaser", "claude_update", "docker_cleanup"):
        if body.get(k) is not None:
            out[k] = bool(body[k])
    if body.get("idle_wake") is not None:
        if not isinstance(body["idle_wake"], bool):
            raise DocError("idle_wake is true or false")
        out["idle_wake"] = body["idle_wake"]
    if body.get("pace") is not None:
        if not isinstance(body["pace"], bool):
            raise DocError("pace is true or false")
        out["pace"] = body["pace"]
    if body.get("builders_max") is not None:
        try:
            n = int(body["builders_max"])
        except (TypeError, ValueError):
            raise DocError("builders_max is a whole number")
        if not 1 <= n <= 20:
            raise DocError("builders_max is between 1 and 20")
        out["builders_max"] = n
    if body.get("docker_keep") is not None:
        out["docker_keep"] = docker_keep_ok(body["docker_keep"])
    if body.get("kit") is not None:
        out["kit"] = kit_ok(body["kit"])
    return out


def docker_keep_ok(v) -> list:
    """The images the crew's Docker cleanup never removes (TR-700): image names, such as
    `supabase/postgres` (every tag) or `myimage:1.2`, `*` as a wildcard. DocError on anything else."""
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise DocError("docker_keep is a list of image names")
    out = []
    for x in v:
        x = x.strip()
        if not x:
            continue
        if len(x) > 200 or any(c.isspace() for c in x):
            raise DocError(f"{x[:40]!r} is not an image name")
        if x not in out:
            out.append(x)
    if len(out) > 50:
        raise DocError("docker_keep holds at most 50 names")
    return out


PACE_SLOW = {"session": 85, "weekly_all": 90}   # percent of a limit at which the crew slows
PACE_HOLD = 95                                  # percent of the 5-hour window at which builders wait
PACE_NAME = {"session": "5-hour window", "weekly_all": "weekly limit"}


def _pace_when(v) -> datetime | None:
    """A limit's reset time (ISO 8601 as plan_now carries it) as an aware datetime, or None."""
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def pace_now(settings: dict, plan: dict | None, now: datetime) -> dict:
    """How hard the plan's limits press on the crew (TR-674). `level` is 0 (ok), 1 (slow: the 5-hour
    window at 85% or the weekly limit at 90%: no new builders, every station on its longest tick) or
    2 (hold: the window at 95%: builders finish their step, push, report and wait). `at_limit` is
    true while any limit is full and has not reset. A limit whose reset time has passed counts as 0.
    With the crew setting `pace` off the state is "off" and the level 0 whatever the plan says;
    `known` is false while no plan reading exists. `until` is when the limits behind the level reset."""
    on = bool((settings or {}).get("pace"))
    out = {"on": on, "known": False, "level": 0, "at_limit": False, "state": "off" if not on else "ok",
           "until": None, "session_pct": None, "weekly_pct": None, "note": "Pacing is off."}
    if not on:
        return out
    limits = [x for x in ((plan or {}).get("limits") or []) if isinstance(x, dict)]
    if not limits:
        out["note"] = "Pacing is on, and no reading of the plan's limits has arrived yet."
        return out
    out["known"] = True
    pct = {}
    causes = []   # (limit, percent) of each limit that sets the level
    level = 0
    for x in limits:
        t = _pace_when(x.get("resets_at"))
        p = x.get("percent")
        p = 0 if x.get("reset") or (t and t <= now) or isinstance(p, bool) or not isinstance(p, (int, float)) else p
        kind = x.get("kind")
        if kind not in PACE_SLOW:
            continue   # a per-model week or any other limit stops that model only, not the crew
        pct[kind] = max(p, pct.get(kind, 0))
        if p >= 100:
            out["at_limit"] = True
        if p >= PACE_SLOW[kind]:
            causes.append((kind, p, t))
        if kind == "session" and p >= PACE_HOLD:
            level = 2
        elif p >= PACE_SLOW.get(kind, 101):
            level = max(level, 1)
    out["level"] = level
    out["session_pct"], out["weekly_pct"] = pct.get("session"), pct.get("weekly_all")
    if out["at_limit"]:
        out["state"] = "limit"
    elif level:
        out["state"] = "hold" if level == 2 else "slow"
    ends = [t for _, _, t in causes if t]
    if ends:
        out["until"] = max(ends).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not causes:
        out["note"] = "Pacing is on, and the plan has room."
        return out
    what = " and ".join(f"the {PACE_NAME.get(k, k)} is at {p:g}%" for k, p, _ in causes)
    do = ("builders finish their step, push, report and wait, and no new builder starts" if level == 2 else
          "no new builder starts and every station ticks every 45 minutes")
    when = f" until {max(ends).astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC" if ends else ""
    out["note"] = f"Pacing: {what}, so {do}{when}."
    return out


def grant_words(words) -> str:
    """The person's words as a grant keeps them: one line, no outer quotes, 400 characters at most."""
    w = " ".join(str(words or "").split()).strip().strip('"')
    if not w:
        raise DocError("a grant quotes the person's words; there are none")
    if len(w) > 400:
        raise DocError(f"the grant is {len(w)} characters; keep it to 400")
    return w


def grant_line(words, when: str) -> str:
    """One grant as it is kept: the person's words, quoted, and the day they said them."""
    return f'"{grant_words(words)}" ({when})'


GRANT_KINDS = ("team", "project", "personal")
GRANT_STATES = ("active", "waiting", "revoked")


# Who may write what (the contract's table). A labeled station is any caller that says which
# station it is (the MCP proxy forwards FACTORY_STATION and FACTORY_ROLE); an unlabeled one (a
# person's own session, a single build session, the factory CLI) may do everything, as in M1.
# Cooperative, not a security boundary.
ONLY = {
    "grants": ("orchestrator",),
    "global_docs": ("orchestrator",),
    "assign": ("orchestrator",),
    "desk": ("orchestrator",),
    "finish": ("orchestrator",),
    "release": ("orchestrator",),
    "takeover": (),
    "assumption_state": ("orchestrator",),
    "review": ("reviewer",),
    "shipped": ("releaser",),
    "end_station": ("orchestrator",),
    "repo_map": ("builder", "helper"),
}
WHO = {"grants": "the orchestrator", "global_docs": "the orchestrator",
       "assign": "the orchestrator", "desk": "the orchestrator", "finish": "the orchestrator",
       "release": "the orchestrator",
       "takeover": "the person, in their own terminal (tares factory session <project>)",
       "assumption_state": "the orchestrator", "review": "the reviewer",
       "shipped": "the releaser", "end_station": "the orchestrator, or the person",
       "repo_map": "a builder"}


def may(caller: dict, action: str) -> str | None:
    """None when `caller` ({station, role, parent}) may do `action`, else the reason to tell it."""
    if not caller.get("station"):
        return None
    allowed = ONLY.get(action)
    if allowed is None or caller.get("role") in allowed:
        return None
    return (f"{caller['station']} is a {caller.get('role') or 'station'}: only {WHO[action]} "
            f"may do this. Send it to {WHO[action]}.")
