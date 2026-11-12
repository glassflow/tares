"""The factory ticket page (TR-760): GET /api/factory/projects/{uid}/tickets/{ref}.

One answer for the whole page about one ticket, so it reads like a small ticket system: its state,
its milestone, who works on it, what it depends on and what waits for it, its pull request, every
review round with its findings, the checks the builder ran, the calls the builder made and the
decisions the person gave, what happened to it, and the working notes. The ticket, its project and
the rest of the plan are read once.

Every string is worded for people, as on the project page (`factory_project`): a state is "in
review", who works on a ticket is "Builder for M1", a review is "passed", "asked for changes" or
"blocked", a finding is "open" or "fixed in round 3", a time is "3 days ago". The crew's folders
stay on the crew's machine: free text that names one shows the repo, and any other folder under a
home directory shows its last name. Only a link (`url`, `href`) starts with a slash. The one place a
station name appears is the terminal command that opens the builder's session, offered only while
someone works on the ticket.

A review round's "verified" and "not_verified" are the reviewer's own words, and a check's
"command" and "result" are the builder's; the page shows them as written (`data-words-exempt`).
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

from fastapi import HTTPException

from .. import factory as factory_mod
from .. import factory_api
from ..envelope import now_utc
from ..factory_home import _iso, plain_ago
from .factory_project import (_GONE, _ROLE_BY, _STAGE_WORD, _pr_out, _stamp, station_roles,
                              ticket_state, who_words)
from .factory_tickets import KEYS

_VERDICT_TEXT = {"pass": "passed", "changes": "asked for changes", "block": "blocked"}
_CALL_STATE = {"open": "waiting for you", "kept": "confirmed", "overturned": "you changed it"}
# a folder under a home directory that is not the crew's own: show its last name. It has to start
# there, or right after file://, not inside a word or a longer path (ui/src/components/Users/List.tsx
# and a link ending in /home/guide stay as written).
_HOME_PATH = re.compile(r"(?:(?<=file://)|(?<![\w.~/-]))(?:(?:/Users|/home)/[^/\s'\"`]+(?:/[^\s'\"`]*)?"
                        r"|~/[^\s'\"`]*)")
# a station of the crew named by its own pattern (crew-reviewer), whether or not it has a session
_CREW_NAME = re.compile(r"(?<![\w/.-])crew-(?:orchestrator|reviewer|releaser|builder|helper)(?![\w/-]|\.\w)")


def _state_label(st: str) -> str:
    return st[:1].upper() + st[1:]


def register(app, store, h, ctx):
    @app.get("/api/factory/projects/{uid}/tickets/{ref}")
    async def factory_ticket(uid: str, ref: str):
        """The whole ticket page in one read (404 for a project that is not a software factory
        project, for a draft, and for a ticket the project does not have)."""
        p = store.get_project(uid)
        if (p is None or p.get("kind") != "software_factory" or p.get("status") == "draft"
                or not h.can_see(p)):   # a private project the caller is not on is not there
            raise HTTPException(404, f"no factory project {uid!r}")
        t = store.get_ticket(uid, ref)
        if t is None:
            raise HTTPException(404, f"no ticket {ref!r} in this project")
        await factory_api.refresh_prs(store, uid)
        return await asyncio.to_thread(build, uid, p, t["id"])

    def build(uid: str, p: dict, tid: str) -> dict:
        now = now_utc()
        t = store.get_ticket(uid, tid)           # read after the pull requests were refreshed
        if t is None:
            raise HTTPException(404, "no such ticket")
        tickets = store.list_tickets(uid)
        by_id = {x["id"]: x for x in tickets}
        milestones = store.list_milestones(uid)
        number = {m["id"]: f"M{i + 1}" for i, m in enumerate(milestones)}
        rd = factory_mod.readiness(tickets)
        try:
            station_rows = store.station_sessions(h.cc_source())
        except Exception:
            station_rows = []
        roles = station_roles(station_rows)

        # the crew's folders stay on the crew's machine
        hidden = {}
        for r in (store.get_handover(uid) or {}).get("repos") or []:
            base = factory_mod.hidden_key(r.get("path"))
            if base:
                hidden[base] = r.get("name") or factory_mod.folder_name(base) or "the repo"

        def by_words(station, role=None) -> str | None:
            name = str(station or "").strip()
            role = role or roles.get(name)
            if not role and name.startswith("crew-"):
                role = name[len("crew-"):].split("-", 1)[0]
            if not role and "-build" in name:
                role = "builder"
            if role == "github":
                return "GitHub"
            return _ROLE_BY.get(role or "") or ("the crew" if name else None)

        # a station named in free text reads as its role: the stations that have a session, the ones
        # that hold a ticket here (when the name has a station's shape), and the crew's own names. A
        # name inside a path or a longer word (origin/main, src/crew-reviewer/x.md) is left alone.
        stations = {n for n in [*roles, *(x.get("holder") for x in tickets)]
                    if n and (n in roles or n.startswith("crew-") or "-build" in n)}
        station_re = (re.compile(r"(?<![\w/.-])(?:" + "|".join(
            re.escape(n) for n in sorted(stations, key=len, reverse=True)) + r")(?![\w/-]|\.\w)")
                      if stations else None)

        def clean(text):
            out = factory_mod.hide_paths(text, hidden)
            out = _HOME_PATH.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1], out)
            if station_re:
                out = station_re.sub(lambda m: by_words(m.group(0)) or "the crew", out)
            return _CREW_NAME.sub(lambda m: by_words(m.group(0)) or "the crew", out)

        def state_of(x: dict) -> str:
            done = factory_mod.finished(x)
            return ticket_state(x, [] if done else rd[x["id"]]["blocked_by"])

        def other(x: dict) -> dict:
            ref = factory_mod.label(x)
            return {"ref": ref, "title": clean(x["title"]), "state_label": _state_label(state_of(x)),
                    "href": f"/projects/{quote(uid, safe='')}/tickets/{quote(ref, safe='')}"}

        full = h.ticket_out(uid, t, with_doc=True)
        ref = full["label"]
        st = state_of(t)
        ms = t.get("milestone")
        ms_row = next(((i, m) for i, m in enumerate(milestones) if m["id"] == ms), None)
        done = factory_mod.finished(t)

        round_of = {r["id"]: r["round"] for r in full["reviews"]}

        def finding(f: dict) -> dict:
            if f["state"] == "open":
                state_text = "open"
            elif f.get("fixed_in") in round_of:
                state_text = f"fixed in round {round_of[f['fixed_in']]}"
            else:
                state_text = {"fixed": "fixed"}.get(f["state"], f["state"])
            path = clean(f.get("file"))
            if path.startswith(("/", "~")) or factory_mod.is_windows_path(path):   # a path that is not the repo's own
                path = path.replace("\\", "/").rsplit("/", 1)[-1]
            return {"label": f["label"], "text": clean(f["text"]), "file": path or None,
                    "line": f.get("line"), "blocking": bool(f["blocking"]), "state_text": state_text}

        reviews = [{"round": r["round"], "verdict_text": _VERDICT_TEXT.get(r["verdict"], r["verdict"]),
                    "by": by_words(r.get("by"), "reviewer"), "when": plain_ago(r["at"], now),
                    "at": _iso(r["at"]), "verified": clean(r["verified"]),
                    "not_verified": clean(r["not_verified"]) or None,
                    "findings": [finding(f) for f in r["findings"]]} for r in full["reviews"]]

        checks = [{"command": clean(c["command"]), "result": clean(c["result"]),
                   "when": plain_ago(c["at"], now)} for c in full["checks"]]

        calls = []
        for a in store.list_assumptions(uid, ticket=t["id"]):
            made = by_words(a.get("made_by"), a.get("made_role")) or "the crew"
            calls.append({"ref": a["label"], "question": clean(a["question"]),
                          "choice": clean(a["choice"]), "why": clean(a["why"]),
                          "made": f"by {made}, {plain_ago(a['made_at'], now)}".rstrip(", "),
                          "state_text": _CALL_STATE.get(a["state"], a["state"]),
                          "words": clean(a["words"]) or None})
        decisions = [{"question": clean(d["question"]),
                      **{k: clean(v) if v else None for k, v in factory_mod.decision_words(d).items()},
                      "when": plain_ago(d["at"], now)}
                     for d in full["decisions"] if ref in d["tickets"]]

        history = []
        first_stage = next((x for x in full["history"] if x["field"] == "stage"), None)
        for x in reversed(full["history"]):                    # newest first
            if x["field"] == "stage":
                if x["value"] == "changes":
                    what = "Changes asked"
                elif x["value"] in ("merged", "shipped"):
                    what = "Merged"
                elif x["value"] is None:
                    what = "Closed"
                elif x is first_stage and x["value"] == "doing":
                    what = "Started"
                else:
                    what = "Moved to " + _STAGE_WORD.get(x["value"], str(x["value"] or ""))
            elif x["field"] == "holder":
                what = (f"Handed to {by_words(x['value'])}" if x["value"]
                        else "Taken off whoever had it")
            else:
                what = "Changed the " + str(x["field"]).replace("_", " ")
            history.append({"what": what, "who": by_words(x.get("by"), x.get("role")),
                            "when": plain_ago(x["at"], now), "at": _iso(x["at"]),
                            "text": clean(x["reason"]) or None})

        # the builder's session opens in a terminal only while someone works on the ticket
        terminal = []
        holder = t.get("holder")
        if holder and not done:
            mine = [r for r in station_rows if r.get("station") == holder]
            newest = max(mine, key=lambda r: _stamp(r.get("last_at")), default=None)
            if (newest and newest.get("role") == "builder"
                    and factory_mod.shown_state(newest.get("state") or "working", newest.get("last_at"),
                                                now) not in _GONE):
                terminal = [{"label": "Open the builder's session",
                             "command": factory_mod.attach_command(holder)}]

        # what a blocked ticket waits for: the tickets it depends on that have not delivered (a
        # canceled one did not), or, when a station marked it blocked, that no ticket is named
        waiting = [by_id[d] for d in t["depends_on"] if d in by_id and not factory_mod.delivered(by_id[d])]
        reason = None
        if st == "blocked":
            reason = ("Waiting for " + " and ".join(
                f"{factory_mod.label(w)}, {clean(w['title'])}"
                + (" (canceled)" if w.get("status") == "canceled" else "") for w in waiting) + "."
                if waiting else "Blocked, but no other ticket is in the way.")

        owner = t.get("owner")
        return {"ref": ref, "title": clean(t["title"]), "state": KEYS[st],
                "state_label": _state_label(st),
                "milestone": ({"id": ms_row[1]["id"], "number": f"M{ms_row[0] + 1}",
                               "name": clean(ms_row[1]["name"])} if ms_row else None),
                "in": {"linear": "Linear", "github": "GitHub"}.get(owner or "", "Tares"),
                "url": t.get("url"),
                "who": None if done and not holder else who_words(holder, roles, number.get(ms)),
                "blocked_reason": reason,
                "depends_on": [other(by_id[d]) for d in t["depends_on"] if d in by_id],
                "blocks": [other(x) for x in tickets if t["id"] in x["depends_on"]],
                "pr": _pr_out(t), "reviews": reviews, "checks": checks, "calls": calls,
                "decisions": decisions, "history": history,
                "notes": clean(full.get("working_doc_body")), "terminal": terminal,
                "project": {"id": p["id"], "name": p["name"], "icon": p.get("icon"),
                            "href": f"/projects/{quote(p['id'], safe='')}"},
                "now": _iso(now)}
