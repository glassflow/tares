# Tares enterprise licence, see ee/LICENSE
"""Asking the brain: the routes the console and the MCP tools (`mcp_brain.py`) share (TR-1200).

    POST /api/brain/ask             {question, deep}   an answer with citations
    GET  /api/brain/search?q=&repo=&k=                 the places the code or docs mention it
    GET  /api/brain/docs/{id}                          one document, whole

Each forwards to lore with the cell's key, which never leaves the cell. Only a repo that is Ready is
used. A repo that is not (queued, cloning, indexing, failed) is left out and named in `notes`:
"glassflow/tares is still indexing (812 of 1,816 files). Use grep on it for now." With no Ready repo
at all the answer is `{ready: false, message}` and nothing else.

Every call is kept in `brain_calls` (tool, question, repos used, how long, the tokens lore reported,
the station and person it came with), so M1 acceptance can count brain calls against grep calls.
Quick asks and deep asks are capped per cell per day: `TARES_BRAIN_ASK_DAILY` (200) and
`TARES_BRAIN_DEEP_DAILY` (20), counted from midnight UTC; over the cap the route answers 429 in
plain words. Lore's `ask` takes one source: with exactly one Ready repo it is that source; with
several, lore searches all of its sources, so a repo still indexing may feed an answer (its note
says so). A search is exact: hits from a repo that is not Ready are dropped here.

Without a licence every route answers 404, as an unmounted route does; with a licence but no brain
settings, 404 too.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import Body, HTTPException, Query, Request

from tares import licence
from tares.envelope import now_utc

from . import repos as brain_repos
from .lore import ASK_MODES, LoreClient, LoreError

__all__ = ["ASK_DAILY", "DEEP_DAILY", "caps", "register_routes"]

ASK_DAILY = 200
DEEP_DAILY = 20
ENV_ASK_DAILY = "TARES_BRAIN_ASK_DAILY"
ENV_DEEP_DAILY = "TARES_BRAIN_DEEP_DAILY"
MAX_QUESTION = 2000      # characters: lore refuses a question or a search over 2,000 (MAX_QUERY_CHARS)
MAX_HITS = 50
KEPT_QUESTION = 1000     # characters of the question kept in brain_calls

NO_REPOS = ("No repo is connected to the brain on this cell yet. "
            "Connect one on the Second Brain page. Use grep for now.")


def _cap(env: str, default: int) -> int:
    """A daily cap from the environment: a whole number, 0 or more; blank or anything else gives the default."""
    raw = (os.environ.get(env) or "").strip()
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if n >= 0 else default


def caps() -> tuple[int, int]:
    """(quick asks a day, deep asks a day), read when asked: a test or an admin may change them."""
    return _cap(ENV_ASK_DAILY, ASK_DAILY), _cap(ENV_DEEP_DAILY, DEEP_DAILY)


def _midnight(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _not_ready(row: dict) -> str:
    """One repo that is not Ready, said in plain words."""
    repo, state = row["repo"], row["state"]
    if state == "failed":
        why = (row.get("error") or "").strip()
        what = f"could not be indexed ({why})" if why else "could not be indexed"
    elif state == "indexing":
        total = row.get("files_total") or 0
        what = (f"is still indexing ({row.get('files_done') or 0:,} of {total:,} files)" if total
                else "is still indexing")
    elif state == "cloning":
        what = "is still being fetched"
    else:
        what = "is waiting to be indexed"
    return f"{repo} {what}. Use grep on it for now."


def _names(row: dict) -> set[str]:
    """What lore may call this repo's source in a hit: its name and its id."""
    return {brain_repos.lore_name(row["repo"]), row.get("repo_source_id") or ""} - {""}


def register_routes(app, host) -> None:
    store = host.store
    lock = threading.Lock()

    def need_brain():
        if not licence.ee_allowed() or not LoreClient.configured():
            raise HTTPException(status_code=404, detail="Not Found")   # as if never mounted

    def split():
        """(ready rows, the notes naming the others, the not-ready rows)."""
        rows = store.list_brain_repos()
        ready = [r for r in rows if r["state"] == "ready"]
        rest = [r for r in rows if r["state"] != "ready"]
        return ready, [_not_ready(r) for r in rest], rest

    def nothing_ready(notes: list[str], connected: bool) -> dict:
        message = " ".join(notes) if notes else NO_REPOS
        if notes:
            message = "No repo is ready on the brain yet. " + message
        return {"ready": False, "message": message if connected else NO_REPOS, "notes": notes}

    def who(request: Request) -> tuple[str | None, str | None, str]:
        person = host.current_person(request)
        station = (request.headers.get("x-tares-station") or "").strip() or None
        via = "mcp" if (request.headers.get("x-tares-via") or "").strip() == "mcp" else "console"
        return station, (person["id"] if person else None), via

    def named_repo(value: str, ready: list[dict], rest: list[dict]) -> dict:
        """The connected row a `repo` argument names, or an error in plain words."""
        try:
            repo = brain_repos.check_repo(value)
        except ValueError as e:
            host.err(e, 400)
        row = next((r for r in [*ready, *rest] if r["repo"] == repo), None)
        if row is None:
            host.err(KeyError(f"{repo} is not connected to the brain"), 404)
        return row

    def begin(request: Request, tool: str, question: str | None, repos: list[str], deep: bool = False,
              capped: bool = False) -> str:
        station, person_id, via = who(request)
        call = {"id": uuid.uuid4().hex, "at": now_utc(), "tool": tool, "deep": deep, "question":
                (question or "")[:KEPT_QUESTION], "repos": repos, "station": station,
                "person_id": person_id, "via": via}
        cap = None
        if capped:
            quick, deeper = caps()
            cap = deeper if deep else quick
        if not store.reserve_brain_call(call, cap, _midnight(call["at"])):
            what = "deep answers" if deep else "answers"
            tip = " Ask without looking deeper, or try again after midnight UTC." if deep else \
                " Search instead, or try again after midnight UTC."
            host.err(ValueError(f"This cell has used its {cap} {what} for today.{tip}"), 429)
        return call["id"]

    def lore_error(e: LoreError):
        status = 400 if e.status == 400 else 404 if e.status == 404 else 429 if e.status == 429 else 502
        host.err(ValueError(str(e)), status)

    async def forward(call_id: str, started: float, coro, usage=None):
        """Await one lore call and keep how it went. `usage` picks (input, output) tokens from the reply."""
        try:
            data = await coro
        except LoreError as e:
            store.finish_brain_call(call_id, int((time.monotonic() - started) * 1000), False, error=str(e))
            lore_error(e)
        ms = int((time.monotonic() - started) * 1000)
        tokens = usage(data) if usage else (None, None)
        store.finish_brain_call(call_id, ms, True, tokens[0], tokens[1])
        return data, ms

    def tokens_of(data: dict) -> tuple[int | None, int | None]:
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        pick = lambda k: u.get(k) if isinstance(u.get(k), int) and not isinstance(u.get(k), bool) else None  # noqa: E731
        return pick("input_tokens"), pick("output_tokens")

    @app.post("/api/brain/ask")
    async def ask(request: Request, body: dict = Body(...)):
        """{question, deep}: an answer from the Ready repos, with citations:
        `{answer, citations: [{label, title, path, start_line, end_line, url}], notes, repos, ...}`
        (lore's reply, plus the notes naming repos left out and the repos used). `deep` takes
        longer and counts against a smaller daily cap. With no Ready repo: `{ready: false, message, notes}`."""
        need_brain()
        extra = sorted(set(body) - {"question", "deep"})
        if extra:
            host.err(ValueError(f"unknown field {extra[0]!r}: send {{\"question\": \"...\", \"deep\": false}}"))
        question = body.get("question")
        if not isinstance(question, str) or not question.strip():
            host.err(ValueError("question: what to ask the brain, as text"))
        question = question.strip()
        if len(question) > MAX_QUESTION:
            host.err(ValueError(f"question: at most {MAX_QUESTION:,} characters"))
        deep = body.get("deep", False)
        if not isinstance(deep, bool):
            host.err(ValueError("deep: true or false"))
        ready, notes, rest = split()
        if not ready:
            return nothing_ready(notes, bool(rest))
        used = [r["repo"] for r in ready]
        source = brain_repos.lore_name(ready[0]["repo"]) if len(ready) == 1 else None
        if rest and len(ready) > 1:
            notes = [n[:-len(". Use grep on it for now.")] + ": it may still feed this answer. Use grep on it too."
                     for n in notes]
        call_id = begin(request, "ask_brain", question, used, deep, capped=True)
        started = time.monotonic()
        mode = ASK_MODES[1] if deep else ASK_MODES[0]

        async def run():
            async with brain_repos.lore_client() as lore:
                return await lore.ask(question, mode=mode, source=source)
        data, ms = await forward(call_id, started, run(), tokens_of)
        return {**data, "notes": notes, "repos": used, "duration_ms": ms}

    @app.get("/api/brain/search")
    async def search(request: Request, q: str = Query(""), repo: str = Query(""), k: int = Query(12, ge=1, le=MAX_HITS)):
        """?q=&repo=&k=: `{hits: [...], notes, repos}`, the places in the Ready repos that mention
        `q` (`repo` narrows it to one, owner/name). With no Ready repo: `{ready: false, message, notes}`."""
        need_brain()
        if not q.strip():
            host.err(ValueError("q: what to search for, as text"))
        if len(q.strip()) > MAX_QUESTION:
            host.err(ValueError(f"q: at most {MAX_QUESTION:,} characters"))
        ready, notes, rest = split()
        source = None
        if repo.strip():
            row = named_repo(repo, ready, rest)
            if row["state"] != "ready":
                return {"ready": False, "message": _not_ready(row), "notes": [_not_ready(row)]}
            ready, notes, rest = [row], [], []
            source = brain_repos.lore_name(row["repo"])
        elif not ready:
            return nothing_ready(notes, bool(rest))
        elif len(ready) == 1:
            source = brain_repos.lore_name(ready[0]["repo"])
        used = [r["repo"] for r in ready]
        call_id = begin(request, "search_brain", q.strip(), used)
        started = time.monotonic()

        async def run():
            async with brain_repos.lore_client() as lore:
                return await lore.search(q, k=k, source=source)
        data, ms = await forward(call_id, started, run())
        out = {n for r in rest for n in _names(r)}
        hits = [h for h in data.get("hits") or [] if not (isinstance(h, dict) and h.get("source") in out)]
        return {**data, "hits": hits, "notes": notes, "repos": used, "duration_ms": ms}

    @app.get("/api/brain/docs/{doc_id}")
    async def document(request: Request, doc_id: str):
        """One document from lore: `{kind, title, uri, metadata, text}`. The id is a hit's `document_id`."""
        need_brain()
        if not doc_id.strip():
            host.err(ValueError("id: the document id a search hit names"))
        call_id = begin(request, "get_brain_doc", doc_id.strip()[:KEPT_QUESTION], [])
        started = time.monotonic()

        async def run():
            async with brain_repos.lore_client() as lore:
                return await lore.get_document(doc_id)
        data, _ = await forward(call_id, started, run())
        return data
