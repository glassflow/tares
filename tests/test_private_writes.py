"""Writes on a private project's objects answer as if the object were not there (TR-944).

A person who is not on a private project, even with an admin-scope key, must not pause, change,
delete, test or rerun anything that belongs only to that project. Each write route that names a
source, trigger, agent, MCP server, run or rule proposal gets the same 404, with the same body,
as it gives for a name that does not exist. Ann leads the project and keeps access; so does the
system credential. An agent or a trigger that a visible project also uses stays reachable.

Run: .venv/bin/python tests/test_private_writes.py
"""
import asyncio, os, shutil, signal, socket, subprocess, sys, tempfile

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env
from tares.envelope import now_utc
from tares.store import Store

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
TMP = tempfile.mkdtemp(prefix="private-writes-")
DB = os.path.join(TMP, "cell.duckdb")
COND = {"aggregate": "count", "predicate": "> 0", "window": "1m"}


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def spawn(port):
    env = {**clean_env(), "TARES_DB": DB, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH}
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        proc.wait()


async def _wait(url, tries=80):
    for _ in range(tries):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def setup(B, ids):
    ROOT = H(AUTH)
    async with httpx.AsyncClient(timeout=15) as cx:
        async def person(name, email):
            return (await cx.post(f"{B}/api/people", headers=ROOT,
                                  json={"name": name, "email": email})).json()["id"]

        async def key(pid):
            r = await cx.post(f"{B}/api/keys", headers=ROOT,
                              json={"name": f"k-{pid}", "scopes": ["admin"], "person_id": pid})
            return r.json()["secret"]

        ann, kim = await person("Ann", "ann@example.org"), await person("Kim", "kim@example.org")
        ids["ann_key"], ids["kim_key"] = await key(ann), await key(kim)
        secret = (await cx.post(f"{B}/api/projects", headers=H(ids["ann_key"]), json={
            "template": "custom", "name": "Secret", "objects": []})).json()["id"]
        team = (await cx.post(f"{B}/api/projects", headers=H(ids["ann_key"]), json={
            "template": "custom", "name": "Open", "objects": []})).json()["id"]
        r = await cx.patch(f"{B}/api/projects/{secret}", headers=H(ids["ann_key"]),
                           json={"scope": "private"})
        ck("set up: Ann leads a private project, Kim is outside it",
           r.status_code == 200 and r.json()["scope"] == "private", r.text[:160])
        ids["secret"], ids["team"] = secret, team


def seed(ids):
    """Objects of the private project, of the team project, and some both use; with the daemon
    stopped."""
    store = Store(DB)
    secret, team = ids["secret"], ids["team"]
    for name, uids in (("secret-src", [secret]), ("open-src", [team]),
                       ("shared-src", [secret, team])):
        store.upsert_catalog_source(name, "application_log", "webhook", "5s", {})
        store.mark_customized("source", name)
        for u in uids:
            store.put_in_project("source", name, u)
    for name, src, uids in (("secret-trig", "secret-src", [secret]),
                            ("open-trig", "open-src", [team]),
                            ("shared-trig", "shared-src", [secret, team])):
        store.upsert_catalog_trigger(name, [src], COND, {}, "5m")
        store.mark_customized("trigger", name)
        for u in uids:
            store.put_in_project("trigger", name, u)
    for name, trig, uids in (("secret-agent", "secret-trig", [secret]),
                             ("open-agent", "open-trig", [team]),
                             ("shared-agent", "shared-trig", [secret, team])):
        store.upsert_catalog_agent(name, trig, "Look.")
        store.mark_customized("agent", name)
        for u in uids:
            store.put_in_project("agent", name, u)
    for name, u in (("secret-mcp", secret), ("open-mcp", team)):
        store.upsert_mcp_server(name, "http://127.0.0.1:9/mcp")
        store.mark_customized("mcp_server", name)
        store.put_in_project("mcp_server", name, u)
    for rid, agent, trig, u in (("run-secret", "secret-agent", "secret-trig", secret),
                                ("run-open", "open-agent", "open-trig", team),
                                ("run-shared-secret", "shared-agent", "shared-trig", secret),
                                ("run-shared-open", "shared-agent", "shared-trig", team),
                                ("run-noproject", "open-agent", "open-trig", None)):
        store.start_agent_run(rid, agent, trig, "d-" + rid, "service-a", "h", project=u)
        store.finish_agent_run(rid, "ok", finding="done")
    store.upsert_skill(secret, "secret-skill", "Hidden know-how.", "Hidden steps.")
    store.upsert_skill(team, "open-skill", "Open know-how.", "Open steps.")
    store.start_agent_run("run-both", "shared-agent", "shared-trig", "d-both", "service-a", "h",
                          project=secret)
    store.add_run_projects("run-both", [secret, team])
    store.finish_agent_run("run-both", "ok", finding="done")
    store.start_agent_run("run-secret-free", "secret-agent", "secret-trig", "d-free", "service-a",
                          "h", project=None)
    store.finish_agent_run("run-secret-free", "ok", finding="done")
    now = now_utc()
    ids["rule_secret"] = store.upsert_rule_proposal("example-org/repo-a", "secret-kind", secret,
                                                    "project", "Rule A", ["Secret T1"], now)["id"]
    ids["rule_open"] = store.upsert_rule_proposal("example-org/repo-b", "open-kind", team,
                                                  "project", "Rule B", ["Open T1"], now)["id"]
    store.con.close()


def body_for(kind, name):
    return {"source": {"name": name, "connector": "webhook"},
            "mcp": {"name": name, "url": "http://127.0.0.1:9/mcp"},
            "trigger": {"name": name, "sources": ["open-src"], "condition": COND},
            "agent": {"name": name, "prompt": "Look."}}.get(kind)


# (label, method, path with {n}, body kind, name of the hidden object, name of the open one)
ROUTES = [
    ("source update", "PUT", "/api/sources/{n}", "source", "secret-src", "open-src"),
    ("source pause", "POST", "/api/sources/{n}/pause", None, "secret-src", "open-src"),
    ("source resume", "POST", "/api/sources/{n}/resume", None, "secret-src", "open-src"),
    ("source delete", "DELETE", "/api/sources/{n}", None, "secret-src", "open-src"),
    ("mcp server update", "PUT", "/api/mcp-servers/{n}", "mcp", "secret-mcp", "open-mcp"),
    ("mcp server test", "POST", "/api/mcp-servers/{n}/test", None, "secret-mcp", "open-mcp"),
    ("mcp server delete", "DELETE", "/api/mcp-servers/{n}", None, "secret-mcp", "open-mcp"),
    ("trigger update", "PUT", "/api/triggers/{n}", "trigger", "secret-trig", "open-trig"),
    ("trigger pause", "POST", "/api/triggers/{n}/pause", None, "secret-trig", "open-trig"),
    ("trigger resume", "POST", "/api/triggers/{n}/resume", None, "secret-trig", "open-trig"),
    ("trigger delete", "DELETE", "/api/triggers/{n}", None, "secret-trig", "open-trig"),
    ("agent update", "PUT", "/api/agents/builtin/{n}", "agent", "secret-agent", "open-agent"),
    ("agent enable", "POST", "/api/agents/builtin/{n}/enable", None, "secret-agent", "open-agent"),
    ("agent disable", "POST", "/api/agents/builtin/{n}/disable", None, "secret-agent", "open-agent"),
    ("agent rerun", "POST", "/api/agents/builtin/{n}/runs/run-secret/rerun", None, "secret-agent",
     "open-agent"),
    ("agent delete", "DELETE", "/api/agents/builtin/{n}", None, "secret-agent", "open-agent"),
]


def shape(text, name):
    """A response body with the name asked about taken out, so two names compare equal."""
    return text.replace(name, "<name>")


async def call(cx, B, hd, meth, path, kind, name):
    return await cx.request(meth, B + path.replace("{n}", name), headers=hd,
                            json=body_for(kind, name))


async def checks(B, ids):
    ROOT, ANN, KIM = H(AUTH), H(ids["ann_key"]), H(ids["kim_key"])
    async with httpx.AsyncClient(timeout=30) as cx:
        print("== Kim, outside the private project, gets the answer for a name that is not there ==")
        for label, meth, path, kind, hidden, _open in ROUTES:
            if "run-secret" in path:
                continue   # the rerun routes are checked below
            gone = await call(cx, B, KIM, meth, path, kind, "nobody-here")
            r = await call(cx, B, KIM, meth, path, kind, hidden)
            ck(f"{label}: as unknown as a name that does not exist",
               gone.status_code == 404 and r.status_code == 404
               and shape(r.text, hidden) == shape(gone.text, "nobody-here"),
               f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")

        print("== a rerun ==")
        rerun = "/api/agents/builtin/{n}/runs/{run}/rerun"
        def url(n, run):
            return B + rerun.replace("{n}", n).replace("{run}", run)
        r = await cx.post(url("secret-agent", "run-secret"), headers=KIM)
        gone = await cx.post(url("nobody-here", "run-secret"), headers=KIM)
        ck("a hidden agent: as unknown as a name that does not exist",
           r.status_code == gone.status_code == 404
           and shape(r.text, "secret-agent") == shape(gone.text, "nobody-here"),
           f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")
        r = await cx.post(url("shared-agent", "run-shared-secret"), headers=KIM)
        gone = await cx.post(url("shared-agent", "run-nobody-here"), headers=KIM)
        ck("a hidden run of an agent Kim can see: as unknown as a run that does not exist",
           r.status_code == gone.status_code == 404
           and shape(r.text, "run-shared-secret") == shape(gone.text, "run-nobody-here"),
           f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")
        r = await cx.post(url("shared-agent", "run-shared-open"), headers=KIM)
        ck("a run of the open project, on the same agent, still reruns", r.status_code == 201,
           f"{r.status_code} {r.text[:120]}")

        r = await cx.post(url("open-agent", "run-noproject"), headers=KIM)
        ck("a run that belongs to no project, on an open agent, still reruns", r.status_code == 201,
           f"{r.status_code} {r.text[:120]}")
        # the name is checked before the body: a body that is wrong too must not tell a hidden
        # trigger from an unknown one
        bad = {"name": "something-else", "sources": ["open-src"], "condition": COND}
        r = await cx.put(f"{B}/api/triggers/secret-trig", headers=KIM, json=bad)
        gone = await cx.put(f"{B}/api/triggers/nobody-here", headers=KIM, json=bad)
        ck("a hidden trigger with a wrong body: the unknown-name answer, not the body's",
           r.status_code == gone.status_code == 404
           and shape(r.text, "secret-trig") == shape(gone.text, "nobody-here"),
           f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")

        print("== a run is hidden only when every project it belongs to is hidden ==")
        def worth(run):
            return cx.put(f"{B}/api/findings/{run}/worth", headers=KIM, json={"worth": True})
        r, gone = await worth("run-secret-free"), await worth("run-nobody-here")
        ck("a run with no project of its own, of a hidden agent: as unknown as a run that is not there",
           r.status_code == gone.status_code == 404
           and shape(r.text, "run-secret-free") == shape(gone.text, "run-nobody-here"),
           f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")
        r = await worth("run-both")
        ck("a run in a hidden project and an open one is still Kim's to read", r.status_code != 404,
           f"{r.status_code} {r.text[:100]}")
        r = await worth("run-shared-secret")
        ck("a run only in the hidden project is not", r.status_code == 404, f"{r.status_code} {r.text[:100]}")

        print("== a rule proposal ==")
        for decision in ("reject", "accept"):
            r = await cx.post(f"{B}/api/rule-proposals/{ids['rule_secret']}/{decision}",
                              headers=KIM, json={})
            gone = await cx.post(f"{B}/api/rule-proposals/rp_nobody/{decision}", headers=KIM, json={})
            ck(f"{decision} a hidden proposal: as unknown as one that does not exist",
               r.status_code == gone.status_code == 404
               and shape(r.text, ids["rule_secret"]) == shape(gone.text, "rp_nobody"),
               f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")

        print("== a hidden name in the body answers as an unknown name (TR-944) ==")
        team = ids["team"]
        sub = {"url": "https://example.org/hook"}
        # (label, method, path, body with {n} for the name asked about, the hidden name)
        cases = [
            ("add a source to a project", "POST", f"/api/projects/{team}/sources",
             {"name": "{n}"}, "secret-src"),
            ("a new trigger that reads it", "POST", "/api/triggers",
             {"name": "new-trig", "sources": ["{n}"], "condition": COND}, "secret-src"),
            ("an open trigger changed to read it", "PUT", "/api/triggers/open-trig",
             {"name": "open-trig", "sources": ["{n}"], "condition": COND}, "secret-src"),
            ("a new agent woken by it", "POST", "/api/agents/builtin",
             {"name": "new-agent", "trigger": "{n}", "prompt": "Look."}, "secret-trig"),
            ("a new agent using its MCP server", "POST", "/api/agents/builtin",
             {"name": "new-agent", "trigger": "open-trig", "prompt": "Look.",
              "mcp_servers": ["{n}"]}, "secret-mcp"),
            ("a new agent that hands off to it", "POST", "/api/agents/builtin",
             {"name": "new-agent", "trigger": "open-trig", "prompt": "Look.",
              "handoffs": [{"verdict": "escalate", "agent": "{n}"}]}, "secret-agent"),
            ("an open agent moved to its trigger", "PUT", "/api/agents/builtin/open-agent",
             {"name": "open-agent", "trigger": "{n}", "prompt": "Look."}, "secret-trig"),
            ("an open agent given its MCP server", "PUT", "/api/agents/builtin/open-agent",
             {"name": "open-agent", "trigger": "open-trig", "prompt": "Look.",
              "mcp_servers": ["{n}"]}, "secret-mcp"),
            ("an open agent handing off to it", "PUT", "/api/agents/builtin/open-agent",
             {"name": "open-agent", "trigger": "open-trig", "prompt": "Look.",
              "handoffs": [{"verdict": "escalate", "agent": "{n}"}]}, "secret-agent"),
            ("a project list that adds a source", "PUT", f"/api/projects/{team}",
             {"objects": [{"kind": "source", "name": "{n}"}]}, "secret-src"),
            ("a project list that adds a trigger", "PUT", f"/api/projects/{team}",
             {"objects": [{"kind": "trigger", "name": "{n}"}]}, "secret-trig"),
            ("a project list that adds an agent", "PUT", f"/api/projects/{team}",
             {"objects": [{"kind": "agent", "name": "{n}"}]}, "secret-agent"),
            ("a project list that adds an MCP server", "PUT", f"/api/projects/{team}",
             {"objects": [{"kind": "mcp_server", "name": "{n}"}]}, "secret-mcp"),
            ("a subscription to the trigger", "POST", "/subscribe",
             {"trigger": "{n}", **sub}, "secret-trig"),
            ("a new project that takes a source", "POST", "/api/projects",
             {"template": "custom", "name": "made-from-{n}", "objects": [{"kind": "source", "name": "{n}"}]},
             "secret-src"),
            ("a new project that takes a trigger", "POST", "/api/projects",
             {"template": "custom", "name": "made-from-{n}", "objects": [{"kind": "trigger", "name": "{n}"}]},
             "secret-trig"),
            ("a new project that takes an agent", "POST", "/api/projects",
             {"template": "custom", "name": "made-from-{n}", "objects": [{"kind": "agent", "name": "{n}"}]},
             "secret-agent"),
            ("a new project that takes an MCP server", "POST", "/api/projects",
             {"template": "custom", "name": "made-from-{n}", "objects": [{"kind": "mcp_server", "name": "{n}"}]},
             "secret-mcp"),
        ]
        def fill(v, n):
            if isinstance(v, str):
                return v.replace("{n}", n)
            if isinstance(v, list):
                return [fill(x, n) for x in v]
            if isinstance(v, dict):
                return {k: fill(x, n) for k, x in v.items()}
            return v
        for label, meth, path, body, hidden in cases:
            gone = await cx.request(meth, B + path, headers=KIM, json=fill(body, "nobody-here"))
            r = await cx.request(meth, B + path, headers=KIM, json=fill(body, hidden))
            ck(f"{label}: as unknown as a name that does not exist",
               r.status_code == gone.status_code and r.status_code in (400, 404)
               and shape(r.text, hidden) == shape(gone.text, "nobody-here"),
               f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")
        made = [p["name"] for p in (await cx.get(f"{B}/api/projects", headers=ROOT)).json()["projects"]]
        ck("no project was made from a hidden object", not any(n.startswith("made-from-") for n in made),
           str(made)[:200])
        proj = (await cx.get(f"{B}/api/projects/{team}", headers=ROOT)).json()
        ck("nothing hidden joined the open project",
           not any(o["name"].startswith("secret-") for o in proj.get("objects", [])),
           str(proj.get("objects"))[:200])
        trigs = {t["name"]: t for t in (await cx.get(f"{B}/api/triggers", headers=ROOT)).json()}
        ck("no trigger was created and the open trigger still reads its own source",
           "new-trig" not in trigs and trigs["open-trig"]["sources"] == ["open-src"], str(trigs)[:200])
        agents = (await cx.get(f"{B}/api/agents/builtin", headers=ROOT)).json()
        agents = {a["name"]: a for a in (agents if isinstance(agents, list) else agents["agents"])}
        ck("no agent was created and the open agent is unchanged",
           "new-agent" not in agents and agents["open-agent"]["trigger"] == "open-trig"
           and not agents["open-agent"].get("mcp_servers") and not agents["open-agent"].get("handoffs"),
           str(agents.get("open-agent"))[:200])
        subs = (await cx.get(f"{B}/api/subscriptions", headers=ROOT)).json()
        ck("no subscription was added to the hidden trigger",
           not any(x["trigger"] == "secret-trig" for x in subs), str(subs)[:200])
        r = await cx.post(f"{B}/subscribe", headers=KIM, json={"trigger": "open-trig", **sub})
        ck("a subscription to an open trigger still works", r.status_code == 200, f"{r.status_code} {r.text[:100]}")

        print("== the guided setup does not find what Kim cannot see ==")
        def plan_of(**part):
            return {"goal": "Keep an eye on things.", "name": "Guided", "who": "tares", **part}
        async def setup_check(plan):
            r = await cx.post(f"{B}/api/setup/check", headers=KIM, json={"plan": plan})
            j = r.json()
            return r, j, [x["message"] for x in j.get("problems", [])]
        def missing(msgs, word):
            return [m for m in msgs if word in m]
        for label, key, kind, hidden, openn, word in (
                ("a source", "watches", "source", "secret-src", "open-src", "no source called"),
                ("an MCP server", "tools", "mcp_server", "secret-mcp", "open-mcp", "no MCP server called")):
            def one(n):
                item = {"key": "x1", "existing": True, "name": n}
                if key == "tools":
                    item.update(enabled=True, url="")
                return plan_of(**{key: [item]})
            _r, _j, gone = await setup_check(one("nobody-here"))
            _r, _j, hid = await setup_check(one(hidden))
            _r, _j, opn = await setup_check(one(openn))
            ck(f"check, {label} named as existing: a hidden one is as missing as an unknown one",
               len(missing(gone, word)) == 1 and [shape(m, hidden) for m in missing(hid, word)]
               == [shape(m, "nobody-here") for m in missing(gone, word)], f"{hid} / {gone}")
            ck(f"check, {label} named as existing: an open one is found", not missing(opn, word), str(opn))
        _r, j, _m = await setup_check(plan_of(agents=[
            {"key": "a1", "existing": True, "name": "secret-agent", "trigger": "k1"}]))
        ck("check, an agent named as existing: a hidden one is not taken, nor its instructions copied",
           j["plan"]["agents"][0]["existing"] is False and j["plan"]["agents"][0]["prompt"] == ""
           and "Look." not in str(j["plan"]["agents"]), str(j["plan"]["agents"])[:200])
        _r, j, _m = await setup_check(plan_of(agents=[
            {"key": "a1", "existing": True, "name": "open-agent", "trigger": "k1"}]))
        ck("check, an agent named as existing: an open one is taken as it is",
           j["plan"]["agents"][0]["existing"] is True and j["plan"]["agents"][0]["prompt"] == "Look.",
           str(j["plan"]["agents"])[:200])
        _r, j, _m = await setup_check(plan_of(skills=[
            {"key": "s1", "existing": True, "name": "secret-skill"}]))
        ck("check, a skill named as existing: a hidden one is not taken, nor its body copied",
           j["plan"]["skills"][0]["existing"] is False and "Hidden" not in str(j["plan"]["skills"]),
           str(j["plan"]["skills"])[:200])
        _r, j, _m = await setup_check(plan_of(skills=[
            {"key": "s1", "existing": True, "name": "open-skill"}]))
        ck("check, a skill named as existing: an open one is taken as it is",
           j["plan"]["skills"][0]["existing"] is True and j["plan"]["skills"][0]["body"] == "Open steps.",
           str(j["plan"]["skills"])[:200])

        def full_plan(src):
            return plan_of(
                watches=[{"key": "w1", "existing": True, "name": src}],
                wakes=[{"key": "k1", "name": "look", "sources": [src], "condition": COND}],
                agents=[{"key": "a1", "name": "looker", "trigger": "look", "prompt": "Look."}])
        r = await cx.post(f"{B}/api/setup/apply", headers=KIM, json={"plan": full_plan("secret-src")})
        gone = await cx.post(f"{B}/api/setup/apply", headers=KIM, json={"plan": full_plan("nobody-here")})
        ck("apply, a hidden source: refused as an unknown one is, and nothing is made",
           r.status_code == gone.status_code == 422
           and shape(r.text, "secret-src") == shape(gone.text, "nobody-here"),
           f"{r.status_code} {r.text[:160]} / {gone.status_code} {gone.text[:160]}")
        made = [p["name"] for p in (await cx.get(f"{B}/api/projects", headers=ROOT)).json()["projects"]]
        ck("apply made no project", "Guided" not in made, str(made)[:200])
        r = await cx.post(f"{B}/api/setup/apply", headers=KIM, json={"plan": full_plan("open-src")})
        ck("apply, an open source: sets the project up", r.status_code in (200, 201), f"{r.status_code} {r.text[:160]}")

        print("== nothing was changed ==")
        def names(r, k):
            j = r.json()
            return {x["name"] for x in (j if isinstance(j, list) else j[k])}
        ck("the sources are all still there",
           {"secret-src", "open-src", "shared-src"} <= names(await cx.get(f"{B}/api/sources", headers=ROOT), "sources"))
        ck("the triggers are all still there",
           {"secret-trig", "open-trig", "shared-trig"} <= names(await cx.get(f"{B}/api/triggers", headers=ROOT), "triggers"))
        ck("the MCP servers are all still there",
           {"secret-mcp", "open-mcp"} <= names(await cx.get(f"{B}/api/mcp-servers", headers=ROOT), "servers"))
        ck("the agents are all still there",
           {"secret-agent", "open-agent", "shared-agent"} <= names(await cx.get(f"{B}/api/agents/builtin", headers=ROOT), "agents"))
        runs = (await cx.get(f"{B}/api/agents/builtin/secret-agent/runs", headers=ROOT)).json()
        rows = runs if isinstance(runs, list) else runs.get("runs", [])
        ck("the private agent has the two runs it had", len(rows) == 2, str(runs)[:200])
        sec = (await cx.get(f"{B}/api/rule-proposals", headers=ROOT)).json()["proposals"]
        ck("the hidden proposal is still open", any(p["id"] == ids["rule_secret"] and p["state"] == "open"
                                                     for p in sec), str(sec)[:200])

        print("== what is shared with a project Kim can see stays hers ==")
        for label, meth, path, kind, _hidden, _open in ROUTES:
            if meth == "DELETE" or "run-secret" in path or "update" in label:
                continue
            shared = {"source": "shared-src", "trigger": "shared-trig", "agent": "shared-agent"}
            word = label.split()[0]
            if word not in shared:
                continue
            r = await call(cx, B, KIM, meth, path, kind, shared[word])
            ck(f"{label} on {shared[word]}, shared with the open project: reachable", r.status_code != 404,
               f"{r.status_code} {r.text[:100]}")

        print("== Ann and the system credential keep access ==")
        for who, hd in (("Ann", ANN), ("the system credential", ROOT)):
            for label, meth, path, kind, hidden, _open in ROUTES:
                if meth == "DELETE" or "run-secret" in path:
                    continue
                r = await call(cx, B, hd, meth, path, kind, hidden)
                ck(f"{who}: {label}", r.status_code != 404, f"{r.status_code} {r.text[:100]}")
            r = await cx.post(url("secret-agent", "run-secret"), headers=hd)
            ck(f"{who}: agent rerun", r.status_code == 201, f"{r.status_code} {r.text[:100]}")
        for who, hd in (("Ann", ANN), ("the system credential", ROOT)):
            for label, meth, path, body, hidden in cases:
                if label not in ("add a source to a project", "a new agent woken by it",
                                 "a subscription to the trigger"):
                    continue   # the rest would rewire the open objects the deletes below use
                r = await cx.request(meth, B + path, headers=hd, json=fill(body, hidden))
                ck(f"{who}: {label} reaches the object", r.status_code != 404,
                   f"{r.status_code} {r.text[:100]}")
        r = await cx.post(f"{B}/api/rule-proposals/{ids['rule_secret']}/reject", headers=ANN, json={})
        ck("Ann: reject the hidden proposal", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
        for label, meth, path, kind, hidden, _open in ROUTES:
            if meth != "DELETE":
                continue
            r = await call(cx, B, ANN, meth, path, kind, hidden)
            ck(f"Ann: {label}", r.status_code == 200, f"{r.status_code} {r.text[:100]}")


async def main():
    ids = {}
    port = free_port()
    B = f"http://127.0.0.1:{port}"
    proc = spawn(port)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        await setup(B, ids)
    finally:
        stop(proc)
    seed(ids)
    proc = spawn(port)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (second start)", False); return
        await checks(B, ids)
    finally:
        stop(proc)
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
