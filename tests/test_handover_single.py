"""Core with no licence: one person hands a project over, and the questions to repo owners are ee (TR-923).

A cell of one person (auth on, only `local`). A project with tickets in two repos nobody owns hands
over at once: no owner is asked and nothing is signed off. `my_work` has `spec_questions: []`. The
three question routes are FALLBACKS rows and answer 402 `licence_required`, and the MCP tools
`ask_area`, `answer_question`, `list_questions` and `sign_off` are not listed. `my_work` reads the
open questions from the team (`open_questions_for`), so a team that has some shows them.

The licensed behaviour of the same routes and tools is ee/tests/test_spec_questions.py.

Run: .venv/bin/python tests/test_handover_single.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-handover-single-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = AUTH = "root-token-handover-single"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import team as team_mod

team_mod._set_ee_for_tests(None)   # no ee team, whatever is installed on this machine

from tares import mcp_server as ms
from tares.daemon import make_app

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def exact_402(r, text):
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return (r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"
            and text in b["message"] and "enterprise licence" in b["message"] and "—" not in b["message"])


QUESTIONS = "Questions to repo owners need an enterprise licence."


class TeamWithQuestions(team_mod.SingleTeam):
    """A stand-in team that has one open question, to see `my_work` ask the team for them."""
    seen: list = []

    def open_questions_for(self, c, person_id, hidden, want):
        self.seen.append((person_id, sorted(hidden), want))
        return [{"project": "stub", "id": "Q1", "repo": "acme/web", "text": "Which page?",
                 "asked_by": "x", "asked_at": "2026-11-07T00:00:00"}]


async def routes():
    print("== one person hands a project over, no owner and no sign-off ==")
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(AUTH)) as cx:
        store = app.state.store
        ck("only local is on the cell", store.only_local_person())
        r = await cx.post("/api/projects", json={"template": "custom", "name": "two-repos", "objects": [],
                                                 "kind": "software_factory"})
        ck("a project is made", r.status_code in (200, 201), f"{r.status_code} {r.text[:100]}")
        uid = r.json()["id"]
        for title, repo in (("web work", "acme/web"), ("api work", "acme/api")):
            r = await cx.post(f"/api/projects/{uid}/tickets", json={"title": title, "repo": repo})
            ck(f"ticket in {repo}", r.status_code == 201, f"{r.status_code} {r.text[:100]}")
        ck("nobody owns either repo", store.list_spec_questions(uid) == [] and team_mod.get().areas(store) == [])
        repos = [{"path": os.path.join(TMP, "web"), "name": "acme/web"},
                 {"path": os.path.join(TMP, "api"), "name": "acme/api"}]
        r = await cx.post(f"/api/projects/{uid}/handover", json={"repos": repos})
        ck("the hand-over is accepted (200)", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        ho = r.json().get("handover") or {}
        ck("both repos are recorded", sorted(x["name"] for x in ho.get("repos") or []) == ["acme/api", "acme/web"],
           str(ho))
        ck("no override is recorded", ho.get("override") is None, str(ho))
        ck("it is stored as handed over", store.get_handover(uid) is not None
           and [p for p in store.handed_over_projects(finished=False, taken=False) if p == uid] == [uid])

        print("== my_work has no questions ==")
        r = await cx.get("/api/crew/my-work")
        ck("my_work: 200 with spec_questions []", r.status_code == 200 and r.json()["spec_questions"] == [],
           f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/crew/my-work", params={"project": "two-repos"})
        ck("my_work for the project: spec_questions []", r.status_code == 200 and r.json()["spec_questions"] == [],
           f"{r.status_code} {r.text[:200]}")

        print("== the question routes answer 402 ==")
        for what, method, path, body in (
                ("POST questions", "POST", f"/api/projects/{uid}/questions", {"repo": "acme/web", "text": "Which page?"}),
                ("GET questions", "GET", f"/api/projects/{uid}/questions", None),
                ("GET questions?state=open", "GET", f"/api/projects/{uid}/questions?state=open", None),
                ("POST answer", "POST", f"/api/projects/{uid}/questions/Q1/answer", {"answer": "the cart"})):
            r = await cx.request(method, path, json=body)
            ck(f"{what}: 402 licence_required with {{code, message, url}}", exact_402(r, QUESTIONS),
               f"{r.status_code} {r.text[:160]}")
        ck("nothing was written", store.list_spec_questions(uid) == [])
        r = await cx.post("/api/keys", json={"name": "ing", "scopes": ["ingest"]})
        ing = r.json().get("secret", "")
        r = await cx.post(f"/api/projects/{uid}/questions/Q1/answer", json={"answer": "x"}, headers=H(ing))
        ck("a key that may only ingest reaches the answer: 402, not 403", exact_402(r, QUESTIONS),
           f"{r.status_code} {r.text[:100]}")
        r = await cx.get("/api/projects/nope/questions")
        ck("an unknown project answers 402 too, before anything is read", exact_402(r, QUESTIONS),
           f"{r.status_code} {r.text[:100]}")

        print("== my_work asks the team for the questions ==")
        real = team_mod.get
        team = TeamWithQuestions()
        team_mod.get = lambda: team
        try:
            r = await cx.get("/api/crew/my-work", params={"project": "two-repos"})
        finally:
            team_mod.get = real
        sq = r.json().get("spec_questions")
        ck("the team's list is what my_work shows", r.status_code == 200 and sq and sq[0]["id"] == "Q1", str(sq))
        ck("the team got the person, the hidden projects and the project filter",
           team.seen == [("local", [], uid)], str(team.seen))
        r = await cx.get("/api/crew/my-work")
        ck("with the single team again it is empty", r.json()["spec_questions"] == [], r.text[:100])


def tools():
    print("== MCP tools without ee ==")
    names = [t.name for t in asyncio.run(ms.mcp.list_tools())]
    for n in ("ask_area", "answer_question", "list_questions", "sign_off"):
        ck(f"{n} is not a tool", n not in names, str(names))
    ck("hand_over and my_work (core tools) are still there", "hand_over" in names and "my_work" in names, str(names))
    ck("no ee tool is registered", ms.EE_TOOLS == set(), str(ms.EE_TOOLS))


asyncio.run(routes())
tools()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
