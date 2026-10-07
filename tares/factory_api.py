"""HTTP routes for the software factory's crew (P-TR-176, M3 onward), registered by the daemon.

One always-on crew (orchestrator, reviewer, releaser) serves every project; builders are started
per project. Tares runs none of it: it keeps the crew's settings and grants on the cell, groups
the Claude Code sessions it receives into stations, and records when a project was handed to the
crew. Who is calling comes from the X-Tares-Station / -Role / -Parent headers the MCP proxy sets
from the station's environment (tares/factory.py `may`).
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from fastapi import Body, Request

from . import docs as docs_mod
from . import factory as factory_mod
from .envelope import now_utc

CREW_SETTING = "crew_settings"
GRANTS_SETTING = "global_grants"   # the id of the cell's "Grants (all projects)" doc
QUIET_MIN = 10   # a working station that sent nothing for this long shows as quiet


def caller(request: Request) -> dict:
    """{station, role, parent} the request says it comes from ("" each when unlabeled)."""
    h = request.headers
    return {"station": (h.get("x-tares-station") or "").strip()[:factory_mod.MAX_NAME],
            "role": (h.get("x-tares-role") or "").strip().lower(),
            "parent": (h.get("x-tares-parent") or "").strip()[:factory_mod.MAX_NAME]}


def register(app, store, h: SimpleNamespace) -> None:
    """`h` carries the daemon's helpers: err(exc, code), project_or_404(uid), by(request, body),
    cc_source(), global_ids(create)."""

    def deny(request: Request, action: str) -> None:
        why = factory_mod.may(caller(request), action)
        if why:
            h.err(PermissionError(why), 403)

    # ── crew settings (TR-419) ────────────────────────────────────────────────
    def settings() -> dict:
        try:
            cur = json.loads(store.get_setting(CREW_SETTING) or "{}")
        except ValueError:
            cur = {}
        return {**factory_mod.CREW_DEFAULTS, **{k: cur.get(k) for k in factory_mod.CREW_DEFAULTS}}

    @app.get("/api/crew/settings")
    async def get_crew_settings():
        """The crew's settings for every project: autonomy (L3-review, L4-ship, L5-dark),
        release profile, prod pattern, challenger on builders, most builders at once. Unset
        ones are null."""
        return {**settings(), "autonomy_levels": factory_mod.AUTONOMY}

    @app.put("/api/crew/settings")
    async def put_crew_settings(request: Request, body: dict = Body(...)):
        """Change the fields given; the rest keep their value."""
        deny(request, "grants")
        try:
            out = factory_mod.crew_settings_ok(body, settings())
        except docs_mod.DocError as e:
            h.err(e)
        store.set_setting(CREW_SETTING, json.dumps(out))
        return {**out, "autonomy_levels": factory_mod.AUTONOMY}

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

    @app.get("/api/grants")
    async def get_grants(project: str = ""):
        """The grants for every project (`all`) and, with `project`, that project's own, as
        markdown."""
        uid = None
        if project.strip():
            uid = h.project_or_404(h.resolve_project(project.strip()))["id"]
        return grants_out(uid)

    @app.post("/api/grants", status_code=201)
    async def add_grant(request: Request, body: dict = Body(...)):
        """{words, scope: all|project, project?}: one grant, quoting the person, dated today.
        Only the orchestrator among the crew may add one; a person's own session may."""
        deny(request, "grants")
        scope = str(body.get("scope") or "all").strip().lower()
        if scope not in ("all", "project"):
            h.err(ValueError("scope is all or project"))
        try:
            line = factory_mod.grant_line(body.get("words"), now_utc().date().isoformat())
        except docs_mod.DocError as e:
            h.err(e)
        if scope == "all":
            doc_id = shared_grants_id(create=True)
            cur = store.get_doc(None, doc_id)["body"]
            uid = None
        else:
            ref = str(body.get("project") or "").strip()
            if not ref:
                h.err(ValueError("name the project the grant is for"))
            uid = h.project_or_404(h.resolve_project(ref))["id"]
            d = project_grants_doc(uid)
            if d is None:
                p = store.get_project(uid)
                doc_id = store.create_doc(uid, "grants", f"Grants: {p['name']}",
                                          f"# Grants for {p['name']}\n\nWhat the person allows "
                                          "the crew in this project, on top of the grants for "
                                          "every project. Their words, dated.\n\n",
                                          by=h.by(request, body))
            else:
                doc_id = d["id"]
            include_shared_grants(uid)
            cur = store.get_doc(None, doc_id)["body"]
        text, added = docs_mod.append_line(cur, line)
        if added:
            store.update_doc(doc_id, body=text, by=h.by(request, body))
        return {**grants_out(uid), "added": added, "line": line}

    # ── stations (TR-420): sessions grouped by the station they play ──────────
    def _ts(v) -> float:
        return v.timestamp() if isinstance(v, datetime) else 0.0

    def stations(project: str | None = None) -> list[dict]:
        rows = store.station_sessions(h.cc_source())
        names = {p["id"]: p["name"] for p in store.list_projects()}
        now = now_utc()
        by: dict[str, list[dict]] = {}
        for s in rows:
            by.setdefault(s["station"], []).append(s)
        out = []
        for name, ss in by.items():
            ss.sort(key=lambda s: _ts(s["last_at"]), reverse=True)
            live = [s for s in ss if s["state"] not in ("ended", "replaced")]
            cur = live[0] if live else ss[0]
            quiet = (int((now - cur["last_at"]).total_seconds() // 60)
                     if isinstance(cur["last_at"], datetime) else None)
            state = cur["state"] or "working"
            if state == "working" and quiet is not None and quiet >= QUIET_MIN:
                state = "quiet"
            # the sessions that played the station before the current one: replaced by it
            earlier = [{"session": s["session"], "started_at": s["started_at"],
                        "last_at": s["last_at"], "lines": s["lines"], "state": "replaced"}
                       for s in ss if s is not cur]
            out.append({"name": name, "role": cur["role"], "parent": cur["parent"],
                        "project": cur["project"], "project_name": names.get(cur["project"]),
                        "state": state, "state_reason": cur["state_reason"],
                        "state_at": cur["state_at"], "quiet_minutes": quiet,
                        "session": cur["session"], "started_at": cur["started_at"],
                        "last_at": cur["last_at"], "lines": cur["lines"], "earlier": earlier,
                        "projects": sorted({s["project"] for s in ss if s["project"]})})
        for st in out:
            st["children"] = sorted(x["name"] for x in out if x["parent"] == st["name"])
        if project:
            out = [s for s in out if project in s["projects"]]
        order = {r: i for i, r in enumerate(factory_mod.ROLES)}
        out.sort(key=lambda s: (order.get(s["role"] or "", 99), s["name"]))
        return out

    @app.get("/api/crew")
    async def get_crew():
        """The crew: one row per station (its current session and state, the earlier sessions
        that played it, the stations it started), the crew's settings, and the shared grants."""
        return {"stations": stations(), "settings": settings(), "grants": grants_out(None)["all"],
                "now": now_utc()}

    @app.get("/api/crew/{name}")
    async def get_station(name: str):
        st = next((s for s in stations() if s["name"] == name), None)
        if st is None:
            h.err(KeyError(f"no station {name!r} has reported to Tares"), 404)
        return st

    # ── the crew on a project, and the hand-over (TR-422, TR-425) ─────────────
    @app.get("/api/projects/{uid}/crew")
    async def get_project_crew(uid: str):
        """The stations that worked on this project (builders started for it among them), and
        when it was handed to the crew."""
        h.project_or_404(uid)
        return {"stations": stations(uid), "handover": store.get_handover(uid),
                "now": now_utc()}

    @app.post("/api/projects/{uid}/handover")
    async def hand_over(uid: str, request: Request, body: dict = Body(default={})):
        """{repo?}: the spec session hands the project to the crew. Recorded with the time; the
        session then messages the orchestrator itself."""
        h.project_or_404(uid)
        repo = str(body.get("repo") or "").strip()[:500]
        store.set_handover(uid, repo, h.by(request, body), now_utc())
        include_shared_grants(uid)   # a crew-built project shows the grants it is built under
        return {"handover": store.get_handover(uid)}
