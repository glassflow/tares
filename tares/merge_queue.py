"""The merge queue (TR-856): Tares merges a ready pull request itself, one at a time per (repo,
base), in order, on every cell. No agent runs and no tokens are spent: the daemon's pull request
sync calls `tick` after each refresh, and `tick` reads GitHub and the project's gates, asks the
person at L3-review, and merges.

A row of the `merge_queue` table is one pull request. Its states:

- queued            ready, in line; the merge happens when it is first of its (repo, base) whose
                    dependencies are merged (and, at L3-review, once it is approved)
- waiting_ci        reserved: a row that waits for checks (the next ticket uses it)
- waiting_approval  L3-review: a desk item asks the person; nothing merges until a yes
- merging           claimed by a tick, the merge call in flight (one per repo and base)
- merged            GitHub shows it merged (by Tares or by anyone)
- sent_back         the ticket left review or the pull request was closed, so the row ended
- blocked           a person must act: they said no, or no credential can write to the repo

A new commit on the pull request deletes its row (and withdraws its question); the pull request
enters again when it is ready, so an approval never carries over to a different commit.
"""
from __future__ import annotations

import asyncio
import re
from fnmatch import fnmatchcase

from . import factory as factory_mod
from . import factory_api
from . import factory_github
from .envelope import now_utc
from .factory_waiting import crew_has, effective_autonomy
from .merge_ready import merge_ready

# Head branches the queue never touches: a milestone branch is merged by the crew's releaser, not
# here.
MILESTONE_BRANCHES = ("factory-modular/*",)

AUTO = "L4-ship"      # any other level is treated as L3-review
BY = "merge-queue"
_FAR = 10 ** 9        # sorts after every real position

# what a person's answer says "yes" with, and the words that turn a yes into a request for a change
_YES = re.compile(r"(yes|approved)\b", re.I)
_CHANGE = re.compile(r"\b(but|however|except|unless|instead|rather|change|changes|fix|before)\b",
                     re.I)


def is_milestone_branch(branch: str | None) -> bool:
    b = str(branch or "").strip()
    return any(fnmatchcase(b, pat) for pat in MILESTONE_BRANCHES)


def _plain(text) -> str:
    """Text for a reason or a desk line: no em dashes (the house rule)."""
    return str(text or "").replace("—", ",").replace("–", "-").strip()


def judge(decision: dict) -> tuple[bool, str]:
    """(approved, the person's words) of a decision row: a yes only when the choice (or the
    words when no choice was picked) starts with yes or approved, and neither asks for a change."""
    choice = str(decision.get("choice") or "").strip()
    words = str(decision.get("words") or "").strip()
    first = (choice or words).lower()
    said = choice or words
    yes = bool(_YES.match(first))
    asks = bool(_CHANGE.search(choice) or _CHANGE.search(words))
    return yes and not asks, said


def autonomy_of(store, project_id: str) -> str:
    """L4-ship when the hand-over (else the crew's setting, with the hand-over person's own over
    it) says so, and the team's stored autonomy, a ceiling, allows it; L3-review for anything
    else. A hand-over's own L4-ship never beats a team set to L3-review."""
    ho = store.get_handover(project_id) or {}
    crew = factory_api.crew_settings(store, ho.get("person")).get("autonomy")
    level = effective_autonomy(store, None, project_id, crew=crew)
    team = factory_api.stored_crew_setting(store, factory_api.CREW_SETTING).get("autonomy")
    level = factory_mod._stricter_autonomy(level, team)
    return AUTO if level == AUTO else "L3-review"


def _person_id(store, project_id: str) -> str | None:
    return ((store.get_handover(project_id) or {}).get("person") or {}).get("id")


def _withdraw(store, row: dict, why: str) -> None:
    """Close the row's open question as withdrawn; nothing when it has none, it is closed, or
    another open row still points at it (the credential question serves every blocked row of a
    repo)."""
    did = row.get("desk_item")
    d = store.get_desk_item(did) if did else None
    if d and d.get("state") == "open" and not any(
            x["id"] != row["id"] and x.get("desk_item") == did for x in store.merge_queue_open()):
        store.close_desk_item(d["id"], "withdrawn", why, now_utc())


def _set(store, row: dict, state: str, reason: str | None = None, **more) -> None:
    """Write the row's state and reason when they changed."""
    reason = _plain(reason) or None
    if row["state"] == state and (row.get("reason") or None) == reason and not more:
        return
    store.merge_queue_update(row["id"], state=state, reason=reason, **more)
    row.update(state=state, reason=reason, **more)


def approve(store, row: dict, person_id: str | None = None) -> dict:
    """A person's yes through the console route: the decision is recorded as a console answer
    on the row's question, the question closes as answered, and the row is approved for the
    commit it holds and goes back in line."""
    d = store.get_desk_item(row["desk_item"]) if row.get("desk_item") else None
    if d and d.get("state") == "open":
        store.add_decision(d["project"], d["id"], d["question"], "Approved in the console",
                           "Yes, merge", False, "project", False, None, False, d["tickets"],
                           now_utc(), via="console", person_id=person_id)
        store.close_desk_item(d["id"], "answered", None, now_utc())
    _set(store, row, "queued", None, approved_head=row["head"])
    return {"id": row["id"], "state": row["state"], "approved_head": row["approved_head"],
            "pr": row["pr"], "repo": row["repo"]}


def _ask(store, row: dict, t: dict) -> None:
    """L3-review: one question for the hand-over's person, and the row waits for the answer."""
    d = store.get_desk_item(row["desk_item"]) if row.get("desk_item") else None
    if d is None or d.get("state") != "open":
        title = " ".join(str(t.get("title") or "").split())
        d = store.add_desk_item(
            row["project_uid"], f"Merge PR #{row['pr']} ({title}) at {row['head'][:7]}?",
            f"{row['repo']} into {row['base']}. The review gate passed and CI is green. Tares "
            "merges it once you say yes.",
            ["Yes, merge", "No, hold"], "Yes, merge",
            "Tares merges ready pull requests itself and asks you first at L3-review.",
            f"The merge of PR #{row['pr']}", [factory_mod.label(t)], True, BY, [], now_utc(),
            to_person_id=_person_id(store, row["project_uid"]))
    _set(store, row, "waiting_approval", None, desk_item=d["id"], approved_head=None)


def no_write_reason(repo: str) -> str:
    return f"No GitHub credential can write to {repo}"


def _blocked_for_credential(store, row: dict, t: dict) -> None:
    """No credential on this Tares can write to the repo: the row is blocked and ONE question
    (not one per pull request, not one per tick) tells the person what to add."""
    q = (f"No GitHub credential on this Tares can write to {row['repo']}. Add one with write "
         "access in Settings, GitHub.")
    d = next((x for x in store.list_desk("open", row["project_uid"]) if x["question"] == q), None)
    if d is None:
        d = store.add_desk_item(
            row["project_uid"], q,
            f"PR #{row['pr']} is ready to merge, but GitHub refused every credential.",
            [], "Add a GitHub credential with write access to the repo",
            "Tares merges ready pull requests itself and needs write access to do it.",
            f"The merge of PR #{row['pr']}", [factory_mod.label(t)], True, BY, [], now_utc(),
            to_person_id=_person_id(store, row["project_uid"]))
    _set(store, row, "blocked", no_write_reason(row["repo"]), desk_item=d["id"])


async def _merged(store, row: dict, t: dict | None, reason: str, by: str, role: str,
                  pr: dict | None = None) -> None:
    """The pull request is merged: the row says so and the ticket's stage follows."""
    await asyncio.to_thread(_set, store, row, "merged", None)
    if t is not None:
        await factory_api.mark_merged(store, row["project_uid"], t, reason, by, role, pr)


# ── the tick ──────────────────────────────────────────────────────────────────

def _gather(store) -> dict:
    """What one tick works on, read together: the projects a crew has, their tickets and
    milestone positions, and the open rows."""
    projects = [p for p in store.list_projects()
                if p.get("kind") == "software_factory" and crew_has(store, p)]
    ids = [p["id"] for p in projects]
    tickets = store.tickets_by_project(ids)
    by_id = {t["id"]: t for ts in tickets.values() for t in ts}
    ms = {m["id"]: m["position"] for uid in ids for m in store.list_milestones(uid)}
    rows = [r for r in store.merge_queue_open() if r["project_uid"] in ids]
    return {"projects": {p["id"]: p for p in projects}, "tickets": tickets, "by_id": by_id,
            "ms": ms, "rows": rows, "level": {uid: autonomy_of(store, uid) for uid in ids}}


def _key(g: dict, row: dict):
    """The queue's order: milestone position, then ticket position, then when it entered."""
    t = g["by_id"].get(row["ticket"]) or {}
    ms = g["ms"].get(t.get("milestone"), _FAR)
    return (ms, t.get("position") or 0, row["entered_at"])


async def _recover(store) -> None:
    """A row still `merging` at the start of a tick is from a tick that died mid-merge: GitHub
    knows whether it merged. Merged is merged; otherwise the row goes back in line."""
    for row in await asyncio.to_thread(store.merge_queue_open):
        if row["state"] != "merging":
            continue
        try:
            pr = await factory_github.read_pr(store, row["repo"], int(row["pr"]))
            if pr.get("error"):
                continue            # GitHub could not be read: the next tick looks again
            t = await asyncio.to_thread(store.get_ticket, row["project_uid"], row["ticket"])
            if pr.get("merged"):
                await _merged(store, row, t, "GitHub shows the pull request merged", "github",
                              "github", pr)
            else:
                await asyncio.to_thread(_set, store, row, "queued", None)
        except Exception as e:  # noqa: BLE001
            print(f"[tares] merge queue recovery of {row['id']} failed: {type(e).__name__}: {e}",
                  flush=True)


def _settle_question(store, g: dict, row: dict) -> None:
    """A row waiting for its person: read what became of the question."""
    d = store.get_desk_item(row["desk_item"]) if row.get("desk_item") else None
    if d is None:       # the question is gone: ask again rather than wait for nobody
        _set(store, row, "queued", None, desk_item=None, approved_head=None)
        return
    if d["state"] == "open":
        return
    if d["state"] == "withdrawn":
        _set(store, row, "blocked", "The question was withdrawn")
        return
    mine = [x for x in store.list_decisions(row["project_uid"], all_=True)
            if x.get("desk_item") == d["id"]]
    if not mine:
        _set(store, row, "blocked", "The question was answered but no answer was recorded")
        return
    yes, words = judge(mine[0])
    if yes:
        _set(store, row, "queued", None, approved_head=row["head"])
    else:
        _set(store, row, "blocked", f"The person said: {words}")


async def _keep_rows(store, g: dict) -> dict[str, dict]:
    """Walk the open rows: ended ones end, a changed head deletes the row, a waiting row reads
    its answer. Returns the fresh merge_ready answer of each row still open, by row id."""
    fresh: dict[str, dict] = {}
    for row in list(g["rows"]):
        if row["state"] == "merging":
            continue
        try:
            t = g["by_id"].get(row["ticket"])
            if t is None or t.get("stage") not in ("review", "merged", "shipped"):
                await asyncio.to_thread(_withdraw, store, row, "the ticket left review")
                await asyncio.to_thread(_set, store, row, "sent_back", "The ticket left review")
                continue
            pr = t.get("pr") or {}
            if (pr.get("repo"), pr.get("number")) != (row["repo"], row["pr"]):
                await asyncio.to_thread(_withdraw, store, row, "the ticket has another pull request")
                await asyncio.to_thread(store.merge_queue_delete, row["id"])
                continue
            if t["stage"] in ("merged", "shipped"):
                await asyncio.to_thread(_set, store, row, "merged", None)
                continue
            ans = await merge_ready(store, f"{row['repo']}#{row['pr']}")
            head = str(ans.get("head") or "")
            brief = ans.get("pr") or {}
            if brief.get("merged"):      # someone else merged it: no error, just the record
                await _merged(store, row, t, "GitHub shows the pull request merged", "github",
                              "github")
                continue
            if not head:
                continue                 # GitHub could not be read: nothing is known, no change
            if brief.get("state") and brief.get("state") != "open":
                await asyncio.to_thread(_withdraw, store, row, "the pull request was closed")
                await asyncio.to_thread(_set, store, row, "sent_back",
                                        "The pull request was closed")
                continue
            if head != row["head"]:
                await asyncio.to_thread(_withdraw, store, row, "the pull request has new commits")
                await asyncio.to_thread(store.merge_queue_delete, row["id"])
                g["rows"].remove(row)
                continue
            fresh[row["id"]] = ans
            if row["state"] == "blocked" and row.get("reason") == no_write_reason(row["repo"]):
                # a credential may have been added since: line up again and try the merge; it
                # blocks again (one question for the repo, no new one) when none can write yet
                await asyncio.to_thread(_set, store, row, "queued", None)
            if row["state"] == "waiting_approval":
                if g["level"].get(row["project_uid"]) == AUTO:
                    await asyncio.to_thread(_withdraw, store, row,
                                            "Tares merges this without asking now")
                    await asyncio.to_thread(_set, store, row, "queued", None, approved_head=None)
                else:
                    await asyncio.to_thread(_settle_question, store, g, row)
        except Exception as e:  # noqa: BLE001 - one row never stops the others
            print(f"[tares] merge queue row {row['id']} failed: {type(e).__name__}: {e}",
                  flush=True)
    return fresh


async def _enter(store, g: dict, fresh: dict[str, dict]) -> None:
    """Tickets in review with an open pull request and no open row: a ready one gets a row."""
    have = {r["ticket"] for r in await asyncio.to_thread(store.merge_queue_open)}
    for uid in g["projects"]:
        for t in g["tickets"].get(uid, []):
            pr = t.get("pr") or {}
            if (t.get("stage") != "review" or not pr.get("repo") or not pr.get("number")
                    or pr.get("merged") or pr.get("state") == "closed" or t["id"] in have):
                continue
            try:
                branch = pr.get("branch")
                if not branch:
                    got = await factory_github.read_pr(store, pr["repo"], int(pr["number"]), pr)
                    branch = got.get("branch")
                    if not branch:
                        continue        # unread: whether it is a milestone branch is not known
                if is_milestone_branch(branch):
                    continue
                ans = await merge_ready(store, f"{pr['repo']}#{pr['number']}")
                brief = ans.get("pr") or {}
                if not ans.get("ready") or not ans.get("head") or not brief.get("base"):
                    continue
                row = await asyncio.to_thread(
                    store.merge_queue_add, pr["repo"], brief["base"], int(pr["number"]),
                    ans["head"], t["id"], uid)
                g["rows"].append(row)
                fresh[row["id"]] = ans
            except Exception as e:  # noqa: BLE001
                print(f"[tares] merge queue entry of {t.get('id')} failed: "
                      f"{type(e).__name__}: {e}", flush=True)


async def _merge(store, row: dict, t: dict) -> None:
    """The row is claimed (`merging`): read the gates once more, then merge the commit just
    evaluated."""
    def back(reason: str) -> None:
        _set(store, row, "queued", reason)

    ans = await merge_ready(store, f"{row['repo']}#{row['pr']}")
    head = str(ans.get("head") or "")
    brief = ans.get("pr") or {}
    if brief.get("merged"):
        await _merged(store, row, t, "GitHub shows the pull request merged", "github", "github")
        return
    if not head:
        await asyncio.to_thread(back, "; ".join(ans.get("reasons") or []) or "GitHub could not be read")
        return
    if head != row["head"]:
        await asyncio.to_thread(_withdraw, store, row, "the pull request has new commits")
        await asyncio.to_thread(store.merge_queue_delete, row["id"])
        return
    if not ans.get("ready"):
        await asyncio.to_thread(back, "; ".join(ans.get("reasons") or []))
        return
    method = await asyncio.to_thread(store.get_merge_method, row["project_uid"])
    res = await factory_github.merge_pr(store, row["repo"], int(row["pr"]), head, method)
    if res["ok"]:
        pr = await factory_github.read_pr(store, row["repo"], int(row["pr"]), t.get("pr"))
        pr = {**pr, "merged": True, "state": "closed"}
        branch = pr.get("branch") or (t.get("pr") or {}).get("branch")
        await asyncio.to_thread(_withdraw, store, row, "a credential can write now")
        await _merged(store, row, t, "Tares merged the pull request", BY, BY, pr)
        same_repo = str(pr.get("head_repo") or "").lower() == str(row["repo"]).lower()
        if branch and same_repo:      # a fork's branch is not the base repo's to delete
            await factory_github.delete_branch(store, row["repo"], branch)
        return
    if res["no_write"]:
        await asyncio.to_thread(_blocked_for_credential, store, row, t)
    elif res["rate_limited"]:
        await asyncio.to_thread(back, "GitHub's rate limit is spent; trying again later")
    else:
        await asyncio.to_thread(back, res.get("message") or f"GitHub answered {res.get('status')}")


async def _run_queue(store, g: dict, fresh: dict[str, dict]) -> None:
    """Per (repo, base), in order: a row waits for its dependencies, asks at L3-review, and
    merges when it can take the claim."""
    groups: dict[tuple, list[dict]] = {}
    for row in g["rows"]:
        groups.setdefault((row["repo"], row["base"]), []).append(row)
    for (repo, base), rows in groups.items():
        for row in sorted(rows, key=lambda r: _key(g, r)):
            if row["state"] != "queued":
                continue
            try:
                if factory_github.rate_low():
                    return
                t = g["by_id"].get(row["ticket"])
                if t is None:
                    continue
                unmet = [d for d in (g["by_id"].get(x) for x in t.get("depends_on") or [])
                         if d and not factory_mod.delivered(d)]
                if unmet:               # it keeps its place; later rows that do not need it pass
                    names = ", ".join(factory_mod.label(d) for d in unmet)
                    await asyncio.to_thread(_set, store, row, "queued", f"Waiting for {names} to merge")
                    continue
                ans = fresh.get(row["id"])
                if ans is not None and not ans.get("ready"):
                    await asyncio.to_thread(_set, store, row, "queued",
                                            "; ".join(ans.get("reasons") or []))
                    continue
                level = g["level"].get(row["project_uid"], "L3-review")
                if level != AUTO and row.get("approved_head") != row["head"]:
                    await asyncio.to_thread(_ask, store, row, t)
                    continue
                if not await asyncio.to_thread(store.merge_queue_claim, row["id"],
                                               level != AUTO):
                    continue
                row["state"] = "merging"
                await _merge(store, row, t)
                if row["state"] == "merged":      # the next row's dependency on it is met now
                    t["stage"] = "merged"
            except Exception as e:  # noqa: BLE001 - one row never stops the others
                print(f"[tares] merge queue merge of {row['id']} failed: "
                      f"{type(e).__name__}: {e}", flush=True)


async def tick(store) -> None:
    """One pass of the queue; the pull request sync calls it after each refresh. It stands
    down for the tick while GitHub says a credential has under RATE_FLOOR calls left, so the
    sync's own reads are never starved."""
    low = factory_github.rate_low()
    if low:
        left = ", ".join(f"{api}: {n} left" for api, n in sorted(low.items()))
        print(f"[tares] merge queue skipped this round, GitHub's rate limit is low ({left})",
              flush=True)
        return
    await _recover(store)
    g = await asyncio.to_thread(_gather, store)
    fresh = await _keep_rows(store, g)
    await _enter(store, g, fresh)
    # what changed above is in `g["rows"]` (the same dicts); re-read states the walkers wrote
    g["rows"] = await asyncio.to_thread(store.merge_queue_open)
    await _run_queue(store, g, fresh)
