"""The merge queue page (TR-1094): GET /api/projects/{uid}/merge-queue.

One answer for the page, in the shape of the TR-1093 mock: the pull requests this project has in
the merge queue, grouped by repo and base branch in queue order, and every sentence the page shows
as a string worded for people, so the screen only displays them. Nothing is written: the route
reads the queue's rows, the tickets with the pull request as GitHub last said it (the sync keeps
it), the reviews, the desk and the decisions. It does not call `merge_ready`, which reads GitHub
and runs the guardrails; the review, CI and guardrail lines come from what is already stored.

A group is {repo, base, title, count, auto, sub, line, you, blocked, sent, rows, merged}. A row is
{id, position, state, chip, ticket, pr, branch, builder, review, ci, guardrail, status, answer,
bounce, needs_you, turn, turn_label, whose}:
- `position` is the place in line (1, 2, ...); a pull request sent back to its builder has none.
  It counts this project's pull requests only, so a queued row reads "Next in this project" or
  "2nd in this project", never a place across the projects the crew works on.
- `chip` is {key, text}: the badge, in words.
- `status` is {lead, text, note}: what is going on, in a sentence. `lead` is the start of `text`
  the page may set in bold; `note` is the second line.
- `answer` is set for a pull request that waits for a person's yes (or whose answer is in), and
  tells the page what to show and do: {kind, text, lead, note, item: {id, label}, can_answer,
  for, asked, now, decision, by, where, when, commit}. `kind` is mine (the caller may answer),
  other (someone else's question), moved (the pull request changed since the question), done (it
  was answered), withdrawn, unrecorded or missing. `item.id` goes to POST /api/desk/{ref}/reply and
  `id` of the row goes to POST /api/factory/merge-queue/{mid}/approve, both only when `can_answer`.
  A transient state of the page (answering, failed) is the page's own.
- `bounce` is set for a pull request sent back: {count, text}.
- `turn`, `turn_label`, `whose`: whose turn the ticket is (factory.turn_of), the same words the
  Tickets, project and Reviews pages show. The chip says what the queue does with it.

Where an answer was given: the decision records only `via`, "console" or "slack" (or nothing, for
an answer from a session). The console's own merge queue approval records fixed words (see
`merge_queue.APPROVE_WORDS`), so that one is told apart; an answer given on Home, on the project
page or on the Calls page is the same "console" decision and cannot be. See `where_of`.

Every string is worded for people: no state key, no stage name, no em dash.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, Request

from .. import factory as factory_mod
from .. import merge_queue
from .. import status_words
from ..envelope import now_utc
from ..factory_home import _dt, plain_ago
from ..integrations import registry
from ..deps import Deps

MERGED_SHOWN = 3               # merged pull requests listed under a group
MERGED_WINDOW = 3 * 86400      # seconds a merged pull request stays in that list (the mock shows 2 days)
LADDER_SENTENCES = (
    "Sent back {n} of 3 times. At 3 the orchestrator takes it.",
    "Sent back {n} times. The orchestrator is on it. At 5 it comes to you.",
    "Sent back {n} times. It is now a question for you on the Calls page.",
)
_BLOCKED_BY = re.compile(r"^Blocked by ([^:]+): (.*)$", re.S)
_RULES = "GitHub will not merge it: "
_UNKNOWN = "GitHub cannot tell whether it merges"
_SAID = "The person said: "
_NO = re.compile(r"no\b", re.I)

INTRO = ("One pull request merges at a time for each repo and base branch, in milestone and "
         "ticket order. No agent and no tokens are used.")
EMPTY = "Nothing is waiting to merge. A pull request shows here once its review is approved."
FOOT_END = "That is the whole queue. Merged and closed pull requests are on the Pull requests tab."


def where_of(decision: dict) -> dict:
    """{key, words, phrase} for where a decision was given. The decision records only `via`
    ("console", "message" or "slack", and "feed" on old rows; none for a session). The merge queue page's own approval is told apart
    by the words and choice it records. Home, the project page and the Calls page all record a
    plain console answer, so they cannot be told apart: any other console answer reads "in the
    console"."""
    via = decision.get("via")
    if (via == "console" and decision.get("words") == merge_queue.APPROVE_WORDS
            and decision.get("choice") == merge_queue.APPROVE_CHOICE):
        return {"key": "merge_queue", "words": "merge queue page", "phrase": "on the merge queue page"}
    if via == "slack":
        return {"key": "slack", "words": "Slack", "phrase": "in Slack"}
    if via == "feed":
        return {"key": "feed", "words": "the feed", "phrase": "from the feed"}
    if via == "message":
        return {"key": "message", "words": "a message", "phrase": "from a message"}
    if via == "console":
        return {"key": "console", "words": "console", "phrase": "in the console"}
    return {"key": "session", "words": "Claude session", "phrase": "in a Claude session"}


def _first(name: str) -> str:
    return (name or "").split()[0] if (name or "").strip() else ""


def _short(sha) -> str:
    return str(sha or "")[:7]


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _sentence(text: str) -> str:
    one = " ".join(str(text or "").split())
    return one if one.endswith((".", "!", "?")) else one + "."


def _ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st."""
    tail = 0 if 10 <= n % 100 <= 20 else n % 10
    return f"{n}{({1: 'st', 2: 'nd', 3: 'rd'}).get(tail, 'th')}"


def _upper(text: str) -> str:
    return text[:1].upper() + text[1:]


def register(app, h: Deps):
    store = h.store
    def guard_name(name: str) -> str:
        """A guardrail as a person reads it: its title, else its key with spaces."""
        name = (name or "").strip()
        cls = registry.get("guardrails", name) if name else None
        return (getattr(cls, "title", "") or name.replace("_", " ").replace("-", " ") or "a guardrail")

    def who_am_i(request: Request) -> str | None:
        """The caller's person id: the key's person, the person `local` for the system
        credential, no one for a key with no person (it cannot answer)."""
        person = h.current_person(request)
        if person:
            return person["id"]
        return "local" if h.is_system(request) else None

    def can_answer(d: dict, me: str | None, p: dict, request: Request) -> bool:
        """The rule of POST /api/desk/{ref}/reply and of the approve route for a question
        (`desk_can_act`): the person it is addressed to; for an old unaddressed one, the person
        `local`, an admin or the project's lead. A crew station is refused by those routes too;
        a read cannot tell a station from a person, so this does not look at it."""
        if me is None:
            return False
        to = d.get("to_person_id")
        if to:
            return to == me
        if me == "local" or h.is_admin(request):
            return True
        lead = (p or {}).get("lead_person_id")
        return lead == me or (me == "local" and not lead)

    def person_name(pid: str | None, row_person: dict | None = None) -> str:
        if row_person and row_person.get("name"):
            return row_person["name"]
        x = store.get_person(pid) if pid else None
        return (x or {}).get("name") or (x or {}).get("email") or ""

    # ── the lines of a row ──
    def review_text(t: dict, reviews: list[dict], gate) -> str:
        last = factory_mod.standing_round(reviews)
        who = ((last or {}).get("person") or {}).get("name") or (gate[1] if gate else "the crew reviewer")
        if not last:
            return f"Waiting for {gate[1]}" if gate else "Not reviewed yet"
        head = (t.get("pr") or {}).get("head")
        old = bool(head and last.get("head") and not factory_mod.same_commit(head, last["head"]))
        if last["verdict"] == "pass":
            return f"Approved by {who}" + (" for an older commit" if old else "")
        if old:
            return "Not reviewed yet"
        if last["verdict"] == "block":
            first = next((x["text"] for x in last.get("findings") or [] if x.get("blocking")), None)
            return status_words.reviewer_stopped(_first_line(first) if first else None, who)
        return f"{_upper(who)} asked for changes"

    def ci_text(t: dict, row: dict) -> str:
        pr = t.get("pr") or {}
        state = pr.get("ci")
        checks = pr.get("ci_checks") or []
        if state == "success":
            return status_words.CI["success"]
        if state == "none":
            return f"{status_words.NO_CI} on this repo"
        if state == "failure":
            names = [c.get("name") for c in checks if c.get("state") == "failure" and c.get("name")]
            return f"{status_words.CI['failure']}: " + (", ".join(names) if names else "a check")
        if state == "pending":
            if row["state"] == "waiting_ci":
                return f"{status_words.CI['pending']}, restarted on the new commit"
            names = [c.get("name") for c in checks if c.get("state") == "pending" and c.get("name")]
            return status_words.CI["pending"] + (f", waiting for {', '.join(names)}" if names else "")
        return "Not checked yet"

    def guardrail_text(row: dict, history: list[dict]) -> str:
        m = _BLOCKED_BY.match(row.get("reason") or "")
        if m:
            return f"Blocked by {guard_name(m.group(1))}: {_sentence(m.group(2)).rstrip('.')}"
        since = row["entered_at"]
        warns = [r for r in history if r["field"] == "guardrail_warn" and _dt(r["at"]) >= _dt(since)]
        if warns:
            r = warns[-1]
            source = (r.get("reason") or "").split(": ", 1)[-1]
            return f"Warning from {guard_name(source)}: {str(r['value']).rstrip('.')}"
        return "Allowed"

    # ── the answer of a pull request that waits for a person ──
    def answer_of(row, t, d, decisions, me, p, request, now) -> dict:
        head_now = (t.get("pr") or {}).get("head") or row["head"]
        asked = _short(row["head"])
        ago = plain_ago(d.get("created_at"), now) if d else ""
        asked_line = f"Asked {ago} for commit {asked}." if ago else f"Asked for commit {asked}."
        out = {"kind": "", "text": "", "lead": "", "note": "", "item": None, "can_answer": False,
               "for": None, "asked": asked, "now": _short(head_now), "decision": None, "by": None,
               "where": None, "when": "", "commit": asked}
        if d:
            out["item"] = {"id": d["id"], "label": d.get("label")}
            to = d.get("to_person_id")
            out["for"] = ({"id": to, "name": person_name(to, d.get("to_person"))} if to else None)
        mine = bool(d) and can_answer(d, me, p, request)
        if row["state"] == "waiting_approval" and not factory_mod.same_commit(head_now, row["head"]):
            lead = "The commit changed, so this question is out of date."
            out.update(kind="moved", lead=lead,
                       text=(f"{lead} It was asked for commit {asked}, and the pull request is now "
                             f"at {_short(head_now)}."),
                       note="It cannot be answered here. Tares asks again once CI has passed on the new commit.")
            return out
        if d is None:
            out.update(kind="missing", text="The question is gone, so Tares asks again on its next check.",
                       note="Nothing to do here.")
            return out
        if d["state"] == "open":
            out["can_answer"] = mine
            if mine:
                out.update(kind="mine", lead="Waiting for your yes.", text="Waiting for your yes.",
                           note=(f"{asked_line} Your answer counts for this commit only; a new commit "
                                 "asks again. It is the same question as on Overview and the project "
                                 "page, so answering it here closes it there too."))
            else:
                name = (out["for"] or {}).get("name") or "someone else"
                out.update(kind="other", lead=f"Waiting for {name}’s yes.",
                           text=f"Waiting for {name}’s yes. Only the person this question is for can answer it.",
                           note=f"{asked_line} You see it read only; the answer shows here once it is given.")
            return out
        if d["state"] == "withdrawn":
            out["can_answer"] = mine
            lead = "The question was withdrawn, so this pull request is on hold."
            out.update(kind="withdrawn", lead=lead, text=lead,
                       note=("Approve it here, or a new commit asks again." if mine else
                             "Only the person it was for can approve it, or a new commit asks again."))
            return out
        dec = decisions.get(d["id"])
        if dec is None:
            lead = "The question was answered, but no answer was recorded, so this pull request is on hold."
            out.update(kind="unrecorded", lead=lead, text=lead,
                       note="A new commit asks again.")
            return out
        yes, said = merge_queue.judge(dec)
        who_p = dec.get("person") or {}
        # the person the decision records; a session answer records none, and the person the
        # question was addressed to is not credited with it
        who = ("you" if who_p.get("id") and who_p.get("id") == me else who_p.get("name") or None)
        where = where_of(dec)
        when = plain_ago(dec.get("at"), now)
        line = _first_line(said)
        lead = ("Yes, merge." if yes else "No, hold." if _NO.match(said) else
                f"Held: {line}." if line else "Held.")
        out.update(kind="done", lead=lead, decision="yes" if yes else "no", by=who,
                   where={"key": where["key"], "words": where["words"]}, when=when,
                   text=(f"{lead} Answered {f'by {who} ' if who else ''}{where['phrase']}, {when}, "
                         f"for commit {asked}."),
                   note=("The question is closed on Overview, the project page and the Calls page. It merges in its turn."
                         if yes else
                         "The question is closed everywhere. The merge queue skips this pull request; a new commit asks again."))
        return out

    def _first_line(text: str, limit: int = 120) -> str:
        one = " ".join(str(text or "").split()).rstrip(".")
        return one if len(one) <= limit else one[:limit - 3].rstrip() + "..."

    @app.get("/api/projects/{uid}/merge-queue")
    def merge_queue_page(uid: str, request: Request):
        """The merge queue page in one read (404 for a project that is not a software factory
        project, for a draft, and for a private project the caller is not on)."""
        p = store.get_project(uid)
        if (p is None or p.get("kind") != "software_factory" or p.get("status") == "draft"
                or not h.can_see(p)):
            raise HTTPException(404, f"unknown project {uid!r}")
        now = now_utc()
        me = who_am_i(request)
        tickets = store.list_tickets(uid)
        by_id = {t["id"]: t for t in tickets}
        g = {"by_id": by_id, "ms": {m["id"]: m["position"] for m in store.list_milestones(uid)}}
        level = merge_queue.autonomy_of(store, uid)
        handover = store.get_handover(uid) or {}
        decisions: dict[str, dict] = {}
        for dec in store.list_decisions(uid, all_=True):        # newest first: the first one wins
            if dec.get("desk_item"):
                decisions.setdefault(dec["desk_item"], dec)
        from .. import factory_api
        gates: dict = {}
        from .factory_project import Turns
        turns = Turns(store)

        def builder_of(t: dict) -> dict | None:
            pid = t.get("assignee_person_id") or p.get("lead_person_id") or \
                (handover.get("person") or {}).get("id")
            name = person_name(pid)
            return {"id": pid, "name": name, "text": f"{_first(name)}’s builder"} if name else None

        def row_out(row: dict, pos: int | None, first_queued: bool) -> dict:
            t = by_id.get(row["ticket"]) or {}
            pr = t.get("pr") or {}
            reviews = store.ticket_reviews(t["id"]) if t else []
            history = store.ticket_history(t["id"]) if t else []
            gate = factory_api.outside_gate(store, uid, gates)
            d = store.get_desk_item(row["desk_item"]) if row.get("desk_item") else None
            reason = row.get("reason") or ""
            state = row["state"]
            answer = None
            bounce = None
            waits = [factory_mod.label(by_id[x]) for x in (t.get("depends_on") or [])
                     if x in by_id and not factory_mod.delivered(by_id[x])]
            status = {"lead": "", "text": "", "note": ""}
            chip = {"key": state, "text": ""}
            you = blocked = False
            held_by_person = False
            if state in ("waiting_approval",) or (state == "blocked" and d is not None and (
                    reason.startswith(_SAID) or reason in (
                        "The question was withdrawn",
                        "The question was answered but no answer was recorded"))):
                answer = answer_of(row, t, d, decisions, me, p, request, now)
                kind = answer["kind"]
                status = {"lead": answer["lead"], "text": answer["text"], "note": answer["note"]}
                if kind == "mine":
                    chip, you = {"key": "approval", "text": "Needs your yes"}, True
                elif kind == "other":
                    chip = {"key": "other", "text": f"Waiting for {_first((answer['for'] or {}).get('name') or '') or 'someone else'}"}
                elif kind == "moved":
                    chip = {"key": "moved", "text": "Commit changed"}
                elif kind == "done" and answer["decision"] == "yes":
                    chip = {"key": "approved", "text": "Approved"}
                elif kind == "withdrawn" and answer["can_answer"]:
                    chip, you = {"key": "hold", "text": "On hold"}, True
                elif kind == "missing":
                    chip = {"key": "asking", "text": "Asking again"}
                else:
                    chip = {"key": "hold", "text": "On hold"}
                held_by_person = state == "blocked"
            elif state == "merging":
                chip, status["text"] = {"key": "merging", "text": "Merging"}, "Merging now."
            elif state == "waiting_ci":
                ago = plain_ago(row["updated_at"], now)
                chip = {"key": "ci", "text": "Waiting for CI"}
                status["text"] = (f"Branch updated to {row['base']} {ago}, waiting for CI on the new commit.")
            elif state == "queued":
                if waits:
                    chip = {"key": "queued", "text": "Queued"}
                    status["text"] = f"Waiting for {', '.join(waits)} to merge."
                elif first_queued:
                    chip = {"key": "next", "text": "Next"}
                    status["text"] = "Next in this project."
                else:
                    chip = {"key": "queued", "text": "Queued"}
                    status["text"] = f"{_ordinal(pos)} in this project." if pos else "In line."
            elif state == "blocked":
                blocked = True
                chip = {"key": "blocked", "text": "Blocked"}
                if reason == merge_queue.no_write_reason(row["repo"]):
                    status = {"lead": f"No GitHub connection can write to {row['repo']}.",
                              "text": f"No GitHub connection can write to {row['repo']}.",
                              "note": "One question on the Calls page covers every blocked pull request in this repo."}
                elif reason.startswith(_RULES):
                    msg = reason[len(_RULES):].strip().rstrip(".")
                    status = {"lead": "Blocked by repo rules:", "text": f"Blocked by repo rules: GitHub says {msg}.",
                              "note": "Fix the rule in GitHub, or merge it by hand."}
                elif reason == _UNKNOWN:
                    status = {"lead": "Blocked by GitHub:",
                              "text": "Blocked by GitHub: it cannot tell whether the pull request merges.",
                              "note": "Look at the pull request in GitHub, or merge it by hand."}
                else:
                    status = {"lead": "Blocked:", "text": f"Blocked: {_sentence(_first_line(reason) or 'no reason was given')}",
                              "note": ""}
            elif state == "sent_back":
                n = merge_queue.bounces(store, row["ticket"])
                step = 0 if n < 3 else (1 if n < 5 else 2)
                chip = {"key": "sent_back", "text": "Sent back"}
                why = _first_line(reason) or "it needs changes"
                status = {"lead": "Sent back to the builder:", "text": f"Sent back to the builder: {why}.",
                          "note": ""}
                bounce = {"count": n, "text": LADDER_SENTENCES[step].format(n=n)}
            if held_by_person:
                blocked = False
            # the row's own state, with a recorded answer read in: a yes makes it next to merge,
            # a no holds it until a person moves it
            turn_state = factory_mod.queue_state_of(row, pr.get("head"))
            if answer and answer["kind"] == "done":
                turn_state = "approved" if answer["decision"] == "yes" else "blocked"
            pr_n = row["pr"]
            return {
                "id": row["id"], "position": pos, "state": state, "chip": chip,
                "ticket": {"ref": factory_mod.label(t) if t else "", "title": t.get("title") or pr.get("title") or ""},
                "pr": {"number": pr_n, "url": pr.get("url") or
                       f"https://github.com/{row['repo']}/pull/{pr_n}"},
                "branch": pr.get("branch") or "",
                "builder": builder_of(t) if t else None,
                "review": review_text(t, reviews, gate) if t else "Not reviewed yet",
                "ci": ci_text(t, row), "guardrail": guardrail_text(row, history),
                "status": status, "answer": answer, "bounce": bounce,
                "needs_you": you or blocked, "_you": you, "_blocked": blocked,
                **(turns.of(uid, t, waits, factory_mod.standing_round(reviews), turn_state) if t else
                   {"turn": None, "turn_label": "", "whose": None}),
            }

        def merged_item(row: dict) -> dict | None:
            t = by_id.get(row["ticket"]) or {}
            at = row["updated_at"]
            if (_dt(now) - _dt(at)).total_seconds() > MERGED_WINDOW:
                return None
            done = [r for r in store.ticket_history(t["id"]) if r["field"] == "stage" and r["value"] == "merged"] \
                if t else []
            if done and done[-1].get("by") == merge_queue.BY:
                by = "the merge queue"
            else:
                login = (t.get("pr") or {}).get("merged_by")
                known = store.find_person_by_github(login) if login else None
                who = (known or {}).get("name") or login
                by = f"{who} on GitHub" if who else "GitHub"
            return {"n": row["pr"], "tk": factory_mod.label(t) if t else "",
                    "title": t.get("title") or (t.get("pr") or {}).get("title") or "",
                    "when": plain_ago(at, now), "by": by}

        rows = [r for r in store.merge_queue_open() + store.merge_queue_sent_back()
                if r["project_uid"] == uid]
        rows.sort(key=lambda r: (merge_queue._key(g, r), r["id"]))
        keys = sorted({(r["repo"], r["base"]) for r in rows})
        groups = []
        for repo, base in keys:
            mine = [r for r in rows if r["repo"] == repo and r["base"] == base]
            line_rows = [r for r in mine if r["state"] != "sent_back"]
            back_rows = [r for r in mine if r["state"] == "sent_back"]
            first_q = next((r["id"] for r in line_rows if r["state"] == "queued"), None)
            out_rows = []
            for i, r in enumerate(line_rows + back_rows):
                pos = i + 1 if r["state"] != "sent_back" else None
                out_rows.append(row_out(r, pos, r["id"] == first_q))
            you = sum(1 for o in out_rows if o["_you"])
            blocked = sum(1 for o in out_rows if o["_blocked"])
            for o in out_rows:
                o.pop("_you")
                o.pop("_blocked")
            auto = ("Merges on its own (L4-ship)" if level == merge_queue.AUTO
                    else "Waits for your yes on each merge (L3-review)")
            bits = [auto]
            if you:
                bits.append(f"{you} waits for you" if you == 1 else f"{you} wait for you")
            if blocked:
                bits.append(f"{blocked} blocked")
            if back_rows:
                bits.append(f"{len(back_rows)} sent back")
            merged = []
            for r in sorted((x for x in store.merge_queue_list(repo, base)
                             if x["state"] == "merged" and x["project_uid"] == uid),
                            key=lambda x: (x["updated_at"], x["id"]), reverse=True):
                item = merged_item(r)
                if item:
                    merged.append(item)
                if len(merged) == MERGED_SHOWN:
                    break
            groups.append({
                "repo": repo, "base": base, "title": f"{repo} into {base}",
                "count": f"{len(line_rows)} in line", "auto": auto, "sub": " · ".join(bits),
                "line": len(line_rows), "you": you, "blocked": blocked, "sent": len(back_rows),
                "rows": out_rows,
                "merged": ({"title": f"Merged just now, {len(merged)}", "items": merged} if merged else None),
            })
        line = sum(x["line"] for x in groups)
        need = sum(x["you"] + x["blocked"] for x in groups)
        need_text = f"{need} need{'s' if need == 1 else ''} you"
        aside = (f"{line} in line" + (f", {need_text}" if need else "")) if groups else ""
        note = ((f"{_count(line, 'pull request')} {'is' if line == 1 else 'are'} in the merge queue"
                 + (f", {need_text}" if need else "") + ".") if groups else "Nothing is in the merge queue.")
        return {
            "project": {"id": p["id"], "name": p.get("name") or ""},
            "aside": aside, "intro": INTRO if groups else "", "empty": "" if groups else EMPTY,
            "tab": {"line": line, "need": need, "need_text": need_text if need else "", "note": note},
            "footer": [f"Milestone pull requests from {merge_queue.MILESTONE_BRANCHES[0]} are merged by you.",
                       FOOT_END],
            "groups": groups,
        }
