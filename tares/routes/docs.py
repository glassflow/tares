"""Docs: a project's docs, the cell's global AGENTS.md and memory, and each person's own."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Request

from .. import docs as docs_mod
from .. import factory as factory_mod
from .. import factory_api, memory_route
from ..deps import Deps
from ..integrations.builtin import memory_tares


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err
    _current_person = deps.current_person
    _hidden_ids = deps.hidden_ids
    _owned_by_hidden = deps.owned_by_hidden
    _project_or_404 = deps.project_or_404
    _by = deps.by
    _global_ids = deps.global_ids
    _ensure_globals = deps.ensure_globals

    # ── a project's docs and tickets (TR-403): what a spec session leaves for the session that
    # builds it. Docs belong to the cell and projects include them; tickets are owned by Linear
    # when the project uses it (written by the sync only), by Tares otherwise ────────────────
    def _marked(d: dict, ids: dict[str, str]) -> dict:
        return {**d, "global": d["id"] in ids.values()}

    def _doc_or_404(uid: str, doc_id: str) -> dict:
        d = store.get_doc(uid, doc_id)
        if d is None:
            _err(KeyError(f"project has no doc {doc_id!r}"), 404)
        return _marked(d, _global_ids())

    @r.get("/api/projects/{uid}/docs")
    def list_project_docs(uid: str, kind: str = ""):
        """The docs the project includes, without bodies, in reading order (starting prompt,
        spec, plan, AGENTS.md, memory, notes, working docs). The cell's global AGENTS.md and
        memory are among them (`global`) once the project has any doc."""
        _project_or_404(uid)
        ids = _ensure_globals(uid)
        return [_marked(d, ids)
                for d in docs_mod.sort_docs(store.list_docs(uid, kind.strip().lower() or None))]

    @r.get("/api/docs/global")
    def get_global_docs(request: Request):
        """The cell's global AGENTS.md and memory, with their bodies; null until the first
        project has docs. `person` and `personal` are the caller's own, see /api/docs/personal."""
        ids = _global_ids()
        out = {k: (_marked(store.get_doc(None, ids[k]), ids) if k in ids else None)
               for k in docs_mod.GLOBAL_DOCS}
        # the caller's own AGENTS.md and memory ride along: only theirs, never another person's
        out["person"], out["personal"] = _personal_out(request)
        return out

    def _personal_out(request: Request) -> tuple[dict | None, dict]:
        """(the caller's person, {agents, memory}: their personal docs with bodies or null)."""
        person = _current_person(request)
        mine = {name: (store.personal_doc(person["id"], kind) if person else None)
                for name, (kind, _title) in docs_mod.PERSONAL_DOCS.items()}
        return ({"id": person["id"], "name": person["name"]} if person else None), mine

    @r.get("/api/docs/personal")
    def get_personal_docs(request: Request):
        """The caller's own personal AGENTS.md and memory, with their bodies (null until the first
        line). Another person's are not reachable by any route."""
        person, mine = _personal_out(request)
        return {"person": person, **mine}

    @r.post("/api/docs/personal/{name}/lines")
    def add_personal_line(name: str, request: Request, body: dict = Body(...)):
        """{text}: add one line to the caller's personal AGENTS.md (`agents`) or memory
        (`memory`), made with the first line. Any station of that person may. The person comes
        from the credential: a key with no person has none to write for."""
        if name not in docs_mod.PERSONAL_DOCS:
            _err(KeyError(f"no personal doc {name!r}; one of: " + ", ".join(docs_mod.PERSONAL_DOCS)),
                 404)
        person = _current_person(request)
        if not person:
            _err(PermissionError("This key has no person, so it has no personal docs."), 403)
        kind, title = docs_mod.PERSONAL_DOCS[name]
        cur = store.personal_doc(person["id"], kind)
        try:
            text, added = docs_mod.append_line(cur["body"] if cur else "", body.get("text"),
                                           limit=docs_mod.MAX_PERSONAL_BYTES)
        except docs_mod.DocError as e:
            _err(e)
        by = _by(request, body)
        if cur is None:
            doc_id = store.create_doc(None, kind, title, text, by=by, owner=person["id"])
        else:
            doc_id = cur["id"]
            if added:
                store.update_doc(doc_id, body=text, by=by)
        return {**store.personal_doc(person["id"], kind), "added": added}

    @r.post("/api/docs/global/{kind}/lines")
    async def add_global_line(kind: str, request: Request, body: dict = Body(...)):
        """{text}: add one line to the global AGENTS.md (`agents`) or memory (`memory`). A line
        already there is not added twice (`added`: false). A memory line goes to the Tares doc
        first, then to the memory integration the cell uses; `pending` is true when that one
        did not take it and it waits for a retry."""
        if kind == "grants":
            _err(ValueError("a grant quotes the person and carries its date: add it with "
                            "add_grant (POST /api/grants)"))
        if kind not in docs_mod.GLOBAL_DOCS:
            _err(KeyError(f"no global doc {kind!r}; one of: " + ", ".join(docs_mod.GLOBAL_DOCS)),
                 404)
        why = factory_mod.may(factory_api.caller(request), "global_docs")
        if why:
            _err(PermissionError(why), 403)
        person = _current_person(request)
        # the name sits in a shared doc: one line, so a name cannot add a bullet of its own
        who = " ".join(str((person or {}).get("name") or "").split())[:60].strip()
        note = (f" (added by {who}, {datetime.now(timezone.utc).date().isoformat()})"
                if who else "")
        if kind == "memory":
            try:
                res = await memory_route.write(store, memory_tares.FACTORY, body.get("text"), note,
                                               _by(request, body))
            except docs_mod.DocError as e:
                _err(e)
            ids = _global_ids()
            return {**_marked(store.get_doc(None, res["doc_id"]), ids), "added": res["added"],
                    "pending": res["pending"]}
        ids = _global_ids(create=True)
        cur = store.get_doc(None, ids[kind])
        try:
            text, added = docs_mod.append_line(cur["body"], body.get("text"), note)
        except docs_mod.DocError as e:
            _err(e)
        if added:
            store.update_doc(ids[kind], body=text, by=_by(request, body))
        return {**_marked(store.get_doc(None, ids[kind]), ids), "added": added}

    @r.post("/api/projects/{uid}/docs", status_code=201)
    def create_project_doc(uid: str, request: Request, body: dict = Body(...)):
        """{kind, title, body}: a new doc in the project."""
        _project_or_404(uid)
        try:
            kind, title, text = docs_mod.validate_doc(body.get("kind"), body.get("title"),
                                                      body.get("body"))
        except docs_mod.DocError as e:
            _err(e)
        if kind == "grants":
            _err(ValueError("a grant quotes the person and carries its date: add it with "
                            "add_grant (POST /api/grants)"))
        doc_id = store.create_doc(uid, kind, title, text, by=_by(request, body))
        _ensure_globals(uid)
        return _doc_or_404(uid, doc_id)

    @r.get("/api/projects/{uid}/docs/{doc_id}")
    def get_project_doc(uid: str, doc_id: str):
        _project_or_404(uid)
        return _doc_or_404(uid, doc_id)

    @r.put("/api/projects/{uid}/docs/{doc_id}")
    def update_project_doc(uid: str, doc_id: str, request: Request,
                                 body: dict = Body(...)):
        """Change the kind, title or body; a field left out keeps its value. Docs are shared: the
        change shows in every project that includes the doc."""
        _project_or_404(uid)
        cur = _doc_or_404(uid, doc_id)
        try:
            kind, title, text = docs_mod.validate_doc(body.get("kind", cur["kind"]),
                                                      body.get("title", cur["title"]),
                                                      body.get("body", cur["body"]))
        except docs_mod.DocError as e:
            _err(e)
        if "grants" in (kind, cur["kind"]) and (kind, title, text) != (cur["kind"], cur["title"],
                                                                         cur["body"]):
            _err(ValueError("grants change only through add_grant (POST /api/grants), which "
                            "quotes the person and dates it"))
        if cur["global"]:
            why = factory_mod.may(factory_api.caller(request), "global_docs")
            if why:
                _err(PermissionError(why), 403)
        store.update_doc(doc_id, kind=kind, title=title, body=text, by=_by(request, body))
        return _doc_or_404(uid, doc_id)

    @r.delete("/api/projects/{uid}/docs/{doc_id}")
    def delete_project_doc(uid: str, doc_id: str):
        """The project stops including the doc; it is deleted when no other project includes
        it."""
        _project_or_404(uid)
        d = _doc_or_404(uid, doc_id)
        if d["global"]:
            _err(ValueError(f"{d['title']} belongs to every project; edit it instead"), 409)
        others = [p for p in d["projects"] if p != uid]
        store.remove_doc(uid, doc_id)
        return {"ok": True, "removed": doc_id, "kept_for": others}

    @r.post("/api/projects/{uid}/docs/{doc_id}/use")
    def use_project_doc(uid: str, doc_id: str):
        """The project includes a doc already on the cell (shared: an edit shows everywhere)."""
        _project_or_404(uid)
        if _owned_by_hidden("doc", doc_id, _hidden_ids()) or not store.use_doc(uid, doc_id):
            _err(KeyError(f"there is no doc {doc_id!r} on Tares"), 404)
        return _doc_or_404(uid, doc_id)

    @r.get("/api/docs")
    def list_cell_docs(request: Request):
        """Every doc on Tares, with the projects that include it (no bodies). A person's own
        AGENTS.md and memory show only to them."""
        viewer = (_current_person(request) or {}).get("id")
        names = {p["id"]: p["name"] for p in store.list_projects()}
        ids = _global_ids()
        hidden = _hidden_ids()
        return [{**_marked(d, ids),
                 "projects": [{"id": p, "name": names.get(p, p)} for p in d["projects"]
                              if p not in hidden]}
                for d in docs_mod.sort_docs(store.list_cell_docs(viewer))
                if not d["projects"] or set(d["projects"]) - hidden]

    return r
