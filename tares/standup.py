"""The crew's standup report: what moved in each handed-over project over a time range.

`facts` reads it from the ledger (ticket history, tickets, the desk) so the numbers never come
from a model; `markdown` renders it for copying and Slack; `summary_prompt` asks the cell's model
for a line or two per project on top of the same facts.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import factory as factory_mod

DONE_STAGES = ("merged", "shipped")
WORKING_STAGES = ("doing", "review", "changes")
MAX_LIST = 12       # tickets listed per group; the rest are counted
SUMMARY_MAX_TOKENS = 1200

SUMMARY_SYSTEM = (
    "You write a software team's standup from facts a tool collected about each project the crew "
    "of coding agents works on. For each project write one or two plain sentences: what got done, "
    "what is moving, and what is stuck or waiting on the person. Use only the facts given; never "
    "invent work, numbers or names. Name tickets by their reference (T12, ENG-4) only when it "
    "helps. No headings, no lists, no emoji, no em dashes. Also write `overall`: one sentence for "
    "the whole crew. Answer with JSON only: {\"overall\": \"...\", \"projects\": {\"<project id>\": "
    "\"...\"}}.")


def _in(at, since: datetime, until: datetime) -> bool:
    return isinstance(at, datetime) and since <= at < until


def _pr_url(t: dict) -> str | None:
    pr = t.get("pr") or {}
    if pr.get("url"):
        return pr["url"]
    if pr.get("repo") and pr.get("number"):
        return f"https://github.com/{pr['repo']}/pull/{pr['number']}"
    return None


def _ticket(t: dict, **extra) -> dict:
    return {"ref": factory_mod.label(t), "title": t["title"], "holder": t.get("holder"),
            "pr": _pr_url(t), "url": t.get("url"), **extra}


def project_facts(store, uid: str, name: str, tickets: list[dict], history: list[dict],
                  since: datetime, until: datetime, progress: dict, finished_at=None) -> dict:
    """One project's standup: done, started, blocked, in review and in progress, plus the
    questions waiting on the person. `history` is this project's changes in the range."""
    by_id = {t["id"]: t for t in tickets}
    last_stage: dict[str, dict] = {}
    for c in history:
        if c["field"] == "stage" and c["ticket"] in by_id:
            last_stage[c["ticket"]] = c
    # what a ticket became inside the range counts, whatever happened to it since: a ticket merged
    # then and reopened later was still done in that range
    done, started, newly_blocked = [], [], set()
    for tid, c in last_stage.items():
        t = by_id[tid]
        if c["value"] in DONE_STAGES:
            done.append(_ticket(t, at=c["at"], stage=c["value"]))
        elif c["value"] == "doing":
            started.append(_ticket(t, at=c["at"]))
        elif c["value"] == "blocked":
            newly_blocked.add(tid)
    # a ticket closed in its home (Linear, GitHub) inside the range, whatever stages the crew
    # moved it through meanwhile; one the crew merged before the range is not closed again here
    counted = {x["ref"] for x in done}
    for t in tickets:
        if (factory_mod.label(t) not in counted and t["status"] == "done"
                and t["stage"] not in DONE_STAGES and _in(t.get("updated_at"), since, until)):
            done.append(_ticket(t, at=t["updated_at"], stage="done"))
    finished_refs = {x["ref"] for x in done}
    started = [x for x in started if x["ref"] not in finished_refs]
    done.sort(key=lambda x: x["at"])
    blocked = [_ticket(t, reason=t.get("stage_reason") or "", new=t["id"] in newly_blocked)
               for t in tickets if t["stage"] == "blocked" and not factory_mod.finished(t)]
    review = [_ticket(t, stage=t["stage"]) for t in tickets
              if t["stage"] in ("review", "changes") and not factory_mod.finished(t)]
    started_ids = {x["ref"] for x in started}
    doing = [_ticket(t) for t in tickets if t["stage"] == "doing" and not factory_mod.finished(t)
             and factory_mod.label(t) not in started_ids]
    desk = [{"label": d["label"], "question": d["question"], "blocking": d["blocking"],
             "permission": bool(d.get("command")), "asked_at": d["created_at"]}
            for d in store.list_desk("open", uid)]
    desk.sort(key=lambda d: (not d["blocking"], str(d["asked_at"])))
    moved = bool(history or done or _in(finished_at, since, until)
                 or any(_in(d["asked_at"], since, until) for d in desk))
    return {"id": uid, "name": name, "moved": moved,
            "finished": _in(finished_at, since, until),
            "progress": {"done": progress["tickets_done"], "total": progress["tickets_total"],
                         "milestone": progress["milestone"]},
            "done": done, "started": started, "doing": doing, "review": review,
            "blocked": blocked, "needs_you": desk}


def facts(store, since: datetime, until: datetime, progress_of, hide=()) -> dict:
    """Every open handed-over project, and the ones finished in the range. Projects that did
    not move are listed by name only, after the ones that did."""
    names = {p["id"]: p["name"] for p in store.list_projects()}
    history: dict[str, list[dict]] = {}
    for c in store.history_between(since, until):
        history.setdefault(c["project"], []).append(c)
    uids = [(u, None) for u in store.handed_over_projects(finished=False, taken=False)]
    for u in store.handed_over_projects(finished=True):
        at = ((store.get_handover(u) or {}).get("finished") or {}).get("at")
        if _in(at, since, until):
            uids.append((u, at))
    out = []
    for uid, finished_at in uids:
        if uid not in names or uid in hide:
            continue
        out.append(project_facts(store, uid, names[uid], store.list_tickets(uid),
                                 history.get(uid, []), since, until, progress_of(uid),
                                 finished_at))
    out.sort(key=lambda p: (not p["moved"], -len(p["done"]), p["name"].lower()))
    return {"since": since, "until": until,
            "projects": [p for p in out if p["moved"]],
            "quiet": [{"id": p["id"], "name": p["name"], "progress": p["progress"],
                       "blocked": len(p["blocked"]), "needs_you": len(p["needs_you"])}
                      for p in out if not p["moved"]]}


# ── rendering ─────────────────────────────────────────────────────────────────

def _range_words(since: datetime, until: datetime) -> str:
    # in the zone the range was asked in (the console sends its own offset)
    a, b = since, until.astimezone(since.tzinfo) if since.tzinfo else until
    if a.date() == b.date():
        return a.strftime("%b %-d")
    return f"{a.strftime('%b %-d')} to {b.strftime('%b %-d')}"


def _link(text: str, url: str | None) -> str:
    return f"[{text}]({url})" if url else text


def _line(x: dict, extra: str = "") -> str:
    ref = _link(x["ref"], x.get("url"))
    pr = f" ({_link('PR', x['pr'])})" if x.get("pr") else ""
    return f"- {ref} {x['title']}{pr}{extra}"


def _group(title: str, rows: list[dict], fmt) -> list[str]:
    if not rows:
        return []
    lines = [f"**{title}**"] + [fmt(x) for x in rows[:MAX_LIST]]
    if len(rows) > MAX_LIST:
        lines.append(f"- and {len(rows) - MAX_LIST} more")
    return lines + [""]


def markdown(report: dict, summary: dict | None = None) -> str:
    """The report as markdown: a heading per project with its numbers, the model's summary when
    there is one, then the tickets by group. Slack gets the same text through slack.to_mrkdwn."""
    summary = summary or {}
    per = summary.get("projects") or {}
    out = [f"# Crew standup, {_range_words(report['since'], report['until'])}", ""]
    if summary.get("overall"):
        out += [summary["overall"], ""]
    if not report["projects"]:
        out += ["Nothing moved in this range.", ""]
    for p in report["projects"]:
        pr = p["progress"]
        head = f"## {p['name']}: {pr['done']}/{pr['total']} done"
        if p["finished"]:
            head += ", finished"
        elif pr.get("milestone"):
            m = pr["milestone"]
            head += f", on {m['name']} ({m['done']}/{m['total']})"
        out += [head, ""]
        if per.get(p["id"]):
            out += [per[p["id"]], ""]
        out += _group("Done", p["done"], _line)
        out += _group("Started", p["started"], lambda x: _line(x, f", {x['holder']}" if x["holder"] else ""))
        out += _group("In review", p["review"],
                      lambda x: _line(x, ", changes asked" if x["stage"] == "changes" else ""))
        out += _group("Blocked", p["blocked"],
                      lambda x: _line(x, f": {x['reason']}" if x["reason"] else ""))
        out += _group("Needs you", p["needs_you"],
                      lambda d: f"- {d['label']} {d['question']}"
                                + (" (permission)" if d["permission"] else "")
                                + (" (blocking)" if d["blocking"] else ""))
    if report["quiet"]:
        out += ["**No movement:** " + ", ".join(
            f"{q['name']} ({q['progress']['done']}/{q['progress']['total']})"
            for q in report["quiet"]), ""]
    return "\n".join(out).rstrip() + "\n"


def summary_prompt(report: dict) -> str:
    """The facts the model summarises: only the projects that moved, without urls."""
    def strip(rows):
        return [{k: v for k, v in r.items() if k not in ("url", "pr", "asked_at", "at")}
                for r in rows]
    projects = [{"id": p["id"], "name": p["name"], "progress": p["progress"],
                 "finished_in_range": p["finished"],
                 **{k: strip(p[k]) for k in ("done", "started", "doing", "review", "blocked",
                                             "needs_you")}}
                for p in report["projects"]]
    return ("Standup range: " + _range_words(report["since"], report["until"]) + "\n\n"
            + json.dumps({"projects": projects,
                          "no_movement": [q["name"] for q in report["quiet"]]},
                         indent=1, default=str))


def parse_summary(text: str, ids: set[str]) -> dict:
    """The model's JSON, tolerating a code fence around it; unknown project ids are dropped."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    start, end = s.find("{"), s.rfind("}")
    try:
        data = json.loads(s[start:end + 1]) if start >= 0 else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    per = data.get("projects") if isinstance(data.get("projects"), dict) else {}

    def clean(v) -> str:
        s = str(v or "").replace(" \u2014 ", ", ").replace("\u2014", ", ")
        return " ".join(s.split())[:600]

    return {"overall": clean(data.get("overall")) or None,
            "projects": {k: clean(v) for k, v in per.items() if k in ids and clean(v)}}
