"""End-to-end test for the shared code context template (TR-171).

Run: PYTHONPATH=. .venv/bin/python tests/test_shared_code_context.py   (no network: a fake GitHub
is served through httpx's MockTransport for every AsyncClient the daemon opens)

Covers: describe() output, validate() rules, deterministic plan and names, create through
POST /api/projects producing exactly the planned objects (sources with credential, trigger,
MCP server with headers + credential ref, enabled agent with max_rounds and the rendered prompt),
update adding and removing a repo, summary shape, commit payloads carrying the changed files with
truncation, and the bootstrap hook scheduling runs.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = "/tmp/tares-sharedctx-test.duckdb"
CATALOG = "/tmp/tares-sharedctx-test.catalog.yaml"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = CATALOG
os.environ.pop("ANTHROPIC_API_KEY", None)   # bootstrap runs must fail fast on "no key", not call out

import httpx

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


# ── fake GitHub ──────────────────────────────────────────────────────────────
BIG_PATCH = "+" + ("x" * 5000)
STATE = {"calls": [], "remaining": "4000"}
COMMIT_LIST = {
    "acme/app": [{"sha": "bbb2222bbb", "html_url": "u", "author": {"login": "alice"},
                  "commit": {"message": "add refunds endpoint\n\nbody",
                             "author": {"name": "A", "date": "2026-08-18T10:01:00Z"}}},
                 {"sha": "aaa1111aaa", "html_url": "u", "author": {"login": "bob"},
                  "commit": {"message": "first", "author": {"name": "B", "date": "2026-08-18T10:00:00Z"}}}],
    "acme/lib": [{"sha": "ccc3333ccc", "html_url": "u", "author": {"login": "alice"},
                  "commit": {"message": "lib change", "author": {"name": "A", "date": "2026-08-18T10:02:00Z"}}}],
}
COMMIT_DETAIL = {
    "bbb2222bbb": {"files": [{"filename": f"src/f{i}.py", "status": "modified", "additions": 1,
                              "deletions": 0, "patch": BIG_PATCH if i == 0 else "+ok"}
                             for i in range(25)]},
    "aaa1111aaa": {"files": [{"filename": "README.md", "status": "added", "additions": 3,
                              "deletions": 0, "patch": "+hello"}]},
    "ccc3333ccc": {"files": []},
}


def fake_github(request: httpx.Request) -> httpx.Response:
    auth = request.headers.get("authorization", "")
    token = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""
    path = request.url.path
    STATE["calls"].append((path, token))
    if request.url.host != "api.github.test":
        return httpx.Response(599, text="unexpected host " + request.url.host)
    if token != "tok-1":
        return httpx.Response(401, json={"message": "Bad credentials"})
    hdr = {"x-ratelimit-remaining": STATE["remaining"]}
    if path == "/user":
        return httpx.Response(200, json={"login": "alice", "name": "Alice"},
                              headers={"x-oauth-scopes": "repo"})
    if path == "/user/repos":
        return httpx.Response(200, json=[] if request.url.params.get("page", "1") != "1" else [
            {"full_name": "acme/app", "default_branch": "main", "private": True, "pushed_at": "2026-08-18T00:00:00Z"},
            {"full_name": "acme/lib", "default_branch": "main", "private": False, "pushed_at": "2026-08-18T00:00:00Z"},
            {"full_name": "acme/context", "default_branch": "main", "private": True, "pushed_at": "2026-08-18T00:00:00Z"}])
    for repo in ("acme/app", "acme/lib", "acme/context"):
        if path == f"/repos/{repo}":
            return httpx.Response(200, json={"default_branch": "main"}, headers=hdr)
        if path == f"/repos/{repo}/commits":
            return httpx.Response(200, json=COMMIT_LIST.get(repo, []), headers=hdr)
        if path.startswith(f"/repos/{repo}/commits/"):
            sha = path.rsplit("/", 1)[1]
            if sha in COMMIT_DETAIL:
                return httpx.Response(200, json=COMMIT_DETAIL[sha], headers=hdr)
    return httpx.Response(404, json={"message": "Not Found"})


_RealClient = httpx.AsyncClient


class PatchedClient(_RealClient):
    def __init__(self, *a, **kw):
        if "transport" not in kw:
            kw["transport"] = httpx.MockTransport(fake_github)
        super().__init__(*a, **kw)


httpx.AsyncClient = PatchedClient
API = "http://api.github.test"


async def main():
    for p in (DB, DB + ".wal", CATALOG):
        if os.path.exists(p):
            os.remove(p)

    from tares.projects.registry import get_template
    from tares.projects.base import ProjectError
    template = get_template("shared_code_context")

    print("== describe ==")
    d = template.describe()
    check("key/title", d["key"] == "shared_code_context" and d["title"] == "Shared code context")
    for name in ("credential", "source_repos", "context_repo", "context_branch", "context_path",
                 "trigger", "write_mode", "model", "max_rounds"):
        check(f"param {name} described with type+label+help",
              name in d["params"] and all(k in d["params"][name] for k in ("type", "label", "help")),
              json.dumps(d["params"].get(name)))
    check("required flags", d["params"]["credential"].get("required") and
          d["params"]["source_repos"].get("required") and d["params"]["context_repo"].get("required"))
    check("defaults", d["params"]["context_path"]["default"] == "" and
          d["params"]["layout"]["default"] == "existing" and
          d["params"]["max_rounds"]["default"] == 12 and d["params"]["write_mode"]["default"] == "pull_request")
    check("trigger offers every commit and every merged PR",
          [o["value"] for o in d["params"]["trigger"]["options"]] == ["every_commit", "every_merged_pr"])

    print("== validate ==")
    base = {"credential": "gh", "source_repos": [{"repo": "acme/app"}, "https://github.com/acme/lib.git"],
            "context_repo": "acme/context"}
    p = template.validate(base)
    check("normalizes repos and fills defaults",
          [r["repo"] for r in p["source_repos"]] == ["acme/app", "acme/lib"] and p["context_branch"] == "main"
          and p["context_path"] == "" and p["layout"] == "existing" and p["max_rounds"] == 12, json.dumps(p))
    check("context_path gets a trailing slash",
          template.validate({**base, "context_path": "/docs/ctx"})["context_path"] == "docs/ctx/")

    def bad(label, params):
        try:
            template.validate(params)
            check(label, False, "no error")
        except ProjectError as e:
            check(label, True, str(e))
    bad("missing credential", {**base, "credential": ""})
    bad("no repos", {**base, "source_repos": []})
    bad("bad repo name", {**base, "source_repos": ["not-a-repo"]})
    bad("duplicate repo", {**base, "source_repos": ["acme/app", "acme/app"]})
    bad("context repo among sources", {**base, "context_repo": "acme/app"})
    bad("too many repos", {**base, "source_repos": [f"acme/r{i}" for i in range(51)]})
    bad("unknown trigger", {**base, "trigger": "daily"})
    bad("unknown write mode", {**base, "write_mode": "push"})
    bad("max_rounds out of range", {**base, "max_rounds": 30})

    print("== plan ==")
    plan1 = template.plan(p)
    plan2 = template.plan(template.validate(base))
    check("plan is deterministic",
          [(o.kind, o.key, o.spec) for o in plan1] == [(o.kind, o.key, o.spec) for o in plan2])
    kinds = [o.kind for o in plan1]
    check("plan has 2 sources, trigger, mcp, agent",
          kinds == ["source", "source", "trigger", "mcp_server", "agent"], str(kinds))
    names = {o.key: o.name for o in plan1}
    check("names prefixed ctx_<slug>_",
          names["trigger"] == "ctx_acme_context_changes" and names["agent"] == "ctx_acme_context_maintainer"
          and names["source:acme/app"] == "ctx_acme_context_acme_app", json.dumps(names))
    src = next(o for o in plan1 if o.key == "source:acme/app").spec
    check("source uses the credential, keyed by repo, poll 60s",
          src["config"]["credential"] == "gh" and src["poll"] == "60s"
          and src["config"]["labels"][0] == {"name": "repo", "field": "repo", "primary": True}, json.dumps(src))
    trig = next(o for o in plan1 if o.kind == "trigger").spec
    check("trigger counts commits per repo with 5m cooldown and 30m context",
          trig["condition"] == {"aggregate": "count", "predicate": "> 0", "window": "5m", "group_by": ["key_value"]}
          and trig["cooldown"] == "5m" and "attach_view" not in trig["emit"]
          and trig["emit"]["context_window"] == "30m", json.dumps(trig))
    check("trigger reads every source repo, keyed by repo",
          trig["sources"] == ["ctx_acme_context_acme_app", "ctx_acme_context_acme_lib"]
          and trig["key_field"] == "repo" and "view" not in trig, json.dumps(trig))
    mcp = next(o for o in plan1 if o.kind == "mcp_server").spec
    check("mcp server: GitHub hosted, credential ref, toolsets header",
          mcp["url"] == "https://api.githubcopilot.com/mcp/" and mcp["auth_value"] == "credential:github/gh"
          and mcp["headers"] == {"X-MCP-Toolsets": "repos,pull_requests"}, json.dumps(mcp))
    agent = next(o for o in plan1 if o.kind == "agent").spec
    check("agent enabled, on the trigger, opted into the mcp server, max_rounds 12",
          agent["enabled"] is True and agent["trigger"] == names["trigger"]
          and agent["mcp_servers"] == [names["mcp"]] and agent["max_rounds"] == 12, json.dumps(agent)[:300])
    prompt = agent["prompt"]
    check("prompt renders params",
          "`acme/context`" in prompt and "`acme/app`" in prompt and "`acme/lib`" in prompt
          and "its own pages under `/`" in prompt and "github__create_pull_request" in prompt
          and "github__get_commit" in prompt and "tares/context-<repo-name>" in prompt)
    per_repo = template.render_prompt(template.validate({**base, "context_path": "context", "layout": "per_repo"}))
    check("per_repo layout keeps the page template",
          "context/<repo-name>.md" in per_repo and "Page template" in per_repo)
    check("existing layout has no per-repo template", "Page template" not in prompt)
    check("token, every commit: the trigger counts commits only (sources also report PRs)",
          trig["filters"] == [{"field": "event_type", "op": "eq", "value": "commit"}], json.dumps(trig))
    prs = template.plan(template.validate({**base, "trigger": "every_merged_pr"}))
    ptrig = next(o for o in prs if o.kind == "trigger").spec
    check("token, every merged PR: per-repo sources, trigger on pull_request + merged",
          [o.kind for o in prs].count("source") == 2
          and ptrig["filters"] == [{"field": "event_type", "op": "eq", "value": "pull_request"},
                                   {"field": "action", "op": "eq", "value": "merged"}], json.dumps(ptrig))
    pprompt = next(o for o in prs if o.kind == "agent").spec["prompt"]
    check("merged PR prompt reads the PR and its files",
          "github__get_pull_request_files" in pprompt and "merged pull requests" in pprompt)

    print("== plan with a GitHub App ==")
    app_params = {**template.validate(base), "app_source": "github"}
    app_plan = template.plan(app_params)
    akinds = [o.kind for o in app_plan]
    atrig = next(o for o in app_plan if o.kind == "trigger").spec
    check("App: no source per repo; trigger on the App's one source",
          "source" not in akinds and atrig["sources"] == ["github"], str(akinds))
    check("App, every commit: pushes to the default branch of the watched repos",
          atrig["filters"] == [{"field": "repo", "op": "in", "value": ["acme/app", "acme/lib"]},
                               {"field": "event_type", "op": "eq", "value": "push"},
                               {"field": "on_default_branch", "op": "eq", "value": "true"}],
          json.dumps(atrig["filters"]))
    app_prs = template.plan({**app_params, "trigger": "every_merged_pr"})
    atrig2 = next(o for o in app_prs if o.kind == "trigger").spec
    check("App, every merged PR: repo in + pull_request + merged",
          atrig2["filters"][0]["op"] == "in" and atrig2["filters"][1:] == [
              {"field": "event_type", "op": "eq", "value": "pull_request"},
              {"field": "action", "op": "eq", "value": "merged"}], json.dumps(atrig2["filters"]))
    aprompt = next(o for o in app_plan if o.kind == "agent").spec["prompt"]
    check("App push prompt reads each push's commits", "push to a repository" in aprompt)
    check("no em dashes in prompt", "—" not in prompt)
    direct = template.render_prompt(template.validate({**base, "write_mode": "commit_to_branch"}))
    check("commit_to_branch prompt has no PR step",
          "Do not open a pull request" in direct and "github__create_pull_request" not in direct)
    check("model only when set",
          "model" not in agent and "model" in next(o for o in template.plan(template.validate(
              {**base, "model": "claude-opus-4-8"})) if o.kind == "agent").spec)

    print("== create through the API ==")
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with _RealClient(transport=transport, base_url="http://test") as cx:
            r = await cx.get("/api/projects/templates")
            check("template listed", any(x["key"] == "shared_code_context" for x in r.json()["templates"]), r.text[:200])

            r = await cx.post("/api/projects", json={"template": "shared_code_context", "params": base})
            check("create without credential -> 400", r.status_code == 400, r.text)

            r = await cx.post("/api/integrations/github", json={"name": "gh", "token": "tok-1", "api_url": API})
            check("credential created", r.status_code == 201, r.text)

            r = await cx.post("/api/projects", json={"template": "shared_code_context",
                                                     "name": "acme context", "params": base})
            check("create -> 201", r.status_code == 201, r.text)
            inst = r.json()
            uid = inst["id"]
            check("instance active with 5 objects, none missing",
                  inst["status"] == "active" and len(inst["objects"]) == 5
                  and not any(o["missing"] for o in inst["objects"]), json.dumps(inst)[:400])

            r = await cx.get("/api/sources")
            sources = {s["name"]: s for s in r.json()}
            check("sources exist and are owned",
                  {"ctx_acme_context_acme_app", "ctx_acme_context_acme_lib"} <= set(sources)
                  and sources["ctx_acme_context_acme_app"]["owned_by"] == uid, str(list(sources)))
            r = await cx.get("/api/triggers")
            trigs = {t["name"]: t for t in r.json()}
            check("trigger exists", "ctx_acme_context_changes" in trigs, str(list(trigs)))
            r = await cx.get("/api/mcp-servers")
            servers = {m["name"]: m for m in r.json()["servers"]}
            m = servers.get("ctx_acme_context_github") or {}
            check("mcp server registered with credential and headers",
                  m.get("auth_credential") == "gh" and m.get("headers") == {"X-MCP-Toolsets": "repos,pull_requests"}
                  and m.get("owned_by") == uid, json.dumps(m))
            r = await cx.get("/api/agents/builtin")
            agents = {a["name"]: a for a in r.json()["agents"]}
            a = agents.get("ctx_acme_context_maintainer") or {}
            check("agent enabled with mcp server and max_rounds",
                  a.get("enabled") is True and a.get("mcp_servers") == ["ctx_acme_context_github"]
                  and a.get("max_rounds") == 12 and a.get("owned_by") == uid, json.dumps(a)[:300])

            # let the sources poll once (poll 60s; the runtime polls right after start)
            for _ in range(60):
                await asyncio.sleep(0.25)
                stats = {s["source"]: s for s in app.state.store.event_stats()}
                if stats.get("ctx_acme_context_acme_app", {}).get("events", 0) >= 2 \
                        and stats.get("ctx_acme_context_acme_lib", {}).get("events", 0) >= 1:
                    break
            check("commits ingested from both repos",
                  stats.get("ctx_acme_context_acme_app", {}).get("events") == 2
                  and stats.get("ctx_acme_context_acme_lib", {}).get("events") == 1, json.dumps(stats, default=str))
            check("connector used the credential's token for commits",
                  any(p == "/repos/acme/app/commits" and t == "tok-1" for p, t in STATE["calls"]))
            check("connector fetched files per commit",
                  any(p == "/repos/acme/app/commits/bbb2222bbb" for p, _ in STATE["calls"]))

            r = await cx.post("/read", json={"project": uid, "selector": {"repo": "acme/app"},
                                             "window": "24h", "include_payload": True})
            rows = r.json().get("rows") or r.json().get("events") or []
            payloads = {x.get("raw", {}).get("sha"): x.get("raw", {}) for x in rows if isinstance(x, dict)}
            big = payloads.get("bbb2222bbb") or {}
            small = payloads.get("aaa1111aaa") or {}
            check("payload carries files, capped at 20 with truncated flag",
                  len(big.get("files") or []) == 20 and big.get("files_truncated") is True
                  and len(big["files"][0]["patch"]) == 4000, json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in big.items() if k in ("files", "files_truncated")}))
            check("small commit keeps its one file, not truncated",
                  len(small.get("files") or []) == 1 and small.get("files_truncated") is False, json.dumps(small.get("files")))

            print("== summary ==")
            r = await cx.get(f"/api/projects/{uid}/summary")
            s = r.json()
            panel = (s.get("panels") or [{}])[0]
            check("summary shape", r.status_code == 200 and panel.get("title") == "Context repo"
                  and any(row["value"] == "acme/context" for row in panel.get("rows", []))
                  and "runs" in s and "triggers" in s
                  and (s.get("cards") or [{}])[0].get("label") == "pull requests", r.text[:400])

            print("== update: add and remove a repo ==")
            r = await cx.put(f"/api/projects/{uid}", json={"params": {**base, "source_repos": ["acme/lib", "acme/context2"]}})
            check("update -> 200", r.status_code == 200, r.text[:300])
            rep = r.json().get("report", {})
            check("report: app removed, context2 created, others updated",
                  "source:ctx_acme_context_acme_app" in rep.get("deleted", [])
                  and "source:ctx_acme_context_acme_context2" in rep.get("created", []), json.dumps(rep))
            r = await cx.get("/api/sources")
            names_now = {x["name"] for x in r.json()}
            check("sources reflect the new list",
                  "ctx_acme_context_acme_app" not in names_now and "ctx_acme_context_acme_context2" in names_now)
            r = await cx.get("/api/triggers")
            v = next(x for x in r.json() if x["name"] == "ctx_acme_context_changes")
            check("trigger sources follow", set(v["sources"]) == {"ctx_acme_context_acme_lib", "ctx_acme_context_acme_context2"}, json.dumps(v))

            print("== bootstrap hook ==")
            from tares.builtin_agents import AgentRunner
            runner = app.state.dispatcher.agents if hasattr(app.state, "dispatcher") else None
            check("run_now and bootstrap exist on the runner",
                  hasattr(AgentRunner, "run_now") and hasattr(AgentRunner, "bootstrap"))
            runtime = app.state.runtime
            runner = runtime.dispatcher.agents
            before = len(app.state.store.list_agent_runs("ctx_acme_context_maintainer", limit=50))
            runner.bootstrap("ctx_acme_context_maintainer", "ctx_acme_context_changes",
                             ["acme/lib", "acme/nothing"], window="7d", delay_s=0)
            for _ in range(40):
                await asyncio.sleep(0.25)
                runs = app.state.store.list_agent_runs("ctx_acme_context_maintainer", limit=50)
                if len(runs) > before and all(r["status"] != "running" for r in runs):
                    break
            check("bootstrap ran the agent once for the repo with commits (skipped the empty one)",
                  len(runs) == before + 1 and runs[0]["key"] == "acme/lib", json.dumps(runs, default=str)[:300])
            check("bootstrap run failed fast on missing key (no network call)",
                  runs and runs[0]["status"] == "failed" and "key" in (runs[0].get("error") or ""), json.dumps(runs[0], default=str)[:300] if runs else "")
            log = s.get("log") or []
            check("create logged and no bootstrap failure",
                  any(x["action"] == "created" for x in log) and not any(x["action"] == "bootstrap_failed" for x in log),
                  json.dumps(log)[:300])

            print("== delete ==")
            srcs = ",".join(o["name"] for o in (await cx.get(f"/api/projects/{uid}")).json()["objects"] if o["kind"] == "source")
            r = await cx.delete(f"/api/projects/{uid}?purge_events=true&delete_sources={srcs}")
            check("delete -> 200", r.status_code == 200, r.text)
            r = await cx.get("/api/sources")
            check("owned sources gone", not any(x["name"].startswith("ctx_acme_context_") for x in r.json()))
            r = await cx.get("/api/mcp-servers")
            check("mcp server gone", not any(m["name"] == "ctx_acme_context_github" for m in r.json()["servers"]))

    print(f"\n{PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
