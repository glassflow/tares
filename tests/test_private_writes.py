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
                                ("run-shared-open", "shared-agent", "shared-trig", team)):
        store.start_agent_run(rid, agent, trig, "d-" + rid, "service-a", "h", project=u)
        store.finish_agent_run(rid, "ok", finding="done")
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

        print("== a rule proposal ==")
        for decision in ("reject", "accept"):
            r = await cx.post(f"{B}/api/rule-proposals/{ids['rule_secret']}/{decision}",
                              headers=KIM, json={})
            gone = await cx.post(f"{B}/api/rule-proposals/rp_nobody/{decision}", headers=KIM, json={})
            ck(f"{decision} a hidden proposal: as unknown as one that does not exist",
               r.status_code == gone.status_code == 404
               and shape(r.text, ids["rule_secret"]) == shape(gone.text, "rp_nobody"),
               f"{r.status_code} {r.text[:100]} / {gone.status_code} {gone.text[:100]}")

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
        ck("the private agent has the one run it had", len(rows) == 1, str(runs)[:200])
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
