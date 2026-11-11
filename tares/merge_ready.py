"""merge_ready (TR-855): one answer to "may this pull request merge?".

It combines three things the project's integrations decide: the review gate's verdict on the
head (`review`), the CI source's state for it (`ci`) and the guardrails' decisions before a merge
(`guardrails`). `merge_ready(store, pr_ref)` returns

    {ready, reasons, head, pr, ticket, project, review, ci, guardrails}

`ready` is True only when the pull request is open and not a draft, the review gate approved the
current head, CI is `success` (or `none`, a project with no CI) and no guardrail blocks. `reasons`
is a list of plain sentences, one per thing that holds the merge back; it is empty exactly when
`ready` is True. A guardrail that only warns adds nothing to `reasons`; it is listed in
`guardrails` with `warn_only` true.

The pull request must belong to a ticket (its ticket names the project, so which integrations
apply). Nothing here raises: an empty or bad reference, no ticket, a failed GitHub read, a
review gate or CI source that cannot be reached, a bad `required` list, all come back as
`ready` False with a reason. Nothing merges here; the merge queue (merge_queue.py) does that.
"""
from __future__ import annotations

from . import factory as factory_mod
from . import factory_github
from .integrations import hooks, registry, resolve
from .integrations.types import PRRef


def _answer(ready: bool, reasons: list[str], **more) -> dict:
    return {"ready": ready, "reasons": reasons, "head": more.pop("head", ""),
            "pr": more.pop("pr", None), "ticket": more.pop("ticket", None),
            "project": more.pop("project", None), "review": more.pop("review", None),
            "ci": more.pop("ci", None), "guardrails": more.pop("guardrails", [])}


def no_ticket(repo: str, number: int) -> dict:
    """The answer for a pull request no ticket has. The route gives it too for a PR whose ticket
    is in a project the caller cannot see, so the two cannot be told apart."""
    return _answer(False, [f"No ticket has {repo}#{number} as its pull request, so Tares "
                           "does not know which project's rules apply"],
                   pr={"repo": repo, "number": number})


async def merge_ready(store, pr_ref: str, source: str | None = None) -> dict:
    """The answer for the pull request `pr_ref` (a PR URL or `owner/repo#12`). `source` is the
    cell's Claude Code source name, which the local CI integration reads the sessions from."""
    parsed = factory_github.parse_pr(pr_ref)
    if parsed is None:
        said = str(pr_ref or "").strip()
        return _answer(False, [("The pull request reference is empty" if not said else
                                f"{said[:80]!r} is not a pull request reference")
                               + ": give a PR URL or owner/repo#12"])
    repo, number = parsed
    t = store.ticket_for_pr(repo, number)
    if t is None:
        return no_ticket(repo, number)
    uid = t["project"]
    base = {"ticket": factory_mod.label(t), "project": uid}
    pr = await factory_github.read_pr(store, repo, number, prev=t.get("pr"))
    head = str(pr.get("head") or "").strip()
    brief = {"repo": repo, "number": number, "url": pr.get("url") or "",
             "state": pr.get("state"), "draft": bool(pr.get("draft")),
             "merged": bool(pr.get("merged")), "base": pr.get("base") or ""}
    if pr.get("error"):
        # read_pr keeps the ticket's last snapshot (head, state, draft) when a read fails; none of
        # that is a fresh answer, so a failed read stops here whatever it kept
        return _answer(False, [f"GitHub could not be read for {repo}#{number}: {pr['error']}"],
                       pr={"repo": repo, "number": number, "url": brief["url"]}, **base)
    reasons: list[str] = []
    if pr.get("merged"):
        reasons.append("The pull request is already merged")
    elif pr.get("state") and pr.get("state") != "open":
        reasons.append("The pull request is closed")
    elif pr.get("draft"):
        reasons.append("The pull request is a draft")
    if not head:
        reasons.append("The pull request has no head commit")
    short = head[:7]
    ref = PRRef(repo=repo, number=number, head=head, base=pr.get("base") or "",
                url=pr.get("url") or "", ticket=t["id"], project_uid=uid)

    # ── the review gate ──
    review = {"integration": "", "state": "none", "head": head, "by": "", "summary": "",
              "findings": [], "url": ""}
    try:
        eff = resolve.effective("review", uid, store=store)
        review["integration"] = eff.integration
        cls = registry.get("review", eff.integration)
        v = await cls(store).verdict(ref)
        review.update({"state": v.state, "by": v.by, "summary": v.summary,
                       "findings": list(v.findings), "url": v.url})
        if v.state == "approved" and v.head and head and not factory_mod.same_commit(v.head, head):
            review["state"] = "stale"
            reasons.append(f"The review was for an older commit; {short} is not reviewed yet")
        elif v.state == "approved":
            pass
        elif v.state == "changes":
            who = v.by or eff.integration or "The review"
            first = (v.findings[0] if v.findings else "") or v.summary or "see the findings"
            reasons.append(f"{who} asked for changes on {short}: {str(first)[:160]}")
        elif v.state == "stale":
            reasons.append(f"The review was for an older commit; {short} is not reviewed yet")
        elif v.state == "pending":
            reasons.append(f"The review of {short} is still pending")
        else:
            reasons.append(f"{short} is not reviewed yet")
    except Exception as e:  # noqa: BLE001 - one slot failing must not hide the other two
        review["state"] = "error"
        reasons.append(f"The review gate could not be read: {type(e).__name__}: {e}")

    # ── CI ──
    ci = {"integration": "", "state": "none", "head": head, "failing": [], "summary": "", "url": ""}
    try:
        eff = resolve.effective("ci", uid, store=store)
        ci["integration"] = eff.integration
        cls = registry.get("ci", eff.integration)
        inst = cls(store, source=source) if eff.integration == "local" else cls(store)
        c = await inst.state(ref, [])
        ci.update({"state": c.state, "failing": list(c.failing), "summary": c.summary, "url": c.url})
        if c.state == "failure":
            names = ", ".join(c.failing)
            reasons.append(f"CI is failing on {short}" + (f": {names}" if names else "") +
                           (f" ({c.summary})" if c.summary and not names else ""))
        elif c.state == "pending":
            reasons.append(f"CI is not finished on {short}" + (f": {c.summary}" if c.summary else ""))
    except ValueError as e:
        ci["state"] = "error"
        reasons.append(f"The project's required checks are not valid: {e}")
    except Exception as e:  # noqa: BLE001
        ci["state"] = "error"
        reasons.append(f"CI could not be read: {type(e).__name__}: {e}")

    # ── guardrails ──
    rails: list[dict] = []
    try:
        decisions = await hooks.run("pre_merge", uid, {
            "repo": repo, "number": number, "head": head, "base": ref.base,
            "ticket": base["ticket"], "url": ref.url})
        for d in decisions or []:
            rails.append({"allow": bool(d.allow), "reason": d.reason,
                          "integration": d.integration, "warn_only": bool(d.warn_only)})
            if not d.allow and not d.warn_only:
                reasons.append(f"Blocked by {d.integration or 'a guardrail'}: {d.reason or 'no reason given'}")
    except Exception as e:  # noqa: BLE001 - a guardrail that cannot be reached blocks
        rails.append({"allow": False, "reason": f"{type(e).__name__}: {e}",
                      "integration": "", "warn_only": False})
        reasons.append(f"The guardrails could not be checked: {type(e).__name__}: {e}")
    return _answer(not reasons, reasons, head=head, pr=brief, review=review, ci=ci,
                   guardrails=rails, **base)
