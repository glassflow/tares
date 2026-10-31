"""Was this worth raising? (TR-746)

A watcher on a schedule hands a finding to an investigator. A person answers yes or no on what the
watcher raised, through PUT /api/findings/{id}/worth. Covers: the answer on the investigator's note
credited to the watcher (agent, run, firing id), a result no watcher raised (409), an unknown run
(404), a quiet check, practice, the same person answering twice, two people, DELETE, the hit rate
(null under 5 answers, 0.8 with 4 of 5), the section in the watcher's system prompt (the watcher's
run only, not an investigator's, none without a "no", none after 14 days, at most 5 lines of 200
characters, none on a platform cell), the CSV and JSON export (admin only) and the worth and
can_answer fields on both results routes.

The model loop is a stand-in that concludes from a script per (agent, key) but runs the real
_loop_with, so the system prompt a model would get is the one captured. Everything else is real.

Run: .venv/bin/python tests/test_worth_raising.py
"""
import asyncio
import csv
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

TOKEN = "root-token"
TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
os.environ["TARES_AUTH_TOKEN"] = TOKEN   # read when the daemon is imported

import httpx  # noqa: E402

from fixtures.sre_cell import build  # noqa: E402
from tares import feedback  # noqa: E402
from tares.builtin_agents import AgentRunner, prompt_hash  # noqa: E402
from tares.models import ModelReply, ToolCall  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


SCRIPT: dict = {}    # (agent, key) -> the arguments of its conclude call
SYSTEMS: list = []   # (agent, key, the system prompt the model was given)
SECTION = "not worth raising"


class Scripted:
    kind = "anthropic"

    def __init__(self, agent, key):
        self.agent, self.key = agent, key

    async def complete(self, *, system="", **kw):
        SYSTEMS.append((self.agent, self.key, system))
        args = SCRIPT.get((self.agent, self.key), {"outcome": "no_op", "summary": "nothing"})
        return ModelReply(text="", tool_calls=[ToolCall("c1", "conclude", args)],
                          usage={"input_tokens": 1, "output_tokens": 1})


class Toolbox:
    tool_defs = []
    failures = []

    def owns(self, name):
        return False


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    return await self._loop_with(agent, trigger_name, key, payload, Scripted(agent["name"], key),
                                 Toolbox(), usage, tracer, obs, model=model, concluded=concluded,
                                 produced=produced, skills_loaded=skills_loaded, handoff=handoff)


AgentRunner._loop = fake_loop


def H(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


async def wait_for(fn, tries=300):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


def section_of(agent, key):
    return next(s for a, k, s in reversed(SYSTEMS) if a == agent and k == key)


async def cell():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H())
        store, runtime, runner = app.state.store, app.state.runtime, app.state.agents
        runner.attach_loop()
        now = datetime.now(timezone.utc).replace(microsecond=0)

        async def key_for(name, scopes):
            r = await cx.post("/api/keys", json={"name": name, "scopes": scopes})
            assert r.status_code == 201, r.text
            return H(r.json()["secret"])

        alice = await key_for("alice", ["admin"])
        bob = await key_for("bob", ["admin"])
        reader = await key_for("reader", ["read"])

        eq("edition set to AI SRE",
           (await cx.put("/api/settings/edition", json={"edition": "sre"})).status_code, 200)
        made = build(store, now, watcher=True, investigator=True)
        uid = made["project"]
        runtime.reload_catalog()

        async def finished(rid):
            return await wait_for(lambda: (lambda x: x if x and x["status"] != "running" else None)(
                store.get_agent_run(rid)))

        async def watch(key, n=0, **args):
            """The watcher woken by its schedule for `key`; waits for it and for what it handed
            on. Returns (the watcher's run, the investigator's run or None)."""
            SCRIPT[("watcher", key)] = args
            store.log_dispatch(f"disp-{key}", "watch-prod", key, "schedule", 1, 1, "", project=uid)
            rid = runner.deliver("watcher", "sub-1", "watch-prod", key, "", f"disp-{key}",
                                 projects=[uid])
            run = await finished(rid)
            assert run and run["status"] == "ok", run
            if args.get("verdict") != "investigate":
                return run, None
            kid = await wait_for(lambda: next(
                (r for r in store.list_agent_runs("investigator", limit=20)
                 if r["parent_run_id"] == rid), None))
            return run, await finished(kid["id"])

        async def put(headers, run_id, worth, **extra):
            return await cx.put(f"/api/findings/{run_id}/worth", json={"worth": worth, **extra},
                                headers=headers)

        async def result(run_id, headers=None, show="all"):
            r = await cx.get("/api/findings", params={"show": show, "limit": 100},
                             headers=headers or H())
            assert r.status_code == 200, r.text
            return next((x for x in r.json()["findings"] if x["id"] == run_id), None)

        print("== a watcher hands a finding to an investigator ==")
        before = len(SYSTEMS)
        SCRIPT[("investigator", "edge-proxy")] = {
            "outcome": "finding", "verdict": "rca", "summary": "The 14:02 deploy left old pods."}
        w1, i1 = await watch("edge-proxy", outcome="finding", verdict="investigate",
                             summary="502s on the edge-proxy rose to 12x.")
        eq("both ran", (w1["status"], i1 is not None), ("ok", True))
        ck("no section yet: nobody has answered", all(SECTION not in s for _a, _k, s in SYSTEMS[before:]))
        r1 = await result(i1["id"])
        eq("the investigator's note is a result nobody has answered", r1["worth"],
           {"yes": 0, "no": 0, "mine": None})
        eq("a person can answer on it", r1["can_answer"], True)
        r = await cx.get(f"/api/projects/{uid}/results", params={"limit": 50})
        got = next(x for x in r.json()["results"] if x["id"] == i1["id"])
        eq("the project's results carry the same fields",
           (got["worth"], got["can_answer"]), (r1["worth"], True))

        print("== an answer on the investigator's note is credited to the watcher ==")
        r = await put(alice, i1["id"], False, note="  known deploy noise ")
        eq("answered", r.status_code, 200)
        eq("the counts, and mine", r.json(), {"ok": True, "worth": {"yes": 0, "no": 1, "mine": False}})
        row = store.feedback_for([i1["id"]])[i1["id"]][0]
        eq("it names the watcher, its run and the firing",
           (row["watcher"], row["watcher_run"], row["dispatch_id"], row["project"]),
           ("watcher", w1["id"], "disp-edge-proxy", uid))
        eq("it names the person and the way they answered",
           (row["person"], row["person_name"], row["via"], row["note"], row["worth"]),
           ("alice", "alice", "console", "known deploy noise", False))

        print("== the same person answering twice keeps one row ==")
        eq("alice changes her mind", (await put(alice, i1["id"], True)).json()["worth"],
           {"yes": 1, "no": 0, "mine": True})
        rows = store.feedback_for([i1["id"]])[i1["id"]]
        eq("one row with the last answer", [(x["person"], x["worth"], x["note"]) for x in rows],
           [("alice", True, None)])

        print("== two people ==")
        r = await put(bob, i1["id"], False, note="same as last week")
        eq("yes 1, no 1, mine is bob's", r.json()["worth"], {"yes": 1, "no": 1, "mine": False})
        eq("alice sees her own answer",
           (await result(i1["id"], alice))["worth"], {"yes": 1, "no": 1, "mine": True})
        eq("bob sees his", (await result(i1["id"], bob))["worth"], {"yes": 1, "no": 1, "mine": False})

        print("== what cannot be answered ==")
        r = await put(alice, "nope", True)
        eq("an unknown run is a 404", r.status_code, 404)
        eq("which says so in words", r.json()["detail"], "no run 'nope'")
        eq("so is taking back an answer on it",
           (await cx.delete("/api/findings/nope/worth", headers=alice)).status_code, 404)
        store.start_agent_run("run_loose", "investigator", "ask", "", "edge-proxy", "h",
                              woken_by="manual", project=uid)
        store.finish_agent_run("run_loose", "ok", finding="Looked on request.", outcome="finding",
                               verdict="rca")
        r = await put(alice, "run_loose", True)
        eq("a note no watcher raised is a 409", r.status_code, 409)
        eq("with the reason", r.json()["detail"], "Only what the watcher raised can be answered")
        store.start_agent_run("run_quiet", "watcher", "watch-prod", "d", "edge-proxy", "h",
                              project=uid)
        store.finish_agent_run("run_quiet", "ok", finding="Nothing changed.", outcome="no_op")
        eq("a quiet check is a 409", (await put(alice, "run_quiet", True)).status_code, 409)
        store.start_agent_run("run_practice", "watcher", "watch-prod", "d", "edge-proxy", "h",
                              project=uid, practice=True)
        store.finish_agent_run("run_practice", "ok", finding="Practice.", outcome="finding",
                               verdict="investigate")
        eq("a practice result is a 409", (await put(alice, "run_practice", True)).status_code, 409)
        _w, _ = await watch("docs-site", outcome="finding", verdict="ok",
                            summary="docs-site is fine.")
        eq("a watcher finding that says there is nothing to do is a 409",
           (await put(alice, _w["id"], True)).status_code, 409)
        eq("and can_answer says so", (await result(_w["id"]))["can_answer"], False)
        eq("nothing was stored for any of them",
           sum(len(v) for v in store.feedback_for(
               ["run_loose", "run_quiet", "run_practice", _w["id"]]).values()), 0)
        SCRIPT[("investigator", "gateway")] = {"outcome": "no_op", "summary": "Looked, nothing wrong."}
        _w, qi = await watch("gateway", outcome="finding", verdict="investigate",
                             summary="Gateway latency doubled.")
        eq("the investigator found nothing: that note is a 409",
           (await put(alice, qi["id"], True)).status_code, 409)
        eq("and can_answer says so", (await result(qi["id"], None, "all"))["can_answer"], False)
        eq("worth must be true or false", (await put(alice, i1["id"], "yes")).status_code, 400)
        eq("a note is at most 300 characters",
           (await put(alice, i1["id"], True, note="x" * 301)).status_code, 400)
        eq("a read key may not answer", (await put(reader, i1["id"], True)).status_code, 403)

        print("== a watcher's own finding is credited to itself ==")
        w2, _none = await watch("service-a", outcome="finding", verdict="degraded",
                                summary="service-a is slow.")
        r2 = await result(w2["id"])
        eq("a raised watcher finding can be answered", (r2["outcome"], r2["can_answer"]),
           ("raised", True))
        eq("answered", (await put(alice, w2["id"], True)).status_code, 200)
        row = store.feedback_for([w2["id"]])[w2["id"]][0]
        eq("it names the run itself", (row["watcher"], row["watcher_run"], row["dispatch_id"]),
           ("watcher", w2["id"], "disp-service-a"))

        print("== taking an answer back ==")
        r = await cx.delete(f"/api/findings/{i1['id']}/worth", headers=alice)
        eq("alice's answer is gone, bob's stays", r.json(),
           {"ok": True, "worth": {"yes": 0, "no": 1, "mine": None}})
        eq("taking back what is not there is fine",
           (await cx.delete(f"/api/findings/{i1['id']}/worth", headers=alice)).status_code, 200)

        print("== the hit rate ==")
        r = await cx.get("/api/watchers/hit-rate")
        eq("two answers on two results, so no rate yet", r.json(), {"watchers": [
            {"agent": "watcher", "project": uid, "answered": 2, "yes": 1, "no": 1,
             "hit_rate": None, "words": "2 answers so far"}]})
        for n in range(2):
            feedback.answer(store, w2["id"], True, f"p{n}", f"P{n}", "slack")
        row = (await cx.get("/api/watchers/hit-rate")).json()["watchers"][0]
        eq("4 answers is still not a rate", (row["answered"], row["hit_rate"], row["words"]),
           (4, None, "4 answers so far"))
        feedback.answer(store, w2["id"], True, "p2", "P2", "slack")
        row = (await cx.get("/api/watchers/hit-rate")).json()["watchers"][0]
        eq("4 of 5 is 0.8", (row["answered"], row["yes"], row["no"], row["hit_rate"], row["words"]),
           (5, 4, 1, 0.8, "4 of 5 worth raising in the last 30 days"))
        eq("a Slack answer says so", store.feedback_for([w2["id"]])[w2["id"]][-1]["via"], "slack")
        eq("a window with no answers is empty",
           (await cx.get("/api/watchers/hit-rate", params={"days": 1})).json()["watchers"][0]["answered"], 5)
        with store._swrite:
            store.sc.execute("UPDATE finding_feedback SET answered_at = ?", [now - timedelta(days=40)])
        eq("answers older than the window do not count",
           (await cx.get("/api/watchers/hit-rate")).json(), {"watchers": []})
        eq("a longer window brings them back",
           (await cx.get("/api/watchers/hit-rate", params={"days": 60})).json()["watchers"][0]["answered"], 5)
        with store._swrite:
            store.sc.execute("UPDATE finding_feedback SET answered_at = ?", [now])

        print("== the export ==")
        r = await cx.get("/api/feedback/export", params={"format": "csv"}, headers=alice)
        eq("a CSV", (r.status_code, r.headers["content-type"].split(";")[0]), (200, "text/csv"))
        lines = list(csv.DictReader(io.StringIO(r.text)))
        eq("the header", r.text.splitlines()[0],
           "at,run_id,watcher,watcher_run,dispatch_id,project,worth,person_name,note,via")
        eq("one line per answer", len(r.text.strip().splitlines()) - 1, 5)
        ck("a line names the firing", any(x["dispatch_id"] == "disp-service-a" for x in lines))
        ck("and the note", any(x["note"] == "same as last week" for x in lines))
        r = await cx.get("/api/feedback/export", headers=alice)
        eq("JSON has the same rows", len(r.json()["feedback"]), 5)
        eq("since keeps the later ones", (await cx.get(
            "/api/feedback/export", params={"since": (now + timedelta(days=1)).isoformat()},
            headers=alice)).json(), {"feedback": []})
        eq("it names people, so a read key is refused",
           (await cx.get("/api/feedback/export", headers=reader)).status_code, 403)
        eq("a bad format is a 400",
           (await cx.get("/api/feedback/export", params={"format": "xml"})).status_code, 400)

        print("== the watcher's next run is told what was not worth raising ==")
        n = len(SYSTEMS)
        store.set_feedback("run_seed", "alice", "alice", False, "seeded", "console", "investigator",
                           "run_w", None, uid)
        ck("the investigator has a case of its own to be told", feedback.prompt_section(
            store, "investigator") != "")
        SCRIPT[("investigator", "edge-proxy-2")] = {"outcome": "finding", "verdict": "rca",
                                                    "summary": "Same cause."}
        w3, i3 = await watch("edge-proxy-2", outcome="finding", verdict="investigate",
                             summary="502s again.")
        system = section_of("watcher", "edge-proxy-2")
        ck("the watcher's prompt has the section", SECTION in system, system[-300:])
        ck("with the case and the reason",
           "edge-proxy" in system and "same as last week" in system, system[-300:])
        ck("and the closing line", system.rstrip().endswith("only if it is clearly worse."))
        agent_prompt = store.get_catalog_agent("watcher")["prompt"]
        ck("the prompt itself comes first", system.startswith(agent_prompt))
        with store._sread:
            stored = store.sc.execute("SELECT prompt_hash FROM agent_runs WHERE id = ?",
                                      [w3["id"]]).fetchone()[0]
        eq("the hash covers the prompt only", stored, prompt_hash(agent_prompt))
        investigator_runs = [s for a, k, s in SYSTEMS[n:] if a == "investigator"]
        ck("the investigator's run (a handoff on the same schedule) has none",
           investigator_runs and all(SECTION not in s for s in investigator_runs))

        print("== older than 14 days, or answered yes, is not told ==")
        with store._swrite:
            store.sc.execute("UPDATE finding_feedback SET answered_at = ? WHERE worth = ?",
                             [now - timedelta(days=15), False])
        eq("nothing after 14 days", feedback.prompt_section(store, "watcher"), "")
        with store._swrite:
            store.sc.execute("UPDATE finding_feedback SET answered_at = ?", [now])
        ck("a yes adds nothing", feedback.prompt_section(store, "watcher").count("\n- ") == 1)
        ck("another watcher is told nothing", feedback.prompt_section(store, "other") == "")

        print("== at most 5 lines of 200 characters ==")
        for n in range(8):
            store.set_feedback(f"run_x{n}", "alice", "alice", False, "long reason " * 40, "console",
                               "watcher2", "run_w", None, uid)
        text = feedback.prompt_section(store, "watcher2")
        bullets = [ln for ln in text.splitlines() if ln.startswith("- ")]
        eq("five lines", len(bullets), 5)
        ck("each cut to 200 characters", all(len(b) <= 200 for b in bullets), str(max(map(len, bullets))))
        ck("no dash in what it says", chr(0x2014) not in text and chr(0x2013) not in text)

        print("== a platform cell's prompt does not change ==")
        eq("edition set to platform",
           (await cx.put("/api/settings/edition", json={"edition": "platform"})).status_code, 200)
        w4, _i = await watch("edge-proxy-3", outcome="finding", verdict="degraded", summary="Slow.")
        system = section_of("watcher", "edge-proxy-3")
        ck("no section on the same answers", SECTION not in system)
        ck("the prompt is the agent's own", system.startswith(agent_prompt))
        ck("it has no list of cases", "Recently you raised" not in system)
        eq("the answers still work there", (await put(alice, w4["id"], True)).status_code, 200)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(cell())
