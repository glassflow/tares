"""The AI SRE Runbooks and memory screen (TR-753): GET /api/sre/runbooks, and the two writes
POST /api/sre/runbooks/{service}/lines and DELETE /api/sre/runbooks/{service}/lines/{index}
(the service may hold a slash, so the route takes the whole path before /lines).

Per service, what the investigator should know. A runbook is written by people and read by the
agents; memory is what the agents wrote back with remember(), shown beside it and never edited here.

Storage: one shared skill per service named runbook-<service> (tares/skills.py), wired into every
project that has a watcher or an investigator (sre.roles()), so the agents list it on their next
check and load it on demand. Its body is a heading and one markdown bullet per line, each ending
" (by <name>, <date>)". People add and remove lines here; shared_skills.updated_by records the last
person to change it. When the last line goes the skill goes with it (a skill needs a body).
A service whose name is not a valid skill name (capitals, dots) gets its name cleaned and a short
hash added, so two services never share a runbook.

Memory is the events of the memory sources whose key is exactly the service, newest first. They
carry no author, so they are shown as the Investigator unless the note names an agent.

The page reads only the GET. Both writes answer the new screen. Writes are admin scope (the
daemon's own check: a read key gets 403). Only the AI SRE edition has these routes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from .. import docs
from .. import edition as edition_mod
from .. import skills, sre
from ..envelope import now_utc
from ..store import day_month_words

PREFIX = "runbook-"
MEMORY_PER_SERVICE = 20
LOOKBACK_DAYS = 7
MAX_BY = 60
# a runbook line ends with who added it and the day
STAMP = re.compile(r"^(.*) \(by (.+), (\d{4}-\d{2}-\d{2})\)$")

_lock = threading.Lock()     # a line is added or removed by reading the body and writing it back


def skill_name(service: str) -> str:
    """The shared skill that holds a service's runbook. A name that is already a valid skill name
    is used as it is; any other is cleaned and gets a short hash so it stays its own."""
    slug = re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-")
    if slug == service and len(PREFIX + slug) <= 64:
        return PREFIX + slug
    tag = hashlib.sha1(service.encode("utf-8")).hexdigest()[:6]
    slug = slug[:64 - len(PREFIX) - 7].strip("-")
    return f"{PREFIX}{slug}-{tag}" if slug else f"{PREFIX}{tag}"


def _description(service: str) -> str:
    one = " ".join(service.split())
    return (f"What to know when looking into {one}: lines people and agents added, newest last. "
            "Read it when a check or an alert is about this service.")


# ── the body ─────────────────────────────────────────────────────────────────
def _bullets(body: str) -> list[int]:
    """The positions (in body.splitlines()) of the lines that are runbook lines."""
    return [i for i, line in enumerate(str(body or "").splitlines()) if line.startswith("- ")]


def parse_line(line: str) -> tuple[str, str, str | None]:
    """(text, who, day) of a bullet; who and day are empty/None when the line has no stamp (a line
    someone wrote into the skill by hand)."""
    text = line[2:].strip()
    m = STAMP.match(text)
    return (m.group(1).strip(), m.group(2).strip(), m.group(3)) if m else (text, "", None)


def lines_of(body: str) -> list[tuple[str, str, str | None]]:
    all_lines = str(body or "").splitlines()
    return [parse_line(all_lines[i]) for i in _bullets(body)]


def when_words(day, today) -> str:
    """"today", "yesterday", "3 days ago", then the date. `day` is a date, an ISO day or a time."""
    if isinstance(day, str):
        try:
            day = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return ""
    if isinstance(day, datetime):
        day = (day if day.tzinfo else day.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()
    if day is None:
        return ""
    n = (today - day).days
    if n <= 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 60:
        return f"{n} days ago"
    return day_month_words(day) + ("" if day.year == today.year else f" {day.year}")


def lines_words(n: int) -> str:
    return "No lines yet" if n == 0 else "1 line" if n == 1 else f"{n} lines"


def _clean_who(who: str) -> str:
    return " ".join(str(who or "").split())[:MAX_BY] or "console"


# ── memory ───────────────────────────────────────────────────────────────────
def _memory_by(payload) -> str:
    try:
        p = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        p = None
    p = p if isinstance(p, dict) else {}
    fields = p.get("fields") if isinstance(p.get("fields"), dict) else {}
    name = p.get("agent") or p.get("by") or fields.get("agent") or fields.get("by")
    name = " ".join(str(name or "").replace("_", " ").replace("-", " ").split())[:MAX_BY]
    return name[:1].upper() + name[1:] if name else "Investigator"


# ── the screen ───────────────────────────────────────────────────────────────
def _used_words(counts: dict, roles: dict) -> str | None:
    inv = {i["agent"] for i in roles["investigators"]}
    n_inv = sum(n for a, n in counts.items() if a in inv)
    n_other = sum(n for a, n in counts.items() if a not in inv)
    total = n_inv + n_other
    if total <= 0:
        return None
    who = "the investigator" if not n_other else \
        "the watcher" if not n_inv else "the watcher and the investigator"
    times = "once" if total == 1 else f"{total} times"
    return f"Read by {who} {times} this week"


def build(store, catalog, service: str | None = None) -> dict:
    """The whole screen. `service`: the one service to show (already resolved by the caller)."""
    now = now_utc()
    today = now.astimezone(timezone.utc).date()
    names = [service] if service else [i["name"] for i in sre.list_services(store, catalog)]
    stored = {s["name"]: s for s in store.list_all_skills() if s["name"].startswith(PREFIX)}
    mem_sources = [s.name for s in catalog.sources.values() if s.connector == "memory"]
    notes = store.memory_notes(mem_sources, MEMORY_PER_SERVICE)
    roles = sre.roles(store, catalog, include_paused=True)
    agents = sorted({w["agent"] for w in roles["watchers"]} | {i["agent"] for i in roles["investigators"]})
    counts = store.skill_load_counts(agents, LOOKBACK_DAYS)
    out = []
    for name in names:
        sk = stored.get(skill_name(name))
        runbook = [{"text": t, "by": who, "when_words": when_words(day, today) if day else ""}
                   for t, who, day in lines_of(sk["body"] if sk else "")]
        memory = [{"text": str(text or ""), "by": _memory_by(payload),
                   "when_words": when_words(at, today)}
                  for at, text, payload in notes.get(name, [])]
        out.append({"name": name, "lines_words": lines_words(len(runbook)), "runbook": runbook,
                    "memory": memory,
                    "used_words": _used_words(counts.get(skill_name(name), {}), roles)})
    if not service:   # the services with something written first, the rest in the order they came
        out.sort(key=lambda s: not (s["runbook"] or s["memory"]))
    empty = None if out else ("No services yet. Once your connections send data, each service "
                              "gets a page here for what the investigator should know.")
    return {"services": out, "empty_words": empty}


def register(app, store, h, ctx):
    runtime = ctx.runtime

    def only_sre():
        if edition_mod.edition(store)[0] != "sre":
            raise HTTPException(404, "There are no runbooks on this kind of cell")

    def resolve(service: str) -> str:
        item = sre.get_service(store, runtime.catalog, service)
        if item is None:
            raise HTTPException(404, f"No service called {service[:80]!r}")
        return item["name"]

    def screen(service: str | None = None) -> dict:
        return build(store, runtime.catalog, resolve(service) if service else None)

    def role_projects() -> list[str]:
        roles = sre.roles(store, runtime.catalog, include_paused=True)
        found = [w["project"] for w in roles["watchers"]] + [i["project"] for i in roles["investigators"]]
        return list(dict.fromkeys(p for p in found if p))

    def wired_projects() -> list[str]:
        out = role_projects()
        if not out:
            raise HTTPException(409, "Set up a watcher and an investigator first, so there is "
                                     "someone to read the runbook.")
        return out

    def save(name: str, service: str, body: str, who: str, projects: list[str]) -> None:
        try:
            skills.validate(name, _description(service), body)
        except skills.SkillError:
            raise HTTPException(400, "This runbook is full. Remove a line before adding another.")
        store.upsert_skill(projects[0], name, _description(service), body, updated_by=who)
        for p in projects[1:]:
            store.use_skill(p, name)

    def add_line(service: str, text, who: str) -> dict:
        svc = resolve(service)
        name = skill_name(svc)
        line = " ".join(str(text or "").split())
        if not line:
            raise HTTPException(400, "Write the line to add.")
        if len(line) > docs.MAX_LINE:
            raise HTTPException(400, f"That line is {len(line)} characters. Keep it to {docs.MAX_LINE}.")
        projects = wired_projects()
        with _lock:
            cur = store.get_skill(None, name)
            body = cur["body"] if cur else f"# Runbook for {svc}\n\n"
            have = "".join(f"- {t}\n" for t, _, _ in lines_of(body))
            try:
                added, changed = docs.append_line(have, line)
            except docs.DocError as e:
                raise HTTPException(400, str(e))
            if not changed:
                raise HTTPException(409, "That line is already in this runbook.")
            clean = added.splitlines()[-1][2:]
            today = now_utc().astimezone(timezone.utc).date().isoformat()
            body = body if body.endswith("\n") or not body else body + "\n"
            save(name, svc, f"{body}- {clean} (by {who}, {today})\n", who, projects)
        return screen()

    def remove_line(service: str, index: int, who: str) -> dict:
        svc = resolve(service)
        name = skill_name(svc)
        with _lock:
            cur = store.get_skill(None, name)
            rows = _bullets(cur["body"]) if cur else []
            if cur is None or not 0 <= index < len(rows):
                raise HTTPException(404, "That line is no longer in this runbook. Reload the page.")
            kept = cur["body"].splitlines()
            del kept[rows[index]]
            if len(rows) == 1:     # the last line: a skill needs a body, so the runbook goes
                for p in store.skill_users(name):
                    store.delete_skill(p, name)
            else:
                # a line can be removed after the watcher and investigator are gone: keep the
                # projects that already hold the runbook
                holders = list(dict.fromkeys(role_projects() + store.skill_users(name)))
                if not holders:
                    wired_projects()
                save(name, svc, "\n".join(kept) + "\n", who, holders)
        return screen()

    @app.get("/api/sre/runbooks")
    async def get_runbooks(service: str = ""):
        only_sre()
        return await asyncio.to_thread(screen, service.strip() or None)

    @app.post("/api/sre/runbooks/{service:path}/lines")
    async def post_line(service: str, request: Request):
        only_sre()
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = body.get("text") if isinstance(body, dict) else None
        return await asyncio.to_thread(add_line, service, text,
                                       _clean_who(h.by(request) or "console"))

    @app.delete("/api/sre/runbooks/{service:path}/lines/{index}")
    async def delete_line(service: str, index: int, request: Request):
        only_sre()
        return await asyncio.to_thread(remove_line, service, index,
                                       _clean_who(h.by(request) or "console"))
