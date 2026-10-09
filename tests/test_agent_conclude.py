"""The `conclude` tool (TR-318): an agent ends a run on purpose, with no finding or with a finding
that carries a verdict and names the entity it is about.

Drives the real model loop (`_loop_with`) and run path (`_run_traced`) with a scripted provider,
and a real store for the part that matters to chaining: a finding's verdict is a label a view
filter can select on, and a concluded key puts the finding on that entity's timeline.
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba
from tares.config import SourceCfg
from tares.connectors.finding import FindingConnector
from tares.envelope import now_utc
from tares.models import ModelReply, ToolCall
from tares.store import Store

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class Provider:
    """Answers each call with the next scripted reply; remembers the tools it was offered."""
    kind = "anthropic"

    def __init__(self, replies):
        self.replies = list(replies)
        self.offered = []
        self.defs = []       # the tool definitions of each call
        self.systems = []    # the system prompt of each call
        self.choices = []    # the tool_choice of each call (None: the model's choice)

    async def complete(self, *, tools, **kw):
        self.offered.append([t["name"] for t in tools])
        self.defs.append(tools)
        self.systems.append(kw.get("system") or "")
        self.choices.append(kw.get("tool_choice"))
        return self.replies.pop(0)


def reply(text="", calls=()):
    return ModelReply(text=text, tool_calls=list(calls), usage={"input_tokens": 1, "output_tokens": 1})


def conclude(**args):
    return ToolCall(id="c1", name="conclude", arguments=args)


class Toolbox:
    tool_defs = []
    failures = []

    def owns(self, name):
        return False


class FakeStore:
    def __init__(self):
        self.finished = {}

    def last_finding(self, *a):
        return None

    def agent_runs_today(self, *a, **k):
        return 0

    def agent_cost_total(self, name):
        return 0.0

    def finish_agent_run(self, run_id, status, **kw):
        self.finished = {"status": status, **kw}

    def record_run_usage(self, *a, **k):
        pass

    def record_model_usage(self, *a, **k):
        pass


class Obs:
    def __init__(self):
        self.attrs = {}

    def set_output(self, *a):
        pass

    def set_input(self, *a):
        pass

    def set_attribute(self, k, v):
        self.attrs[k] = v


WATCHER = {"name": "watcher", "prompt": "Classify. End with the conclude tool."}
PLAIN = {"name": "first-look", "prompt": "Take a first look."}


def runner(store, provider):
    r = object.__new__(ba.AgentRunner)
    r.store = store
    recorded = []

    async def record(agent, trigger, key, finding, verdict=None, label=None, **kw):
        recorded.append({"key": key, "finding": finding, "verdict": verdict, "label": label})

    async def loop(agent, trigger, key, payload, prov, model, usage, tracer, obs, concluded=None,
                   produced=None, **kw):
        return await r._loop_with(agent, trigger, key, payload, provider, Toolbox(), usage,
                                  tracer, obs, model=model, concluded=concluded, produced=produced, **kw)

    r._record, r._loop, r.recorded = record, loop, recorded
    r._callback_anchor = lambda agent, trigger, key: (key, {})
    return r


async def run(agent, replies, key="ingress-nginx"):
    store, provider = FakeStore(), Provider(replies)
    r = runner(store, provider)
    obs = Obs()
    status, err = await r._run_traced(agent, "watch-tick", key, "payload", "run_1", "d1", None, obs)
    return status, err, store.finished, r.recorded, provider, obs


async def main():
    ba.resolve_for_agent = lambda store, agent: (Provider([]), "test", "anthropic", "")
    ba.default_model_for = lambda store, provider_id: "claude-test"
    ba.price_usage = lambda *a: 0.0

    print("== offered only when the prompt names it ==")
    check("parse: no_op", ba.parse_conclude({"outcome": "no_op", "summary": "calm"})[0]["outcome"] == "no_op")
    check("parse: bad outcome is an error", ba.parse_conclude({"outcome": "maybe"})[1] is not None)
    check("parse: a finding needs a summary", ba.parse_conclude({"outcome": "finding"})[1] is not None)
    _s, _e, _f, _r, prov, _o = await run(PLAIN, [reply("a finding")])
    check("a plain agent is not offered conclude", "conclude" not in prov.offered[0], str(prov.offered))

    print("== no_op: a quiet success ==")
    status, err, fin, rec, prov, obs = await run(WATCHER, [
        reply(calls=[conclude(outcome="no_op", summary="traffic as usual")])])
    check("the watcher is offered conclude", "conclude" in prov.offered[0], str(prov.offered))
    check("run is ok", (status, err) == ("ok", None), f"{status} {err}")
    check("no finding recorded", rec == [], str(rec))
    check("run keeps the reason and says no_op",
          fin.get("outcome") == "no_op" and fin.get("finding") == "traffic as usual", str(fin))
    check("span says no_op", obs.attrs.get("tares.outcome") == "no_op", str(obs.attrs))

    print("== finding: verdict and a named entity ==")
    status, err, fin, rec, _p, obs = await run(WATCHER, [
        reply(calls=[conclude(outcome="finding", summary="argus-ui 404s up 40x",
                              verdict="Investigate", key="glassflow-argus-ui")])])
    check("run is ok", (status, err) == ("ok", None), f"{status} {err}")
    check("finding on the named entity with the verdict",
          rec == [{"key": "glassflow-argus-ui", "finding": "argus-ui 404s up 40x",
                   "verdict": "investigate", "label": None}], str(rec))
    check("run records outcome and verdict",
          fin.get("outcome") == "finding" and fin.get("verdict") == "investigate", str(fin))

    print("== a bad conclude call goes back to the model ==")
    status, err, fin, rec, prov, _o = await run(WATCHER, [
        reply(calls=[conclude(outcome="finding")]),
        reply(calls=[conclude(outcome="no_op", summary="calm after all")])])
    check("second call concluded", fin.get("outcome") == "no_op" and len(prov.offered) == 2, str(fin))

    print("== without conclude, the last text is the finding (as before) ==")
    status, err, fin, rec, _p, _o = await run(PLAIN, [reply("svc is fine")])
    check("finding recorded on the woken key",
          rec == [{"key": "ingress-nginx", "finding": "svc is fine", "verdict": None, "label": None}], str(rec))

    print("== set to conclude, with its own verdicts ==")
    SET = {"name": "triager", "prompt": "Look at the window.", "concludes": True,
           "verdicts": [{"verdict": "page-oncall", "when": "users are hurt now"},
                        {"verdict": "ignore"}]}
    status, err, fin, rec, prov, _o = await run(SET, [
        reply(calls=[conclude(outcome="finding", summary="checkout down", verdict="page-oncall")])])
    check("offered conclude though its prompt never names it", "conclude" in prov.offered[0],
          str(prov.offered))
    vdef = next(t for t in prov.defs[0] if t["name"] == "conclude")["input_schema"]["properties"]["verdict"]
    check("the verdict is a choice of exactly its words, each with what it means",
          vdef.get("enum") == ["page-oncall", "ignore"] and "users are hurt now" in vdef["description"],
          str(vdef))
    check("the system prompt tells it to end with conclude and lists the verdicts",
          "End every run with the conclude tool" in prov.systems[0]
          and "- page-oncall: users are hurt now" in prov.systems[0], prov.systems[0][-400:])
    check("its verdict is recorded", fin.get("verdict") == "page-oncall" and rec[0]["verdict"] == "page-oncall",
          str(fin))
    status, err, fin, rec, prov, _o = await run(SET, [
        reply(calls=[conclude(outcome="finding", summary="checkout down", verdict="escalate")]),
        reply(calls=[conclude(outcome="finding", summary="checkout down")]),
        reply(calls=[conclude(outcome="finding", summary="checkout down", verdict="ignore")])])
    check("a word not on its list, then no verdict, each go back to the model",
          fin.get("verdict") == "ignore" and len(prov.offered) == 3, str(fin))
    check("parse: a listed verdict is required on a finding, not on no_op",
          ba.parse_conclude({"outcome": "finding", "summary": "x"}, ["a"])[1] is not None
          and ba.parse_conclude({"outcome": "no_op", "summary": "x"}, ["a"])[1] is None)
    status, err, fin, rec, prov, _o = await run(SET, [
        reply("all calm, I think"),
        reply(calls=[conclude(outcome="no_op", summary="all calm")])])
    check("it stopped without concluding: asked again with conclude the only choice",
          prov.choices[-1] == "conclude" and fin.get("outcome") == "no_op" and rec == [],
          f"{prov.choices} {fin}")
    status, err, fin, rec, prov, _o = await run({**SET, "verdicts": []}, [
        reply(calls=[conclude(outcome="finding", summary="odd spike", verdict="whatever")])])
    check("set to conclude without verdicts: any word goes", fin.get("verdict") == "whatever", str(fin))
    status, err, fin, rec, prov, _o = await run(WATCHER, [reply("calm")])
    check("a prompt-only agent that stops without concluding is not pushed (as before)",
          len(prov.offered) == 1 and rec and rec[0]["finding"] == "calm", str(rec))

    print("== a verdict label is selectable in a view ==")
    path = os.path.join(tempfile.mkdtemp(), "t.duckdb")
    store = Store(path)
    conn = FindingConnector(SourceCfg("findings", "finding", "finding", 5.0), store)
    store.append(conn.map_payload([
        {"key": "glassflow-argus-ui", "finding": "404s up", "agent": "watcher",
         "verdict": "investigate", "labels": {"service": "glassflow-argus-ui", "verdict": "investigate"}},
        {"key": "ingress-nginx", "finding": "rca note", "agent": "rca",
         "labels": {"service": "ingress-nginx"}},
    ]))
    since = now_utc() - timedelta(minutes=5)
    rows = store.read_window(["findings"], None, since, filters=[
        {"field": "agent", "op": "eq", "value": "watcher"},
        {"field": "verdict", "op": "eq", "value": "investigate"}])
    check("filter agent=watcher, verdict=investigate selects the watcher's finding only",
          len(rows) == 1 and rows[0][2] == "404s up", str(rows))
    rows = store.read_window(["findings"], "glassflow-argus-ui", since)
    check("the finding is on the named entity's timeline", len(rows) == 1, str(rows))


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
