"""The session lines coming in (/ingest, through the runtime integrations, and the GitHub App's
webhook), and the sessions that worked in a project, with the pick-up of a stopped build."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import factory as factory_mod
from .. import factory_api, session_ingest
from .. import metrics as _metrics
from ..deps import Deps
from ..envelope import now_utc

log = logging.getLogger("tares")


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err
    _current_person = deps.current_person
    _project_or_404 = deps.project_or_404
    _cc_source = deps.cc_source
    _global_ids = deps.global_ids
    _ticket_out = deps.ticket_out

    # ── push ingestion: the runtimes' session lines and the GitHub App's webhook ──
    async def _parse_ingest_body(request: Request):
        """Accept a JSON object/array OR NDJSON (one JSON object per line). An empty body (a
        ping) parses to []."""
        raw = (await request.body()).decode("utf-8", "replace").strip()
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                return [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
            except json.JSONDecodeError:
                _err(ValueError("body must be JSON or NDJSON"))

    async def _refuse_if_full() -> None:
        """507 Insufficient Storage when the store is at the pause mark: the producer gets a plain
        reason and can retry later; the database keeps working for everything else. Measured off
        the event loop (it can query the database). When the size cannot be read the batch goes on
        to `store.append`, whose refusal answers 503 so the sender keeps its lines."""
        try:
            st = await asyncio.to_thread(deps.storage)
        except Exception as e:
            log.warning("ingest: the storage size could not be read (%s); the batch goes on", e)
            return
        if st["paused"]:
            raise HTTPException(status_code=507, detail=st["detail"])

    async def _store(source: str, envelopes: list) -> int:
        """Store a batch of events off the event loop and count it on /metrics."""
        await asyncio.to_thread(store.append, envelopes)
        _metrics.events_ingested(source, len(envelopes))
        return len(envelopes)

    async def _ingest_runtime(name: str, request: Request):
        """A runtime's intake (TR-1804): the runtime slot's integration `name` makes session lines
        of the body, and they are stored under the session source."""
        from ..integrations import registry as integrations_registry
        from ..session_map import SOURCE as SESSION_SOURCE
        await _refuse_if_full()
        body = await _parse_ingest_body(request)
        person = _current_person(request)   # from the credential, never from the body
        cls = integrations_registry.get("runtime", name)
        if cls is None:
            raise HTTPException(status_code=503, detail=f"the {name} runtime is not loaded on this "
                                                        "cell, so its lines cannot be stored now")
        try:
            envs = cls().push_lines(body, store=store, person_id=person["id"] if person else None)
            n = await _store(SESSION_SOURCE, envs)
        except ValueError as e:
            _err(e, 400)
        except session_ingest.LinesNotStored as e:
            _err(e, 503)         # not acknowledged: the sender keeps the lines and sends them again
        return JSONResponse({"ingested": n}, status_code=202)

    @r.post("/ingest/claude_code", status_code=202)
    async def ingest_claude_code(request: Request):
        """Transcript lines from the Claude Code plugin, as NDJSON, a JSON object or an array."""
        return await _ingest_runtime("claude_code", request)

    @r.post("/ingest/codex", status_code=202)
    async def ingest_codex(request: Request):
        """What a crew's recorder read of its Codex stations (TR-1595)."""
        return await _ingest_runtime("codex", request)

    async def _ingest_github(row: dict, request: Request):
        """A GitHub App webhook delivery on its key (TR-1807): the ci_github integration's. Checked
        with the App credential's webhook secret on the raw body, mapped, stored, its pull
        requests kept in pr_events and every event told to the GitHub subscribers (the brain
        sync)."""
        from .. import github_webhook
        from ..webhook_verify import verify
        source = row.get("source") or "github"
        cred = store.get_github_credential(row.get("credential") or "") or {}
        secret = str((cred.get("config") or {}).get("webhook_secret") or "")
        reason = verify("github", secret, await request.body(), dict(request.headers), None)
        if reason:
            github_webhook.count_rejected(source, reason)
            return JSONResponse({"detail": f"signature check failed (github): {reason}"},
                                status_code=401)
        body = await _parse_ingest_body(request)
        try:
            envs = github_webhook.GithubAppDeliveries(
                store, source, row.get("credential") or "").map_payload(
                body, headers=dict(request.headers))
        except ValueError as e:
            _err(e, 400)
        n = await _store(source, envs)
        if envs:
            github_webhook.after_store(store, source, envs)   # pr_events and the GitHub hub
        return JSONResponse({"ingested": n}, status_code=202)

    @r.post("/ingest/{token}", status_code=202)
    async def ingest(token: str, request: Request):
        """A GitHub App's webhook delivery, on the key its hook URL carries (integration_keys).
        Any other key answers 404."""
        await _refuse_if_full()
        try:
            keyed = await asyncio.to_thread(store.integration_key, token)
        except Exception:   # noqa: BLE001  (a store that cannot answer: the delivery is retried)
            raise HTTPException(status_code=503, detail="the store could not look the key up now")
        if keyed is None or keyed.get("integration") != "ci_github":
            _err(KeyError(f"unknown ingest key {token!r}"), 404)
        return await _ingest_github(keyed, request)

    # (there is no /api/security — whether auth is on is a boolean on /health; producers authenticate
    #  with scoped ingest API keys, not a shared token.)

    # ── the Claude Code sessions that worked in a project (TR-405) ────────────
    @r.get("/api/projects/{uid}/sessions")
    def list_project_sessions(uid: str):
        """The sessions that worked in the project (the spec session among them), newest activity
        first, each with its state (working, idle between turns, waiting for the person, ended) and, for a waiting
        one, the last thing it said (`last_said`). A session that also worked in a private project
        the caller cannot see is not listed, unless it is the caller's own station's (TR-1710)."""
        _project_or_404(uid)
        src = _cc_source()   # the one source session lines are stored under (TR-1804)
        out = store.project_sessions(uid)
        unread = factory_api.unread_sessions(deps, [s["session"] for s in out])
        out = [s for s in out if s["session"] not in unread]
        now = now_utc()
        for s in out:
            # a session whose end never reached Tares does not wait forever (TR-473)
            s["state"] = factory_mod.shown_state(s["state"], s["last_at"], now)
            if s["state"] == "waiting":
                said = store.last_said(src, s["session"]) or ""
                s["last_said"] = said[:1200]
        return {"sessions": out, "now": now}

    @r.get("/api/projects/{uid}/sessions/{sid}")
    def get_project_session(uid: str, sid: str, limit: int = 200, offset: int = 0):
        """One session's lines, oldest first, the ones from before it named the project
        included. A session the list leaves out for this caller answers as one that did not work
        here (TR-1710)."""
        _project_or_404(uid)
        if (not any(s["session"] == sid for s in store.project_sessions(uid))
                or factory_api.unread_sessions(deps, [sid])):
            _err(KeyError(f"session {sid!r} did not work in this project"), 404)
        src = _cc_source()   # the one source session lines are stored under (TR-1804)
        lines, where = store.key_events_from(src, sid, limit=min(max(limit, 1), 1000),
                                             offset=max(offset, 0))
        return {"session": sid, "lines": lines, "lines_from": where}

    # ── picking up a build where a session stopped: what a fresh session needs to carry on the
    # ticket in progress, from what Tares recorded (works after a crash too) ────────────────
    _QUIET_MIN = 10   # a working session that sent nothing for this long is taken as gone

    def _previous_session(sessions: list[dict], ticket: dict | None) -> dict | None:
        """The session that was working on the ticket: active when it went in progress (its
        lines span that moment), else the latest one that started before it, else the latest."""
        live = [s for s in sessions if s["state"] != "replaced"]
        if not live:
            return None
        t = ticket["updated_at"] if ticket else None
        if t is not None:
            spans = [s for s in live if s["started_at"] and s["last_at"]
                     and s["started_at"] <= t <= s["last_at"]]
            if spans:
                return max(spans, key=lambda s: s["last_at"])
            before = [s for s in live if s["started_at"] and s["started_at"] <= t]
            if before:
                return max(before, key=lambda s: s["last_at"] or s["started_at"])
        return live[0]

    def _pickup(uid: str) -> tuple[dict, dict | None]:
        """The hand-over as the caller may see it, and the session that was on the ticket, which
        the caller may not read (TR-1710): then `previous_session` in the hand-over is None."""
        tickets = store.list_tickets(uid)
        ready = factory_mod.readiness(tickets)
        # the ticket a session stopped on: in progress, and not waiting on someone else (in
        # review, changes asked, blocked: picking those up would redo work that is done)
        ticket = next((t for t in tickets if t["status"] == "in_progress"
                       and t["stage"] not in ("review", "changes", "blocked", "merged",
                                              "shipped")), None)
        nxt = ticket or next((t for t in tickets if t["status"] == "todo"
                              and ready[t["id"]]["ready"]), None)
        sessions = store.project_sessions(uid)
        prev = _previous_session(sessions, ticket) if ticket else None
        out: dict = {"ticket": _ticket_out(uid, nxt, with_doc=True) if nxt else None,
                     "in_progress": ticket is not None, "previous_session": None,
                     "may_be_open": False}
        if prev:
            now = now_utc()
            quiet = int((now - prev["last_at"]).total_seconds() // 60) if prev["last_at"] else None
            open_ = prev["state"] in ("idle", "waiting") or (
                prev["state"] in (None, "working") and quiet is not None and quiet < _QUIET_MIN)
            src = _cc_source()
            # no other session takes its place: the build was on this one, readable or not
            if not factory_api.unread_sessions(deps, [prev["session"]]):
                out["previous_session"] = {
                    "session": prev["session"], "repo": prev["repo"], "state": prev["state"],
                    "reason": prev["state_reason"], "last_at": prev["last_at"],
                    "quiet_minutes": quiet,
                    "said_lately": store.said_lately(src, prev["session"], 3),
                    "files_touched": store.files_touched(src, prev["session"])}
            out["may_be_open"] = bool(open_)
        gmem = _global_ids().get("memory")
        mem = next((d for d in store.list_docs(uid, "memory") if d["id"] != gmem), None)
        body = (store.get_doc(uid, mem["id"]) or {}).get("body", "") if mem else ""
        if len(body) > 6000:   # newest last: keep the newest lines
            body = "...\n" + body[-6000:].split("\n", 1)[-1]
        out["memory"] = body
        out["decisions"] = [{k: d[k] for k in ("label", "question", "words", "choice", "at")}
                            for d in store.list_decisions(uid, all_=True)[:10]]
        notes = [d for d in store.list_docs(uid, "note")]
        out["notes"] = [{"id": d["id"], "title": d["title"],
                         "body": (store.get_doc(uid, d["id"]) or {}).get("body", "")[:4000]}
                        for d in notes]
        out["checklist"] = (
            [f"Check the branch and uncommitted changes (git status, git log -3) before editing: "
             f"the previous session may have left work on ticket \"{ticket['title']}\" "
             "uncommitted.",
             "Restart what the project runs on (local database, dev server) the way AGENTS.md "
             "says; the previous session's processes are gone.",
             "Read the ticket's working doc, its Progress section first, then the previous "
             "session's last messages: they say what was done and what was left.",
             "Finish what is left, run the ticket's Verify, commit, mark it done, then carry on "
             "with the next ticket."]
            if ticket else
            ["No ticket is in progress: start with the next one, the way the starting prompt says."]
            if nxt else
            ["No ticket is ready: " + "; ".join(
                f"{factory_mod.label(t)} waits for {', '.join(ready[t['id']]['blocked_by'])}"
                for t in tickets if t["status"] == "todo" and ready[t["id"]]["blocked_by"])
             + ". Finish or re-plan what they wait for." if any(
                t["status"] == "todo" for t in tickets) else
             "Every ticket is finished." if tickets else
             "The project has no tickets yet: spec it first (/tares:spec)."])
        return out, prev

    @r.get("/api/projects/{uid}/pickup")
    def get_pickup(uid: str):
        """What a fresh session needs to pick the build up where a session stopped: the ticket in
        progress (else the next) with its working doc, the session that was on it (its state,
        last messages, files it touched, whether it may still be open), the builders' notes and
        a checklist. `previous_session` is null when that session also worked in a private project
        the caller cannot see and is not their own station's (TR-1710). Reads only."""
        _project_or_404(uid)
        return _pickup(uid)[0]

    @r.post("/api/projects/{uid}/pickup")
    def take_over(uid: str, body: dict = Body(default={})):
        """A fresh session takes the build over: the previous session is marked replaced, so two
        sessions never work the same ticket. When it may still be open (waiting, or active in
        the last minutes) this needs {"take_over": true}. A caller who must not read that session
        takes the build over all the same, and is told neither its state nor its id (TR-1710)."""
        _project_or_404(uid)
        out, prev = _pickup(uid)
        shown = out["previous_session"]
        if prev and out["may_be_open"] and not body.get("take_over"):
            state = f" ({shown['state'] or 'active'})" if shown else ""
            _err(ValueError(f"the session that was building may still be open{state}; ask the "
                            "person, then take over with take_over"), 409)
        if prev:
            store.set_session_state(prev["session"], "replaced",
                                    "a fresh session picked the build up", now_utc(), force=True)
        return {**out, "taken_over": shown["session"] if shown else None}

    return r
