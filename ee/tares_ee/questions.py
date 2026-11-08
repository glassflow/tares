# Tares enterprise licence, see ee/LICENSE
"""Questions a spec asks the owner of a repo (TR-636), as an ee feature (TR-923).

Without ee nothing asks and nothing waits: a single person hands a project over exactly as before,
`my_work` has no `spec_questions`, and the three routes here answer `licence_required` (from
`tares.team.FALLBACKS` when ee is not installed at all). Each route checks the licence before it
does anything with the request. The tools are in `mcp_questions.py`; the store tables stay in core.

`c` is the namespace `factory_api.team_reader()` builds: `store`, `h` (the host helpers) and
`work_person`.
"""
from __future__ import annotations

from fastapi import Body, Request

from tares import factory as factory_mod
from tares import licence
from tares.envelope import now_utc
from tares.factory_api import caller

__all__ = ["NEEDS_LICENCE", "QUESTION_STATES", "open_questions_for", "register_routes"]

NEEDS_LICENCE = "Questions to repo owners need an enterprise licence."
QUESTION_STATES = ("open", "answered")


def open_questions_for(c, person_id: str, hidden, want) -> list[dict]:
    """The open questions addressed to `person_id` as `my_work` lists them, leaving out the
    projects in `hidden`; `want` (a project id) keeps only that project's."""
    names = {x["id"]: x["name"] for x in c.store.list_projects()}
    return [{"project": names[q["project"]], "id": q["id"], "repo": q["repo"],
             "text": q["text"], "asked_by": (q["asked_by_person"] or {}).get("name"),
             "asked_at": q["asked_at"]}
            for q in c.store.list_spec_questions(state="open", to_person_id=person_id)
            if q["project"] in names and q["project"] not in hidden
            and (not want or q["project"] == want)]


def register_routes(app, host) -> None:
    store = host.store

    def need_licence():
        if not licence.ee_allowed():
            host.licence_required(NEEDS_LICENCE)

    def console_via(request: Request) -> str | None:
        """"console" for a person acting on the page (no station header), else None."""
        return None if caller(request)["station"] else "console"

    @app.post("/api/projects/{uid}/questions", status_code=201)
    def ask_area(uid: str, request: Request, body: dict = Body(...)):
        """{repo, text}: a question from the spec for the person who owns the repo (owner/name),
        numbered Q1, Q2... in the project. It shows in the owner's my_work until they answer it.
        Anyone who can see the project may ask. A repo nobody owns is refused."""
        need_licence()
        c = host.team_reader()
        project = host.project_or_404(uid)
        me = c.work_person(request)
        try:
            repo = factory_mod.norm_repo(body.get("repo"))
        except ValueError as e:
            host.err(e)
        text = str(body.get("text") or "").strip()
        if not text:
            host.err(ValueError("text: the question for the area owner"))
        if len(text) > 4000:
            host.err(ValueError("keep the question to 4000 characters"))
        owner = host.area_owner(repo)
        if owner is None:
            host.err(ValueError(f"{repo} has no owner on this cell"), 409)
        if project.get("scope") == "private" and not store.is_project_member(uid, owner["id"]):
            host.err(PermissionError(f"{owner['name']} owns {repo} but is not on this private "
                                     "project: ask the project lead to add them"), 403)
        return store.add_spec_question(uid, owner["id"], repo, text, me["id"], now_utc())

    @app.get("/api/projects/{uid}/questions")
    def list_questions(uid: str, state: str = ""):
        """The project's questions to area owners, oldest first; `state` open or answered."""
        need_licence()
        host.project_or_404(uid)
        st = state.strip().lower()
        if st and st not in QUESTION_STATES:
            host.err(ValueError("state is one of: " + ", ".join(QUESTION_STATES)))
        return {"questions": store.list_spec_questions(uid, st or None)}

    @app.post("/api/projects/{uid}/questions/{qid}/answer")
    def answer_question(uid: str, qid: str, request: Request, body: dict = Body(...)):
        """{answer}: the person the question is addressed to answers it. Anyone else is refused."""
        need_licence()
        c = host.team_reader()
        host.project_or_404(uid)
        me = c.work_person(request)
        q = store.get_spec_question(uid, qid)
        if q is None:
            host.err(KeyError(f"project has no question {qid!r}"), 404)
        if q["to_person_id"] != me["id"]:
            who = (q["to_person"] or {}).get("name") or q["to_person_id"]
            host.err(PermissionError(f"{q['id']} is for {who}: only they answer it"), 403)
        if q["state"] != "open":
            host.err(ValueError(f"{q['id']} is already answered"), 409)
        answer = str(body.get("answer") or "").strip()
        if not answer:
            host.err(ValueError("answer: your reply to the question"))
        if len(answer) > 4000:
            host.err(ValueError("keep the answer to 4000 characters"))
        if not store.answer_spec_question(uid, q["id"], answer, me["id"], now_utc(),
                                          via=console_via(request)):
            host.err(ValueError(f"{q['id']} is already answered"), 409)
        return store.get_spec_question(uid, q["id"])
