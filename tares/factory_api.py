"""HTTP routes for the software factory's crew (P-TR-176, M3 onward), registered by the daemon.

One always-on crew (orchestrator, reviewer, releaser) serves every project; builders are started
per project. Tares runs none of it: it keeps the crew's settings and grants on the cell, groups
the Claude Code sessions it receives into stations, and records when a project was handed to the
crew. Who is calling comes from the X-Tares-Station / -Role / -Parent headers the MCP proxy sets
from the station's environment (tares/factory.py `may`).
"""
from __future__ import annotations

import asyncio
import os
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import Body, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder

from . import answers as answers_mod
from . import claude_usage
from . import codex_usage
from . import crew_conversation
from . import crew_onboarding
from . import docs as docs_mod
from . import factory as factory_mod
from . import factory_github
from . import factory_home
from . import factory_waiting as waiting_mod
from . import factory_owners as owners_mod
from . import memory_route
from . import merge_ready as merge_ready_mod
from . import metrics
from . import report as report_mod
from . import team as team_mod
from .integrations import hooks as integrations_hooks
from .integrations import registry as integrations_registry
from .integrations import resolve as integrations_resolve
from .integrations.review import CREW_REVIEWED_GATES
from .integrations.types import PRRef, Status
from .envelope import now_utc
from .factory_store import MapChanged
from .deps import Deps

CREW_SETTING = "crew_settings"
CLAUDE_RETRY_SETTING = "crew_claude_retry"   # stations the watch is retrying onto a new Claude Code
GRANTS_SETTING = "global_grants"   # the id of the cell's "Grants (all projects)" doc
GRANTS_MIGRATED_SETTING = "grants_migrated"   # set once the old grants docs became rows (TR-630)
NO_PERSON = "This key has no person. An admin binds it in Settings, Keys."


def caller(request: Request) -> dict:
    """{station, role, parent} the request says it comes from ("" each when unlabeled)."""
    h = request.headers
    return {"station": (h.get("x-tares-station") or "").strip()[:factory_mod.MAX_NAME],
            "role": (h.get("x-tares-role") or "").strip().lower(),
            "parent": (h.get("x-tares-parent") or "").strip()[:factory_mod.MAX_NAME]}


def crew_name_of(request: Request, said: str | None = None) -> str:
    """The crew the call means (TR-633): `said` (the `crew` parameter), else the X-Tares-Crew
    header, else `crew`, the name a crew has when nobody named it. Raises ValueError for a name
    that cannot be a crew's."""
    name = str(said or request.headers.get("x-tares-crew") or "").strip() or "crew"
    if not factory_mod.CREW_NAME_RE.match(name):
        raise ValueError(f"{name!r} is not a crew name: lowercase letters, digits and hyphens")
    return name


_REFRESHING: dict[str, asyncio.Task] = {}
REFRESH_WAIT = 2.5     # seconds a ticket read waits for its pull request before serving what it has
_READS_AT_ONCE = 4


def lines_from(read: list[str]) -> str:
    """Where a response's lines came from (TR-1507): `table`, `archive` or `both`. One read says so
    itself; several say `both` when they differ, and `events` when nothing was read (or a read said
    `events`, which none does since the event store went)."""
    kinds = set(read)
    if not kinds or "events" in kinds:
        return "events"
    return kinds.pop() if len(kinds) == 1 else "both"


def _shared(store, uid: str) -> "asyncio.Task":
    """The one refresh of a project that is running, or a new one. Only the background sync
    (`sync_prs`) runs it: no read waits on a whole project's pull requests (TR-1474)."""
    task = _REFRESHING.get(uid)
    if task is None or task.done():
        task = asyncio.create_task(_refresh(store, uid))
        _REFRESHING[uid] = task
    return task


_REFRESHING_PR: dict[tuple, asyncio.Task] = {}


async def refresh_pr(store, uid: str, ticket: dict | None) -> bool:
    """Bring one ticket's own pull request up to date before the ticket is read (TR-1474), and
    no other: a ticket read has no business waiting on GitHub for the project's other pull
    requests. One refresh per pull request at a time, shared by every read that asks meanwhile;
    a read waits at most REFRESH_WAIT seconds, then gets the cached state while the refresh
    finishes in the background. A ticket with no pull request linked, or one that is not stale
    and has nothing to follow, makes no GitHub call and does not wait. Answers whether the read
    finished, so the caller reads the ticket again to see what it wrote; never raises."""
    t = ticket or {}
    pr = t.get("pr") or {}
    task = None
    try:
        if not (isinstance(pr, dict) and pr.get("repo") and pr.get("number") and t.get("id")):
            return False
        if not (factory_github.stale(pr) or merged_fields(t, pr)
                or (t.get("stage") == "changes" and pr.get("head"))):
            return False
        key = (t["id"], pr["repo"], str(pr["number"]))
        task = _REFRESHING_PR.get(key)
        if task is None or task.done():
            task = asyncio.create_task(_refresh_ticket_pr(store, uid, t))
            _REFRESHING_PR[key] = task

            def _done(done: "asyncio.Task") -> None:
                if _REFRESHING_PR.get(key) is done:
                    del _REFRESHING_PR[key]
                if not done.cancelled() and done.exception() is not None:
                    e = done.exception()
                    print(f"[tares] pull request refresh for {key[0]} failed: "
                          f"{type(e).__name__}: {e}", flush=True)
            task.add_done_callback(_done)
        await asyncio.wait_for(asyncio.shield(task), REFRESH_WAIT)
        return True
    except asyncio.TimeoutError:
        return False             # the refresh carries on; this read serves what is cached
    except Exception as e:       # a refresh must never fail a read, but say why it did not run
        if task is None or not task.done() or task.exception() is not e:   # `_done` said it already
            print(f"[tares] pull request refresh for {t.get('id')} failed: "
                  f"{type(e).__name__}: {e}", flush=True)
        return False


async def _refresh_ticket_pr(store, uid: str, t: dict) -> None:
    """The refresh of one ticket's pull request: what `_refresh` does for each linked one, for
    this ticket alone. The review gate and the last verdict are looked up only when the rules in
    `_refresh_one` can use them."""
    pr = t["pr"]
    last = None
    if t.get("stage") == "changes":
        last = (await asyncio.to_thread(store.last_reviews, [t["id"]])).get(t["id"])
    gate = None
    if t.get("stage") == "review" and factory_github.stale(pr):
        gate = await asyncio.to_thread(outside_gate, store, uid)
    await _refresh_one(store, uid, t, asyncio.Semaphore(1), gate, last)


def projects_with_open_prs(store) -> list[str]:
    """The factory projects with a ticket still open whose pull request is linked and neither
    merged nor closed: the ones the background sync keeps current (TR-1010). Also a project that
    has a ticket in the work with no pull request linked yet and a repo to look in, since only
    the sync links a `factory/<ref>` branch now (a ticket read no longer does, TR-1474)."""
    out = []
    ids = [p["id"] for p in store.list_projects(kind="software_factory")]
    for uid, tickets in store.tickets_by_project(ids).items():
        has_repo = any((t.get("pr") or {}).get("repo") for t in tickets)
        for t in tickets:
            pr = t.get("pr") or {}
            if (pr.get("repo") and not pr.get("merged") and pr.get("state") != "closed"
                    and factory_mod.state_of(t) not in ("merged", "canceled")):
                out.append(uid)
                break
            if has_repo and not pr and t.get("stage") in ("doing", "review", "changes", "blocked"):
                out.append(uid)
                break
    return out


async def sync_prs(store, stop: asyncio.Event) -> None:
    """The daemon's pull request sync (TR-1010): every TARES_PR_SYNC_SECONDS (120), the refresh
    of every pull request of a project (a ticket read refreshes only its own, TR-1474), for every
    project with an open pull request, one project at a time and only for the pull requests
    whose last read is stale (factory_github.stale). So the ticket lists, the Reviews and home
    pages, which never refresh, show what GitHub shows without anyone opening the project. It
    never stops on an error.
    The first pass waits one interval, so a daemon's start does not read GitHub at once; "off"
    turns it off."""
    every = os.getenv("TARES_PR_SYNC_SECONDS", "120").strip().lower()
    if every in ("off", "0", "none"):
        print("[tares] the merge queue does not run: TARES_PR_SYNC_SECONDS is off", flush=True)
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=float(every))
            return
        except asyncio.TimeoutError:
            pass
        try:
            for uid in await asyncio.to_thread(projects_with_open_prs, store):
                if stop.is_set():
                    break
                try:
                    await _shared(store, uid)
                except Exception as e:     # one project's failure never stops the others
                    print(f"[tares] pull request sync for {uid} failed: {type(e).__name__}: {e}",
                          flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[tares] pull request sync failed: {type(e).__name__}: {e}", flush=True)
        try:   # the merge queue (TR-856) runs after the refresh, on what it just read
            from . import merge_queue
            await merge_queue.tick(store)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[tares] merge queue failed: {type(e).__name__}: {e}", flush=True)
        try:   # memory lines the cell's memory integration did not take yet (TR-865)
            await memory_route.retry_pending(store)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[tares] memory retry failed: {type(e).__name__}: {e}", flush=True)


def merged_fields(t: dict, pr: dict) -> dict:
    """What a merge seen on GitHub changes on the ticket: its stage becomes merged (nothing when
    it already is). A pull request counts once it is merged into its base, which for a project
    with milestone branches is the milestone branch, not the default one. The ticket's status
    follows through `write_status`, whichever system holds the ticket."""
    if not pr.get("merged") or t["stage"] in ("merged", "shipped"):
        return {}
    return {"stage": "merged"}


def cell_wide(system: bool, person: dict | None, is_admin, admin_only: bool = True) -> bool:
    """Does this caller speak for the whole cell? The system credential does, and the person
    `local` (auth off, or a key nobody bound on a one-person cell). Anyone else speaks for
    themselves. `admin_only` also asks the admin scope of `local`, through `is_admin` (a function,
    asked only for `local`): the crew's settings (`writes_default`) need it, because a cell default
    is a privilege. The machine report (`machine_key`) does not: a name and a version, which the
    ingest-only key `tares login` makes on a one-person cell may send, as `local`."""
    return system or ((person or {}).get("id") == "local" and (not admin_only or bool(is_admin())))


def stored_crew_setting(store, key: str) -> dict:
    """The crew settings saved under `key` (the cell's default or a person's own), with the
    defaults under what was never set."""
    try:
        cur = json.loads(store.get_setting(key) or "{}")
    except ValueError:
        cur = {}
    return {k: cur.get(k, d) for k, d in factory_mod.CREW_DEFAULTS.items()}


def crew_settings(store, person: dict | None = None, system: bool = False) -> dict:
    """The cell's crew settings, with `person`'s own over them where they set one (a request's
    caller, or a hand-over's person for the merge queue, which has no request). The cell's
    default alone for no person, the person `local` and the system credential. The team's
    autonomy and challenger are a ceiling, also when the team tightened them after the person
    chose theirs."""
    out = {**factory_mod.CREW_DEFAULTS, **stored_crew_setting(store, CREW_SETTING)}
    if person and person.get("id") and person.get("id") != "local" and not system:
        mine = stored_crew_setting(store, f"{CREW_SETTING}:{person['id']}")
        out.update({k: v for k, v in mine.items() if v is not None and k != "kit"})   # the kit is the crew's
        out.update({k: v for k, v in factory_mod.stricter_than_team(
            stored_crew_setting(store, CREW_SETTING), out).items()
            if k in ("autonomy", "challenger")})
    return factory_mod.crew_effective(out)


async def mark_merged(store, uid: str, t: dict, reason: str, by: str = "github",
                      role: str = "github", pr: dict | None = None) -> dict:
    """The ticket's stage becomes merged, in the history too, and its status follows in the
    system that holds it (as set_stage does). `pr` is the pull request as it is now, kept on
    the ticket (the sync no longer reads a merged ticket's pull request). Nothing when the ticket
    already is merged or shipped. Returns the ticket as it is now."""
    if t.get("stage") in ("merged", "shipped"):
        return t
    def write():
        store.update_ticket(t["id"], stage="merged", **({"pr": pr} if pr else {}))
        store.add_ticket_history(t["id"], uid, "stage", "merged", reason, by, role, now_utc())
        return store.get_ticket(uid, t["id"]) or t
    after = await asyncio.to_thread(write)
    await write_status(store, uid, after, "merged")
    return after


STATUS_WAIT = 10.0      # seconds a route waits for the ticket system before it hands the write on
_WRITING: set = set()   # status writes still running after their route moved on


async def write_status(store, uid: str, t: dict, stage: str) -> None:
    """The status the ticket's `stage` asks for, written through the project's tickets
    integration (TR-848). Never raises, and never holds the caller longer than STATUS_WAIT:

    - a ticket the project's integration does not hold (a Tares ticket in a project whose tickets
      live in Linear, or whose cell-wide choice is Linear) is offered to the Tares integration,
      which holds it; the refusal is logged. A ticket nobody holds is left as it is;
    - a write that fails, or is still waiting on a rate limit when STATUS_WAIT is up, leaves a
      pending_writeback row, so the integration tries again on its next sync and the sync leaves
      the ticket's status and stage alone meanwhile. The write itself carries on in the
      background and clears the row when it lands."""
    if stage not in factory_mod.STATUS_OF_STAGE or not isinstance(t, dict) or not t.get("id"):
        return      # a stage with no status, or no ticket: nothing to write
    status = Status(factory_mod.STATUS_OF_STAGE[stage])
    name = ""
    try:
        name = integrations_resolve.effective("tickets", uid, store=store).integration
        try:
            await _write(store, t, name, status)
        except (ValueError, KeyError) as refused:
            if name == "tares":
                return      # nobody holds this ticket: nothing to write
            print(f"[tares] {name} does not hold {t.get('id')} ({refused}); "
                  "trying the Tares integration", flush=True)
            name = "tares"
            try:
                await _write(store, t, name, status)
            except (ValueError, KeyError):
                return
    except Exception as e:  # noqa: BLE001 - a status write never fails the route that asked
        print(f"[tares] status write for {t.get('id')} failed: {type(e).__name__}: {e}", flush=True)
        _keep(store, t, name, status, f"the write failed: {type(e).__name__}")


async def _write(store, t: dict, name: str, status: Status) -> None:
    """One integration's `set_status`, given STATUS_WAIT to answer; after that it carries on in
    the background and a row keeps the status wanted. Raises what the integration raises."""
    cls = integrations_registry.get("tickets", name)
    if cls is None:
        raise KeyError(f"no tickets integration {name!r}")
    task = asyncio.ensure_future(cls(store).set_status(t, status))
    _WRITING.add(task)
    task.add_done_callback(_WRITING.discard)
    done, _ = await asyncio.wait({task}, timeout=STATUS_WAIT)
    if not done:
        _keep(store, t, name, status, "the ticket system is slow to answer")
        return
    exc = task.exception()
    if exc is not None:
        raise exc


def _keep(store, t: dict, name: str, status: Status, error: str) -> None:
    """Leave the status wanted where the integration's next sync finds it. Only an integration
    that retries (it has `retry_pending`) is given a row: Tares tickets have nothing to retry."""
    cls = integrations_registry.get("tickets", name) if name else None
    if cls is None or not hasattr(cls, "retry_pending"):
        return
    try:
        store.pending_writeback_fail(t["id"], name, status.value, error, now_utc())
    except Exception as e:  # noqa: BLE001
        print(f"[tares] pending write-back for {t.get('id')} not kept: {type(e).__name__}: {e}",
              flush=True)


EDIT_WAIT = 10.0        # seconds an edit route waits for the ticket system before it carries on
_EDITING: set = set()   # edits still running after their route moved on
_LATE: set = set()


async def edit_ticket(store, t: dict, name: str, fields: dict, mirror: dict) -> bool:
    """`fields` written to the ticket where it lives, through the integration `name`'s `update`
    (TR-849); on success `mirror` (the same change in Tares's own columns) is saved, so the
    console shows it before the next sync brings it in. Raises what `update` raises (ValueError for
    a refused field, KeyError for an unknown ticket or milestone, the system's own error), and
    nothing is saved then. Never holds the caller longer than EDIT_WAIT: a write still waiting
    (Linear's rate limit) carries on in the background, saves `mirror` when it lands, and logs a
    failure; the next sync shows the truth either way. Returns False for that case, True when the
    edit landed."""
    cls = integrations_registry.get("tickets", name)
    if cls is None:
        raise KeyError(f"no tickets integration {name!r}")

    async def go() -> None:
        await cls(store).update(t, fields)
        if mirror:
            await asyncio.to_thread(lambda: store.update_ticket(t["id"], **mirror))

    task = asyncio.ensure_future(go())
    _EDITING.add(task)

    def _done(tk) -> None:
        _EDITING.discard(tk)
        if tk in _LATE:
            _LATE.discard(tk)
            if not tk.cancelled() and tk.exception() is not None:
                e = tk.exception()
                print(f"[tares] edit of {t.get('id')} in {name} failed: {type(e).__name__}: {e}",
                      flush=True)

    task.add_done_callback(_done)
    done, _ = await asyncio.wait({task}, timeout=EDIT_WAIT)
    if not done:
        _LATE.add(task)
        return False
    exc = task.exception()
    if exc is not None:
        raise exc
    return True


def _reread_only(cur: dict, fields: dict) -> bool:
    """The PR was read again and nothing but when (or whether the read failed) differs: that is
    not ticket activity, and the Projects list's Updated column must not move for it."""
    if set(fields) != {"pr"} or not cur["pr"]:
        return False
    volatile = {"checked_at": None, "error": None}
    return {**cur["pr"], **volatile} == {**fields["pr"], **volatile}


def outside_gate(store, uid: str, cache: dict | None = None) -> tuple[str, str] | None:
    """(name, title) of the project's review gate when it is an outside tool, None when the crew
    reviewer (or no usable choice: the default is the Tares reviewer) gives the verdict (TR-857).
    `cache` lets a caller that asks for many tickets resolve each project once."""
    if cache is not None and uid in cache:
        return cache[uid]
    found = None
    try:
        name = integrations_resolve.effective("review", uid, store=store).integration
        if name not in CREW_REVIEWED_GATES:
            cls = integrations_registry.get("review", name)
            found = (name, (getattr(cls, "title", "") or name)) if cls is not None else None
    except Exception as e:  # noqa: BLE001 - a gate that cannot be resolved is the crew's
        print(f"[tares] review gate of {uid} not resolved: {type(e).__name__}: {e}", flush=True)
    if cache is not None:
        cache[uid] = found
    return found


def warm_gates(store, uids, cache: dict) -> None:
    """Resolve the review gate of every project in `uids` into `cache` (the one outside_gate
    reads) with one query for the projects that hold their own choice. The rest share the
    factory's answer, which is the same for each (the review slot carries nothing from a
    project), so it is read once; a project with its own choice resolves alone, as before."""
    todo = [u for u in dict.fromkeys(uids) if u not in cache]
    if not todo:
        return
    own = store.projects_with_choice("review", todo)
    shared = [u for u in todo if u not in own]
    if shared:
        found = outside_gate(store, None)
        for u in shared:
            cache[u] = found


async def _outside_verdict(store, uid: str, t: dict, pr: dict, gate: tuple[str, str]) -> None:
    """A ticket in review whose project's gate is an outside tool: ask the gate for its verdict on
    the pull request's current head and keep it as a review record, once per head and verdict
    (TR-857). `approved` stays in review with a pass on record; `changes` records the gate's
    findings and moves the ticket to `changes` (the rule in `_refresh` moves it back to review
    when new commits land); `pending`, `stale` and `none` change nothing."""
    name, title = gate
    head = str(pr.get("head") or "").strip().lower()
    cls = integrations_registry.get("review", name)
    if cls is None or not head or not re.match(r"^[0-9a-f]{7,64}$", head):
        return
    if any(r["by"] == name and r["head"] and factory_mod.same_commit(r["head"], head)
           for r in store.ticket_reviews(t["id"])):
        return                  # the gate has its say on this head: no second call to it
    try:
        v = await cls(store).verdict(PRRef(
            repo=pr.get("repo") or "", number=int(pr.get("number") or 0), head=head,
            base=pr.get("base") or "", url=pr.get("url") or "", ticket=t["id"], project_uid=uid))
    except Exception as e:  # noqa: BLE001 - a gate that cannot be reached leaves the ticket as it is
        print(f"[tares] review gate {name} for {t['id']} failed: {type(e).__name__}: {e}", flush=True)
        return
    state = getattr(v, "state", "")
    vhead = str(getattr(v, "head", "") or "").strip()
    if state not in ("approved", "changes") or (vhead and not factory_mod.same_commit(vhead, head)):
        return
    verdict = "pass" if state == "approved" else "changes"

    def write() -> bool:
        cur = store.get_ticket(uid, t["id"])
        if cur is None or cur["stage"] != "review" or not cur["pr"] or (
                cur["pr"].get("repo"), cur["pr"].get("number")) != (pr.get("repo"), pr.get("number")):
            return False        # moved meanwhile: this read is out of date
        for r in store.ticket_reviews(cur["id"]):
            if (r["by"] == name and r["verdict"] == verdict and r["head"]
                    and factory_mod.same_commit(r["head"], head)):
                return False    # recorded already for this head
        summary = " ".join(str(getattr(v, "summary", "") or "").split())[:500]
        if verdict == "pass":
            resolved = {f["label"]: "fixed" for r in store.ticket_reviews(cur["id"])
                        if r["by"] == name for f in r["findings"] if f["state"] == "open"}
            store.add_review(uid, cur["id"], cur["pr"].get("url"), head, "pass",
                             f"{title} approved this commit", "", name, [], resolved,
                             factory_mod.finding_kind, now_utc())
            return False
        findings = [{"severity": "P2", "file": "", "line": 0, "blocking": True, "text": text}
                    for text in (" ".join(str(x).split())[:2000] for x in (v.findings or [])) if text]
        store.add_review(uid, cur["id"], cur["pr"].get("url"), head, "changes",
                         f"{title} asked for changes on this commit", "", name, findings, {},
                         factory_mod.finding_kind, now_utc())
        reason = summary or f"{title} asked for changes"
        store.update_ticket(cur["id"], stage="changes", stage_reason=reason)
        store.add_ticket_history(cur["id"], uid, "stage", "changes", reason, name, "github",
                                 now_utc())
        return True

    if await asyncio.to_thread(write):
        await write_status(store, uid, store.get_ticket(uid, t["id"]) or t, "changes")


async def _refresh(store, uid: str) -> None:
    """A linked PR read again when its last read is stale; a ticket in the work with no PR linked
    by a `factory/<ref>` branch in the project's repos, only when no other project has a ticket
    in the work with that ref in that repo; a merge seen on GitHub marks the ticket merged even
    when the builder did not."""
    tickets = store.list_tickets(uid)
    repos = {t["pr"]["repo"] for t in tickets if t["pr"] and t["pr"].get("repo")}
    if repos:
        seen = store.factory_branch_prs(repos)
        for t in tickets:
            if t["pr"] or t["stage"] not in ("doing", "review", "changes", "blocked"):
                continue
            lab = factory_mod.label(t).upper()
            hit = next(((r, n) for (r, ref), (_, n) in seen.items() if ref == lab), None)
            if hit and not store.factory_ref_elsewhere(uid, hit[0], factory_mod.label(t)):
                t["pr"] = {"repo": hit[0], "number": hit[1], "checked_at": None,
                           "linked_by": "branch"}
                store.update_ticket(t["id"], pr=t["pr"])
    sem = asyncio.Semaphore(_READS_AT_ONCE)
    linked = [t for t in tickets if t["pr"] and t["pr"].get("repo")]
    lasts = store.last_reviews([t["id"] for t in linked])
    gate = outside_gate(store, uid)    # an outside review tool: its verdict is read (TR-857)
    await asyncio.gather(*(_refresh_one(store, uid, t, sem, gate, lasts.get(t["id"]))
                           for t in linked), return_exceptions=True)


async def _refresh_one(store, uid: str, t: dict, sem: "asyncio.Semaphore",
                       gate: tuple[str, str] | None, last: dict | None) -> None:
    """One linked pull request read again when its last read is stale, and what that changes on
    its ticket: shared by the project's refresh (`_refresh`) and a single ticket's (`refresh_pr`).
    `last` is the ticket's last review, `gate` the project's outside review tool, if any."""
    pr = t["pr"]
    watch = bool(gate) and t["stage"] == "review"
    reread = False
    if factory_github.stale(pr):
        async with sem:
            pr = await factory_github.read_pr(store, pr["repo"], int(pr["number"]), pr)
            if t["stage"] == "review" and pr.get("state") == "open" and not pr.get("merged"):
                # how many commits the base has that the branch lacks, for the merge a person
                # is asked for (TR-1449): one compare per read, only for a PR in review
                cmp = await factory_github.compare(store, pr["repo"], pr.get("base") or "",
                                                   pr.get("head") or "")
                pr["behind_by"] = cmp["behind_by"] if cmp["ok"] else None
        reread = True
    elif not _follows(t, pr, last):
        return               # not read again and nothing to write: skip the re-read below
    cur = store.get_ticket(uid, t["id"])   # re-read: a set_stage may have run meanwhile
    if cur is None or not cur["pr"] or (cur["pr"].get("repo"), cur["pr"].get("number")) != (
            pr.get("repo"), pr.get("number")):
        return
    fields: dict = {"pr": pr} if pr is not t["pr"] else {}
    fields.update(merged_fields(cur, pr))
    # new commits after the reviewer asked for changes: it is back in review (TR-444)
    standing = factory_mod.standing_round(store.ticket_reviews(cur["id"]))
    if (not fields.get("stage") and cur["stage"] == "changes" and standing and pr.get("head")
            and not verdict_now({"pr": pr}, standing)["current"]):
        fields["stage"] = "review"
        store.add_ticket_history(cur["id"], uid, "stage", "review",
                                 "new commits pushed after the verdict", "github", "github",
                                 now_utc())
    if fields:
        store.update_ticket(cur["id"], touch=not _reread_only(cur, fields), **fields)
    if fields.get("stage") == "merged":
        store.add_ticket_history(cur["id"], uid, "stage", "merged",
                                 "GitHub shows the pull request merged", "github", "github",
                                 now_utc())
        await write_status(store, uid, cur, "merged")
    elif (watch and reread and (fields.get("stage") or cur["stage"]) == "review"
          and pr.get("state") == "open"):
        async with sem:      # the gate is asked when the PR was read again, as many at once
            await _outside_verdict(store, uid, cur, pr, gate)


def _follows(t: dict, pr: dict, last: dict | None) -> bool:
    """Whether a PR that was not read again still changes the ticket: a merge it does not show
    yet, or new commits after the verdict that asked for changes (the same tests `one` makes)."""
    if merged_fields(t, pr):
        return True
    return bool(t["stage"] == "changes" and last and pr.get("head")
                and not verdict_now({"pr": pr}, last)["current"])


def verdict_now(t: dict, last: dict | None) -> dict | None:
    """The reviewer's last verdict on the ticket and whether it still counts (TR-444): it was
    given on a commit; once the pull request's head moves on, it is for an older commit."""
    if not last:
        return None
    head = ((t.get("pr") or {}).get("head") or "").lower()
    mine = (last["head"] or "").lower()
    current = not head or not mine or factory_mod.same_commit(head, mine)
    return {"verdict": last["verdict"], "head": last["head"], "round": last["round"],
            "at": last["at"], "current": bool(current),
            "note": None if current else
            f"verdict is for an older commit ({last['head'][:7]}); the PR is now at {head[:7]}"}


def commands_for(store, uid: str, t: dict, source: str) -> tuple[list[dict] | None, object]:
    """(the commands the ticket's sessions ran, the time before which a run does not count) that
    a ticket's recorded checks are compared with. The sessions are the holder's station sessions,
    or the project's when the ticket has no holder. `None` for the commands means there is no
    session to compare with (not the same as a session that ran nothing: that gives `[]`).
    `since` is 5 minutes before the ticket first went into the work, or None when it never did."""
    holder = t.get("holder")
    sessions = (store.station_session_ids(holder) if holder
                else [s["session"] for s in store.project_sessions(uid)])
    with metrics.step("recorded_commands"):
        commands = store.recorded_commands(source, sessions, ends=True) if sessions else None
    # only what ran since the ticket went into the work counts for its checks
    since = next((x["at"] for x in store.ticket_history(t["id"])
                  if x["field"] == "stage" and x["value"] == "doing"), None)
    if since is not None:
        from datetime import timedelta
        since = since - timedelta(minutes=5)
    return commands, since


def review_state(h: Deps, uid: str, t: dict, source: str) -> dict:
    """What a ticket's full view adds for review (TR-440..444): its checks matched against the
    recording, the challenger's layer, the review rounds, and whether the last verdict counts.

    A check's `match` is the same for every reader, as the merge gate reads it. Its `recorded`
    text is given only when the sessions this reader may open say the same on their own
    (TR-1711). The challenger's findings are those such sessions carry (readable_layer)."""
    store = h.store
    raw = store.ticket_checks(t["id"], 50)
    checks = []
    if raw:
        commands, since = commands_for(store, uid, t, source)
        for c in raw:
            state, recorded = (factory_mod.match_check(c, commands, since)
                               if commands is not None else (None, None))
            checks.append({**c, "match": state, "recorded": recorded})
        if any(c["recorded"] for c in checks):
            closed = unread_sessions(h, list({r["session"] for r in commands}))
            if closed:
                readable = [r for r in commands if r["session"] not in closed]
                for c in checks:
                    if c["recorded"] and (factory_mod.match_check(c, readable, since)
                                          != (c["match"], c["recorded"])):
                        c["recorded"] = None
    reviews = store.ticket_reviews(t["id"])
    with metrics.step("challenger"):
        events = store.challenge_events(source, uid, factory_mod.label(t))
        layer = readable_layer(h, factory_mod.challenger_layer(events), events)
    return {"checks": checks, "reviews": reviews, "challenger": layer,
            "verdict": verdict_now(t, factory_mod.standing_round(reviews))}


def pr_note(t: dict) -> str | None:
    """When the ticket's stage and its pull request disagree, the sentence that says so."""
    pr = t.get("pr") or {}
    if not pr.get("repo"):
        return None
    if t.get("stage") in ("merged", "shipped") and pr.get("state") == "open":
        return "the ticket says merged, but GitHub shows the pull request still open"
    if t.get("stage") in ("merged", "shipped") and pr.get("state") == "closed" and not pr.get("merged"):
        return "the ticket says merged, but GitHub shows the pull request closed without merging"
    if t.get("stage") in ("todo", "doing", "review", "changes") and pr.get("state") == "closed" \
            and not pr.get("merged"):
        return "GitHub shows the pull request closed without merging"
    return None


def project_progress(store, uid: str, tickets: list[dict] | None = None,
                     milestones: list[dict] | None = None) -> dict:
    """Tickets done of all, by stage, and the first milestone with work left: what the crew's
    project list shows (canceled tickets count as done here), and `progress`, the same tickets
    as the console's ring counts them (canceled kept out, tares/factory.py `progress`). `tickets`
    and `milestones` are the project's when the caller already read them."""
    tickets = store.list_tickets(uid) if tickets is None else tickets
    milestones = store.list_milestones(uid) if milestones is None else milestones
    done = [t for t in tickets if (t["stage"] in ("merged", "shipped")
                                   or t["status"] in factory_mod.DONE_STATUSES)]
    stages: dict[str, int] = {}
    for t in tickets:
        k = t["stage"] or ("done" if t in done else "todo")
        stages[k] = stages.get(k, 0) + 1
    current = None
    for m in milestones:
        mine = [t for t in tickets if t["milestone"] == m["id"]]
        if any(t not in done for t in mine):
            current = {"name": m["name"], "done": sum(t in done for t in mine),
                       "total": len(mine)}
            break
    return {"tickets_done": len(done), "tickets_total": len(tickets), "stages": stages,
            "milestone": current, "progress": factory_mod.progress(tickets)}


def own_crew_ids(h: Deps, crews) -> set[str]:
    """The ids of those of `crews` (rows of store.list_crews) that are the caller's own (TR-1704):
    a station's owner reads every session of it. They are the crews of the caller's person; a
    credential with no person owns none, not even those filed under `local`. With auth off there
    is no credential and the one person using the cell is `local`, so those crews are theirs."""
    if h.auth_off():
        me = "local"
    else:
        request = h.caller()
        person = h.current_person(request) if request is not None else None
        me = person["id"] if person else None
    return {c["id"] for c in crews if me and c["person_id"] == me}


def unread_sessions(h: Deps, sessions: list[str]) -> set[str]:
    """Those of `sessions` the caller must not read (TR-1710): they worked in a project the
    caller cannot see and belong to no crew of theirs (store.unread_among)."""
    hidden = h.hidden_ids()
    if not hidden:
        return set()
    return h.store.unread_among(sessions, hidden, own_crew_ids(h, h.store.list_crews()))


def readable_layer(h: Deps, layer: dict, events: list[dict]) -> dict:
    """The challenger's layer `layer`, made from all of `events`, with the findings the caller may
    read (TR-1724): an open or fixed finding whose title a review in a session they can open
    carries, and the waivers such sessions recorded. Rounds, verdict, clean and sha stay as all
    the events give them."""
    if not (layer["open"] or layer["fixed"] or layer["waived"]) or not h.hidden_ids():
        return layer
    closed = unread_sessions(h, list({e["session"] for e in events if e["session"]}))
    titles: dict = {}
    waived = []
    for e in events:
        if e["session"] in closed:
            continue
        for f in e["challenge"].get("findings") or []:
            if e["type"] == "challenge_commit":
                titles.setdefault(f.get("title"), []).append(f)
            elif e["type"] == "challenge_waived":
                waived.append(f)

    def mine(findings: list[dict]) -> list[dict]:
        # as a session the caller can open wrote it: the latest such version when this one is not
        return [f if f in titles[f.get("title")] else titles[f.get("title")][-1]
                for f in findings if f.get("title") in titles]

    return {**layer, "open": mine(layer["open"]), "fixed": mine(layer["fixed"]), "waived": waived}


def rule_text(repo: str, kind: str, count: int | None = None) -> str:
    """The sentence a rule offer is worded as. `count` is the number of tickets the finding came
    back in; None leaves that part out."""
    start = f"In {repo}, check for {kind} before asking for review"
    if count is None:
        return f"{start}."
    return f"{start}: it came back in {count} ticket{'' if count == 1 else 's'}."


def tickets_of(tickets: list, projects: list[dict], shut) -> list:
    """Those of a rule offer's `tickets` ("<project name> <label>") whose name is that of a current
    project in `projects`, and of none for which `shut(project)` is true."""
    names = {p["name"] for p in projects} - {p["name"] for p in projects if shut(p)}
    return [t for t in tickets if str(t).rsplit(" ", 1)[0] in names]


def root_offers(h: Deps, offers: list[dict], hidden, projects: list[dict]) -> list[dict]:
    """`offers` as the root caller reads them (TR-1713): every one, its text as stored. A ticket
    is left out when its name is that of a project in `hidden`, or of a deleted private project
    while no project has the name (TR-1708)."""
    names = {p["name"] for p in projects}
    closed = {p["name"] for p in projects if p["id"] in hidden}
    deleted = h.store.deleted_private_projects()

    def kept(ticket) -> bool:
        name = str(ticket).rsplit(" ", 1)[0]
        if name in names:
            return name not in closed
        return h.store.deleted_private_ref(name, deleted) is None

    return [{**o, "tickets": [t for t in o.get("tickets") or [] if kept(t)]} for o in offers]


def readable_offers(h: Deps, offers: list[dict], hidden, projects=None) -> list[dict]:
    """The rule offers `offers` for a caller who cannot see the projects in `hidden` (TR-1713),
    from those of the other projects. The root caller (the cell's root token, or auth off) reads
    them as root_offers says. Anyone else gets each with the tickets whose recorded name is a
    current project they can open, and its generated sentence counting those; an offer that has
    tickets and keeps none is left out (TR-1724). `projects` is `store.list_projects()`, for a
    caller who has it already."""
    if not hidden:
        return offers
    offers = [o for o in offers if o.get("project") not in hidden]
    if not any(o.get("tickets") for o in offers):
        return offers
    if projects is None:
        projects = h.store.list_projects()
    request = h.caller()   # None with auth off
    if request is None or h.is_system(request):
        return root_offers(h, offers, hidden, projects)
    out = []
    for o in offers:
        had = o.get("tickets") or []
        kept = tickets_of(had, projects, lambda p: p["id"] in hidden)
        if had and not kept:
            continue
        text = o.get("text")
        if len(kept) < len(had) and text == rule_text(o["repo"], o["kind"], len(had)):
            text = rule_text(o["repo"], o["kind"], len(kept))
        out.append({**o, "tickets": kept, "text": text})
    return out


def blank_session(row: dict) -> None:
    """Take a station row's current session off it, for a caller who must not read it (TR-1704):
    which session it is, when it started, its lines, context window, Claude Code version and the
    reason of its state, which the session wrote. The station's state and when it was last heard
    stay, so the row still says whether the station works."""
    row.update(session=None, started_at=None, lines=None, claude_version=None, state_reason=None)
    if row["context"]:
        row["context"] = dict.fromkeys(row["context"])
    if row.get("runtime") != "codex":   # read from the session's status line; a Codex station's is its launch's
        row["model"] = None


def register(app, h: Deps) -> SimpleNamespace:
    """Add the crew's routes. `h` is the daemon's Deps (tares/deps.py), which documents the helpers
    it carries: err, project_or_404, by, current_person, is_admin, area_owner and the rest. Returns
    the crew readers the home page shares."""
    store = h.store

    def deny(request: Request, action: str) -> None:
        why = factory_mod.may(caller(request), action)
        if why:
            h.err(PermissionError(why), 403)

    # ── crew settings (TR-419, TR-624): the cell's default, and each person's own over it ──
    def stored(key: str) -> dict:
        return stored_crew_setting(store, key)

    def personal_key(person: dict) -> str:
        return f"{CREW_SETTING}:{person['id']}"

    def writes_default(request: Request) -> bool:
        """Does this caller's write change the cell-wide default? The system credential does, and
        the person `local`, and auth off (a single-user cell stays as it was). Anyone else
        changes their own."""
        return cell_wide(h.is_system(request), h.current_person(request), lambda: h.is_admin(request))

    def settings(request: Request | None = None) -> dict:
        """The cell's default, with the caller's own settings over it where they set one."""
        person = h.current_person(request) if request is not None else None
        # is_system is asked only for a person who could have settings of their own, as before
        mine = bool(person and person.get("id") and person.get("id") != "local")
        return crew_settings(store, person, system=mine and h.is_system(request))

    @app.get("/api/crew/settings")
    def get_crew_settings(request: Request):
        """The crew's settings for every project: autonomy (L3-review, L4-ship), whether it runs
        a releaser, release profile, prod pattern, challenger on builders, most builders at
        once, whether the watch moves stations onto a new Claude Code (claude_update, on unless
        false). Unset ones are null. `kit.frozen` says when `factory crew freeze` froze the
        crew's plugin kit, and by whom, or is null while the crew follows the checkout. `idle_wake`
        (true or false; null counts as true) makes the watch wake an idle station with a message
        when it has work, and hold the station's liveness tick, instead of each station ticking
        on its own. `pace` (true or false; null counts as false) makes the crew slow down before
        the plan's limits: see GET /api/crew/pace. The caller's own come over the cell's
        default."""
        return {**settings(request), "autonomy_levels": factory_mod.AUTONOMY}

    @app.put("/api/crew/settings")
    def put_crew_settings(request: Request, body: dict = Body(...)):
        """Change the fields given; the rest keep their value. The system credential and the
        person `local` change the cell's default; any other person changes their own."""
        deny(request, "grants")
        if writes_default(request):
            key = CREW_SETTING
        else:
            person = h.current_person(request)
            if not person:
                h.err(PermissionError("This key has no person"), 403)
            if person["id"] == "local":
                h.err(PermissionError("The cell's settings need the admin scope"), 403)
            key = personal_key(person)
            if body.get("kit") is not None:
                # the kit (the freeze) is the crew's: settings() leaves a person's copy out, so
                # storing it would answer 200 and change nothing. A null is no change (as
                # crew_settings_ok reads it), so it passes.
                h.err(PermissionError("The kit (the freeze) belongs to the whole cell: only the "
                                      "cell's own credential, or its local admin, changes it"), 403)
        try:
            if key != CREW_SETTING:
                factory_mod.personal_ceiling_ok(stored(CREW_SETTING), body)
            out = factory_mod.crew_settings_ok(body, stored(key))
        except docs_mod.DocError as e:
            h.err(e)
        store.set_setting(key, json.dumps(out))
        factory_home.clear_cache()   # the home card shows the change on the next read
        return {**settings(request), "autonomy_levels": factory_mod.AUTONOMY}

    # ── the first run (TR-1312): the machine the crew runs on ──
    def machine_key(request: Request | None) -> str | None:
        """Where the caller's machine report is kept, as `writes_default` and `personal_key` decide
        for the crew's settings (one rule, `cell_wide`): the cell's own for the system credential
        and the person `local`, a person's own for anyone else, None for a key with no person."""
        if request is None:
            return crew_onboarding.machine_key()
        person = h.current_person(request)
        if cell_wide(h.is_system(request), person, lambda: h.is_admin(request), admin_only=False):
            return crew_onboarding.machine_key()
        return crew_onboarding.machine_key(person["id"]) if person else None

    @app.post("/api/crew/machine")
    def post_crew_machine(request: Request, body: dict = Body(...)):
        """The command line says which machine signed in and which version it is (`tares login`
        and the crew's start do it), so the first-run panel can tick its install and sign-in
        steps. A name and a version, nothing secret; the last report wins. It is the caller's
        own, kept the way their crew settings are: a key with no person is refused."""
        key = machine_key(request)
        if key is None:
            h.err(PermissionError("This key has no person"), 403)
        out = crew_onboarding.save_machine(store, body.get("machine"), body.get("version"), now_utc(), key)
        factory_home.clear_cache()
        return {"machine": out["machine"], "version": out["version"]}

    # ── grants (TR-419): the person's words, dated; shared, and per project ───
    def shared_grants_id(create: bool = False) -> str | None:
        """The cell's "Grants (all projects)" doc, made with the first grant."""
        gid = store.get_setting(GRANTS_SETTING)
        if gid and store.get_doc(None, gid) is not None:
            return gid
        if not create:
            return None
        title, body = docs_mod.GRANTS_DOC
        gid = store.create_doc(None, "grants", title, body, by="tares")
        store.set_setting(GRANTS_SETTING, gid)
        for uid in store.handed_over_projects():   # the crew's projects show it from now on
            store.use_doc(uid, gid)
        return gid

    def include_shared_grants(uid: str) -> None:
        gid = shared_grants_id()
        if gid:
            store.use_doc(uid, gid)

    def project_grants_doc(uid: str) -> dict | None:
        gid = shared_grants_id()
        return next((d for d in store.list_docs(uid, "grants") if d["id"] != gid), None)

    def grants_out(uid: str | None) -> dict:
        gid = shared_grants_id()
        shared = store.get_doc(None, gid) if gid else None
        own = None
        if uid:
            d = project_grants_doc(uid)
            own = store.get_doc(uid, d["id"]) if d else None
        return {"all": shared["body"] if shared else "",
                "project": own["body"] if own else "",
                "all_id": shared["id"] if shared else None,
                "project_id": own["id"] if own else None}

    # ── grants by kind (TR-630) ───────────────────────────────────────────────
    # A grant is a row: team (what every project starts from), project (what the work may do) or
    # personal (what may run on the holder's machine). For one person all three take effect at once.
    # With an enterprise licence a project grant that names a repo whose area owner is not the giver
    # waits until that owner consents, and one from someone who is neither the project's lead nor an
    # admin also waits for the lead (for an admin when the project has no lead): the waiting is
    # `team_mod.get().grant_waiting_for`, and agreeing is in ee (tares_ee/grants.py). A grant in
    # effect is also written to the grants docs (as before), so what reads those docs keeps working.
    # A grant that waits is never in effect and never in the docs: without ee it stays waiting and
    # can only be revoked.
    def grant_out(g: dict, refs: dict) -> dict:
        def ref(pid):
            return refs.get(pid) or {"id": pid, "name": None}
        return {"id": g["id"], "kind": g["kind"], "project": g["project"], "words": g["words"],
                "state": g["state"], "given_at": g["given_at"],
                "given_by": ref(g["given_by"]) if g["given_by"] else None,
                "holder": ref(g["holder_person_id"]) if g["holder_person_id"] else None,
                "areas": g["areas"],
                "consents": [{**ref(c["person_id"]), "at": c["at"],
                             **{k: c[k] for k in ("via", "declined", "reason") if c.get(k)}}
                            for c in g["consents"]],
                "waiting_for": team_mod.get().grant_waiting_for(team_reader(), g),
                "in_effect": g["state"] == "active"}

    def grants_view(rows: list[dict]) -> list[dict]:
        ids = {g["given_by"] for g in rows} | {g["holder_person_id"] for g in rows}
        ids |= {c["person_id"] for g in rows for c in g["consents"]}
        refs = store._person_refs(ids)
        order = {k: i for i, k in enumerate(factory_mod.GRANT_KINDS)}
        rows = sorted(rows, key=lambda g: (g["state"] != "active", order.get(g["kind"], 9),
                                           int(g["id"][1:])))
        return [grant_out(g, refs) for g in rows]

    _LEGACY_LINE = re.compile(r'^- "(.*)" \((\d{4}-\d{2}-\d{2})\)(?: \(added by .+\))?$')

    def legacy_lines(body: str):
        """(words, day or None) of each line of an old grants doc."""
        for raw in (body or "").splitlines():
            if not raw.startswith("- "):
                continue
            m = _LEGACY_LINE.match(raw.strip())
            if m:
                yield m.group(1), m.group(2)
            elif raw[2:].strip():
                yield raw[2:].strip(), None

    def migrate_grants() -> None:
        """Once: each line of the old grants docs becomes an active team or project grant. The
        docs stay as they are."""
        if store.get_setting(GRANTS_MIGRATED_SETTING):
            return
        have = {(g["kind"], g["project"], g["words"]) for g in store.all_grant_rows()}

        def take(kind: str, uid: str | None, body: str) -> None:
            for words, day in legacy_lines(body):
                if (kind, uid, words) in have:
                    continue
                have.add((kind, uid, words))
                at = (datetime.fromisoformat(day).replace(tzinfo=timezone.utc) if day
                      else now_utc())
                store.add_grant_row(kind, words, project=uid, given_at=at)

        gid = shared_grants_id()
        if gid:
            take("team", None, store.get_doc(None, gid)["body"])
        for p in store.list_projects():
            d = project_grants_doc(p["id"])
            if d:
                take("project", p["id"], store.get_doc(p["id"], d["id"])["body"])
        store.set_setting(GRANTS_MIGRATED_SETTING, "1")

    grants_doc_lock = threading.RLock()   # one writer at a time on a grants doc's text

    def mirror_in(g: dict, by: str) -> None:
        """A team or project grant in effect, into the grants doc that readers of the docs use."""
        if g["kind"] == "personal":
            return
        day = g["given_at"].date().isoformat() if g["given_at"] else now_utc().date().isoformat()
        append_grant(g["project"] if g["kind"] == "project" else None,
                     factory_mod.grant_line(g["words"], day), by)

    def mirror_out(g: dict, by: str) -> None:
        """A grant revoked: its lines go from the grants doc (the doc is the old view of it)."""
        if g["kind"] == "personal":
            return
        uid = g["project"] if g["kind"] == "project" else None
        if uid is None:
            doc_id = shared_grants_id()
        else:
            d = project_grants_doc(uid)
            doc_id = d["id"] if d else None
        if not doc_id:
            return
        with grants_doc_lock:   # read and write as one step: a line added meanwhile is kept
            doc = store.get_doc(None, doc_id) if uid is None else store.get_doc(uid, doc_id)
            keep = [ln for ln in doc["body"].splitlines()
                    if not any(w == g["words"] for w, _ in legacy_lines(ln))]
            text = "\n".join(keep) + "\n"
            if text != doc["body"]:
                store.update_doc(doc_id, body=text, by=by)

    def visible_grant(g: dict, request: Request) -> bool:
        """May this caller read this grant? Personal: its holder. Project: whoever can see the
        project. Team: anyone."""
        person = h.current_person(request)
        if g["kind"] == "personal":
            return bool(person and g["holder_person_id"] == person["id"])
        if g["kind"] == "project":
            try:
                h.project_or_404(g["project"])
            except Exception:
                return False
        return True

    def grant_or_404(gid: str, request: Request) -> dict:
        """The grant, or 404."""
        g = store.get_grant_row(gid)
        if g is None or not visible_grant(g, request):
            h.err(KeyError(f"unknown grant {gid!r}"), 404)
        return g

    @app.get("/api/grants")
    def get_grants(request: Request, project: str = ""):
        """The grants for every project (`all`) and, with `project`, that project's own, as
        markdown (what the docs hold), and `grants`: the team grants, that project's and the
        caller's own personal ones, each {id, kind, words, state, given_by, consents,
        waiting_for, in_effect}, those in effect first. Revoked ones are left out.
        `waiting_for_me` holds the grants of any project that wait for the caller's consent (the
        caller may not see that project), and `known_words` the words of every team and project
        grant in any state, revoked ones too."""
        uid = None
        if project.strip():
            uid = h.project_or_404(h.resolve_project(project.strip()))["id"]
        person = h.current_person(request)
        mine = person["id"] if person else None
        team = team_mod.get()
        every = [team.settle_grant(team_reader(), g, request) for g in store.all_grant_rows()]
        rows = [g for g in every if g["state"] != "revoked" and (
            g["kind"] == "team"
            or (g["kind"] == "project" and uid is not None and g["project"] == uid)
            or (g["kind"] == "personal" and mine is not None and g["holder_person_id"] == mine
                and g["project"] in (None, uid)))]
        mine_to_consent = team.grants_waiting_for_me(team_reader(), every, request)
        return {**grants_out(uid), "grants": grants_view(rows),
                "waiting_for_me": grants_view(mine_to_consent),
                "known_words": sorted({g["words"] for g in every if g["kind"] == "team" or (
                    g["kind"] == "project" and g["project"] == uid)})}

    def put_grant(kind: str, words: str, uid: str | None, pid: str | None, areas: list[str],
                  needs_lead: bool, by: str) -> tuple[dict, bool]:
        """The grant row for these words, given by `pid`: the one already there when the same
        words are in effect or waiting, else a new one that waits for the owners of `areas` (and
        the lead when `needs_lead`) or takes effect now. Returns (row, added). The caller has
        checked that `pid` may give it."""
        holder = pid if kind == "personal" else None
        twins = [g for g in store.all_grant_rows()
                 if g["state"] != "revoked" and (g["kind"], g["project"], g["words"],
                                                 g["holder_person_id"]) == (kind, uid, words, holder)]
        active = next((g for g in twins if g["state"] == "active"), None)
        waiting = next((g for g in twins if g["state"] == "waiting"), None)
        waits = bool(team_mod.get().grant_waiting_for(team_reader(), {
            "state": "waiting", "kind": kind, "project": uid, "areas": areas, "given_by": pid,
            "needs_lead": needs_lead, "consents": []}))
        if active is not None or (waiting is not None and waits):
            return active or waiting, False
        if waiting is not None and not team_mod.enabled():
            # without ee a waiting grant is a permission nobody can agree to: it stays waiting
            # (revoke is the one way out), and the same words added again do not make it take effect
            return waiting, False
        if waiting is not None:
            # the lead's (or an admin's) own add of words that sit waiting for someone else's
            # consent: it takes effect, with the areas and owner consents of this add
            # only while it still waits: a decline that came since the read must stay a decline
            if not store.activate_waiting_grant(waiting["id"], areas=areas, needs_lead=False):
                row = store.get_grant_row(waiting["id"])
                if row is not None and row["state"] == "active":
                    return row, False   # another add took it into effect first
                h.err(ValueError(f"{waiting['id']} is no longer waiting"), 409)
            row = store.get_grant_row(waiting["id"])
            mirror_in(row, by)
            return row, True
        row = store.add_grant_row(kind, words, project=uid, holder=holder, given_by=pid,
                                  areas=areas, state="waiting" if waits else "active",
                                  needs_lead=needs_lead)
        if row["state"] == "active":
            mirror_in(row, by)
        return row, True

    @app.post("/api/grants", status_code=201)
    def add_grant(request: Request, body: dict = Body(...)):
        """{kind: team|project|personal, words, project?, areas?}: one grant, quoting the person,
        dated today (`scope: all|project` is the older name for kind). Team needs the admin
        scope; project, the project's lead, an area owner for repos they own, or the admin
        scope; personal, a key that belongs to a person, for themselves. All three take effect at
        once. With an enterprise licence a project grant that names a repo owned by someone other
        than the giver waits for that owner, and one from a person who is neither the project's
        lead nor an admin waits for the lead too (for an admin when the project has no lead);
        without one, `areas` answers 402. Only the
        orchestrator among the crew may add one; a person's own session may."""
        deny(request, "grants")
        project_ref = str(body.get("project") or "").strip()
        kind = str(body.get("kind") or body.get("scope") or "").strip().lower()
        kind = "team" if kind == "all" else kind or ("project" if project_ref else "team")
        if kind not in factory_mod.GRANT_KINDS:
            h.err(ValueError("kind is team, project or personal"))
        try:
            words = factory_mod.grant_words(body.get("words"))
        except docs_mod.DocError as e:
            h.err(e)
        raw_areas = body.get("areas") or []
        if not isinstance(raw_areas, list):
            h.err(ValueError("areas is a list of repos, like [\"acme/web\"]"))
        if raw_areas and kind != "project":
            h.err(ValueError("areas belong to a project grant"))
        if raw_areas and not team_mod.enabled():
            h.licence_required("Grants that wait for an area owner need an enterprise licence.")
        try:
            areas = list(dict.fromkeys(factory_mod.norm_repo(a) for a in raw_areas))
        except ValueError as e:
            h.err(e)
        person = h.current_person(request)
        pid = person["id"] if person else None
        p = None
        if kind == "project" and not project_ref:
            h.err(ValueError("name the project the grant is for"))
        if kind != "team" and project_ref:
            p = h.project_or_404(h.resolve_project(project_ref))
        needs_lead = False
        if kind == "team":
            if not h.is_admin(request):
                h.err(PermissionError("a team grant needs the admin scope"), 403)
        elif kind == "project":
            owns_all = bool(areas) and pid is not None and all(
                (h.area_owner(a) or {}).get("id") == pid for a in areas)
            leads = bool(pid and p.get("lead_person_id") == pid)
            if not (h.is_admin(request) or leads or owns_all):
                h.err(PermissionError("a project grant comes from the project's lead, or from "
                                      "the owner of the repos it names"), 403)
            needs_lead = not (h.is_admin(request) or leads)
        elif pid is None:
            h.err(PermissionError("a personal grant belongs to a person, and this key has none"),
                  403)
        uid = p["id"] if p else None
        line = factory_mod.grant_line(words, now_utc().date().isoformat())
        row, added = put_grant(kind, words, uid, pid, areas, needs_lead, h.by(request, body))
        return {**grants_out(uid if kind != "personal" else None), "added": added, "line": line,
                "grant": grants_view([row])[0]}

    @app.post("/api/grants/{gid}/revoke")
    def revoke_grant(gid: str, request: Request):
        """The giver, the project's lead, the holder of a personal grant, or the admin scope for
        a team or project one, ends it. A revoked grant is never in effect again."""
        deny(request, "grants")
        g = grant_or_404(gid, request)
        person = h.current_person(request)
        pid = person["id"] if person else None
        lead = False
        if g["kind"] == "project":
            lead = bool(pid and (store.get_project(g["project"]) or {}).get("lead_person_id") == pid)
        allowed = bool(pid and pid in (g["given_by"], g["holder_person_id"])) or lead or (
            h.is_admin(request))
        if not allowed:
            h.err(PermissionError("a grant is ended by who gave it, the project's lead, or its "
                                  "holder"), 403)
        if g["state"] == "revoked":
            h.err(ValueError(f"{g['id']} is already revoked"), 409)
        was = g["state"]
        store.update_grant_row(g["id"], state="revoked")
        g = store.get_grant_row(g["id"])
        if was == "active":
            mirror_out(g, h.by(request, None))
        return {"grant": grants_view([g])[0]}

    def append_grant(uid: str | None, line: str, by: str) -> bool:
        """One grant line into the shared grants (uid None) or the project's own."""
        if uid is None:
            doc_id = shared_grants_id(create=True)
        else:
            d = project_grants_doc(uid)
            if d is None:
                p = store.get_project(uid)
                doc_id = store.create_doc(uid, "grants", f"Grants: {p['name']}",
                                          f"# Grants for {p['name']}\n\nWhat the person allows "
                                          "the crew in this project, on top of the grants for "
                                          "every project. Their words, dated.\n\n", by=by)
            else:
                doc_id = d["id"]
            include_shared_grants(uid)
        with grants_doc_lock:   # read and write as one step: a line removed meanwhile stays removed
            text, added = docs_mod.append_line(store.get_doc(None, doc_id)["body"], line)
            if added:
                store.update_doc(doc_id, body=text, by=by)
        return added

    try:
        migrate_grants()
    except Exception as e:   # the grants docs still serve; the next start tries again
        print(f"[tares] moving the grants into the grants table failed: {type(e).__name__}: {e}",
              flush=True)

    # ── stations (TR-420): sessions grouped by the station they play ──────────
    def _ts(v) -> float:
        return v.timestamp() if isinstance(v, datetime) else 0.0

    retry_lock = threading.Lock()

    def retry_key(person_id: str | None, crew: str | None) -> str:
        return f"{person_id or 'local'}/{crew or ''}"

    def claude_retry() -> dict[str, dict]:
        """The retry marks as {"<person id>/<crew name>": {station: {target, fails, next_at}}}.
        What an older Tares stored (one flat {station: {target, ...}} for the whole cell) reads as
        the crew-less marks of the person `local`."""
        try:
            cur = json.loads(store.get_setting(CLAUDE_RETRY_SETTING) or "{}")
        except ValueError:
            cur = {}
        if not isinstance(cur, dict):
            return {}
        if any(isinstance(v, dict) and isinstance(v.get("target"), str) for v in cur.values()):
            return {retry_key("local", ""): {k: v for k, v in cur.items() if isinstance(v, dict)}}
        return {k: v for k, v in cur.items() if isinstance(v, dict)}

    def claude_retry_of(person_id: str | None) -> dict:
        """One person's marks as a flat {station: row}: all their crews' merged, the crew-less last."""
        prefix = retry_key(person_id, None)
        marks = claude_retry()
        out: dict = {}
        for k in sorted(k for k in marks if k.startswith(prefix) and k != prefix):
            out.update(marks[k])
        out.update(marks.get(prefix, {}))
        return out

    def shown_station_state(cur: dict, now: datetime) -> str:
        """The state a station shows, from its newest session."""
        state = factory_mod.shown_state(cur["state"] or "working", cur["last_at"], now)
        return "ended" if state == "replaced" else state

    def in_sight(rows: list[dict], hidden: set) -> list[dict]:
        """`rows` without the stations that worked only in projects the caller cannot see (TR-629)."""
        return [st for st in rows if not (st["projects"] and set(st["projects"]) <= hidden)]

    def in_crew_order(rows: list[dict]) -> list[dict]:
        order = {r: i for i, r in enumerate(factory_mod.ROLES)}
        return sorted(rows, key=lambda s: (order.get(s["role"] or "", 99), s["name"], s["crew_id"] or ""))

    def station_states() -> list[dict]:
        """Who runs, for a reader that needs no more of `stations()` (Setup, TR-1634): name, role,
        state, last_at, crew_id, person and projects of every station the caller may see, in the
        same order. Reads no earlier session, no usage and no line count."""
        crews = {c["id"]: c for c in store.list_crews()}
        now = now_utc()
        out = [{"name": g["station"], "role": g["cur"]["role"], "crew_id": g["crew_id"],
                "state": shown_station_state(g["cur"], now), "last_at": g["cur"]["last_at"],
                "person": (crews.get(g["crew_id"]) or {}).get("person"), "projects": g["projects"]}
               for g in store.station_rollup(h.cc_source(), totals=False)]
        hidden = h.hidden_ids()
        if hidden:
            out = in_sight(out, hidden)
            for st in out:
                st["projects"] = [u for u in st["projects"] if u not in hidden]
        return in_crew_order(out)

    def stations(project: str | None = None, whole: bool = False) -> list[dict]:
        """The stations, one row each per crew (TR-628): two crews with the same station names
        never merge. Each row names its crew, machine and person. A current session the caller must
        not read is taken off its row (`blank_session`, TR-1704); `whole` leaves it on, for a write
        that acts on that session."""
        # one row per station with its newest session, the few before it and the totals over all of
        # them, so a station that has had many sessions costs no more than one that has had few
        # (TR-1500); the earlier ones are read a page at a time (GET /api/factory/crew/.../sessions)
        hidden = h.hidden_ids()
        crews = {c["id"]: c for c in store.list_crews()}
        own = own_crew_ids(h, crews.values()) if hidden else set()
        # a project the caller cannot see adds nothing to a station's usage (TR-1696), and a
        # session that worked in one is not among its earlier sessions or calls (TR-1704)
        rollup = store.station_rollup(h.cc_source(), hidden=hidden, own=own)
        names = {p["id"]: p["name"] for p in store.list_projects()}
        now = now_utc()
        out = []
        unread: set[str] = set()   # the current sessions the caller must not read
        for g in rollup:
            cid, name, cur = g["crew_id"], g["station"], g["cur"]
            if g["cur_hidden"]:
                unread.add(cur["session"])
            quiet = (int((now - cur["last_at"]).total_seconds() // 60)
                     if isinstance(cur["last_at"], datetime) else None)
            state = shown_station_state(cur, now)
            # the most recent sessions that played the station before the current one: replaced by it
            earlier = [{"session": s["session"], "started_at": s["started_at"],
                        "last_at": s["last_at"], "lines": s["lines"], "state": "replaced"}
                       for s in g["earlier"]]
            crew = crews.get(cid) or {}
            out.append({"name": name, "role": cur["role"], "parent": cur["parent"],
                        "project": cur["project"], "project_name": names.get(cur["project"]),
                        "state": state, "state_reason": cur["state_reason"],
                        "state_at": cur["state_at"], "quiet_minutes": quiet,
                        "session": cur["session"], "started_at": cur["started_at"],
                        "last_at": cur["last_at"], "lines": cur["lines"], "earlier": earlier,
                        "earlier_count": g["earlier_count"],
                        "crew_id": cid, "crew": crew.get("name"),
                        "machine": crew.get("machine") or "", "person": crew.get("person"),
                        "projects": g["projects"], "usage": g["usage"],
                        "coordination_calls": g["coordination_calls"],
                        # what it runs on (TR-1600): a Codex station's lines say so; the rest are Claude Code
                        "runtime": cur.get("runtime") or "claude_code", "model": cur.get("model") or None})
        # the status line gives one cost for the whole session, so none of it is shown for a
        # session in a hidden project or with a reply that counts for one (TR-1696)
        no_cost: set[str] = set()
        if hidden:   # a project the caller cannot see is not named by a station (TR-629)
            no_cost = store.sessions_counting_for([st["session"] for st in out], hidden)
            gone = hidden & set(store.deleted_private_projects())
            if gone:   # a row's project passes over a deleted one, so the session is asked itself
                no_cost |= store.sessions_tied_to([st["session"] for st in out], gone)
            out = in_sight(out, hidden)
            for st in out:
                seen = [u for u in st["projects"] if u not in hidden]
                if len(seen) != len(st["projects"]) or st["project"] in hidden:
                    # the station also works on a project the caller cannot see: its reason
                    # may name that project, so none of it is shown
                    st["state_reason"] = None
                st["projects"] = seen
                if st["project"] in hidden:
                    st["project"], st["project_name"] = None, None
                    no_cost.add(st["session"])
        meters = store.session_meters([st["session"] for st in out])
        retry = claude_retry()
        for st in out:
            # the stations this one started: in its own crew, whose names may repeat in another
            st["children"] = sorted(x["name"] for x in out
                                    if x["parent"] == st["name"] and x["crew_id"] == st["crew_id"])
            # the context window from the station's status line, and what all its sessions spent
            st["context"] = claude_usage.context_now(meters.get(st["session"]))
            if st["context"] and st["session"] in no_cost:
                st["context"]["cost_usd"] = None
            st["claude_version"] = claude_usage.code_version(meters.get(st["session"]))
            if st["runtime"] != "codex":   # a Claude Code station's model is the one its status line names
                st["model"] = (st["context"] or {}).get("model") or None
            # set while the watch retries its move, else null: the crew's own marks, else those
            # of its person that name no crew
            pid = (crews.get(st["crew_id"]) or {}).get("person_id")
            st["claude_retry"] = (retry.get(retry_key(pid, st["crew"]), {}).get(st["name"])
                                  or retry.get(retry_key(pid, None), {}).get(st["name"]))
            if st["session"] in unread and not whole:
                blank_session(st)
        if project:
            out = [s for s in out if project in s["projects"]]
        return in_crew_order(out)

    def crew_runtime(asked: str = "") -> str:
        """The runtime whose limits pace the crew: the one the caller names (a crew knows what it runs on),
        else the cell's (the `runtime` slot, which is what a new station is started on)."""
        asked = (asked or "").strip()
        if asked:
            return asked
        return integrations_resolve.effective("runtime", None, store=store).integration

    def pace(request: Request | None = None, runtime: str = "") -> dict:
        """The pace under the caller's settings (a person's own `pace` over the cell's). A crew on Codex is
        paced from the Codex account's limits (tares/codex_usage.py), never from Claude's plan."""
        if crew_runtime(runtime) == "codex":
            return codex_usage.pace(store, settings(request), now_utc(), factory_mod.pace_now)
        return factory_mod.pace_now(settings(request), claude_usage.plan(store), now_utc())

    def caller_person_id(request: Request) -> str:
        """The person whose crews the caller means by "mine". A recording with no person (the
        system credential) is filed under `local`, so a caller with no person is `local` too."""
        p = h.current_person(request)
        return p["id"] if p else "local"

    def work_person(request: Request) -> dict:
        """The person whose work the caller means (TR-633): the key's person; the system
        credential is the person `local`, like its recordings. A key with no person is refused."""
        p = h.current_person(request)
        if p:
            return {"id": p["id"], "name": p.get("name") or p["id"]}
        if not h.is_system(request):
            h.err(PermissionError(NO_PERSON), 403)
        local = store.get_person("local") or {}
        return {"id": "local", "name": local.get("name") or "You"}

    # Who owns what is one rule, factory_owners (TR: project page waiting): an assignee, else the
    # project's lead, else the cell's admins; a person who left, or an unclaimed `local`, owns
    # nothing. These three read it, so every list and every check agrees with the home.
    def is_mine(t: dict, me: str, p: dict | None, o=None) -> bool:
        """A ticket is the person's when it is assigned to them; one with no assignee (or one who
        left) is its project's owner's (factory_owners)."""
        o = o or owners_mod.Owners(store)
        return o.owns_all(me) or me in o.ticket(t, p)

    def desk_is_mine(d: dict, me: str, o=None) -> bool:
        """A desk item is the person's when it is addressed to them; one addressed to nobody here
        is its project's owner's (factory_owners)."""
        o = o or owners_mod.Owners(store)
        if o.owns_all(me):
            return True
        p = store.get_project(d["project"]) if d.get("project") else None
        return me in o.desk(d, p)

    def desk_can_act(d: dict, me: str, request: Request) -> bool:
        """Who may answer or allow a desk item from the console (TR-898): the person it waits for
        (desk_is_mine). One addressed to nobody who is here also lets an admin key act on it;
        nobody else (a key that can only ingest must not grant a permission)."""
        # who it waits for, by the rule itself: seeing every item (the root credential) is not a
        # right to act on one addressed to someone else
        o = owners_mod.Owners(store)
        p = store.get_project(d["project"]) if d.get("project") else None
        ids = o.desk(d, p)
        if o.single or me in ids:
            return True
        # addressed to nobody who is here (TR-898), or an orphan: an admin key and the cell's own
        # credential may act on it too. A right, not a list: the lists show it to its owner.
        unaddressed = o.here(d.get("to_person_id")) is None or o.orphan(ids)
        return unaddressed and (h.is_admin(request) or h.is_system(request))

    def leads(p: dict | None, me: str, o=None) -> bool:
        """The person owns the project's work: its lead, else (no lead, or the lead left) an admin
        (factory_owners)."""
        o = o or owners_mod.Owners(store)
        return o.owns_all(me) or me in o.project(p)

    def my_projects(request: Request) -> set[str] | None:
        """The projects the caller's person leads or holds a ticket assignment in, or None when
        nothing narrows the list: the system credential, or a key with no person (a project key)."""
        if h.is_system(request) or not h.current_person(request):
            return None
        return projects_of(h.current_person(request)["id"])

    def projects_of(me: str) -> set[str]:
        """The projects person `me` owns or owns a ticket in (my_projects' rule): one read of the
        people, the projects and their tickets."""
        o = owners_mod.Owners(store)
        ps = store.list_projects()
        tickets = store.tickets_by_project([p["id"] for p in ps])
        return {p["id"] for p in ps
                if leads(p, me, o) or any(is_mine(t, me, p, o) for t in tickets.get(p["id"]) or [])}

    def crews_of(rows: list[dict]) -> list[dict]:
        """The stations (as `stations()` lists them) grouped by crew, the crew seen last first."""
        info = {c["id"]: c for c in store.list_crews()}
        out = []
        for cid in dict.fromkeys(r["crew_id"] for r in rows):
            c = info.get(cid) or {}
            out.append({"id": cid, "name": c.get("name"), "machine": c.get("machine") or "",
                        "person": c.get("person"), "person_id": c.get("person_id"),
                        "last_seen": c.get("last_seen"),
                        "stations": [r for r in rows if r["crew_id"] == cid]})
        out.sort(key=lambda c: (-_ts(c["last_seen"]), c["id"] or ""))
        return out

    def pick_station(name: str, request: Request, crew: str | None = None,
                     whole: bool = False) -> dict:
        """The station called `name`. Crews can repeat a name: `crew` (a crew id or name) narrows
        it down, and of several that remain the caller's own crew's wins, then the newest.
        `whole` as in `stations`."""
        rows = [s for s in stations(whole=whole) if s["name"] == name]
        if crew:
            rows = [s for s in rows if crew in (s["crew_id"], s["crew"])]
        if not rows:
            h.err(KeyError(f"no station {name!r} has reported to Tares"
                           + (f" in crew {crew!r}" if crew else "")), 404)
        me = caller_person_id(request)
        rows.sort(key=lambda s: (((s["person"] or {}).get("id") != me), -_ts(s["last_at"])))
        return rows[0]

    @app.get("/api/crew")
    def get_crew(request: Request, mine: bool = False):
        """The crews (TR-628): one per person, crew name and machine, each with its stations (the
        current session and state, `earlier_count` and the 5 most recent earlier sessions that
        played it as `earlier`, the stations it started; `usage`, `coordination_calls` and
        `projects` cover every session; the rest of the earlier ones are paged by GET
        /api/factory/crew/{crew_id}/{station}/sessions), the crews' settings, and the shared grants.
        In another person's crew, a session that worked in a private project the caller cannot see
        is not in `earlier`, `earlier_count` or `coordination_calls`, and when it is the current
        one its row has no session, `started_at`, `lines`, `claude_version` or `state_reason`, and
        a `context` of nulls (TR-1704). `mine=1` keeps the caller's own
        crews. `stations` is the same rows flat, for a client that does not group by crew yet. `pace`
        is the crew's pace as GET /api/crew/pace gives it. `claude_retry` maps each station the
        caller's person's crews are retrying onto a newer Claude Code to {target, fails, next_at};
        the row of each station shows its own crew's as `claude_retry`."""
        rows = stations()
        if mine:
            me = caller_person_id(request)
            rows = [s for s in rows if (s["person"] or {}).get("id") == me]
        return {"crews": crews_of(rows), "stations": rows, "settings": settings(request),
                "grants": grants_out(None)["all"], "pace": pace(request),
                "claude_retry": claude_retry_of(caller_person_id(request)), "now": now_utc()}

    @app.put("/api/crew/claude-retry")
    def put_claude_retry(request: Request, body: dict = Body(...), crew: str = ""):
        """{stations: {name: {target, fails, next_at}}}: the stations the factory watch is retrying
        onto a new Claude Code, after a restart kept landing on an older pre-warmed spare. It
        replaces the caller's list for the crew named by `crew` (a query parameter; none: the
        caller's crew-less list); another person's or another crew's list is left alone. A station
        left out is not retrying. `target` is the version it is moving to, `fails` the failed
        moves so far and `next_at` the unix time of the next try. Each row of /api/crew shows its
        crew's entry as `claude_retry`, or null. The factory CLI sends this; it is only
        information for the Crew page."""
        rows = body.get("stations")
        if not isinstance(rows, dict):
            h.err(ValueError("stations must be an object of station name to {target, fails, next_at}"))
        if len(rows) > 200:
            h.err(ValueError("at most 200 stations"))
        if len(crew) > 128:
            h.err(ValueError("a crew name is at most 128 characters"))
        def num(v, lo: int, hi: int, default: int) -> int:
            return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else default
        out = {}
        for name, r in rows.items():
            if not isinstance(name, str) or not 0 < len(name) <= 128:
                h.err(ValueError("a station name is 1 to 128 characters"))
            if not isinstance(r, dict) or not isinstance(r.get("target"), str) or not r["target"]:
                h.err(ValueError(f"{name}: target must be the Claude Code version it is moving to"))
            out[name] = {"target": r["target"][:40],
                         "fails": num(r.get("fails"), 0, 1_000_000, 1),
                         "next_at": num(r.get("next_at"), 0, 4_102_444_800, 0)}
        key = retry_key(caller_person_id(request), crew.strip())
        with retry_lock:   # read, change one key, write: two people's reports must not undo each other
            marks = claude_retry()
            if out:
                marks[key] = out
            else:
                marks.pop(key, None)
            store.set_setting(CLAUDE_RETRY_SETTING, json.dumps(marks))
        return {"stations": out}

    @app.get("/api/crew/pace")
    def get_crew_pace(request: Request, runtime: str = ""):
        """Whether the crew is pacing itself before the plan's limits (the crew setting `pace`),
        worked out from the plan usage Tares keeps. `level` is 0 (ok), 1 (slow: the 5-hour window
        at 85% or the weekly limit at 90%) or 2 (hold: the window at 95%); `at_limit` is true while
        a limit is full and has not reset; `state` is off, ok, slow, hold or limit; `until` is when
        the limits behind it reset; `known` is false when no plan reading has arrived. The factory
        watch reads this (TR-674); a person's own `pace` setting counts for that person.

        `runtime` (a query parameter) names the runtime the asking crew runs on; without it the
        cell's own runtime counts. For `codex` the pace comes from the Codex account's limits (TR-1597)
        and the answer also has `source` ("codex"), `reading` (ok, none, stale or unsupported; `known`
        is true only for ok), `read_at`, `limits` (every window Codex reported, each {kind, label,
        percent, resets_at, reset, paces}; one with `paces` false is shown and never slows the crew),
        `account` ({type, plan}) and `reached`. Claude's plan is never used for it."""
        if runtime not in ("", "claude_code", "codex"):
            h.err(ValueError("runtime is claude_code or codex"))
        return pace(request, runtime)

    @app.put("/api/crew/codex-limits")
    def put_codex_limits(request: Request, body: dict = Body(...)):
        """The Codex account's limits as a crew on Codex just read them (TR-1597): {limits, account,
        taken_at}. `limits` is what its app-server answered to `account/rateLimits/read`, `account` the
        `account` of `account/read`. The windows and the account's type and plan are kept, with the
        cell's own time; an account id, an address and a workspace name are not. It replaces the last
        reading. GET /api/crew/pace?runtime=codex is worked out from it; a reading older than 30
        minutes is not used. The factory watch sends this once a round."""
        if not isinstance(body.get("limits"), dict):
            h.err(ValueError("limits must be the answer to account/rateLimits/read, an object"))
        if len(json.dumps(body)) > 200_000:
            h.err(ValueError("the reading is too large"))
        kept = codex_usage.save(store, body, caller_person_id(request), now_utc())
        return {"windows": len(kept["windows"]), "account": kept["account"]}

    def as_utc(x):
        """A stored time (a datetime, or its text) as an aware UTC datetime; None when there is none or it
        is not a time. The crew's watch reads the ISO form of this."""
        if isinstance(x, str):
            try:
                x = datetime.fromisoformat(x)
            except ValueError:
                return None
        if not isinstance(x, datetime):
            return None
        return (x if x.tzinfo else x.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    def work_of(me: dict, hidden: set, want: str = "", detail: bool = False) -> dict:
        """What belongs to person `me` ({id, name}) as GET /api/crew/my-work gives it: `ready`,
        `in_progress`, `bouncing`, `led_projects`, `desk` and `spec_questions`, leaving out the projects in
        `hidden`. `want` (a project id) keeps only that project's spec questions. `detail` adds,
        to each led project, its `project_id` and `milestones` ([{id, name, checks, since}]) for
        the home page; the route does not ask for it."""
        from . import merge_queue       # here: merge_queue imports this module
        o = owners_mod.Owners(store)    # one read of the people for every check below
        ready, doing, led, bouncing = [], [], [], []
        for uid in store.handed_over_projects(finished=False, taken=False):
            p = store.get_project(uid)
            if p is None or uid in hidden:
                continue
            tickets = store.list_tickets(uid)
            notes = None    # the working docs' last edits, read once a project has a held ticket
            wait = factory_mod.readiness(tickets)
            for t in tickets:
                if not is_mine(t, me["id"], p, o) or factory_mod.finished(t):
                    continue
                if t["holder"]:
                    if notes is None:
                        notes = {d["id"]: d["updated_at"] for d in store.list_docs(uid, "working")}
                    news = [d for d in (as_utc(t.get("updated_at")), as_utc(notes.get(t.get("working_doc")))) if d]
                    doing.append({"project": p["name"], "ticket": factory_mod.label(t),
                                  "title": t["title"], "holder": t["holder"],
                                  "stage": t["stage"] or "", "head": (t.get("pr") or {}).get("head") or "",
                                  "updated_at": max(news).isoformat() if news else None})
                elif wait[t["id"]]["ready"]:
                    ready.append({"project": p["name"], "ticket": factory_mod.label(t),
                                  "title": t["title"], "repo": t.get("repo") or ""})
                step = merge_queue.ladder_step(store, t["id"])
                if step:
                    bouncing.append({
                        "project": p["name"], "ticket": factory_mod.label(t), "title": t["title"],
                        "holder": t["holder"] or "", "stage": t["stage"] or "",
                        "count": merge_queue.bounces(store, t["id"]), "ladder": step,
                        "reason": merge_queue.last_bounce_reason(store, t["id"])})
            if leads(p, me["id"], o):
                ms, full = [], []
                for m in store.list_milestones(uid):
                    mine = [t for t in tickets if t["milestone"] == m["id"]
                            and t["status"] != "canceled"]
                    if mine and all(factory_mod.delivered(t) for t in mine):
                        ms.append(m["name"])
                        full.append({"id": m["id"], "name": m["name"], "checks": len(m["checks"]),
                                     "since": max((t["updated_at"] for t in mine
                                                   if t.get("updated_at")), default=None)
                                     or m["updated_at"]})
                led.append({"project": p["name"], "milestones_ready_to_check": ms,
                            **({"project_id": uid, "milestones": full} if detail else {})})
        desk = [{k: d[k] for k in ("label", "project_name", "question", "options", "recommendation",
                                   "why", "context", "blocks", "tickets", "blocking", "created_at")}
                for d in map(desk_out, store.list_desk("open"))
                if d["project"] not in hidden and desk_is_mine(d, me["id"], o)]
        asked = team_mod.get().open_questions_for(team_reader(), me["id"], hidden, want)
        return {"ready": ready, "in_progress": doing, "bouncing": bouncing, "led_projects": led,
                "desk": desk, "spec_questions": asked}

    @app.get("/api/crew/my-work")
    def my_work(request: Request, crew: str | None = None, project: str | None = None):
        """What belongs to the caller's person (TR-633), across the projects that have an open
        hand-over and that the caller can see: `ready` (their tickets that can start),
        `in_progress` (their tickets a station holds, each with its pull request's `head` and
        `updated_at`, the later of the ticket's last change and its working doc's, so a stage change or a
        check; a ticket with no assignee is its project
        lead's, or `local`'s when the project has no lead), `bouncing` (those of them that came
        back from review three or more times, each with `count`, `ladder` (3 or 5) and the last
        `reason`; TR-1092), and `led_projects` with the milestones
        whose tickets are all delivered. `desk` is the open desk items addressed to them (TR-634);
        `spec_questions` is the open questions a spec asked of them (TR-636), in any project they
        can see, finished or not; `project` (an id or a name) keeps only that project's, and one
        that does not exist, or that the caller cannot see, answers 404. Question numbers restart
        per project, so a caller working on one project asks for it by name."""
        me = work_person(request)
        try:
            crew_name = crew_name_of(request, crew)
        except ValueError as e:
            h.err(e)
        hidden = h.hidden_ids()
        want = (project or "").strip()
        if want:
            # an id or an exact name; a project the caller cannot see is as unknown as a mistyped one
            wp = store.get_project(want) or store.get_project_by_name(want)
            if wp is None or wp["id"] in hidden:
                h.err(ValueError(f"no project {want!r} you can see: use a project id or its exact "
                               "name"), 404)
            want = wp["id"]
        return {"person": me, "crew": crew_name, **work_of(me, hidden, want)}

    def team_reader() -> SimpleNamespace:
        """The `c` of the team interface (tares/team.py): what the team hooks read of this module
        (TR-739). Also `h.team_reader`."""
        return SimpleNamespace(store=store, h=h, stations=stations, work_of=work_of,
                               work_person=work_person, my_projects=my_projects, queue=queue_all,
                               settings=settings,
                               desk_is_mine=desk_is_mine, is_mine=is_mine, leads=leads,
                               stored=stored, crews_of=crews_of, mirror_in=mirror_in,
                               mirror_out=mirror_out, visible_grant=visible_grant,
                               grants_view=grants_view)

    h.team_reader = team_reader

    def team_home(request: Request, now=None, stations=None) -> dict:
        """The team part of GET /api/home for the caller's person (TR-739), or {} on a cell of
        one person: see the team interface. It reads what this module holds; the route is in tares/routes/home.py."""
        return team_mod.get().team_home(team_reader(), request, now or now_utc(), stations)

    def project_waiting(request: Request, uid: str, now=None) -> dict | None:
        """What waits in one project on a team cell: {mine, others} (see the team interface), or
        None on a cell of one person."""
        return team_mod.get().project_waiting(team_reader(), request, now or now_utc(), uid)

    def waiting_for_person(person: dict, now=None) -> list[dict]:
        """What waits for `person` ({id, is_admin, ...}), as their home lists it, built with no
        request (TR-1458: a message sent to them). The private projects they are not on are
        hidden as for their own key; on a cell of one person everything is theirs."""
        now = now or now_utc()
        hidden = {p["id"] for p in store.private_projects()
                  if not store.is_project_member(p["id"], person["id"])}
        got = team_mod.get().waiting_for_person(team_reader(), person, hidden, now)
        if got is not None:
            return got
        if store.only_local_person():
            return factory_home.waiting_items(store, fx, now, hidden)
        return owners_mod.mine(factory_home.waiting_items(store, fx, now, hidden), person["id"],
                               admin=bool(person.get("is_admin")))

    def own_stations(request: Request, rows: list[dict]) -> list[dict]:
        """`rows` kept to the caller's own crews on a team cell (see the team interface)."""
        return team_mod.get().own_stations(team_reader(), request, rows)

    def own_desk(request: Request, rows: list[dict]) -> list[dict]:
        """`rows` (open desk items) kept to the caller's person on a team cell."""
        return team_mod.get().own_desk(team_reader(), request, rows)

    def own_tickets(request: Request, tickets: dict, projects: list[dict]) -> dict:
        """`tickets` ({project: tickets}) kept to the caller's person on a team cell."""
        return team_mod.get().own_tickets(team_reader(), request, tickets, projects)

    def team_projects(request: Request, open_ps: list, tickets: dict, rows: list, which: str,
                      now=None, waiting: list | None = None) -> dict:
        """The team's version of the home's `projects` (TR-739): lead, members, scope, what is
        the caller's, the Mine and Team counts; {} on a cell of one person."""
        return team_mod.get().team_projects(team_reader(), request, open_ps, tickets, rows, which,
                                            now or now_utc(), waiting or [])

    @app.get("/api/crew/projects")
    def crew_projects(request: Request, crew: str | None = None):
        """The projects handed to the crew, oldest hand-over first: each with its repos (the
        folder on the crew's machine and the GitHub name), the autonomy it was handed over at
        when stricter than the crew's, its tickets by stage, the milestone it is on, and which
        tickets each repo has. The orchestrator works from this list (TR-462). A person's key
        sees the projects the person leads or holds a ticket in; the system credential sees all
        (TR-633). A repo's `path` is the folder the asking crew (`crew`, else X-Tares-Crew)
        handed over, else the first one handed over (TR-1222)."""
        try:
            key = factory_mod.crew_key(person_of(request), crew_name_of(request, crew))
        except ValueError as e:
            h.err(e)
        names = {p["id"]: p["name"] for p in store.list_projects()}
        hidden = h.hidden_ids()
        mine = my_projects(request)
        names = {u: n for u, n in names.items()
                 if u not in hidden and (mine is None or u in mine)}
        active = store.handed_over_projects(finished=False, taken=False)
        finished = store.handed_over_projects(finished=True)
        # every listed project's tickets in one query, shared by progress and repos
        tk = store.tickets_by_project(sorted({u for u in [*active, *finished] if u in names}))
        out = []
        for uid in active:
            if uid not in names:
                continue
            ho = store.get_handover(uid) or {}
            out.append({"id": uid, "name": names[uid], **project_progress(store, uid, tk[uid]),
                        "repos": repo_rows(uid, factory_mod.own_paths(ho.get("repos") or [], key),
                                           tk[uid]),
                        "autonomy": ho.get("autonomy"), "handed_over_at": ho.get("at"),
                        "handed_over_by": ho.get("by_label") or ho.get("by"),
                        "handed_over_person": ho.get("person")})
        out.sort(key=lambda p: str(p["handed_over_at"] or ""))
        done = []
        for uid in finished:
            if uid in names:
                ho = store.get_handover(uid) or {}
                done.append({"id": uid, "name": names[uid], **project_progress(store, uid, tk[uid]),
                             "finished": ho.get("finished")})
        done.sort(key=lambda p: str((p["finished"] or {}).get("at") or ""), reverse=True)
        releasing = []
        for uid in store.handed_over_projects(finished=False, taken=True):
            ho = store.get_handover(uid) or {}
            if uid in names and not (ho.get("takeover") or {}).get("released_at"):
                releasing.append({"id": uid, "name": names[uid], "takeover": ho["takeover"],
                                  "repos": factory_mod.own_paths(ho.get("repos") or [], key),
                                  "holders": sorted({t["holder"] for t in store.list_tickets(uid)
                                                     if t["holder"]})})
        return {"projects": out, "finished": done, "releasing": releasing, "now": now_utc()}

    SINCE_MAX = 1000   # lines a refresh from `since` reads at most (TR-1177)
    @app.get("/api/crew/{name}")
    def get_station(name: str, request: Request, recent: int = 30,
                          crew: str | None = None, conversation: int = 0, before: int | None = None,
                          since: int | None = None):
        """One station: its row as in /api/crew, the tickets it holds in every project, its
        current session's last lines (`recent`, at most 200), and the command that opens it.
        Crews can repeat a station name: `crew` (id or name) says which; without it the caller's
        own crew's station comes back. `conversation` (at most 500) adds the session's last lines as a
        conversation (tares/crew_conversation.py, TR-1177), oldest first; `before` (a `conversation_from`
        from an earlier answer) reads the lines before that point instead, for "Show earlier". `since`
        reads from that line to the newest, at most SINCE_MAX lines (the newest when there are more), so a
        reader who loaded earlier lines refreshes without a gap; `conversation_from` says where it starts."""
        st = pick_station(name, request, crew)
        held = []
        hidden = h.hidden_ids()
        for p in store.list_projects():
            if p["id"] in hidden:
                continue
            for t in store.list_tickets(p["id"]):
                if t["holder"] == name:
                    held.append({"project": p["id"], "project_name": p["name"],
                                 "ticket": factory_mod.label(t), "title": t["title"],
                                 "stage": t["stage"], "repo": t.get("repo"),
                                 "pr": (t["pr"] or {}).get("url")})
        n = min(max(recent, 0), 200)
        read = []       # where each read of the session's lines came from (TR-1507)
        lines = []
        if n and st["session"]:   # none on the row: the caller must not read it (TR-1704)
            # `tail`: fewer lines than counted can be read, so the newest readable ones show
            lines, where = store.key_events_from(h.cc_source(), st["session"], limit=n,
                                                 offset=max((st["lines"] or 0) - n, 0), tail={})
            read.append(where)
        m = (store.session_meters([st["session"]]).get(st["session"]) or {}) if st["session"] else {}
        convo = {}
        c = min(max(conversation, 0), 500)
        if c and st["session"]:
            end = (st["lines"] or 0) if before is None else max(min(before, st["lines"] or 0), 0)
            start = max(end - c, 0)
            if since is not None and before is None:
                start = max(min(since, end), end - SINCE_MAX, 0)
            tail = {} if before is None and since is None else None     # the newest lines asked
            rows, where = store.key_payload_rows_from(h.cc_source(), st["session"],
                                                      limit=end - start, offset=start, tail=tail)
            read.append(where)
            convo = {"conversation": crew_conversation.build(rows)[0],
                     "conversation_from": (tail or {}).get("start", start)}
        return {**st, "tickets": held, **convo, "lines_from": lines_from(read),
                "context_detail": m.get("context"), "context_at": m.get("context_at"),
                "context_asked_at": m.get("context_asked_at"),
                "recent": [{"at": x.get("event_time"), "type": x.get("event_type"),
                            "text": str(x.get("text") or "")[:600]} for x in lines],
                "attach": factory_mod.attach_command(name, st.get("role") or "")}

    @app.get("/api/crew/{name}/tool/{tool_id}")
    def station_tool(name: str, tool_id: str, request: Request, crew: str | None = None):
        """One tool call's whole result in the station's current session (TR-1177): what a card on
        the Crew page loads when it is opened and the list held only the start of it."""
        st = pick_station(name, request, crew)
        b, where = (store.key_tool_result_from(h.cc_source(), st["session"], tool_id)
                    if st["session"] else (None, "events"))
        if b is None:
            raise HTTPException(404, f"no tool call {tool_id} in {name}'s session")
        return {"id": tool_id, "output": crew_conversation._result_text(b.get("content")),
                "failed": bool(b.get("is_error")), "lines_from": where}

    @app.post("/api/crew/{name}/end")
    def end_station(name: str, request: Request, body: dict | None = Body(None),
                          crew: str | None = None):
        """{reason?}: mark the station's current session ended, for a station that was retired or
        whose session is gone, so it stops showing as working or quiet. Its earlier sessions and
        lines are kept. The orchestrator or the person (unlabeled, such as `factory retire`) may.
        A session that is still alive turns the row back with its next line."""
        deny(request, "end_station")
        st = pick_station(name, request, crew, whole=True)
        # `crew` narrows the name; with none, pick_station falls back to another person's station
        # of that name, which a caller must not end (the system credential may)
        if not h.is_system(request) and (st["person"] or {}).get("id") != caller_person_id(request):
            h.err(PermissionError(f"{name} belongs to another person's crew: only its owner "
                                  "ends it"), 403)
        reason = " ".join(str((body or {}).get("reason") or "").split())[:100] or "marked ended"
        if (store.session_state(st["session"]) or {}).get("state") != "ended":
            store.set_session_state(st["session"], "ended", reason, now_utc(), force=True)
            factory_home.clear_cache()   # the Crew page shows it ended on the next read
        row = (st["crew_id"], st["name"])
        return next(s for s in stations() if (s["crew_id"], s["name"]) == row)

    # ── a station's context by category: asked from the console, read on the crew's machine ──
    ASK_TTL = 15   # minutes an unanswered ask stays open; the watch reads on the crew's machine

    @app.post("/api/crew/{name}/context")
    def ask_station_context(name: str, request: Request, crew: str | None = None):
        """Ask for a new /context reading of the station's session. The factory watch on the
        crew's machine reads it (no model call) within a few seconds, if it runs. `session` is
        null for a caller who must not read that session (TR-1710); the ask is recorded all the
        same."""
        st = pick_station(name, request, crew, whole=True)
        # the reading is taken on its owner's machine: nobody else asks it (the system credential may)
        if not h.is_system(request) and (st["person"] or {}).get("id") != caller_person_id(request):
            h.err(PermissionError(f"{name} belongs to another person's crew: only its owner "
                                  "asks for its context"), 403)
        at = now_utc()
        store.ask_session_context(st["session"], at)
        unread = unread_sessions(h, [st["session"]])
        return {"session": None if unread else st["session"], "asked_at": at}

    @app.get("/api/crew/context/asks")
    def station_context_asks():
        """The stations someone asked a /context reading of that has not come in yet, for
        `factory crew watch`. An ask for a session the caller must not read is left out
        (TR-1710)."""
        out = []
        asked = store.context_asks(now_utc() - timedelta(minutes=ASK_TTL))
        unread = unread_sessions(h, asked)
        for x in asked:
            if x in unread:
                continue
            st = store.session_station(x)
            if st:
                out.append({"station": st["station"], "session": x})
        return {"asks": out}

    # ── the crew on a project, and the hand-over (TR-422, TR-425) ─────────────
    @app.get("/api/projects/{uid}/crew")
    def get_project_crew(uid: str):
        """The stations that worked on this project (builders started for it among them), and
        when it was handed to the crew."""
        h.project_or_404(uid)
        rows = stations(uid)
        return {"crews": crews_of(rows), "stations": rows, "handover": store.get_handover(uid),
                "coordination": coordination_summary(uid, rows), "now": now_utc()}

    def coordination_summary(uid: str, ss: list[dict]) -> dict:
        """Coordination calls of the project's builders and helpers (the always-on stations
        serve every project, so their calls are not the project's) against its merged pull
        requests (TR-683)."""
        mine = {s["name"]: s["coordination_calls"] for s in ss
                if s["role"] in ("builder", "helper") and s["project"] == uid}
        merged = sum(1 for t in store.list_tickets(uid)
                     if (t["pr"] or {}).get("merged") or (t["stage"] in ("merged", "shipped") and t["pr"]))
        calls = sum(mine.values())
        return {"calls": calls, "merged_prs": merged,
                "per_merged_pr": round(calls / merged, 1) if merged else None, "stations": mine}

    # ── the ledger (TR-427, TR-428, TR-410, TR-429) ───────────────────────────
    def ticket_or_404(uid: str, ref: str) -> dict:
        t = store.get_ticket(uid, ref)
        if t is None:
            h.err(KeyError(f"project has no ticket {ref!r}"), 404)
        return t

    def by_of(request: Request, body: dict | None = None) -> str:
        """The name a record keeps for the call: the station, else what the caller says. A key's
        own name stands in only when no person stands behind the call (TR-619): next to a person
        it is not a station, and the person's name shows instead."""
        c = caller(request)
        said = str((body or {}).get("by") or "").strip()[:100]
        if c["station"] or said:
            return c["station"] or said
        return "" if h.current_person(request) else (h.by(request, body) or "")

    def who(request: Request, body: dict | None = None) -> tuple[str, str]:
        return (by_of(request, body) or "a session"), caller(request)["role"]

    def person_of(request: Request) -> str | None:
        """The person behind the call's credential, for the record of who did it (TR-619). None
        for the system credential."""
        p = h.current_person(request)
        return p["id"] if p else None

    def answerer(request: Request) -> str | None:
        """Who an answer is recorded under (TR-1451): the person the caller works as, the system
        credential's being `local`, as on a desk reply; None for a key with no person."""
        p = h.current_person(request)
        return p["id"] if p else ("local" if h.is_system(request) else None)

    @app.post("/api/projects/{uid}/tickets/{ref}/assign")
    def assign_ticket(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{holder}: the station that works the ticket. Only the orchestrator (or a person's own
        session) assigns. A ticket with no stage yet starts at todo."""
        h.project_or_404(uid)
        deny(request, "assign")
        t = ticket_or_404(uid, ref)
        holder = str(body.get("holder") or "").strip()[:factory_mod.MAX_NAME]
        if not holder:
            h.err(ValueError("name the station that holds it (holder)"))
        fields = {"holder": holder}
        if not t["stage"]:
            fields["stage"] = "todo"
        store.update_ticket(t["id"], **fields)
        by, role = who(request, body)
        store.add_ticket_history(t["id"], uid, "holder", holder, None, by, role, now_utc(),
                                 person_of(request))
        return h.ticket_out(uid, ticket_or_404(uid, t["id"]), with_doc=False)

    @app.post("/api/projects/{uid}/tickets/{ref}/stage")
    async def set_stage(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{stage, reason?, pr?, checks?}: move the ticket. Only the station holding it (or a helper
        it started) moves it; only the releaser marks it shipped; a person's own session may do
        anything. `checks` ({command, result, commit?, broke_test?} each) are recorded in the same
        call, so a builder asking for review need not record them one by one. `pr` (a PR URL or owner/repo#12) links the ticket to its pull request, read
        from GitHub from then on. A ticket's status follows its stage in the system that
        holds it (Tares, Linear or GitHub), through the project's tickets integration; a write
        that does not land is kept and tried again, and never fails this call."""
        # the store work runs in worker threads, before and after the one GitHub read

        def check():
            h.project_or_404(uid)
            t = ticket_or_404(uid, ref)
            try:
                stage, reason = factory_mod.stage_ok(body.get("stage"), body.get("reason"))
            except docs_mod.DocError as e:
                h.err(e)
            why = factory_mod.may_move(caller(request), t, stage)
            if why:
                h.err(PermissionError(why), 403)
            return t, stage, reason

        t, stage, reason = await asyncio.to_thread(check)
        checks = parse_checks(body) if body.get("checks") else []
        fields: dict = {"stage": stage, "stage_reason": reason or None}
        pr_ref = str(body.get("pr") or "").strip()
        if pr_ref:
            parsed = factory_github.parse_pr(pr_ref)
            if parsed is None:
                h.err(ValueError("pr is the pull request's URL (https://github.com/o/r/pull/12) "
                                 "or o/r#12"))
            prev = t["pr"] if t["pr"] and (t["pr"].get("repo"), t["pr"].get("number")) == parsed else None
            fields["pr"] = await factory_github.read_pr(store, parsed[0], parsed[1], prev)

        def write():
            nonlocal t
            if checks:
                record_checks(uid, t, request, body, checks)
                t = ticket_or_404(uid, t["id"])
            was = t["stage"]
            store.update_ticket(t["id"], **fields)
            by, role = who(request, body)
            store.add_ticket_history(t["id"], uid, "stage", stage, reason, by, role or None,
                                     now_utc(), person_of(request))
            if stage == "blocked":
                stuck_on_desk(uid, t, reason, request, by)
            elif was == "blocked":
                unstuck_on_desk(uid, t, stage)
            if fields.get("pr"):   # a PR GitHub already shows merged makes the ticket merged
                after = ticket_or_404(uid, t["id"])
                merged = merged_fields(after, fields["pr"])
                if merged:
                    store.update_ticket(t["id"], **merged)
                    store.add_ticket_history(t["id"], uid, "stage", "merged",
                                             "GitHub shows the pull request merged", "github",
                                             "github", now_utc())
            return ticket_or_404(uid, t["id"])

        after = await asyncio.to_thread(write)
        # the status follows the stage in the project's ticket system; a merge GitHub already
        # showed made the stage merged above, so the status is the one of the stage it ended on
        await write_status(store, uid, after, after["stage"] or stage)
        return await asyncio.to_thread(
            lambda: h.ticket_out(uid, ticket_or_404(uid, t["id"]), with_doc=False))

    def stuck_items(uid: str, label: str) -> list[dict]:
        """The open items a block filed for the ticket: Tares's question, about it alone."""
        said = f"{label} is stuck. "
        return [d for d in store.list_desk("open", uid)
                if d["question"].startswith(said) and (d.get("tickets") or []) == [label]]

    def stuck_on_desk(uid: str, t: dict, reason: str, request: Request, by: str) -> None:
        """A stuck ticket is a person's turn (the status-words spec, decision 5: no "waiting for
        you" without an item), so blocking one files a blocking desk item, addressed like any
        other (the ticket's assignee, else the project's lead) and naming the station that
        stopped, unless an open item already names the ticket: the station's own ASK, or this
        one from an earlier block. An ASK that comes after it replaces it (`desk_add`)."""
        label = factory_mod.label(t)
        if any(label in (d.get("tickets") or []) for d in store.list_desk("open", uid)):
            return
        station = caller(request)["station"]
        short = reason.rstrip(".")
        short = short[:1].upper() + short[1:]
        short = short if len(short) <= 160 else short[:157].rstrip() + "..."
        store.add_desk_item(
            uid, f"{label} is stuck. {short}. What should happen?",
            f"{station or by or 'The crew'} moved {label} to blocked: {reason}", [],
            "Say what the crew should do so it can continue",
            "The station holding it cannot continue without a person.",
            f"What happens to {label}", [label], True, station or by or "the crew", [], now_utc(),
            station or None, None, person_of(request), desk_target(request, None, uid, [label]))

    def unstuck_on_desk(uid: str, t: dict, stage: str) -> None:
        """The ticket moved on: the item its block filed has nothing left to ask. Items a person
        or the orchestrator wrote about it stay."""
        label = factory_mod.label(t)
        for d in stuck_items(uid, label):
            store.close_desk_item(d["id"], "withdrawn", f"{label} moved on to {stage}", now_utc())

    @app.get("/api/projects/{uid}/tickets/{ref}/messages")
    def ticket_messages(uid: str, ref: str):
        """The crew's protocol messages ([TF:<TYPE>]) that name this ticket, oldest first, from
        the recorded SendMessage calls (TR-429)."""
        p = h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        return {"messages": messages_for(p, t)}

    def messages_for(project: dict, t: dict) -> list[dict]:
        builders = {s["station"] for s in store.station_sessions(h.cc_source())
                    if s["project"] == project["id"]}
        lab = factory_mod.label(t)
        name = re.compile(rf"(?<![\w-]){re.escape(project['name'])}(?![\w-])", re.I)
        out = []
        for m in store.protocol_messages(h.cc_source()):
            text = m["text"]
            if not re.search(rf"(?<![\w-]){re.escape(lab)}(?![\w-])", text):
                continue
            # a builder of this project took part, or the message names the project (the
            # crew's own stations serve every project)
            if not (m["to"] in builders or m["from"] in builders or name.search(text)):
                continue
            out.append(m)
        # of those, the messages of the sessions the caller may read (TR-1713); a message
        # recorded with no session stays
        sids = sorted({m["session"] for m in out if m["session"]})
        closed = unread_sessions(h, sids) if sids else set()
        return [m for m in out if m["session"] not in closed]

    # ── the build, recorded (TR-433, TR-435, TR-436) ──────────────────────────
    def ticket_ids(uid: str, refs) -> list[str]:
        try:
            refs = factory_mod.depends_ok(refs)
        except docs_mod.DocError as e:
            h.err(e)
        return [ticket_or_404(uid, r)["id"] for r in refs]

    def assumption_out(uid: str, a: dict, by_id: dict | None = None) -> dict:
        if by_id is None:
            by_id = {t["id"]: t for t in store.list_tickets(uid)}
        return {**a, "tickets": [factory_mod.label(by_id[t]) for t in a["tickets"] if t in by_id]}

    @app.post("/api/projects/{uid}/assumptions", status_code=201)
    def add_assumption(uid: str, request: Request, body: dict = Body(...)):
        """{question, choice, why, affects?, tickets?}: an assumption made so the work did not
        block. Numbered A1, A2... per project; open until the person keeps or overturns it."""
        h.project_or_404(uid)
        try:
            q, choice, why, affects = factory_mod.assumption_ok(
                body.get("question"), body.get("choice"), body.get("why"), body.get("affects"))
        except docs_mod.DocError as e:
            h.err(e)
        by, role = who(request, body)
        a = store.add_assumption(uid, q, choice, why, affects, ticket_ids(uid, body.get("tickets")),
                                 by, role, now_utc(), person_of(request))
        return assumption_out(uid, a)

    @app.get("/api/projects/{uid}/assumptions")
    def list_assumptions(uid: str, state: str = ""):
        h.project_or_404(uid)
        st = state.strip().lower() or None
        if st and st not in factory_mod.ASSUMPTION_STATES:
            h.err(ValueError("state is one of: " + ", ".join(factory_mod.ASSUMPTION_STATES)))
        by_id = {t["id"]: t for t in store.list_tickets(uid)}
        return {"assumptions": [assumption_out(uid, a, by_id)
                                for a in store.list_assumptions(uid, st)]}

    @app.post("/api/projects/{uid}/assumptions/{ref}/state")
    def decide_assumption(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{state: kept|overturned|open, words?, batch?, item?}: what the person said about it.
        `item` is the Home item it was kept from, the id the answer is recorded under. Only the
        orchestrator (or a person's own session) records it; an overturned one keeps the
        person's words."""
        h.project_or_404(uid)
        deny(request, "assumption_state")
        a = store.get_assumption(uid, ref)
        if a is None:
            h.err(KeyError(f"project has no assumption {ref!r}"), 404)
        st = str(body.get("state") or "").strip().lower()
        if st not in factory_mod.ASSUMPTION_STATES:
            h.err(ValueError("state is one of: " + ", ".join(factory_mod.ASSUMPTION_STATES)))
        words = " ".join(str(body.get("words") or "").split())[:1000] or None
        if st == "overturned" and not words:
            h.err(ValueError("an overturned assumption keeps the person's words: say them"))
        store.decide_assumption(a["id"], st, words, str(body.get("batch") or "") or None,
                                now_utc() if st != "open" else None)
        if st != "open":
            # the record of the answer (TR-1451); a station records what the person said in its session
            # the Home item that offered it names itself (`calls_to_confirm:<project>:<owners>` when
            # the project's calls wait for more than one owner); any other id is not this project's
            item = f"calls_to_confirm:{uid}"
            said_item = str(body.get("item") or "")
            if said_item.startswith(item + ":") and len(said_item) <= 300:
                item = said_item
            answers_mod.record(
                store, request, item=item, kind="calls_to_confirm", project=uid,
                ask=f"Keep the call: {a['question']}", choice="yes" if st == "kept" else "no",
                said=f"Kept: {a['choice']}" if st == "kept" else words or "Overturned",
                via="session" if caller(request)["station"] else None, person_id=answerer(request))
        return assumption_out(uid, store.get_assumption(uid, a["id"]))

    def parse_checks(body: dict) -> list[tuple[str, str, str, str]]:
        """The checks a body carries: {command, result, commit?, broke_test?} itself, and `checks`,
        a list of the same, for the ones recorded together (TR-683)."""
        items = ([body] if body.get("command") is not None or body.get("result") is not None else [])
        more = body.get("checks")
        if more is not None and not isinstance(more, list):
            h.err(ValueError("checks is a list of {command, result, commit?, broke_test?}"))
        items += [x if isinstance(x, dict) else {} for x in more or []]
        if not items:
            h.err(ValueError("say the command you ran"))
        try:
            return [factory_mod.check_ok(x.get("command"), x.get("result"), x.get("commit"),
                                         x.get("broke_test")) for x in items]
        except docs_mod.DocError as e:
            h.err(e)

    def record_checks(uid: str, t: dict, request: Request, body: dict,
                      checks: list[tuple[str, str, str, str]]) -> tuple[list[str], str]:
        by, _ = who(request, body)
        lines = []
        for cmd, result, commit, broke in checks:
            store.add_check(uid, t["id"], cmd, result, commit, broke, caller(request)["station"],
                            "", now_utc(), person_of(request))
            lines.append(factory_mod.check_line(cmd, result, commit, broke))
        doc_id = t["working_doc"]
        if doc_id and store.get_doc(uid, doc_id):
            body_now = store.get_doc(uid, doc_id)["body"]
            for line in lines:
                body_now = factory_mod.add_check_to_doc(body_now, line)
            store.update_doc(doc_id, body=body_now, by=by)
        else:
            body_now = f"# {t['title']}\n"
            for line in lines:
                body_now = factory_mod.add_check_to_doc(body_now, line)
            doc_id = store.create_doc(uid, "working", f"Working: {t['title']}", body_now, by=by)
            store.update_ticket(t["id"], working_doc=doc_id)
        return lines, doc_id

    @app.post("/api/projects/{uid}/tickets/{ref}/checks", status_code=201)
    def add_check(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{command, result, commit?, broke_test?}: a check run on the ticket, and `checks`, more
        of the same recorded in this call. Kept, and added as one line each to the Checks part of
        the ticket's working doc (made when there is none)."""
        p = h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        lines, doc_id = record_checks(uid, t, request, body, parse_checks(body))
        return {"line": lines[0], "lines": lines, "working_doc": doc_id,
                "checks": store.ticket_checks(t["id"], 10), "project": p["name"]}

    # the project's own memory (TR-436): what builders learned about this project; TR-865 routes
    # it through the memory integration (memory_route)
    @app.post("/api/projects/{uid}/memory/lines", status_code=201)
    async def remember_for_project(uid: str, request: Request, body: dict = Body(...)):
        """{text}: one dated line in the project's memory (made with the first line). The Tares
        doc takes it first; the cell's memory integration, when it is not Tares, takes it next,
        and `pending` is true when that one did not and the line waits for a retry."""
        h.project_or_404(uid)
        by, _ = who(request, body)
        said = str(body.get("text") or "").strip()
        try:
            res = await memory_route.write(
                store, uid, f"{said} ({now_utc().date().isoformat()}, {by})" if said else "",
                by=by)
        except docs_mod.DocError as e:
            h.err(e)
        h.ensure_globals(uid)
        return {"id": res["doc_id"], "added": res["added"], "pending": res["pending"],
                "body": store.get_doc(uid, res["doc_id"])["body"]}

    @app.get("/api/projects/{uid}/recall")
    async def recall(uid: str, q: str = "", limit: int = 10):
        """The lines of the project's memory and the shared memory that share words with `q`,
        best first, each prefixed [project] or [shared]: {lines, integration, note}."""
        h.project_or_404(uid)
        if not q.strip():
            h.err(ValueError("say what to recall: q is a question or a few words"))
        return await memory_route.recall(store, uid, q, limit)

    # ── review and merge (TR-441..445) ────────────────────────────────────────
    @app.post("/api/projects/{uid}/tickets/{ref}/reviews", status_code=201)
    async def record_review(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{head, verdict, verified, not_verified?, findings?, resolved?}: one review round, the
        verdict on the commit it was given on. `resolved` marks earlier findings ({F1: fixed}).
        Only the reviewer (or a person's own session) records one.

        The answer's `github` says whether Tares also posted the review to the pull request as
        the cell's GitHub App (the comment and the `factory/review` check run, TR-1244): `posted`,
        and `why` when it did not, so the reviewer posts with `gh` as before."""
        rv = await asyncio.to_thread(add_review_round, uid, ref, request, body)
        t = await asyncio.to_thread(ticket_or_404, uid, ref)
        rv["github"] = await post_review_to_github(t, rv, request, body)
        return rv

    async def post_review_to_github(t: dict, rv: dict, request: Request, body: dict) -> dict:
        pr = t.get("pr") or {}
        parsed = factory_github.parse_pr(pr.get("url") or "") if pr.get("url") else None
        repo, number = parsed if parsed else (pr.get("repo"), pr.get("number"))
        if not repo or not number:
            return {"posted": False, "posted_check": False, "posted_comment": False,
                    "why": "the ticket has no linked pull request"}
        by, _ = who(request, body)
        person = h.current_person(request) or {}
        blocking = sum(1 for f in rv["findings"] if f.get("blocking"))
        title = factory_github._VERDICT_WORDS.get(rv["verdict"], rv["verdict"])
        title += f", {blocking} blocking" if blocking else ""
        this_round = {f["label"] for f in rv["findings"]}
        try:
            earlier = [f for f in await asyncio.to_thread(store.open_findings, t["id"])
                       if f["label"] not in this_round]
            comment = factory_github.review_comment(
                rv["verdict"], by, str(person.get("name") or ""), rv.get("verified") or "",
                rv.get("not_verified") or "", rv["findings"], rv["head"],
                fixed=rv.get("fixed") or [], earlier_open=earlier)
            # the check run goes on the full commit when the PR knows it (the reviewer may give a
            # short one)
            full = str(pr.get("head") or "").strip()
            sha = full if factory_mod.same_commit(full, rv["head"]) else rv["head"]
            return await asyncio.wait_for(
                factory_github.post_review(store, repo, int(number), sha, rv["verdict"], comment,
                                           title),
                factory_github.REVIEW_POST_TIMEOUT)
        except asyncio.TimeoutError:
            return {"posted": False, "posted_check": False, "posted_comment": False,
                    "why": "GitHub did not answer in time: check the PR, and post with gh "
                           "what is missing"}
        except Exception as e:   # the record is kept whatever GitHub does
            return {"posted": False, "posted_check": False, "posted_comment": False,
                    "why": f"could not post to GitHub: {type(e).__name__}"}

    def add_review_round(uid: str, ref: str, request: Request, body: dict) -> dict:
        h.project_or_404(uid)
        deny(request, "review")
        t = ticket_or_404(uid, ref)
        try:
            verdict, verified, not_verified, findings, head = factory_mod.review_ok(
                body.get("verdict"), body.get("verified"), body.get("not_verified"),
                body.get("findings"), body.get("head"))
        except docs_mod.DocError as e:
            h.err(e)
        raw = body.get("resolved") or {}
        if not isinstance(raw, dict) or any(v not in ("fixed", "open") for v in raw.values()):
            h.err(ValueError("resolved is {finding: fixed|open}, e.g. {\"F1\": \"fixed\"}"))
        # one spelling for every finding ref (F1, f1, 1, its id); an unknown one is a mistake
        known = {f["id"]: f["label"] for r in store.ticket_reviews(t["id"]) for f in r["findings"]}
        resolved = {}
        for k, v in raw.items():
            m = re.match(r"^[Ff]?(\d{1,6})$", str(k).strip())
            lab = f"F{int(m.group(1))}" if m else known.get(str(k))
            if lab not in known.values():
                h.err(KeyError(f"no finding {k!r} on {factory_mod.label(t)}; it has: "
                               + (", ".join(sorted(known.values())) or "none")), 404)
            resolved[lab] = v
        if verdict == "pass" and any(
                f["blocking"] and resolved.get(f["label"]) != "fixed"
                for f in store.open_findings(t["id"])):
            h.err(ValueError("a pass leaves no blocking finding open: mark the earlier ones "
                             "fixed in `resolved`, or ask for changes"))
        by, _ = who(request, body)
        r = store.add_review(uid, t["id"], (t["pr"] or {}).get("url"), head, verdict, verified,
                             not_verified, by, findings, resolved, factory_mod.finding_kind,
                             now_utc(), person_of(request))
        repo = (t["pr"] or {}).get("repo")
        if repo:
            offer_rules(repo)
        rv = next(x for x in store.ticket_reviews(t["id"]) if x["id"] == r["id"])
        return {**rv, "open": [f["label"] for f in store.open_findings(t["id"])]}

    def queue(keep=None, outside=False) -> list[dict]:
        # the tickets in review, their milestones' other tickets, when each went to review and
        # its last review: one query each, however many are waiting (TR-648). `keep` narrows them.
        # A ticket whose project's review gate is an outside tool is not the crew reviewer's to
        # review (TR-857): it is left out, unless `outside` (the pages that show who has it) asks
        # for it too; its row then names the gate in `outside_gate`.
        hidden = h.hidden_ids()
        gates: dict = {}
        reviewing = [(n, t) for n, t in store.review_tickets()
                     if t["project"] not in hidden and (keep is None or keep(t))]
        warm_gates(store, [t["project"] for _, t in reviewing], gates)
        waiting = [(n, t) for n, t in reviewing
                   if outside or outside_gate(store, t["project"], gates) is None]
        ids = [t["id"] for _, t in waiting]
        events = store.challenge_events_many(
            h.cc_source(), [(t["project"], factory_mod.label(t)) for _, t in waiting])
        siblings = store.milestone_statuses(sorted({t["milestone"] for _, t in waiting
                                                    if t["milestone"]}))
        entered = store.entered_stage(ids, "review")
        lasts = store.last_reviews(ids)
        out = []
        for pname, t in waiting:
            uid = t["project"]
            # it blocks its milestone when it is the last of the milestone's tickets open
            others = [x for x in siblings if x["project"] == uid
                      and x["milestone"] == t["milestone"] and x["id"] != t["id"]]
            blocks = bool(t["milestone"]) and all(
                x["status"] in factory_mod.DONE_STATUSES for x in others)
            since = entered.get(t["id"], t["updated_at"])
            layer = factory_mod.challenger_layer(events[(uid, factory_mod.label(t))])
            v = verdict_now(t, lasts.get(t["id"]))
            out.append({"project": uid, "project_name": pname, "ticket": factory_mod.label(t),
                        "title": t["title"], "holder": t["holder"],
                        "repo": t.get("repo") or (t["pr"] or {}).get("repo"),
                        "pr": (t["pr"] or {}).get("url"), "head": (t["pr"] or {}).get("head"),
                        # the pull request's number, its own title (`title` is the ticket's), what
                        # CI said when Tares last read it (success, failure, pending, none, or
                        # null before a read) and the ticket's milestone (TR-735)
                        "number": (t["pr"] or {}).get("number"),
                        "pr_title": (t["pr"] or {}).get("title"),
                        "ci": (t["pr"] or {}).get("ci"), "milestone": t["milestone"],
                        # the branch it merges into and how many commits that branch has that
                        # the pull request lacks, at Tares's last read (TR-1449; null unread)
                        "base": (t["pr"] or {}).get("base"),
                        "behind_by": (t["pr"] or {}).get("behind_by"),
                        # open or closed on GitHub when Tares last read it, and whether it was
                        # merged: a pull request closed without merging is nobody's (TR-1009)
                        "pr_state": (t["pr"] or {}).get("state"),
                        "pr_merged": bool((t["pr"] or {}).get("merged")),
                        "waiting_since": since,
                        "blocks_milestone": blocks,
                        "layer1_clean": layer["clean"],
                        "outside_gate": (outside_gate(store, uid, gates) or (None, None))[1],
                        "verdict": v})
        now = now_utc()
        for q in out:
            q["waiting_minutes"] = (int((now - q["waiting_since"]).total_seconds() // 60)
                                    if isinstance(q["waiting_since"], datetime) else None)
        out.sort(key=lambda q: (not q["blocks_milestone"], -(q["waiting_minutes"] or 0)))
        return out

    def queue_all(keep=None) -> list[dict]:
        """The queue with the pull requests an outside review tool has too: what the Reviews and
        Home pages split by who has each one (factory_waiting.review_split)."""
        return queue(keep, outside=True)

    @app.get("/api/review-queue")
    def review_queue(request: Request, mine: bool = False):
        """Tickets in review across every project: those blocking their milestone first, then
        the longest waiting (TR-444). `mine=1` keeps the caller's person's tickets (assigned to
        them, or unassigned in a project they lead), for a crew that shares the cell with
        another person's."""
        if not mine:
            return {"queue": queue(), "now": now_utc()}
        me = work_person(request)["id"]
        projects: dict[str, dict | None] = {}
        o = owners_mod.Owners(store)

        def keep(t: dict) -> bool:
            if t["project"] not in projects:
                projects[t["project"]] = store.get_project(t["project"])
            return is_mine(t, me, projects[t["project"]], o)
        return {"queue": queue(keep), "now": now_utc()}

    @app.get("/api/factory/merge-ready")
    async def merge_ready(request: Request, pr: str = ""):
        """May this pull request merge (TR-855)? One answer from the review gate, CI and the
        guardrails of the project its ticket belongs to: {ready, reasons, head, pr, ticket, project,
        review, ci, guardrails}. `pr` is a PR URL or `owner/repo#12`. A bad reference, a pull
        request no ticket has, or a read that fails is `ready` false with a reason, not an
        error. A crew station may read this; nothing is merged."""
        parsed = factory_github.parse_pr(pr)
        t = store.ticket_for_pr(*parsed) if parsed else None
        if t is not None:
            try:
                h.project_or_404(t["project"])
            except HTTPException:
                # a private project the caller is not on answers like a PR no ticket has, so the
                # reply does not say which PRs it tracks
                return merge_ready_mod.no_ticket(*parsed)
        return await merge_ready_mod.merge_ready(store, pr, source=h.cc_source())

    @app.get("/api/factory/release-ready")
    async def release_ready(request: Request, project: str = "", repo: str = "", commit: str = "",
                            milestone: str = "", merged_prs: str = ""):
        """May this commit be shipped (TR-861)? Asks the project's guardrails at the pre_release hook:
        {ready, reasons, warnings, guardrails}. `merged_prs` is a comma separated list. `ready` is false for a
        guardrail that blocks or cannot be reached, and for a missing project or commit. A crew
        station may read this; nothing is shipped or recorded."""
        if not project.strip():
            return {"ready": False, "reasons": ["Name the project to check"], "warnings": [],
                    "guardrails": []}
        p = h.project_or_404(h.resolve_project(project.strip()))
        if not commit.strip():
            return {"ready": False, "reasons": ["Name the commit to ship"], "warnings": [],
                    "guardrails": []}
        gate = await integrations_hooks.gate("pre_release", p["id"], {
            "repo": repo.strip(), "commit": commit.strip(), "milestone": milestone.strip(),
            "merged_prs": [x.strip() for x in merged_prs.split(",") if x.strip()]})
        return {"ready": gate["ok"], "reasons": gate["reasons"], "warnings": gate["warnings"],
                "guardrails": gate["decisions"]}

    # ── the repo map (TR-681): one per GitHub repo, shared by every project that names it ──
    def pr_repo(t: dict) -> str | None:
        """The repo of a ticket's PR, once GitHub has answered for it: a repo typed into a PR link is
        not yet a repo anyone has seen."""
        pr = t.get("pr") or {}
        return pr.get("repo") if pr.get("checked_at") and not pr.get("error") else None

    def repo_of(uid: str, t: dict) -> str | None:
        """The repo a ticket changes: its own, its PR's, else the project's only repo (when it has a
        GitHub name). With several repos, or one without a name, a ticket that names none has none."""
        repo = t.get("repo") or pr_repo(t)
        if repo:
            return repo
        repos = (store.get_handover(uid) or {}).get("repos") or []
        return repos[0].get("name") or None if len(repos) == 1 else None

    def repo_map_md(repo: str | None, builder: bool, known: str = "") -> list[str]:
        if not repo:
            return []
        m = store.get_repo_map(repo)
        if not m:
            if not builder:
                return []
            return [f"## Repo map: {repo}",
                    "There is none yet, and you are the first builder here. Once you know the layout, "
                    "write it with write_repo_map (layout, entry points, how to install, build, test "
                    f"and lint, conventions, the files tickets touch most, known traps; at most "
                    f"{factory_mod.MAP_MAX_CHARS} characters). Every project on this repo then starts "
                    "from it.", ""]
        version = factory_mod.map_version(m["body"])
        if known and known == version:
            return [f"## Repo map: {repo}",
                    f"Version {version}, the one you already read: unchanged. Call review_brief "
                    "without known_map to read it again.", ""]
        L = [f"## Repo map: {repo} (shared by every project on it; version {version}, changed "
             f"{str(m['updated_at'])[:10]} by {m['updated_by']})", m["body"].strip()]
        recent = store.repo_map_history(repo, 3)
        if recent:
            L += ["Recent changes to the map:"] + [f"- {str(x['at'])[:10]} {x['by']}: {x['note']}"
                                                   for x in recent]
        if builder:
            L += ["If your pull request changes the layout or the build, test or lint commands, "
                  f"update the map with write_repo_map(base=\"{version}\") after it merges. If someone "
                  "changed the map meanwhile, the answer is the map now: fold your change into it and "
                  "write again."]
        return L + [""]

    @app.get("/api/repo-map")
    def get_repo_map(repo: str):
        try:
            name = factory_mod.repo_ok(repo)
        except docs_mod.DocError as e:
            h.err(e)
        m = store.get_repo_map(name) if name else None
        return {"repo": name, "map": m, "history": store.repo_map_history(name, 10) if m else [],
                "max_chars": factory_mod.MAP_MAX_CHARS}

    def named_repos() -> set[str]:
        """Every GitHub repo some project names: on its hand-over, or on a ticket (its repo, else its PR's)."""
        out: set[str] = set()
        for p in store.list_projects():
            for r in (store.get_handover(p["id"]) or {}).get("repos") or []:
                if r.get("name"):
                    out.add(r["name"].lower())
            for t in store.list_tickets(p["id"]):
                repo = t.get("repo") or pr_repo(t)
                if repo:
                    out.add(repo.lower())
        return out

    @app.put("/api/repo-map")
    def put_repo_map(request: Request, body: dict = Body(...)):
        """{repo, body, base, note, project?}: replace the repo's map. Builders (and a person's own
        session) write it; `base` is the version of the map they read (none when there was none),
        `note` becomes a dated line in the map's history. The repo must be one a project names."""
        deny(request, "repo_map")
        try:
            name = factory_mod.repo_ok(body.get("repo"))
            if not name:
                raise docs_mod.DocError("say which repo the map is for (owner/name)")
            text, note = factory_mod.map_ok(body.get("body"), body.get("note"))
        except docs_mod.DocError as e:
            h.err(e)
        if name.lower() not in named_repos():
            raise HTTPException(404, f"no project names {name}: hand a project over with that repo, "
                                     "or give one of its tickets that repo, before writing its map")
        by, _ = who(request, body)
        try:
            changed = store.set_repo_map(name, text, note, by, str(body.get("project") or "") or None,
                                         now_utc(), str(body.get("base") or "").strip())
        except MapChanged as e:
            cur = e.current
            if not cur:
                raise HTTPException(409, "there is no map for this repo yet: write it with an empty base")
            raise HTTPException(409, (
                f"the map changed since you read it (now version {factory_mod.map_version(cur['body'])}, "
                f"changed {str(cur['updated_at'])[:10]} by {cur['updated_by']}), or you gave no base. "
                "Nothing was written. Fold your change into this map and write again with that "
                f"version as base:\n\n{cur['body']}"))
        return {"repo": name, "changed": changed, "chars": len(text), "version": factory_mod.map_version(
                    store.get_repo_map(name)["body"]),
                "max_chars": factory_mod.MAP_MAX_CHARS, "history": store.repo_map_history(name, 5)}

    @app.get("/api/projects/{uid}/tickets/{ref}/brief")
    async def review_brief(uid: str, ref: str, head: str = "", known_map: str = ""):
        """Everything the reviewer needs for one review, as markdown (TR-445). `known_map` is the
        repo map version the reviewer already read: an unchanged map comes as one line."""
        p = h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        with metrics.bind_route("brief"):
            with metrics.step("pr_refresh"):
                if await refresh_pr(store, uid, t):    # this ticket's pull request only (TR-1474)
                    t = ticket_or_404(uid, ref)
            with metrics.step("render"):    # what the phases inside brief_md leave over
                return {"brief": brief_md(p, t, head.strip(), known_map.strip())}

    def brief_md(p: dict, t: dict, head: str, known_map: str = "") -> str:
        with metrics.step("ticket_read"):
            full = h.ticket_out(p["id"], t, with_doc=True)
        pr = t["pr"] or {}
        lab = factory_mod.label(t)
        L = [f"# Review brief: {lab} {t['title']} ({p['name']})", ""]
        # 1. GitHub's facts first: stop early when there is nothing to review
        L += ["## The pull request, as GitHub has it"]
        if not pr.get("url"):
            L += ["No pull request is linked to this ticket yet (set_stage review with pr=...).", ""]
        else:
            L += [f"- {pr['url']}, head {str(pr.get('head') or '?')[:12]}, CI {pr.get('ci') or 'unknown'}"
                  + (f", read {pr.get('checked_at')}" if pr.get("checked_at") else "")]
            if pr.get("error"):
                L += [f"- Could not read it fresh: {pr['error']}"]
            if pr.get("merged"):
                L += ["", "STOP: the pull request is already merged. There is nothing to review."]
                return "\n".join(L)
            hd, now_head = head.lower(), str(pr.get("head") or "").lower()
            if len(hd) >= 7 and now_head and not (now_head.startswith(hd) or hd.startswith(now_head)):
                L += ["", f"STOP: the head moved. You were asked about {head[:12]}, the PR is now "
                          f"at {pr['head'][:12]}. Review the new head and say so."]
                return "\n".join(L)
            L.append("")
        # 2. what was asked
        repo = t.get("repo") or pr.get("repo")
        L += ["## The ticket and its working doc",
              f"Milestone: {full.get('milestone_name') or 'none'}. Held by {t['holder'] or 'nobody'}."
              + (f" Repo: {repo}." if repo else ""),
              "", (full.get("working_doc_body") or "(no working doc)").strip(), ""]
        # 3. the builder's claims against the recording, unmatched first
        checks = sorted(full.get("checks") or [], key=lambda c: c.get("match") == "matched")
        L += ["## The builder's checks, against the recording"]
        if not checks:
            L += ["The builder recorded no checks: verify everything yourself."]
        for c in checks:
            mark = {"matched": "matched", "not_found": "NOT IN THE RECORDING",
                    "differs": "RECORDING DIFFERS"}.get(c.get("match") or "", "not checked")
            L += [f"- [{mark}] `{c['command']}` -> {c['result']}"
                  + (f" (broke {c['broke_test']})" if c.get("broke_test") else "")
                  + (f". Recording: {c['recorded']}" if c.get("recorded") else "")]
        L.append("")
        # 4. layer 1: Codex
        layer = full.get("challenger") or {}
        L += ["## Codex (the builder's challenger)"]
        if not layer.get("rounds"):
            L += ["No Codex review is recorded for this ticket's branch."]
        else:
            L += [f"{layer['rounds']} rounds, last: {layer.get('verdict')}"
                  + (f" on {str(layer.get('sha'))[:7]}" if layer.get("sha") else "")]
            L += [f"- open [{f.get('priority')}] {f.get('title')}" for f in layer.get("open") or []]
            L += [f"- fixed [{f.get('priority')}] {f.get('title')}" for f in layer.get("fixed") or []]
            L += [f"- WAIVED [{f.get('priority')}] {f.get('title')}: "
                  f"{f.get('reason') or 'no reason given'} (judge this waiver)"
                  for f in layer.get("waived") or []]
        L.append("")
        # 5. earlier rounds
        reviews = full.get("reviews") or []
        L += ["## Earlier review rounds"]
        if not reviews:
            L += ["This is the first review."]
        for r in reviews:
            L += [f"- Round {r['round']} on {r['head'][:7]}: {r['verdict']}"]
            L += [f"  - {f['label']} [{f['severity']}{', blocking' if f['blocking'] else ''}] "
                  f"{f['state']}: {f['text']}" + (f" ({f['file']}:{f['line']})" if f.get("file") else "")
                  for f in r["findings"]]
        L.append("")
        # 6. assumptions and decisions on it
        if full.get("assumptions"):
            L += ["## Assumptions made on this ticket"]
            L += [f"- {a['label']} ({a['state']}): {a['question']} -> {a['choice']}"
                  for a in full["assumptions"]] + [""]
        with metrics.step("decisions"):
            ds = [d for d in store.list_decisions(p["id"], all_=True)
                  if lab in (d.get("tickets") or []) or not d.get("tickets")][:10]
        if ds:
            L += ["## Decisions the person made on this project"]
            for d in ds:   # the answer chosen, then the person's words when they add something
                dw = factory_mod.decision_words(d)
                L.append(f"- {d.get('question') or ''}: {dw['answer']}"
                         + (f" (they said: \"{dw['said']}\")" if dw["said"] else ""))
            L += [""]
        # 7. recurring findings in this repo: check these first
        if pr.get("repo"):
            with metrics.step("recurring"):
                rec = factory_mod.recurring_findings(store, pr["repo"], now_utc(), h.hidden_ids())
            if rec:
                L += [f"## What keeps coming back in {pr['repo']} (check these first)"]
                L += [f"- {r['kind']}: {r['count']} tickets ({', '.join(r['tickets'][:5])})" for r in rec]
                L.append("")
        # 8. the repo map: read it instead of exploring the repo
        with metrics.step("repo_map"):
            L += repo_map_md(repo_of(p["id"], t), builder=False, known=known_map)
        # 8. links, not inlined
        L += ["## Read as needed",
              "- The spec, AGENTS.md (the project's and the shared one) and the project memory: "
              "list_docs, then get_doc(id).", "- The crew's messages about it: get_ticket."]
        return "\n".join(L)

    # ── the builder brief: one read at the start of a ticket and after a revive (TR-683) ──
    @app.get("/api/projects/{uid}/tickets/{ref}/builder-brief")
    async def builder_brief(uid: str, ref: str, request: Request, start: bool = False):
        """Everything a builder needs to start or resume one ticket, as markdown. `start` also
        moves a ticket that has not been started (no stage, or todo, and not done or canceled) to doing; a ticket already
        past that (review, changes, blocked, merged) keeps its stage."""
        p = h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        with metrics.bind_route("builder_brief"):
            with metrics.step("pr_refresh"):
                if await refresh_pr(store, uid, t):    # this ticket's pull request only (TR-1474)
                    t = ticket_or_404(uid, ref)
            note = ""
            if start and (t["stage"] or "todo") == "todo" and not factory_mod.finished(t):
                try:
                    await set_stage(uid, ref, request, {"stage": "doing"})
                except HTTPException as e:
                    note = f"Could not move the ticket to doing: {e.detail}\n\n"
                t = ticket_or_404(uid, ref)
            with metrics.step("render"):    # what the phases inside builder_brief_md leave over
                return {"brief": note + builder_brief_md(p, t)}

    def builder_brief_md(p: dict, t: dict) -> str:
        uid, lab = p["id"], factory_mod.label(t)
        with metrics.step("ticket_read"):
            full = h.ticket_out(uid, t, with_doc=True)
        pr = t["pr"] or {}
        repo = repo_of(uid, t)
        L = [f"# Builder brief: {lab} {t['title']} ({p['name']})", ""]
        # 1. where it stands
        L += ["## Where it stands",
              f"Stage {t['stage'] or 'none'}" + (f" ({t['stage_reason']})" if t.get("stage_reason") else "")
              + f". Held by {t['holder'] or 'nobody'}." + (f" Repo: {repo}." if repo else "")
              + (f" Link: {t['url']}." if t.get("url") else "")]
        if pr.get("url"):
            L += [f"- Pull request {pr['url']}, head {str(pr.get('head') or '?')[:12]}, "
                  f"CI {pr.get('ci') or 'unknown'}" + (", merged" if pr.get("merged") else "")]
        if full.get("pr_note"):
            L += [f"- {full['pr_note']}"]
        v = full.get("verdict") or {}
        if v.get("verdict"):
            L += [f"- Reviewer's verdict: {v['verdict']}" + (f". {v['note']}" if v.get("note") else "")]
        L.append("")
        # 2. what was asked: the ticket's working doc
        L += ["## The ticket and its working doc",
              (full.get("working_doc_body") or "(no working doc)").strip(), ""]
        # 3. the milestone it belongs to and the checks that close it
        with metrics.step("milestone"):
            ms = store.get_milestone(uid, t["milestone"]) if t.get("milestone") else None
        L += [f"## Milestone: {ms['name']}" if ms else "## Milestone", ]
        if ms:
            L += ([ms["goal"]] if ms["goal"] else [])
            L += [f"- Acceptance check: `{c.get('check')}` should show: {c.get('expect')}"
                  for c in ms["checks"]] or ["No acceptance checks written."]
        else:
            L += ["The ticket is in no milestone."]
        L.append("")
        # 4. what it depends on
        with metrics.step("dependencies"):
            by_id = {x["id"]: x for x in store.list_tickets(uid)}
            deps = [by_id[d] for d in t["depends_on"] if d in by_id]
        L += ["## Dependencies"]
        if not deps:
            L += ["None."]
        for d in deps:
            dpr = d["pr"] or {}
            L += [f"- {factory_mod.label(d)} {d['title']}: {d['stage'] or d['status']}"
                  + (f", {dpr['url']}" if dpr.get("url") else "")
                  + ("" if factory_mod.delivered(d) else
                     " (CANCELED: it delivered nothing and still blocks this ticket)"
                     if d["status"] == "canceled" else " (NOT DONE: it blocks this ticket)")]
        L.append("")
        # 5. the rules: the project's AGENTS.md wins over the generic flow, then the shared one
        with metrics.step("agents_docs"):
            gids = h.global_ids(False) or {}
            agents = store.list_docs(uid, "agents")
            mine = [d for d in agents if d["id"] not in gids.values()]
            shared = [d for d in agents if d["id"] == gids.get("agents")]
            for title, docs in (("AGENTS.md of this project (wins over the generic flow)", mine),
                                ("AGENTS.md shared by every project", shared)):
                for d in docs:
                    L += [f"## {title}", (store.get_doc(uid, d["id"])["body"] or "").strip(), ""]
        if not (mine or shared):
            L += ["## AGENTS.md", "This project has none.", ""]
        with metrics.step("repo_map"):
            L += repo_map_md(repo, builder=True)
        # 6. what is open on it: assumptions to honour, questions waiting for the person
        asm = [a for a in full.get("assumptions") or [] if a["state"] != "kept"]
        if asm:
            L += ["## Assumptions on this ticket (open, or overturned: follow the person's words)"]
            L += [f"- {a['label']} ({a['state']}): {a['question']} -> {a['choice']}"
                  + (f". The person: \"{a['words']}\"" if a.get("words") else "") for a in asm] + [""]
        with metrics.step("desk"):
            desk = [d for d in store.list_desk("open", uid) if lab in (d.get("tickets") or [])]
        if desk:
            L += ["## Questions on the desk about this ticket"]
            L += [f"- {d['label']}{' (blocking)' if d['blocking'] else ''}: {d['question']}. "
                  f"Recommended: {d['recommendation']}" for d in desk] + [""]
        # 7. where you left off: for a revive, and a ticket sent back
        checks = full.get("checks") or []
        layer = full.get("challenger") or {}
        reviews = full.get("reviews") or []
        if checks or layer.get("rounds") or reviews:
            L += ["## Where you left off"]
            L += [f"- Check recorded: `{c['command']}` -> {c['result']}"
                  + (f" (broke {c['broke_test']})" if c.get("broke_test") else "") for c in checks[-8:]]
            if layer.get("rounds"):
                L += [f"- Codex: {layer['rounds']} rounds, last {layer.get('verdict')}, "
                      f"{len(layer.get('open') or [])} open, {len(layer.get('waived') or [])} waived"]
            for r in reviews[-2:]:
                L += [f"- Review round {r['round']} on {r['head'][:7]}: {r['verdict']}"]
                L += [f"  - {f['label']} [{f['severity']}] {f['state']}: {f['text']}"
                      for f in r["findings"] if f["state"] == "open"]
            L.append("")
        with metrics.step("protocol_messages"):
            msgs = messages_for(p, t)[-5:]
        if msgs:
            L += ["## The crew's latest messages about it"]
            L += [f"- {m['from'] or '?'} -> {m['to']}: [TF:{m['type']}] {m['first_line']}" for m in msgs]
            L.append("")
        # 8. links, not inlined
        L += ["## Read as needed",
              "- The spec, plan and the project memory: list_docs, then get_doc(id).",
              "- The ticket's full history: get_ticket."]
        return "\n".join(L)

    # ── recurring findings and rules offered from them (TR-443) ───────────────
    def offer_rules(repo: str) -> None:
        for r in factory_mod.recurring_findings(store, repo, now_utc()):
            scope = "all" if len(r["projects"]) > 1 else "project"
            pid = r["projects"][0] if scope == "project" else None
            text = rule_text(repo, r["kind"], r["count"])
            store.upsert_rule_proposal(repo, r["kind"], pid, scope, text, r["tickets"], now_utc())

    @app.get("/api/recurring-findings")
    def recurring_findings(repo: str):
        return {"repo": repo,
                "recurring": factory_mod.recurring_findings(store, repo.strip(), now_utc(), h.hidden_ids())}

    @app.get("/api/rule-proposals")
    def list_rule_proposals(state: str = "open"):
        # a rule that came back across projects lists the tickets it came from
        return {"proposals": readable_offers(
            h, store.list_rule_proposals(state.strip() or None), h.hidden_ids())}

    @app.post("/api/rule-proposals/{pid}/{decision}")
    def decide_rule(pid: str, decision: str, request: Request,
                          body: dict = Body(default={})):
        """accept (optionally with the text edited) adds the rule to AGENTS.md (the project's,
        or the shared one when it came back across projects); reject keeps it from being
        offered again. A person decides: refused from any crew station. The offer's own sentence,
        left as it was, is written without its ticket count unless every ticket behind it is in a
        project the whole team can open (TR-1724)."""
        if caller(request)["station"]:
            h.err(PermissionError("a rule is the person's call: accept or reject it in the "
                                  "console"), 403)
        rp = store.get_rule_proposal(pid)
        hidden = h.hidden_ids()
        mine = readable_offers(h, [rp], hidden) if rp else []
        if not mine:
            h.err(KeyError(f"no rule proposal {pid!r}"), 404)   # one the caller cannot read is none
        if rp["state"] != "open":
            h.err(ValueError(f"already {rp['state']}"), 409)

        def decided() -> dict:   # the offer as it is now, as this caller may read it
            return readable_offers(h, [store.get_rule_proposal(pid)], hidden)[0]

        if decision == "reject":
            store.decide_rule_proposal(pid, "rejected", None, now_utc())
            factory_home.clear_cache()   # the Crew page reads again right after
            return decided()
        if decision != "accept":
            h.err(KeyError("accept or reject"), 404)
        text = " ".join(str(body.get("text") or "").split())
        # nothing typed: no text, or the offer's own sentence sent back, as stored or as read
        if not text or text in {" ".join(str(o["text"]).split()) for o in (rp, mine[0])}:
            text = " ".join(str(rp["text"]).split())
            tickets = rp.get("tickets") or []
            team = tickets_of(tickets, store.list_projects(), lambda p: p.get("scope") == "private")
            if len(team) < len(tickets) and text == rule_text(rp["repo"], rp["kind"], len(tickets)):
                text = rule_text(rp["repo"], rp["kind"])
        if rp["scope"] == "project" and not (rp["project"] and store.get_project(rp["project"])):
            h.err(ValueError("the project this rule was for is gone; reject it"), 409)
        if rp["scope"] == "project":
            doc = next((d for d in store.list_docs(rp["project"], "agents")
                        if d["id"] not in h.global_ids(False).values()), None)
            doc_id = doc["id"] if doc else store.create_doc(
                rp["project"], "agents", "AGENTS.md", "# AGENTS.md\n\n", by="console")
        else:
            doc_id = h.global_ids(True)["agents"]
        cur = store.get_doc(None, doc_id)["body"]
        new, _ = docs_mod.append_line(cur, text)
        store.update_doc(doc_id, body=new, by="console")
        store.decide_rule_proposal(pid, "accepted", text, now_utc())
        factory_home.clear_cache()
        return {**decided(), "doc": doc_id}

    # ── the desk: questions for the person, and their answers (TR-449..452) ────
    def desk_out(d: dict) -> dict:
        p = store.get_project(d["project"]) if d["project"] else None
        return {**d, "project_name": p["name"] if p else None,
                "project_finished": bool(p and p.get("status") == "done")}

    def desk_target(request: Request, to, uid: str | None, tickets: list[str]) -> str | None:
        """The person a desk item is addressed to (TR-634): `to` (a person id or an email on this
        cell) when given, else the assignee of the first ticket named that has one, else the
        project's lead, else the caller's own person. None only when the caller has no person
        and is not the system credential (a project key): the item then keeps the older rule."""
        said = str(to or "").strip()
        if said:
            person = store.get_person(said) or store.find_person_by_email(said)
            if person is None:
                h.err(KeyError(f"no person {said!r} on this cell: give a person id or an email "
                               "(list_people shows who is here)"), 404)
            if not team_mod.enabled():
                # addressing a second person is a team feature; the caller, or `local` on a
                # cell of one (a key nobody bound acts as them), is not
                caller = person_of(request) or ("local" if h.is_system(request) else None)
                if person["id"] != caller and not (
                        person["id"] == "local" and store.only_local_person()):
                    h.licence_required(team_mod.ADDRESS_REASON)
            project = store.get_project(uid) if uid else None
            if (project and project.get("scope") == "private"
                    and not store.is_project_member(uid, person["id"])):
                # the question would reach someone who cannot see the project
                h.err(PermissionError("that person is not on this private project: address "
                                      "the item to someone who is, or ask the project lead to "
                                      "add them"), 403)
            return person["id"]
        if uid:
            by_ref = {factory_mod.label(t): t for t in store.list_tickets(uid)}
            for ref in tickets:
                if (by_ref.get(ref) or {}).get("assignee_person_id"):
                    return by_ref[ref]["assignee_person_id"]
            lead = (store.get_project(uid) or {}).get("lead_person_id")
            if lead:
                return lead
        me = person_of(request)
        return me or ("local" if h.is_system(request) else None)

    def close_project_desk(uid: str) -> None:
        """A finished project's open questions have nothing left to decide (TR-559)."""
        for d in store.list_desk("open", uid):
            store.close_desk_item(d["id"], "withdrawn", "the project is finished", now_utc())

    @app.post("/api/desk", status_code=201)
    def desk_add(request: Request, body: dict = Body(...)):
        """{question, recommendation, why, options?, context?, blocks?, tickets?, blocking?,
        assumptions?, project?, station?, command?, to?}: a question for the person, numbered D1,
        D2... for the whole cell. `to` is the person it is addressed to (an id or an email): it
        shows in their my_work, and only words found in their own recorded sessions answer it.
        Without `to` it goes to the assignee of the first ticket named, else the project's lead,
        else the caller's own person. With `command` (and the `station` that was refused it) it is a
        permission request, which the person can allow from the console. Only the orchestrator
        (or a person's own session) writes the desk."""
        deny(request, "desk")
        try:
            q, rec, why, opts, ctx, station, command = factory_mod.desk_ok(
                body.get("question"), body.get("recommendation"), body.get("why"),
                body.get("options"), body.get("context"), body.get("station"),
                body.get("command"))
        except docs_mod.DocError as e:
            h.err(e)
        uid = None
        if str(body.get("project") or "").strip():
            uid = h.project_or_404(h.resolve_project(str(body["project"]).strip()))["id"]
        tickets = []
        if uid:
            tickets = [factory_mod.label(ticket_or_404(uid, r))
                       for r in factory_mod.depends_ok(body.get("tickets"))]
        asm = []
        for a in body.get("assumptions") or []:
            got = store.get_assumption(uid, str(a)) if uid else None
            if got is None:
                h.err(KeyError(f"no assumption {a!r} in that project"), 404)
            asm.append(got["label"])
        by, _ = who(request, body)
        to = desk_target(request, body.get("to"), uid, tickets)
        stuck = [x for lb in tickets for x in stuck_items(uid, lb)]
        d = store.add_desk_item(uid, q, ctx, opts, rec, why,
                                " ".join(str(body.get("blocks") or "").split())[:300], tickets,
                                bool(body.get("blocking")), str(body.get("asked_by") or by)[:120],
                                asm, now_utc(), station or next((x["station"] for x in stuck
                                                                 if x.get("station")), None),
                                command, person_of(request), to)
        for x in stuck:     # the station's own question says more than "is stuck"
            store.close_desk_item(x["id"], "withdrawn", f"asked in {d['label']}", now_utc())
        return desk_out(d)

    @app.post("/api/factory/challenger/stopped")
    def challenger_stopped(request: Request, body: dict = Body(...)):
        """{reviewer, reason}: a station's commit review could not run and won't until someone acts
        (out of quota, signed out). Any station may say so, unlike the rest of the desk, because
        it is the builder's commit that finds out. Puts one open item on the person's desk, however
        many commits report it; the answer is the item the person already has."""
        reason = " ".join(str(body.get("reason") or "").split())[:500]
        if not reason:
            h.err(ValueError("reason is required"))
        reviewer = " ".join(str(body.get("reviewer") or "The reviewer").split())[:40]
        question = f"Commit reviews have stopped: {reviewer} cannot run."
        to = desk_target(request, None, None, [])
        for d in store.list_desk("open", None):
            if d["question"] == question and d.get("to_person_id") == to:
                return desk_out(d)
        d = store.add_desk_item(None, question, "", [], f"Sort out {reviewer} so reviews can run again.",
                                f"The crew keeps working, but its commits are not being reviewed. {reason}",
                                "", [], False, "the challenger", [], now_utc(), None, None,
                                person_of(request), to)
        return desk_out(d)

    @app.get("/api/desk")
    def desk_list(request: Request, state: str = "open", project: str = "",
                        mine: bool = False):
        """The desk, blockers first, then oldest. `mine=1` keeps the items addressed to the
        caller's person (TR-634)."""
        st = state.strip().lower() or None
        if st and st not in factory_mod.DESK_STATES:
            h.err(ValueError("state is one of: " + ", ".join(factory_mod.DESK_STATES)))
        uid = h.project_or_404(h.resolve_project(project.strip()))["id"] if project.strip() else None
        hidden = h.hidden_ids()
        me = work_person(request)["id"] if mine else None
        o = owners_mod.Owners(store)
        return {"desk": [desk_out(d) for d in store.list_desk(st, uid)
                         if d["project"] not in hidden and (me is None or desk_is_mine(d, me, o))]}

    @app.get("/api/desk/answered")
    def desk_answered(request: Request, since: str = "", mine: bool = False):
        """{count, latest}: how many desk answers closed after `since` (a `closed_at` the caller has
        already seen; empty counts every answer), and the latest `closed_at` among them (`since` when
        none). Only the answers the orchestrator's idle wake looks at (a permission grant is not one).
        The watch asks this every few seconds, so a person's answer in the console reaches the
        orchestrator without waiting for the round. `mine=1` as on /api/desk."""
        hidden = h.hidden_ids()
        me = work_person(request)["id"] if mine else None
        o = owners_mod.Owners(store)
        try:
            floor = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
            if floor is not None and floor.tzinfo is None:
                floor = floor.replace(tzinfo=timezone.utc)
        except ValueError:
            floor = None   # not a time: the store narrows nothing, the text compare below decides
        # the store keeps the answers closed at or after `since`; the text compare below is the exact one,
        # on what /api/desk sends, which is what the watch keeps as its baseline
        fresh = [t for d in store.list_desk("answered", None, closed_since=floor)
                 if d["project"] not in hidden and (me is None or desk_is_mine(d, me, o))
                 and d.get("command") is None and d.get("closed_at") is not None
                 and (t := str(jsonable_encoder(d["closed_at"]))) > since]
        return {"count": len(fresh), "latest": max(fresh, default=since)}

    @app.get("/api/answers")
    def answers_list(request: Request, since: str = "", mine: bool = False, item: str = ""):
        """The answers people gave to what waited for them since `since` (default: the start of
        today, UTC), newest first (TR-1451): each the home item it answered (`item`), its ask as it
        read then, yes or no where it had two ways, the words or option given, who, when and where
        (`via`: console, feed, session, message). An answer about a project the caller cannot see
        is left out. `mine=1` keeps the caller's own. `item=<home item id>` keeps the answers to
        that item, of any day unless `since` is given (TR-1459: a link to an item opened later says
        who answered it); a desk item matches under any of its ids (`question:`, `permission:`,
        `desk:`)."""
        try:
            floor = datetime.fromisoformat(since.replace("Z", "+00:00")) if since.strip() else None
        except ValueError:
            h.err(ValueError("since is a time, like 2026-11-18T00:00:00Z"))
        items = answers_mod.item_ids(item) if item.strip() else None
        if floor is None:
            floor = (datetime(1970, 1, 1, tzinfo=timezone.utc) if items
                     else now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
        elif floor.tzinfo is None:
            floor = floor.replace(tzinfo=timezone.utc)
        me = answerer(request)
        if mine and me is None:
            return {"answers": []}
        # hidden projects and `mine` filter in the query, before its limit (a busy cell's other answers
        # must not push the caller's own out of the 500 it reads). The caller's own grant answer on a
        # project hidden from them (a grant over a repo they own) stays, its ask already kept to what
        # they saw, and names no project; their other answers there do not (their ids and asks would)
        hidden = h.hidden_ids()
        rows = store.list_answers(floor, hidden=sorted(hidden), person=me if mine else None, own=me,
                                 items=items)
        return {"answers": [{"id": r["item"], "kind": r["kind"], "ask": r["ask"],
                             "project": None if r["project"] in hidden else r["project"],
                             "choice": r["choice"], "said": r["said"], "via": r["via"], "at": r["at"],
                             "by": (r.get("person") or {}).get("name"),
                             "mine": me is not None and (r.get("person") or {}).get("id") == me}
                            for r in rows]}

    def desk_or_404(ref: str) -> dict:
        d = store.get_desk_item(ref)
        # an item of a project the caller cannot see does not exist for them (TR-629)
        if d is None or (d["project"] and d["project"] in h.hidden_ids()):
            h.err(KeyError(f"no desk item {ref!r}"), 404)
        return d

    @app.post("/api/desk/{ref}/withdraw")
    def desk_withdraw(ref: str, request: Request, body: dict = Body(default={})):
        deny(request, "desk")
        d = desk_or_404(ref)
        if d["state"] != "open":
            h.err(ValueError(f"{d['label']} is already {d['state']}"), 409)
        store.close_desk_item(d["id"], "withdrawn",
                              " ".join(str(body.get("reason") or "").split())[:500] or None,
                              now_utc())
        return desk_out(desk_or_404(d["id"]))

    def standing_grant(d: dict, giver: str | None, scope: str, grant_kind: str,
                       request: Request, may_ask_lead: bool = False) -> tuple:
        """What a standing answer becomes (TR-634): (kind, project, areas, needs_lead) for
        put_grant, given by the person who answered (`giver`, the item's person), by the same rules as POST /api/grants:
        a team grant (scope all, or an item with no project) needs an admin; a project grant, the
        project's lead, an admin or the owner of every repo it reaches into; personal, a person,
        for themselves. The repos are those of the item's tickets; one owned by someone else
        makes the grant wait for them, and one from a person who is neither lead nor admin waits
        for the lead too. `may_ask_lead` (the console's Allow, TR-1651): with an enterprise
        licence a project grant from someone who may not give it is not refused, it waits for the
        lead (an admin when there is none) and the owners of its repos."""
        person = store.get_person(giver) if giver else None
        admin = bool(person["is_admin"]) if person else h.is_admin(request)
        if grant_kind == "personal":
            if giver is None:
                h.err(PermissionError("a personal grant belongs to a person, and this answer has "
                                      "none"), 403)
            uid = d["project"] if scope == "project" else None
            return "personal", uid, [], False
        if scope == "all" or not d["project"]:
            if not admin:
                other = ("an admin has to allow it" if may_ask_lead
                         else "this answer can be a project grant, or an admin gives it")
                h.err(PermissionError(f"a grant for every project needs an admin: {other}"), 403)
            return "team", None, [], False
        areas = []
        if team_mod.enabled():   # without a team nobody owns a repo, so a repo is nothing to wait for
            by_ref = {factory_mod.label(t): t for t in store.list_tickets(d["project"])}
            for ref in d["tickets"]:
                try:
                    areas.append(factory_mod.norm_repo((by_ref.get(ref) or {}).get("repo")))
                except ValueError:
                    pass
            areas = list(dict.fromkeys(areas))
        p = store.get_project(d["project"]) or {}
        leads_it = bool(giver and p.get("lead_person_id") == giver)
        owns_all = bool(areas) and giver is not None and all(
            (h.area_owner(a) or {}).get("id") == giver for a in areas)
        # without a team a grant waits for no one (it would take effect), so it is refused there
        asks_lead = may_ask_lead and giver is not None and team_mod.enabled()
        if not (admin or leads_it or owns_all or asks_lead):
            h.err(PermissionError("a project grant comes from the project's lead, or from the "
                                  "owner of the repos it names"), 403)
        return "project", d["project"], areas, not (admin or leads_it)

    def console_via(request: Request) -> str | None:
        """"console" for a person acting on the page (no station header), else None. A crew
        session always sends its station header; an unlabelled Claude session holding a person's
        key looks the same as the console, which is the accepted risk of `desk_allow` too."""
        return None if caller(request)["station"] else "console"

    def parse_answer(d: dict, body: dict) -> dict:
        """The fields of a desk answer, checked: {words, scope, grant_kind, choice, letter,
        standing, kept, over}. Shared by the session route (/answer) and the console's (/reply)."""
        words = str(body.get("words") or "").strip()
        if not words:
            h.err(ValueError("words: the person's reply exactly as they typed it"))
        if len(words) > 4000:
            h.err(ValueError("keep the words to 4000 characters"))
        scope = str(body.get("scope") or "project").strip().lower()
        if scope not in ("project", "all"):
            h.err(ValueError("scope is project or all"))
        if scope == "all" and d["project"] and not factory_mod.says_every_project(words):
            h.err(ValueError("a project's question is answered for that project; it holds for "
                             "every project only when the person says so"))
        grant_kind = str(body.get("grant_kind") or "project").strip().lower()
        if grant_kind not in ("project", "personal"):
            h.err(ValueError("grant_kind is project or personal"))
        choice = str(body.get("choice") or "").strip()
        # a reply of one letter picks an option by position; Tares maps it, never the model
        letter = factory_mod.option_letter(words)
        if letter is not None:
            opts = d["options"] or []
            if letter >= len(opts):
                h.err(ValueError(
                    f"{words} is not one of {d['label']}'s options"
                    + (": " + ", ".join(f"{factory_mod.OPTION_LETTERS[i]}) {o}"
                                        for i, o in enumerate(opts)) if opts else "; it has none")))
            picked = opts[letter]
            if choice and factory_mod._plain(choice) not in (
                    factory_mod._plain(picked), factory_mod._plain(words)):
                h.err(ValueError(f"{words} is {factory_mod.OPTION_LETTERS[letter]}) {picked}, "
                                 f"not {choice!r}"))
            choice = picked
        elif d["options"]:
            # an item with options always records the option chosen (TR-1018): "D85 A", "B - next
            # week" or the option's own words map to it; the orchestrator's own full choice (a
            # refinement such as "keep all but change A31: ...") is kept as given
            if choice and body.get("own"):
                # the person's own words from the console's text box: an option only when they are
                # its words exactly, never one they start (or cut short), so "Red" stays "Red"
                choice = next((o for o in d["options"]
                               if factory_mod._plain(o) == factory_mod._plain(choice)), choice)
            elif choice:
                # "A", or an option's text cut short, becomes the option; a longer refinement stays
                named = factory_mod.picked_option("", choice, d["options"])
                bare = re.sub(r"^\(?[a-h][).:]\s*", "", factory_mod._plain(choice))
                if named is not None and len(bare) <= len(factory_mod._plain(named)):
                    choice = named
            elif (picked := factory_mod.picked_option(words, "", d["options"])) is not None:
                choice = picked
            else:
                h.err(ValueError(
                    f"say which of {d['label']}'s options this answer picks: pass choice, the "
                    "option's full text (or the person's own answer when they chose none of them). "
                    "Its options: " + ", ".join(f"{factory_mod.OPTION_LETTERS[i]}) {o}"
                                                for i, o in enumerate(d["options"]))))
        standing = bool(body.get("standing"))
        # a letter stands for the option it picked: a standing answer keeps that option, never "A"
        grant_words = choice if letter is not None else words
        if standing:
            try:
                factory_mod.grant_line(grant_words, now_utc().date().isoformat())
            except docs_mod.DocError as e:
                h.err(e)
        over = body.get("overturned") or {}
        if not isinstance(over, dict):
            h.err(ValueError("overturned is {A<n>: the person's words}"))
        return {"words": words, "scope": scope, "grant_kind": grant_kind, "choice": choice,
                "letter": letter, "grant_words": grant_words,
                "standing": standing, "kept": [str(a) for a in body.get("kept") or []], "over": over}

    def assumption_plan(d: dict, kept: list[str], over: dict) -> list[tuple]:
        """An assumption batch: every assumption of the item kept or overturned, with the
        person's words for an overturned one. [(assumption, "kept"|"overturned", words)]."""
        plan = []
        for ref_a, st, w in [(a, "kept", None) for a in kept] + [
                (a, "overturned", str(w)) for a, w in over.items()]:
            a = store.get_assumption(d["project"], ref_a) if d["project"] else None
            if a is None or a["label"] not in d["assumptions"]:
                h.err(KeyError(f"{ref_a!r} is not one of {d['label']}'s assumptions: "
                               + (", ".join(d["assumptions"]) or "it has none")), 404)
            if a["state"] != "open":
                h.err(ValueError(f"{a['label']} was already {a['state']}"), 409)
            plan.append((a, st, w))
        # one already decided elsewhere (the project's calls page) has nothing left to say
        still_open = {x for x in d["assumptions"]
                      if (s := store.get_assumption(d["project"], x)) is not None and s["state"] == "open"}
        missing = still_open - {a["label"] for a, _, _ in plan}
        if missing:
            h.err(ValueError("the answer must keep or overturn every assumption of "
                             f"{d['label']}; left out: " + ", ".join(sorted(missing))))
        return plan

    def record_answer(d: dict, a: dict, plan: list[tuple], giver: str | None, grant,
                      by: str, matched: bool, session: str | None, via: str | None,
                      person_id: str | None, request: Request | None = None) -> dict:
        """Write a desk answer that has been accepted: the grant a standing answer becomes, the
        assumptions it decides, the decision, and the item closed. The response of /answer and
        /reply."""
        granted = False
        row = None
        if grant:
            kind, grant_project, areas, needs_lead = grant
            row, _ = put_grant(kind, a["grant_words"], grant_project, giver, areas, needs_lead, by)
            granted = row["state"] == "active"   # one waiting for consent is not in effect yet
        changed = []
        for asm, st, w in plan:
            store.decide_assumption(asm["id"], st, w, d["label"], now_utc())
            changed.append({"label": asm["label"], "state": st, "tickets": asm["tickets"]})
        dec = store.add_decision(d["project"], d["id"], d["question"], a["words"],
                                 a["choice"][:300], a["standing"], a["scope"],
                                 matched, session, granted,
                                 d["tickets"], now_utc(), via=via, person_id=person_id)
        store.close_desk_item(d["id"], "answered", None, now_utc())
        # the record of the answer (TR-1451): matched in a session, or given in the console or feed
        kind = "permission" if d.get("command") else "question"
        answers_mod.record(
            store, request, item=f"{kind}:{d['label']}", kind=kind, project=d["project"],
            ask=(waiting_mod.ask_permission(d["command"]) if d.get("command")
                 else waiting_mod.ask_question(d["question"])),
            said=a["choice"] or a["words"], via="session" if matched else None,
            person_id=person_id)
        by_id = {t["id"]: t for t in store.list_tickets(d["project"])} if d["project"] else {}
        follow = [{"assumption": c["label"],
                   "tickets": [factory_mod.label(by_id[x]) for x in c["tickets"] if x in by_id]}
                  for c in changed if c["state"] == "overturned"]
        return {"desk_item": desk_out(desk_or_404(d["id"])), "decision": dec,
                "matched": matched, "recorded": True, "granted": granted,
                "grant": grants_view([row])[0] if grant else None, "assumptions": changed,
                "follow_up": follow, "unheard": []}

    @app.post("/api/desk/{ref}/answer")
    async def desk_answer(ref: str, request: Request, body: dict = Body(...)):
        """{words, choice?, standing?, scope?: project|all, grant_kind?: project|personal, kept?:
        [A<n>], overturned?: {A<n>: words}}: the person's answer, their words as typed. Tares
        looks for those words in what the person the item is addressed to typed in their own
        recorded sessions, whichever station or session that was (TR-634). Words not found yet
        are a retryable error: the recording can take a minute to arrive. An item from before
        items were addressed is matched as before: in the orchestrator's session, or from a
        person's own session in any session. A standing answer that matches becomes a grant
        given by that person: a project grant (the default; `scope` all makes it a team grant,
        which needs an admin) or, with `grant_kind` personal, one for their own machine. A
        project grant that reaches into repos other people own waits for their consent. An
        assumption batch keeps or overturns each assumption."""
        deny(request, "desk")
        d = desk_or_404(ref)
        if d["state"] != "open":
            h.err(ValueError(f"{d['label']} is already {d['state']}"), 409)
        a = parse_answer(d, body)
        words, letter, over = a["words"], a["letter"], a["over"]
        from datetime import timedelta
        import asyncio
        # only what the person typed after the question was asked can answer it; their machine's
        # clock may run behind the cell's, so a reply a minute "early" still counts
        since = max(d["created_at"] - timedelta(seconds=factory_mod.MATCH_SKEW_SECONDS),
                    now_utc() - timedelta(hours=factory_mod.MATCH_HOURS))
        # an item addressed to a person is answered in that person's own recorded sessions; one
        # from before (no person) keeps the orchestrator's session, or any from an unlabelled caller
        to = d.get("to_person_id")
        labeled = bool(caller(request)["station"])
        sessions = store.role_sessions("orchestrator") if labeled and not to else None
        giver = to or person_of(request)
        grant = (standing_grant(d, giver, a["scope"], a["grant_kind"], request)
                 if a["standing"] else None)

        def look():
            turns = store.person_turns(h.cc_source(), sessions, since, to)
            hit = (factory_mod.letter_match(letter, turns) if letter is not None
                   else factory_mod.words_match(words, turns))
            heard = all(factory_mod.words_match(str(w), turns)
                        or factory_mod._plain(str(w)) in factory_mod._plain(words)
                        for w in over.values())
            return turns, hit, heard

        turns, hit, heard = await asyncio.to_thread(look)  # scans events
        # the session is read a few seconds behind the person: look again before refusing
        waited = 0.0
        while (hit is None or not heard) and waited < factory_mod.MATCH_WAIT_SECONDS:
            await asyncio.sleep(1.0)
            waited += 1.0
            turns, hit, heard = await asyncio.to_thread(look)  # scans events
        plan = assumption_plan(d, a["kept"], over)
        unheard = [x["label"] for x, st, w in plan if st == "overturned"
                   and not (factory_mod.words_match(w, turns)
                            or factory_mod._plain(w) in factory_mod._plain(words))]
        if (hit is None or unheard) and to:
            name = (store.get_person(to) or {}).get("name") or to
            h.err(ValueError(f"Answer not found yet in {name}'s recorded sessions. Recordings can "
                             "take a minute to arrive; try again after the next sync."), 409)
        if hit is None or unheard:
            # nothing is recorded as the person's unless they typed it: the question stays open
            return {"desk_item": desk_out(d), "matched": False, "granted": False,
                    "recorded": False, "assumptions": [], "follow_up": [],
                    "unheard": unheard}
        return record_answer(d, a, plan, giver, grant, "the person, on the desk", True,
                             hit["session"], None, to or person_of(request))

    @app.post("/api/desk/{ref}/reply")
    def desk_reply(ref: str, request: Request, body: dict = Body(...)):
        """{words, choice?, standing?, scope?: project|all, grant_kind?: project|personal, kept?:
        [A<n>], overturned?: {A<n>: words}}: the person the item is addressed to answers it from
        the console (TR-740). The same fields and checks as /answer, but their words are taken as
        typed: there is no recorded session to find them in. Recorded as made in the console,
        and a standing answer becomes a grant like one typed in a session. Refused from any crew
        station, and for anyone but the person the item is addressed to (an item with no
        addressee: an admin key, the project's lead or `local`)."""
        if caller(request)["station"]:
            h.err(PermissionError("a person answers this in the console"), 403)
        me = work_person(request)
        d = desk_or_404(ref)
        if not desk_can_act(d, me["id"], request):
            who = (store.get_person(d.get("to_person_id")) or {}).get("name") or d.get("to_person_id")
            h.err(PermissionError(f"{d['label']} is for {who}: only they answer it" if who else
                                  f"{d['label']} is not addressed to you"), 403)
        if d["state"] != "open":
            h.err(ValueError(f"{d['label']} is already {d['state']}"), 409)
        a = parse_answer(d, body)
        grant = (standing_grant(d, me["id"], a["scope"], a["grant_kind"], request)
                 if a["standing"] else None)
        plan = assumption_plan(d, a["kept"], a["over"])
        return record_answer(d, a, plan, me["id"], grant, "the person, in the console", False,
                             None, answers_mod.surface(request), me["id"], request)

    @app.post("/api/desk/{ref}/allow")
    def desk_allow(ref: str, request: Request, body: dict = Body(...)):
        """{words, scope: project|all}: the person allows a permission request (a desk item with
        a command) from the console. `words` is the grant as they let it stand (the console
        offers a sentence naming the station and the command; they may edit it). It becomes a
        grant (`standing_grant`): for every project it needs an admin. For the item's project it
        needs the lead or an admin. With an enterprise licence anyone else's is not refused: it
        waits for the lead (an admin when there is none), and any project grant, the lead's or an
        admin's too, also waits for the owner of a repo it names who is not the giver (`granted`
        is false until it takes effect). It is recorded as their decision made in the console and
        the item is answered. A grant in effect reaches the stations' rules, and the crew's watch
        restarts the station with it. A person decides: refused from any crew station."""
        if caller(request)["station"]:
            h.err(PermissionError("a permission is the person's to allow: they allow it in the "
                                  "console"), 403)
        me = work_person(request)
        d = desk_or_404(ref)
        if not desk_can_act(d, me["id"], request):
            who = (store.get_person(d.get("to_person_id")) or {}).get("name") or d.get("to_person_id")
            h.err(PermissionError(f"{d['label']} is for {who}: only they allow it" if who else
                                  f"{d['label']} is not addressed to you"), 403)
        if d["state"] != "open":
            h.err(ValueError(f"{d['label']} is already {d['state']}"), 409)
        if not d.get("command"):
            h.err(ValueError(f"{d['label']} is a question, not a permission request: answer it "
                             "in the orchestrator's session"), 409)
        scope = str(body.get("scope") or "").strip().lower()
        if scope not in ("project", "all"):
            h.err(ValueError("scope is project or all"))
        if scope == "project" and not d["project"]:
            h.err(ValueError(f"{d['label']} has no project: allow it for every project"))
        # A crew station (<crew>-reviewer, <crew>-releaser and their helpers) serves every project
        # and starts with only the grants for every project: one kept for a project never reaches it.
        if scope == "project" and d["station"] and factory_mod.on_crew_side(d["station"], stations()):
            h.err(ValueError(f"{d['station']} works for every project, so a grant for one project "
                             "never reaches it: allow it for every project"))
        if scope == "project" and store.get_project(d["project"]) is None:
            h.err(ValueError("the project this was asked for is gone; allow it for every "
                             "project"), 409)
        words = " ".join(str(body.get("words") or "").split()).strip().strip('"')
        try:
            line = factory_mod.grant_line(words, now_utc().date().isoformat())
        except docs_mod.DocError as e:
            h.err(e)
        # who may give it is refused here, before anything is written
        kind, grant_project, areas, needs_lead = standing_grant(d, me["id"], scope, "project",
                                                                request, may_ask_lead=True)
        # the grant row, not only its line in the doc: station settings are built from the rows
        row, added = put_grant(kind, factory_mod.grant_words(words), grant_project, me["id"],
                               areas, needs_lead, "the person, in the console")
        granted = row["state"] == "active"   # one waiting for consent is not in effect yet
        dec = store.add_decision(d["project"], d["id"], d["question"], words, "allowed", True,
                                 scope, False, None, granted, d["tickets"], now_utc(),
                                 via=answers_mod.surface(request), person_id=person_of(request))
        store.close_desk_item(d["id"], "answered", None, now_utc())
        answers_mod.record(store, request, item=f"permission:{d['label']}", kind="permission",
                           project=d["project"], ask=waiting_mod.ask_permission(d["command"]),
                           choice="yes", said="Allowed for " + (
                               "this project" if scope == "project" else "every project"),
                           person_id=answerer(request))
        return {"desk_item": desk_out(desk_or_404(d["id"])), "decision": dec,
                "station": d["station"], "line": line, "added": added, "scope": scope,
                "granted": granted, "grant": grants_view([row])[0]}

    @app.get("/api/projects/{uid}/decisions")
    def project_decisions(uid: str):
        """The person's answers on this project, newest first."""
        h.project_or_404(uid)
        return {"decisions": store.list_decisions(uid)}

    def lead_or_admin(request: Request, uid: str | None) -> None:
        """403 unless the caller is an admin or the project's lead."""
        pid = (h.current_person(request) or {}).get("id")
        lead = (store.get_project(uid) or {}).get("lead_person_id") if uid else None
        if not (h.is_admin(request) or (pid and lead == pid)):
            h.err(PermissionError("this is for an admin or the project's lead"), 403)

    @app.post("/api/factory/merge-queue/{mid}/approve")
    async def merge_queue_approve(mid: str, request: Request):
        """Approve the merge of a queued pull request that waits for a person (autonomy
        L3-review), without answering its desk item by hand: the same as a yes there. Also lifts
        a hold the person's earlier answer left (the question was withdrawn, or they said no).
        Refused from any crew station: a person approves in the console. The person the row's
        question is addressed to approves it (as on /api/desk/{ref}/reply); a row with no
        question is for an admin or the project's lead. The merge follows within one sync
        interval. A new commit on the pull request voids the approval."""
        from . import merge_queue as merge_queue_mod
        if caller(request)["station"]:
            h.err(PermissionError("a person approves a merge in the console"), 403)
        row = await asyncio.to_thread(store.merge_queue_get, mid)
        if row is None:
            h.err(KeyError(f"no merge queue entry {mid!r}"), 404)
        d = await asyncio.to_thread(store.get_desk_item, row["desk_item"]) if row.get("desk_item") else None
        if d is not None:
            me = work_person(request)
            if not desk_can_act(d, me["id"], request):
                who = (store.get_person(d.get("to_person_id")) or {}).get("name") or d.get("to_person_id")
                h.err(PermissionError(f"the merge of PR #{row['pr']} is for {who}: only they approve it"
                                      if who else f"the merge of PR #{row['pr']} is not addressed to you"),
                      403)
        else:
            lead_or_admin(request, row["project_uid"])
        held = row["state"] == "blocked" and row.get("reason") != merge_queue_mod.no_write_reason(
            row["repo"])
        if row["state"] != "waiting_approval" and not held:
            h.err(ValueError(f"the merge of PR #{row['pr']} is {row['state']}, "
                             "not waiting for approval"), 409)
        person = h.current_person(request) or {}
        out = await asyncio.to_thread(merge_queue_mod.approve, store, row, person.get("id"))
        # the answer to the pull request's home item, whichever surface approved it (TR-1450)
        t = await asyncio.to_thread(store.get_ticket, row["project_uid"], row["ticket"])
        ref = factory_mod.label(t) if t else row["ticket"]
        await asyncio.to_thread(
            lambda: answers_mod.record(store, request, item=f"prs_to_review:{row['project_uid']}:{ref}",
                                       kind="prs_to_review", project=row["project_uid"],
                                       ask=f"Merge PR {row['pr']}", choice="yes", said="Merge",
                                       person_id=answerer(request)))
        return out

    @app.put("/api/projects/{uid}/merge-method")
    def set_merge_method(uid: str, request: Request, body: dict = Body(...)):
        """{merge_method: squash|merge|rebase}: how Tares merges this project's pull requests
        (squash when never set). Only an admin or the project's lead."""
        h.project_or_404(uid)
        lead_or_admin(request, uid)
        try:
            method = store.set_merge_method(uid, str(body.get("merge_method") or ""),
                                            h.by(request, body))
        except ValueError as e:
            h.err(e, 422)
        return {"merge_method": method}

    @app.get("/api/projects/{uid}/merge-method")
    def get_merge_method(uid: str):
        h.project_or_404(uid)
        return {"merge_method": store.get_merge_method(uid)}

    @app.post("/api/projects/{uid}/handover")
    async def hand_over(uid: str, request: Request, body: dict = Body(default={})):
        """{repos?: [{path, name}], repo?, autonomy?}: the spec session hands
        the project to the crew. Each repo is the folder it is checked out in on the crew's
        machine and its GitHub name (owner/name). A later hand-over adds repos, never replaces
        them; a folder is kept for the crew that gave it, so a teammate's crew hands over its own
        clone of the same repo. `autonomy` only when the person wants this project stricter than the crew
        (L3-review). Recorded with the time, the person and the crew (`crew`, else X-Tares-Crew,
        else `crew`); the orchestrator finds it in /api/crew/projects. A key with no person is
        refused (TR-633). Nobody signs off first: a repo's owner is asked through a question when
        the spec needs them. A project kept for later starts here. Answers `handover`, and the
        guardrails' `guardrails` (each decision) and `warnings` (the ones that only warn)."""
        p = h.project_or_404(uid)
        me = work_person(request)
        try:
            crew = crew_name_of(request, body.get("crew"))
            if clash := factory_mod.project_name_clash(p["name"], crew):
                raise ValueError(clash)
            new = factory_mod.handover_repos(body.get("repos"))
            if body.get("repo"):
                new = factory_mod.merge_repos(new, factory_mod.handover_repos(body.get("repo")))
            # each folder is this crew's own: another crew's machine keeps its own (TR-1222)
            key = factory_mod.crew_key(me["id"], crew)
            for r in new:
                r.pop("paths", None)
                if r["path"]:
                    r["paths"] = {key: r["path"]}
            autonomy = str(body.get("autonomy") or "").strip() or None
            if autonomy and autonomy not in factory_mod.AUTONOMY:
                raise docs_mod.DocError(f"unknown autonomy {autonomy!r}; one of: "
                                        + ", ".join(factory_mod.AUTONOMY))
        except (docs_mod.DocError, ValueError) as e:
            h.err(e)
        old = store.get_handover(uid) or {}
        repos = factory_mod.merge_repos(old.get("repos") or [], new)
        autonomy = autonomy or old.get("autonomy")
        # the pre_build hook (TR-861): a guardrail that blocks refuses the hand-over before anything is recorded
        spec = next(iter(store.list_docs(uid, "spec")), None)
        spec_text = str((store.get_doc(uid, spec["id"]) or {}).get("body") or "") if spec else ""
        gate = await integrations_hooks.gate("pre_build", uid, {
            "project": uid, "goal": p.get("goal") or "", "spec_text": spec_text,
            "repos": [{"path": r.get("path") or "", "name": r.get("name") or ""} for r in repos],
            "autonomy": autonomy or ""})
        if not gate["ok"]:
            h.err(ValueError("The project was not handed to the crew. " + " ".join(gate["reasons"])), 409)
        store.set_handover(uid, repos, autonomy, by_of(request, body), now_utc(),
                           person_of(request), crew)
        # more work on a finished project opens it again; one kept for later starts now
        if old.get("finished") or p.get("status") == "later":
            store.update_project(uid, status="active")
        include_shared_grants(uid)   # a crew-built project shows the grants it is built under
        ho = store.get_handover(uid)
        return {"handover": {**ho, "repos": factory_mod.own_paths(ho.get("repos") or [], key)},
                "guardrails": gate["decisions"], "warnings": gate["warnings"]}

    @app.post("/api/projects/{uid}/later")
    def set_later(uid: str, request: Request, body: dict = Body(default={})):
        """{later: true|false}: keep a project for later, off the home until someone starts it,
        or start it again. Only a project the crew has not been handed, and not a finished one;
        handing it over starts it too."""
        p = h.project_or_404(uid)
        later = bool(body.get("later", True))
        if later:
            if p.get("status") == "done":
                h.err(ValueError("the project is finished"), 409)
            ho = store.get_handover(uid)
            if ho and not ho.get("finished"):
                h.err(ValueError("the crew has the project: take it back before keeping it for later"), 409)
            if p.get("status") != "later":
                store.update_project(uid, status="later")
                answers_mod.record(store, request, item=f"ready_to_start:{uid}", kind="ready_to_start",
                                   project=uid, ask=f"Hand {p['name']} to the crew", choice="no",
                                   said="Later", person_id=answerer(request))
        elif p.get("status") == "later":
            store.update_project(uid, status="active")
            factory_home.clear_cache()
        return {"later": later}

    @app.post("/api/projects/{uid}/takeover")
    def take_over(uid: str, request: Request, body: dict = Body(default={})):
        """{words}: the person takes the project back from the crew to build it in their own
        session (`tares factory session <project>`, TR-520). Only the person, never a station.
        The project leaves the crew's work list at once and is listed under `releasing` in
        /api/crew/projects until the orchestrator has stopped its builders (they push what they
        have and write their progress first) and calls release. Asking again while it is being
        released changes nothing."""
        deny(request, "takeover")
        p = h.project_or_404(uid)
        ho = store.get_handover(uid)
        if not ho:
            h.err(ValueError(f"{p['name']} is not with the crew; there is nothing to take over"),
                  409)
        if ho.get("finished"):
            h.err(ValueError(f"{p['name']} is finished; hand it to the crew again or work on it "
                             "in a session without taking it over"), 409)
        if not ho.get("takeover"):
            words = " ".join(str(body.get("words") or "").split())[:500] or \
                "Taken over by the person with tares factory session"
            by, _ = who(request, body)
            store.take_over(uid, words, by, now_utc(), person_of(request))
        return {"project": uid, "name": p["name"], "takeover": store.get_handover(uid)["takeover"],
                "holders": sorted({t["holder"] for t in store.list_tickets(uid) if t["holder"]})}

    @app.post("/api/projects/{uid}/release")
    def release_project(uid: str, request: Request):
        """The orchestrator has stopped the builders of a project the person took over: the
        person's session owns it now. Clears every ticket's holder (the stages, pull requests and
        working docs stay as the builders left them). The person's own session may release it too,
        when no orchestrator runs."""
        deny(request, "release")
        p = h.project_or_404(uid)
        ho = store.get_handover(uid) or {}
        tk = ho.get("takeover")
        if not tk:
            h.err(ValueError(f"{p['name']} was not taken over; there is nothing to release"), 409)
        if not tk.get("released_at"):
            by, role = who(request)
            at = now_utc()
            for t in store.list_tickets(uid):
                if t["holder"]:
                    store.update_ticket(t["id"], holder=None)
                    store.add_ticket_history(t["id"], uid, "holder", None,
                                             "released to the person's session", by, role, at,
                                             person_of(request))
            store.release_takeover(uid, at)
        return {"project": uid, "name": p["name"],
                "takeover": store.get_handover(uid)["takeover"]}

    def pr_row(t: dict, last: dict | None) -> dict:
        """A ticket's pull request as the GitHub section shows it: what GitHub said when Tares last
        read it (title, CI, the factory/review status), and the crew's own last verdict (`last`,
        its latest review round)."""
        pr = t["pr"] or {}
        v = verdict_now(t, last)
        return {"url": pr["url"], "ticket": factory_mod.label(t), "number": pr.get("number"),
                "title": pr.get("title") or t["title"], "branch": pr.get("branch"),
                "head": (pr.get("head") or "")[:7] or None, "ci": pr.get("ci"),
                "github_verdict": pr.get("verdict"), "stage": t["stage"], "holder": t["holder"],
                "review": ({"verdict": v["verdict"], "round": v["round"], "current": v["current"]}
                           if v else None),
                "merged_at": pr.get("merged_at"), "checked_at": pr.get("checked_at"),
                "error": pr.get("error")}

    def repo_rows(uid: str, repos: list[dict], tickets: list[dict] | None = None) -> list[dict]:
        """Each hand-over repo with the tickets that change it, their open pull requests (newest
        first) and the ones merged. A ticket with no repo belongs to the first one; a repo only
        tickets name is added. `github_error` says when Tares could not read GitHub for it.
        `tickets` is the project's tickets when the caller already read them."""
        tickets = store.list_tickets(uid) if tickets is None else tickets
        lasts = store.last_reviews([t["id"] for t in tickets if (t["pr"] or {}).get("url")])
        rows = [{**r, "tickets": [], "open_prs": [], "merged_prs": [], "github_error": None}
                for r in repos]
        for t in tickets:
            name = t.get("repo") or (t["pr"] or {}).get("repo")
            row = next((r for r in rows if name and (r["name"] or "").lower() == name.lower()),
                       None)
            if row is None and name:
                row = {"path": None, "name": name, "tickets": [], "open_prs": [], "merged_prs": [],
                       "github_error": None}
                rows.append(row)
            if row is None and rows:
                row = rows[0]
            if row is None:
                continue
            row["tickets"].append(factory_mod.label(t))
            pr = t["pr"] or {}
            # merged as far as the ticket knows (a builder sets it when Tares can't read GitHub)
            done = (t["stage"] in ("merged", "shipped")
                    or t["status"] in factory_mod.DONE_STATUSES)
            if not pr.get("url"):
                continue
            if pr.get("error"):
                row["github_error"] = pr["error"]
            if pr.get("merged") or (done and pr.get("state") != "closed"):
                row["merged_prs"].append(pr_row(t, lasts.get(t["id"])))
            elif pr.get("state") != "closed":
                row["open_prs"].append(pr_row(t, lasts.get(t["id"])))
        for row in rows:
            row["open_prs"].sort(key=lambda p: -(p["number"] or 0))
            row["merged_prs"].sort(key=lambda p: -(p["number"] or 0))
        return rows

    def closed_answer(uid: str, p: dict, words: str, request: Request) -> None:
        """The record of a person closing a project themselves (TR-1451). One closed on a desk
        answer is already recorded as that answer."""
        answers_mod.record(store, request, item=f"signoff:{uid}", kind="signoff", project=uid,
                           ask=f"Close {p['name']}", choice="yes", said=words,
                           via="session" if caller(request)["station"] else None,
                           person_id=answerer(request))

    @app.post("/api/projects/{uid}/finish")
    def finish_project(uid: str, request: Request, body: dict = Body(...)):
        """{decision} or {words}: the person said the project is done. A station names the desk
        item (`decision`, D<n>) that asked them, answered in the orchestrator's session: its words,
        matched against what they typed, are what is kept. The person's own session (no station:
        `tares factory finish` in their terminal) may give `words` instead. Refused while a ticket
        is still open. The project's status becomes done and it leaves the crew's work list; a
        later hand-over opens it again."""
        deny(request, "finish")
        p = h.project_or_404(uid)
        # a person closes a project whose work is theirs (factory_owners: its lead, else the
        # admins; an orphan, any admin key); a crew station finishes what its desk was told to
        me = h.current_person(request)
        if me and not caller(request)["station"] and not h.is_system(request):
            o = owners_mod.Owners(store)
            ids = o.project(p)
            if not (me["id"] in ids or (o.orphan(ids) and h.is_admin(request))):
                h.err(PermissionError(f"{p['name']} is led by someone else: its lead closes it"), 403)
        ho = store.get_handover(uid)
        if not ho and caller(request)["station"]:
            h.err(ValueError(f"{p['name']} was never handed to the crew; there is nothing to finish"))
        if ho and ho.get("finished"):
            h.err(ValueError(f"{p['name']} was already finished "
                             f"({ho['finished']['decision'] or 'by hand'})"), 409)
        open_ = [factory_mod.label(t) for t in store.list_tickets(uid)
                 if not (t["stage"] in ("merged", "shipped")
                         or t["status"] in factory_mod.DONE_STATUSES)]
        if open_:
            h.err(ValueError(f"{p['name']} still has open tickets: {', '.join(open_)}. Finish or "
                             "cancel them first"), 409)
        if not ho:
            # built in the person's own session, never by the crew (tares factory session, TR-520):
            # their words are what the session typed for them; the project is done
            if p.get("status") == "done":
                h.err(ValueError(f"{p['name']} was already finished"), 409)
            words = " ".join(str(body.get("words") or "").split())[:500] or \
                "Marked done by the person"
            by, _ = who(request, body)
            store.update_project(uid, status="done")
            close_project_desk(uid)
            closed_answer(uid, p, words, request)
            return {"project": uid, "name": p["name"],
                    "finished": {"at": now_utc(), "words": words, "decision": None, "by": by},
                    "open_projects": len(store.handed_over_projects(finished=False, taken=False))}
        ref = str(body.get("decision") or "").strip()
        by, _ = who(request, body)
        if not ref and not caller(request)["station"]:
            # the person, in their own terminal: their words, or that they marked it themselves
            words = " ".join(str(body.get("words") or "").split())[:500] or \
                "Marked done by the person with tares factory finish"
            store.finish_handover(uid, words, None, by, now_utc(), person_of(request))
            store.update_project(uid, status="done")
            close_project_desk(uid)
            closed_answer(uid, p, words, request)
            return {"project": uid, "name": p["name"], "finished": store.get_handover(uid)["finished"],
                    "open_projects": len(store.handed_over_projects(finished=False, taken=False))}
        if not ref:
            h.err(ValueError("name the desk item where the person said the project is done "
                             "(decision=D<n>): ask them on the desk first, then record their answer"))
        d = store.get_desk_item(ref)
        if d is None:
            h.err(KeyError(f"no desk item {ref!r}"), 404)
        if d["project"] not in (uid, None):
            h.err(ValueError(f"{d['label']} was asked about another project"), 409)
        dec = next((x for x in store.list_decisions(None) if x["desk_item"] == d["id"]), None)
        # the person's own words count: matched against their session, or typed by them in the console
        said = dec is not None and (dec["matched"] or (dec.get("via") in answers_mod.PERSON_SURFACES and dec.get("person")
                                  and not d.get("command")))
        if d["state"] != "answered" or not said:
            h.err(ValueError(f"{d['label']} has no answer from the person yet: record their words "
                             "with desk_answer first"), 409)
        store.finish_handover(uid, dec["words"], d["label"], by, now_utc(), person_of(request))
        store.update_project(uid, status="done")
        close_project_desk(uid)
        return {"project": uid, "name": p["name"], "finished": store.get_handover(uid)["finished"],
                "open_projects": len(store.handed_over_projects(finished=False, taken=False))}

    @app.get("/api/projects/{uid}/repos")
    def project_repos(uid: str):
        """The repos this project touches, each with its tickets and open pull requests
        (TR-463). Empty for a project nobody handed over and whose tickets name no repo."""
        h.project_or_404(uid)
        ho = store.get_handover(uid) or {}
        rows = repo_rows(uid, ho.get("repos") or [])
        return {"repos": [r for r in rows if r["path"] or r["name"]]}

    @app.get("/api/factory/report")
    def report_read(request: Request, scope: str = "factory", to: str | None = None,
                    from_: str | None = Query(None, alias="from")):
        """The Report page in one read, from the weekly figures: {range, scope, cannot_see,
        sections}. `from` and `to` are days (year-month-day) widened to whole ISO weeks in UTC;
        with neither it is the last 8 weeks. `scope` is factory (default), mine (Home's rule),
        team:<person id> (what that person leads) or project:<id>. Every scope counts only the
        projects the caller can read, so a private one is not in it and, asked for by name, is
        unknown. What is stuck now lists a desk question of no project only in the factory scope,
        and only one the caller is asked (desk_is_mine). Needs the read scope; a project key is
        refused."""
        hidden = h.hidden_ids()
        projects = [p for p in store.list_projects() if p["id"] not in hidden]
        me, o = answerer(request), owners_mod.Owners(store)

        def accepted_rules() -> set[tuple]:   # (repo, kind) of each accepted rule the offer list
            offers = store.list_rule_proposals("accepted")           # shows this caller
            return {(r["repo"], r["kind"]) for r in readable_offers(h, offers, hidden)}

        try:
            return report_mod.page(
                store, projects, lambda: my_projects(request), o, scope, from_, to,
                asks_me=lambda d: me is not None and desk_is_mine(d, me, o),
                accepted_rules=accepted_rules)
        except report_mod.UnknownScope as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/factory/report/rebuild")
    def report_rebuild():
        """Work out the report's weekly figures again, every week of every project (they are kept
        current in the background; this is for after a repair). Answers {rows, changed, removed}.
        Needs the admin scope, as every write does; a project key is refused."""
        return report_mod.rebuild_all(store)

    fx = SimpleNamespace(stations=stations, station_states=station_states, queue=queue_all, pace=pace,
                         settings=settings,
                         team_home=team_home, project_waiting=project_waiting,
                         waiting_for_person=waiting_for_person,
                         team_projects=team_projects, own_stations=own_stations,
                         own_desk=own_desk, own_tickets=own_tickets,
                         hidden_ids=lambda: h.hidden_ids(),
                         caller_person=lambda request: h.current_person(request),
                         is_system=lambda request: h.is_system(request),
                         is_admin=lambda request: h.is_admin(request),
                         machine_key=machine_key)

    def for_request(request):
        """This fx with `settings` and `pace` read for the caller: a person's own over the cell's
        default, the scope `PUT /api/crew/settings` writes. A screen that shows the crew's
        settings reads them through this, or it shows the default and the control snaps back."""
        return SimpleNamespace(**{**vars(fx), "settings": lambda: settings(request),
                                  "pace": lambda: pace(request)})

    fx.for_request = for_request
    return fx
