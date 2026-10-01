"""Skills (TR-332, TR-333): a project's instructions its agents load by name.

Covers validation and SKILL.md parsing, the REST routes (CRUD, upload, auth scopes), deletion with
the project, catalog export/import, a template planning a skill, and the agent runtime with a
scripted model: the prompt section and the `skill` tool only when the project has skills, a
loaded skill recorded on the run, an unknown name answered with the names, and an agent in
another project never seeing them.

Run: .venv/bin/python tests/test_skills.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-skills-")
DB = os.path.join(_TMP, "api.duckdb")
TOKEN = "root-token-skills"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx
import yaml

import tares.builtin_agents as ba
from tares import skills
from tares.models import ModelReply, ToolCall
from tares.projects import PlannedObject, Template, register
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def raises(fn, *a):
    try:
        fn(*a)
    except skills.SkillError as e:
        return str(e)
    return None


GOOD_MD = """---
name: triage-notes
description: How to triage a checkout incident. Use when the checkout service errors.
license: MIT
---

# Triage

1. Read the last hour of checkout logs.
"""


class SkillTemplate(Template):
    """Tests only: a template that plans one skill."""
    key = "test_skill_tpl"
    title = "Test skill template"
    PARAMS = {"body": {"type": "string", "default": "Step one."}}

    def plan(self, params):
        return [PlannedObject("skill", "skill:playbook", {
            "name": "playbook", "description": "The team playbook.", "body": params["body"]})]


register(SkillTemplate())


def unit():
    print("== validation ==")
    ck("a good skill passes", skills.validate("a-1", "does x", "body") == ("a-1", "does x", "body"))
    ck("uppercase name refused", raises(skills.validate, "Bad", "d", "b") is not None)
    ck("underscore refused", raises(skills.validate, "a_b", "d", "b") is not None)
    ck("65 chars refused", raises(skills.validate, "a" * 65, "d", "b") is not None)
    ck("64 chars fine", raises(skills.validate, "a" * 64, "d", "b") is None)
    ck("empty description refused", "description" in (raises(skills.validate, "a", " ", "b") or ""))
    ck("1025 char description refused", raises(skills.validate, "a", "d" * 1025, "b") is not None)
    ck("two paragraphs refused", "one paragraph" in (raises(skills.validate, "a", "x\n\ny", "b") or ""))
    ck("empty body refused", raises(skills.validate, "a", "d", "\n  \n") is not None)
    ck("body over 64 KB refused", "64 KB" in (raises(skills.validate, "a", "d", "x" * 65537) or ""))
    ck("body of exactly 64 KB fine", raises(skills.validate, "a", "d", "x" * 65536) is None)

    print("== SKILL.md parsing ==")
    name, desc, body = skills.parse_skill_md(GOOD_MD)
    ck("name and description from the front matter", name == "triage-notes"
       and desc.startswith("How to triage"), f"{name!r} {desc!r}")
    ck("body is what follows, without the front matter", body.startswith("# Triage")
       and "license" not in body, repr(body))
    ck("CRLF line endings read the same", skills.parse_skill_md(GOOD_MD.replace("\n", "\r\n"))[0]
       == "triage-notes")
    folded = "---\nname: x\ndescription: >\n  one\n  line\n---\nbody\n"
    ck("a folded YAML description is one paragraph", skills.parse_skill_md(folded)[1] == "one line")
    for label, text, want in (
            ("no front matter", "# just markdown\n", "front matter"),
            ("front matter not closed", "---\nname: x\ndescription: y\n# body", "not closed"),
            ("no description", "---\nname: x\n---\nbody", "no description"),
            ("no name", "---\ndescription: y\n---\nbody", "no name"),
            ("bad name", "---\nname: Not Valid\ndescription: y\n---\nbody", "lowercase"),
            ("not YAML", "---\nname: [x\ndescription: y\n---\nbody", "not valid YAML"),
            ("empty body", "---\nname: x\ndescription: y\n---\n\n", "empty")):
        err = raises(skills.parse_skill_md, text)
        ck(f"refused: {label}", err is not None and want in err, repr(err))

    print("== prompt section ==")
    sec = skills.prompt_section([{"name": "a", "description": "does a"},
                                 {"name": "b", "description": "does\nb"}])
    ck("heading, one line per skill, and the load line",
       "## Skills available in this project" in sec and "- a: does a" in sec
       and "- b: does b" in sec and "`skill` tool before relying on it" in sec, sec)
    ck("no em dash in the section", "—" not in sec)


async def api():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=root)
        r = await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})
        reader = {"Authorization": f"Bearer {r.json()['secret']}"}
        p1 = (await cx.post("/api/projects", json={"template": "custom", "name": "Checkout",
                                                   "objects": []})).json()["id"]
        p2 = (await cx.post("/api/projects", json={"template": "custom", "name": "Payments",
                                                   "objects": []})).json()["id"]
        base = f"/api/projects/{p1}/skills"

        print("== CRUD ==")
        r = await cx.post(base, json={"name": "triage", "description": "Triage steps.",
                                      "body": "# Steps\n\n1. look"})
        ck("create -> 201", r.status_code == 201 and r.json()["name"] == "triage", r.text)
        r = await cx.post(base, json={"name": "triage", "description": "again", "body": "x"})
        ck("same name again -> 409", r.status_code == 409, r.text)
        r = await cx.post(base, json={"name": "Bad Name", "description": "d", "body": "x"})
        ck("bad name -> 400 with a plain message", r.status_code == 400
           and "lowercase" in r.json()["detail"], r.text)
        r = await cx.post(base, json={"name": "x", "description": "a\n\nb", "body": "x"})
        ck("two-paragraph description -> 400", r.status_code == 400, r.text)
        r = await cx.post(base, json={"name": "x", "description": "d", "body": "x" * 70000})
        ck("body over 64 KB -> 400", r.status_code == 400, r.text)
        r = await cx.post("/api/projects/uc_nope/skills", json={"name": "x", "description": "d",
                                                                "body": "x"})
        ck("unknown project -> 404", r.status_code == 404, r.text)
        r = await cx.get(base)
        ck("list: name, description, size, updated_at, loaded_by",
           r.status_code == 200 and len(r.json()) == 1
           and r.json()[0]["size"] == len("# Steps\n\n1. look")
           and r.json()[0]["updated_at"] and r.json()[0]["loaded_by"] == [], r.text)
        r = await cx.get(f"{base}/triage")
        ck("get one -> body", r.status_code == 200 and r.json()["body"] == "# Steps\n\n1. look",
           r.text)
        ck("get unknown -> 404", (await cx.get(f"{base}/nope")).status_code == 404)
        r = await cx.put(f"{base}/triage", json={"description": "Triage, revised."})
        ck("put description only keeps the body", r.status_code == 200
           and r.json()["description"] == "Triage, revised."
           and r.json()["body"] == "# Steps\n\n1. look", r.text)
        r = await cx.put(f"{base}/triage", json={"body": ""})
        ck("put an empty body -> 400", r.status_code == 400, r.text)
        ck("put unknown -> 404", (await cx.put(f"{base}/nope", json={"body": "x"})).status_code == 404)
        ck("the other project has none", (await cx.get(f"/api/projects/{p2}/skills")).json() == [])

        print("== auth ==")
        ck("read key lists", (await cx.get(base, headers=reader)).status_code == 200)
        ck("read key reads one", (await cx.get(f"{base}/triage", headers=reader)).status_code == 200)
        r = await cx.post(base, headers=reader, json={"name": "y", "description": "d", "body": "x"})
        ck("read key cannot create (403)", r.status_code == 403, r.text)
        ck("read key cannot edit (403)",
           (await cx.put(f"{base}/triage", headers=reader, json={"body": "x"})).status_code == 403)
        ck("read key cannot delete (403)",
           (await cx.delete(f"{base}/triage", headers=reader)).status_code == 403)
        ck("read key cannot upload (403)",
           (await cx.post(f"{base}/upload", headers=reader, content=GOOD_MD)).status_code == 403)

        print("== upload ==")
        r = await cx.post(f"{base}/upload", content=GOOD_MD.encode())
        ck("upload creates", r.status_code == 200 and r.json()["created"] is True
           and r.json()["name"] == "triage-notes" and r.json()["body"].startswith("# Triage"), r.text)
        r = await cx.post(f"{base}/upload", content=GOOD_MD.replace("1. Read", "1. Reread").encode())
        ck("upload of the same name replaces", r.status_code == 200 and r.json()["created"] is False
           and "Reread" in r.json()["body"], r.text)
        r = await cx.post(f"{base}/upload", content=b"# no front matter")
        ck("upload without front matter -> 400 with a plain message",
           r.status_code == 400 and "front matter" in r.json()["detail"], r.text)
        r = await cx.post(f"{base}/upload", content=b"---\nname: q\n---\nbody")
        ck("upload without a description -> 400", r.status_code == 400
           and "description" in r.json()["detail"], r.text)
        r = await cx.post(f"{base}/upload", content=b"\xff\xfe")
        ck("upload that is not UTF-8 -> 400", r.status_code == 400, r.text)

        print("== export / import ==")
        # skills are shared (P-TR-216): Payments uses Checkout's triage instead of its own copy
        r = await cx.post(f"/api/projects/{p2}/skills", json={"name": "triage", "description": "Pay.",
                                                               "body": "pay body"})
        ck("a second skill of a name already on Tares -> 409, use it instead",
           r.status_code == 409 and "already on Tares" in r.text, r.text)
        r = await cx.post(f"/api/projects/{p2}/skills/triage/use")
        ck("Payments uses Checkout's triage", r.status_code == 200
           and r.json()["body"] == "# Steps\n\n1. look", r.text)
        text = (await cx.get("/api/catalog/export")).text
        doc = yaml.safe_load(text)
        got = {(s["project"], s["name"]): s for s in doc.get("skills") or []}
        ck("export lists every skill once, by the project that made it",
           set(got) == {("Checkout", "triage"), ("Checkout", "triage-notes")}, str(set(got)))
        ck("export carries description, body and who else uses it",
           got[("Checkout", "triage")]["description"] == "Triage, revised."
           and got[("Checkout", "triage")]["used_by"] == ["Payments"], str(got))
        ck("a partial export carries no skills",
           "skills" not in yaml.safe_load((await cx.get("/api/catalog/export?sources=x")).text))
        r = await cx.delete(f"{base}/triage")
        ck("Checkout stops using it; Payments still does, so it stays",
           r.json().get("kept_for") == [p2] and (await cx.get(f"/api/projects/{p2}/skills/triage")).status_code == 200,
           r.text)
        await cx.put(f"/api/projects/{p2}/skills/triage", json={"body": "changed"})
        r = await cx.post("/api/catalog/import", json={"yaml": text, "mode": "merge"})
        ck("import -> 200 counting skills", r.status_code == 200 and r.json()["skills"] == 2, r.text)
        r = await cx.get(f"{base}/triage")
        ck("Checkout uses it again, set back to the file", r.status_code == 200
           and r.json()["body"] == "# Steps\n\n1. look", r.text)
        r = await cx.get(f"/api/projects/{p2}/skills/triage")
        ck("and Payments sees the same skill", r.json()["body"] == "# Steps\n\n1. look", r.text)
        bad = yaml.safe_dump({"skills": [{"project": "Nowhere", "name": "x", "description": "d",
                                          "body": "b"}]})
        r = await cx.post("/api/catalog/import", json={"yaml": bad, "mode": "merge"})
        ck("import naming an unknown project -> 400", r.status_code == 400
           and "Nowhere" in r.text, r.text)
        bad = yaml.safe_dump({"skills": [{"project": "Checkout", "name": "UPPER",
                                          "description": "d", "body": "b"}]})
        r = await cx.post("/api/catalog/import", json={"yaml": bad, "mode": "merge"})
        ck("import of an invalid skill -> 400", r.status_code == 400, r.text)
        fresh = yaml.safe_dump({
            "projects": [{"template": "custom", "name": "New one", "objects": []}],
            "skills": [{"project": "New one", "name": "hello", "description": "d", "body": "b"},
                       {"name": "orphan", "description": "d", "body": "b"}]})
        r = await cx.post("/api/catalog/import", json={"yaml": fresh, "mode": "merge"})
        ck("import creates the project the skill names", r.status_code == 200, r.text)
        plist = (await cx.get("/api/projects")).json()["projects"]
        new = next(p for p in plist if p["name"] == "New one")["id"]
        default = next(p for p in plist if p["default"])["id"]
        ck("the skill is in it", [s["name"] for s in
                                  (await cx.get(f"/api/projects/{new}/skills")).json()] == ["hello"])
        ck("a skill with no project lands in the default project",
           "orphan" in [s["name"] for s in (await cx.get(f"/api/projects/{default}/skills")).json()])

        print("== a template plans a skill ==")
        r = await cx.post("/api/projects", json={"template": "test_skill_tpl", "name": "Tpl",
                                                 "params": {}})
        ck("created", r.status_code == 201, r.text[:300])
        tpl = r.json()["id"]
        objs = [o for o in r.json()["objects"] if o["kind"] == "skill"]
        ck("the planned skill is one of its objects", len(objs) == 1
           and objs[0]["name"] == "playbook" and not objs[0]["missing"], str(objs))
        r = await cx.get(f"/api/projects/{tpl}/skills/playbook")
        ck("the skill exists", r.status_code == 200 and r.json()["body"] == "Step one.", r.text)
        r = await cx.put(f"/api/projects/{tpl}", json={"params": {"body": "Step two."}})
        ck("a re-plan updates it", r.status_code == 200 and
           (await cx.get(f"/api/projects/{tpl}/skills/playbook")).json()["body"] == "Step two.",
           r.text[:300])
        await cx.put(f"/api/projects/{tpl}/skills/playbook", json={"body": "My own."})
        await cx.put(f"/api/projects/{tpl}", json={"params": {"body": "Step three."}})
        ck("an edited skill survives a re-plan",
           (await cx.get(f"/api/projects/{tpl}/skills/playbook")).json()["body"] == "My own.")
        await cx.delete(f"/api/projects/{tpl}/skills/playbook")
        r = await cx.post(f"/api/projects/{tpl}/repair", json={"key": "skill:playbook"})
        ck("repair brings a deleted planned skill back", r.status_code == 200 and
           (await cx.get(f"/api/projects/{tpl}/skills/playbook")).status_code == 200, r.text[:300])
        r = await cx.post("/api/projects", json={"template": "test_skill_tpl", "name": "Tpl bad",
                                                 "params": {"body": ""}})
        ck("a template planning an invalid skill fails to create", r.status_code == 400, r.text)

        print("== deleted with the project ==")
        r = await cx.delete(f"/api/projects/{p1}")
        ck("project deleted", r.status_code == 200, r.text)
        ck("its skills route answers 404", (await cx.get(base)).status_code == 404)
        doc = yaml.safe_load((await cx.get("/api/catalog/export")).text)
        ck("its skills are gone from the export",
           not [s for s in doc.get("skills") or [] if s["project"] == "Checkout"], str(doc.get("skills")))
        ck("the other project's skill stays",
           [s["name"] for s in (await cx.get(f"/api/projects/{p2}/skills")).json()] == ["triage"])
        await cx.aclose()


# ── the agent runtime, with a scripted model ─────────────────────────────────
class Provider:
    """Answers each call with the next scripted reply; remembers the system prompt, the tools
    and the tool results it was handed."""
    kind = "anthropic"

    def __init__(self, replies):
        self.replies = list(replies)
        self.systems, self.offered, self.tool_results = [], [], []

    async def complete(self, *, system, tools, messages, **kw):
        self.systems.append(system)
        self.offered.append([t["name"] for t in tools])
        last = messages[-1]
        if last.get("role") == "tool":
            self.tool_results.extend(r["content"] for r in last["results"])
        return self.replies.pop(0)


def reply(text="", calls=()):
    return ModelReply(text=text, tool_calls=list(calls),
                      usage={"input_tokens": 1, "output_tokens": 1})


def load(name, cid):
    return ToolCall(id=cid, name="skill", arguments={"name": name})


class Toolbox:
    tool_defs, failures = [], []

    def owns(self, name):
        return False


def runner(store, provider):
    r = object.__new__(ba.AgentRunner)
    r.store = store
    r.tracing = None

    async def record(*a, **k):
        return []

    async def loop(agent, trigger, key, payload, prov, model, usage, tracer, obs, **kw):
        return await r._loop_with(agent, trigger, key, payload, provider, Toolbox(), usage,
                                  tracer, obs, model=model, **kw)

    r._record, r._loop = record, loop
    r._callback_anchor = lambda agent, trigger, key: (key, {})
    return r


class Tracing:
    def tracer_for(self, name):
        return None


async def runtime():
    ba.resolve_for_agent = lambda store, agent: (Provider([]), "test", "anthropic", "")
    ba.default_model_for = lambda store, provider_id: "claude-test"
    ba.price_usage = lambda *a: 0.0
    store = Store(os.path.join(_TMP, "runtime.duckdb"))
    store.create_project("uc_a", "custom", "A", {})
    store.create_project("uc_b", "custom", "B", {})
    store.upsert_skill("uc_a", "triage-notes", "How to triage checkout.", "STEP ONE")
    store.upsert_skill("uc_a", "rollback", "How to roll back.", "ROLL")
    with_skills = {"name": "a-agent", "prompt": "You watch checkout.", "owned_by": "uc_a"}
    other = {"name": "b-agent", "prompt": "You watch payments.", "owned_by": "uc_b"}

    print("== no skills in the project: prompt and tools as before ==")
    prov = Provider([reply("all fine")])
    await runner(store, prov)._loop_with(other, "t", "k", "payload", prov, Toolbox(),
                                         ba.empty_usage(), None, None, model="m")
    ck("system prompt is the agent's own, exactly", prov.systems[0] == "You watch payments.",
       repr(prov.systems[0]))
    ck("tools are read and stats only", prov.offered[0] == ["read", "stats"], str(prov.offered))
    ck("an agent in another project never sees a project's skills",
       "triage-notes" not in prov.systems[0])
    prov = Provider([reply("fine")])
    await runner(store, prov)._loop_with({"name": "x", "prompt": "p"}, "t", "k", "payload", prov,
                                         Toolbox(), ba.empty_usage(), None, None, model="m")
    ck("an agent with no project: no section, no tool",
       prov.systems[0] == "p" and "skill" not in prov.offered[0])

    print("== skills in the project: the section and the tool ==")
    prov = Provider([reply("", [load("triage-notes", "c1")]),
                     reply("", [load("nope", "c2"), load("triage-notes", "c3")]),
                     reply("the finding")])
    loaded = []
    finding, *_ = await runner(store, prov)._loop_with(
        with_skills, "t", "k", "payload", prov, Toolbox(), ba.empty_usage(), None, None,
        model="m", skills_loaded=loaded)
    sysp = prov.systems[0]
    ck("the prompt starts with the agent's own", sysp.startswith("You watch checkout.\n\n"), sysp)
    ck("then the section listing each skill",
       "## Skills available in this project" in sysp
       and "- rollback: How to roll back." in sysp
       and "- triage-notes: How to triage checkout." in sysp, sysp)
    ck("bodies are not in the prompt", "STEP ONE" not in sysp and "ROLL\n" not in sysp)
    ck("the skill tool is offered", "skill" in prov.offered[0], str(prov.offered))
    ck("the tool returns the body", prov.tool_results[0] == "STEP ONE", str(prov.tool_results))
    ck("an unknown name is a tool error listing the names",
       prov.tool_results[1].startswith("tool error:") and "rollback" in prov.tool_results[1]
       and "triage-notes" in prov.tool_results[1], prov.tool_results[1])
    ck("loaded skills are recorded once, in order", loaded == ["triage-notes"], str(loaded))
    ck("the run still concludes", finding == "the finding", finding)
    ck("prompt_hash hashes only the agent's own prompt",
       ba.prompt_hash(with_skills["prompt"]) == ba.prompt_hash("You watch checkout."))

    print("== a run records the skills it loaded ==")
    r = runner(store, None)
    r.tracing = Tracing()
    prov = Provider([reply("", [load("rollback", "c1")]), reply("rolled back")])

    async def loop(agent, trigger, key, payload, _prov, model, usage, tracer, obs, **kw):
        return await r._loop_with(agent, trigger, key, payload, prov, Toolbox(), usage, tracer,
                                  obs, model=model, **kw)
    r._loop = loop
    store.start_agent_run("run_s1", "a-agent", "t", "", "k", "h", 8)
    status, err = await r._run(with_skills, "t", "k", "payload", "run_s1")
    run = next(x for x in store.list_agent_runs("a-agent") if x["id"] == "run_s1")
    ck("run ok", status == "ok", f"{status} {err}")
    ck("agent_runs.skills holds the loaded skill", run["skills"] == ["rollback"], str(run))
    r2 = runner(store, None)
    r2.tracing = Tracing()
    prov2 = Provider([reply("nothing loaded")])

    async def loop2(agent, trigger, key, payload, _prov, model, usage, tracer, obs, **kw):
        return await r2._loop_with(agent, trigger, key, payload, prov2, Toolbox(), usage, tracer,
                                   obs, model=model, **kw)
    r2._loop = loop2
    store.start_agent_run("run_s2", "a-agent", "t", "", "k", "h", 8)
    await r2._run(with_skills, "t", "k", "payload", "run_s2")
    run = next(x for x in store.list_agent_runs("a-agent") if x["id"] == "run_s2")
    ck("a run that loaded none records none", run["skills"] == [], str(run))
    ck("loads in the last 7 days per skill", store.skill_loads(["a-agent"]) ==
       {"rollback": ["a-agent"]}, str(store.skill_loads(["a-agent"])))
    ck("no agents, no loads", store.skill_loads([]) == {})


async def main():
    unit()
    await api()
    await runtime()
    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
