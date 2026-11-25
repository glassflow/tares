"""The Codex runtime on the cell (tares/integrations/builtin/runtime_codex.py, TR-1592).

 a. the slot: `codex` is installed next to `claude_code`, which stays the default; setup does not offer it yet;
 b. the spec: the app-server on the station's socket with the account's extras off, the same FACTORY_* and
    TARES_* variables a Claude Code station gets (and CODEX_HOME), the thread's settings (model, approval policy,
    sandbox, identity, the guard's hook and the trust for it), the MCP server by name, the rules filled, the
    placeholders it uses and no other, and no secret; what Claude Code alone needs is not in it;
 c. it refuses what the Claude Code runtime refuses: a role that is not one, a blank station, a helper without
    its parent's role, a level that is not one, no rules;
 d. the route: with the factory's runtime set to codex, GET /api/factory/stations/{station}/launch answers with
    the Codex spec and names the runtime; set back, it answers with Claude Code's.

The crew's side (starting what the spec describes) is tests/test_crew_launch_codex.py.

Run: python tests/test_runtime_codex.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-runtime-codex-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
FAKE_MODEL_KEY = "sk-fake-runtime-codex-test"
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
    ANTHROPIC_API_KEY=FAKE_MODEL_KEY, TARES_HOME=str(SCRATCH / "tares-home"),
    TARES_DB=str(SCRATCH / "cell.duckdb"), TARES_CATALOG=str(SCRATCH / "none.yaml"),
    TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN="root-token-runtime-codex",
)
(SCRATCH / "home").mkdir()
ROOT_TOKEN = os.environ["TARES_AUTH_TOKEN"]

sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tares.integrations import registry, resolve  # noqa: E402
from tares.integrations.builtin.runtime_claude_code import ROLES, RuntimeClaudeCode  # noqa: E402
from tares.integrations.builtin.runtime_codex import GUARD_HOOK, LEAN_FLAGS, MCP_IDENTITY, RuntimeCodex  # noqa: E402
from tares.integrations.runtime import PLACEHOLDERS, fill_placeholders  # noqa: E402
from tares.screens import factory_setup  # noqa: E402

P = F = 0
NAME = re.compile(r"\{([A-Z_]+)\}")


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def raises_value(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except ValueError as e:
        return str(e)
    return None


ctx0 = {
    "crew": "shop", "orchestrator": "shop-orchestrator", "parent": "", "project": "", "project_id": "", "repo": "",
    "human": "Pat", "authorized_at": "2026-01-01", "autonomy": "", "crew_autonomy": "L3-review",
    "model": "gpt-5.6-luna", "effort": "low", "autocompact": "200000", "tares_url": "http://cell.test",
    "mcp_url": "http://cell.test/mcp", "cluster": "factory", "token_file": True,
    "rules": {"allow": ["{human} lets this station work in {where} and keep notes in {home}"],
              "soft_deny": ["Never push to a branch outside {wt_repos}", "  "]},
    "monitoring": None,
}
proj = {**ctx0, "project": "alpha", "project_id": "uc_alpha", "repo": "/work/alpha"}

# ---------------------------------------------------------------------------------------------
print("a. the slot")
registry.load(refresh=True)
ck("codex is installed for the runtime slot, next to claude_code",
   registry.get("runtime", "codex") is RuntimeCodex and [c.name for c in registry.available("runtime")] == ["claude_code", "codex"],
   [c.name for c in registry.available("runtime")])
ck("claude_code stays the default", resolve.DEFAULTS["runtime"] == "claude_code")
ck("it is named Codex and is not an ee integration", RuntimeCodex.title == "Codex" and RuntimeCodex.slot == "runtime" and not RuntimeCodex.ee)
ck("setup does not offer it yet", ("runtime", "codex") in factory_setup.NOT_OFFERED)

# ---------------------------------------------------------------------------------------------
print("b. the spec")
spec = RuntimeCodex().launch_spec("builder", "shop-build-api", proj)
ck("the command is the app-server on the station's socket, the account's extras off",
   spec.command == ["app-server", "--listen", "unix://{STATION_SOCKET}", "--disable", "apps", "--disable", "plugins",
                    "--disable", "remote_plugin"] and tuple(spec.command[3:]) == LEAN_FLAGS, spec.command)
claude_env = RuntimeClaudeCode().launch_spec("builder", "shop-build-api", proj).env
ck("the environment is a Claude Code station's, without --autocompact's and with CODEX_HOME",
   spec.env == {**{k: v for k, v in claude_env.items() if k != "FACTORY_AUTOCOMPACT"}, "CODEX_HOME": "{CODEX_HOME}"},
   sorted(set(spec.env) ^ set(claude_env)))
ck("the token is the crew's file, never a value", spec.env["TARES_AUTH_TOKEN"] == "" and spec.env["TARES_FACTORY_TOKEN_FILE"] == "{TOKEN_FILE}")
thread = spec.settings["thread"]
ck("the thread starts with the model, approvals on request and the workspace sandbox",
   thread["model"] == "gpt-5.6-luna" and thread["approvalPolicy"] == "on-request" and thread["sandbox"] == "workspace-write", thread)
ck("the effort is a turn's, next to the thread", spec.settings["effort"] == "low" and "effort" not in thread)
ck("the identity says who the station is and nothing of Claude Code's",
   thread["developerInstructions"] == "You are a tares-factory station: role builder, session name shop-build-api, "
   "project alpha, repo /work/alpha. Act only within your role.", thread["developerInstructions"])
hooks = thread["config"]["hooks"]
ck("the guard is registered for every tool call, with the thread's trust for it",
   list(hooks) == ["PreToolUse"] and hooks["PreToolUse"] == [{"matcher": "", "hooks": [
       {"type": "command", "command": GUARD_HOOK, "timeout": 5}]}] and thread["config"]["bypass_hook_trust"] is True
   and GUARD_HOOK == "tares hook guard --plugin-root {PLUGIN_DIR_SHELL}", thread["config"])
ck("the MCP server is named, with its address and the identity headers read from the station's variables, and no token",
   spec.settings["mcp_servers"] == {"tares": {"url": "http://cell.test/mcp", "env_http_headers": MCP_IDENTITY}}
   and all(v in spec.env or v == "TARES_PROJECT" for v in MCP_IDENTITY.values()), spec.settings["mcp_servers"])
ck("the rules are filled on the cell, with the machine's words left as placeholders, and blank ones dropped",
   spec.settings["rules"] == {
       "allow": ["Pat lets this station work in the repository of project alpha, checked out at /work/alpha and keep notes in {CREW_HOME}"],
       "soft_deny": ["Never push to a branch outside {WT_REPOS}"]}, spec.settings["rules"])
ck("the level is the crew's", spec.settings["autonomy"] == "L3-review")
used = set(NAME.findall(json.dumps([spec.command, spec.env, spec.settings])))
ck("the placeholders are the ones the spec uses, each with what it stands for",
   set(spec.placeholders) == used == {"STATION_SOCKET", "CODEX_HOME", "CREW_HOME", "TOKEN_FILE", "PLUGIN_DIR_SHELL", "WT_REPOS"}
   and all(spec.placeholders[k] == PLACEHOLDERS[k] for k in used), (sorted(spec.placeholders), sorted(used)))
filled = fill_placeholders([spec.command, spec.env, spec.settings], {k: f"/fixed/{k.lower()}" for k in PLACEHOLDERS})
ck("filled, no placeholder is left", not NAME.search(json.dumps(filled)), NAME.findall(json.dumps(filled)))
text = json.dumps([spec.command, spec.env, spec.settings])
ck("nothing of Claude Code's is in it",
   not any(w in text for w in ("--bg", "--plugin-dir", "autoMode", "permissions", "statusLine", "enabledPlugins", "Claude")), text[:300])
ck("no secret is in it", not any(s in text for s in (ROOT_TOKEN, FAKE_MODEL_KEY)) and not re.search(r"sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+", text))
bare = RuntimeCodex().launch_spec("reviewer", "shop-reviewer", {**ctx0, "model": "", "effort": ""})
ck("no model or effort given: neither is named, and Codex uses its own",
   "model" not in bare.settings["thread"] and "effort" not in bare.settings and "project" not in bare.settings["thread"]["developerInstructions"])
ck("a station with no project works from the crew's folder", bare.env["FACTORY_WORKTREES"] == "{CREW_HOME}/worktrees" and "TARES_PROJECT" not in bare.env)
helper = RuntimeCodex().launch_spec("helper", "shop-build-api-h1", {**proj, "parent": "shop-build-api", "parent_role": "builder"})
ck("a helper is told whose role it works under",
   "started by shop-build-api. You work under the builder role and follow its rules." in helper.settings["thread"]["developerInstructions"]
   and helper.env["FACTORY_ROLE"] == "helper" and helper.env["FACTORY_PARENT"] == "shop-build-api", helper.settings["thread"]["developerInstructions"])
ck("a crew lowered below the station's level takes it down",
   RuntimeCodex().launch_spec("builder", "s", {**ctx0, "autonomy": "L4-ship"}).settings["autonomy"] == "L3-review")
ck("monitoring adds nothing yet", RuntimeCodex().launch_spec("builder", "s", {**ctx0, "monitoring": object()}).env
   == RuntimeCodex().launch_spec("builder", "s", ctx0).env)

# ---------------------------------------------------------------------------------------------
print("c. what it refuses")
for role in ROLES:
    ck(f"a {role} builds", raises_value(RuntimeCodex().launch_spec, role, "s", {**ctx0, "parent_role": "builder"}) is None)
ck("a role that is not one is a ValueError naming the runtime",
   "Codex runtime cannot run a janitor station" in (raises_value(RuntimeCodex().launch_spec, "janitor", "s", ctx0) or ""))
ck("a blank station is a ValueError", raises_value(RuntimeCodex().launch_spec, "builder", "  ", ctx0) is not None)
ck("a helper without its parent's role is a ValueError",
   "parent_role" in (raises_value(RuntimeCodex().launch_spec, "helper", "s", ctx0) or ""))
ck("no crew autonomy is a ValueError", "crew_autonomy" in (raises_value(RuntimeCodex().launch_spec, "builder", "s", {**ctx0, "crew_autonomy": ""}) or ""))
ck("a level that is not one is a ValueError", raises_value(RuntimeCodex().launch_spec, "builder", "s", {**ctx0, "autonomy": "L9"}) is not None)
ck("no rules is a ValueError, never a station without rules",
   "rules" in (raises_value(RuntimeCodex().launch_spec, "builder", "s", {**ctx0, "rules": {"allow": [], "soft_deny": []}}) or "")
   and raises_value(RuntimeCodex().launch_spec, "builder", "s", {k: v for k, v in ctx0.items() if k != "rules"}) is not None)


# ---------------------------------------------------------------------------------------------
async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    hdr = {"Authorization": f"Bearer {ROOT_TOKEN}"}
    async with app.router.lifespan_context(app):
        store.create_project("p1", "custom", "alpha", {}, lead_person_id=None)
        store.set_setting("crew_settings", json.dumps({"autonomy": "L3-review"}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr) as root:
            print("d. the route")
            URL = "/api/factory/stations/shop-build-api/launch"
            params = {"role": "builder", "model": "gpt-5.6-luna", "effort": "low", "token_file": "true", "human": "Pat",
                      "authorized_at": "2026-01-01", "tares_url": "http://cell.test", "mcp_url": "http://cell.test/mcp",
                      "crew": "shop", "orchestrator": "shop-orchestrator", "project": "alpha", "repo": "/work/alpha"}
            r = await root.get(URL, params=params)
            ck("with nothing chosen the launch is Claude Code's", r.status_code == 200 and r.json()["runtime"] == "claude_code", r.text[:200])
            r = await root.put("/api/factory/integrations/runtime", json={"name": "codex", "settings": {}})
            ck("the factory's runtime can be set to codex through the integrations API", r.status_code == 200, r.text[:300])
            ck("the cell now resolves the runtime to codex", resolve.effective("runtime", None, store=store).integration == "codex")
            r = await root.get(URL, params=params)
            body = r.json() if r.status_code == 200 else {}
            ck("the launch names the Codex runtime", r.status_code == 200 and (body.get("runtime"), body.get("station"), body.get("role"))
               == ("codex", "shop-build-api", "builder"), r.text[:300])
            ck("its command, env and settings are the Codex runtime's for those values",
               body.get("command", [None])[:3] == ["app-server", "--listen", "unix://{STATION_SOCKET}"]
               and body["env"]["FACTORY_STATION"] == "shop-build-api" and body["env"]["CODEX_HOME"] == "{CODEX_HOME}"
               and body["env"]["TARES_PROJECT_NAME"] == "alpha" and body["settings"]["thread"]["model"] == "gpt-5.6-luna"
               and body["settings"]["mcp_servers"]["tares"]["url"] == "http://cell.test/mcp"
               and set(body["placeholders"]) <= set(PLACEHOLDERS), json.dumps(body)[:400])
            ck("the cell's own rules are in it, filled for this station",
               len(body["settings"]["rules"]["allow"]) > 1 and body["settings"]["rules"]["soft_deny"]
               and any("project alpha, checked out at /work/alpha" in s for s in body["settings"]["rules"]["allow"]),
               body.get("settings", {}).get("rules"))
            ck("the answer holds no secret", ROOT_TOKEN not in r.text and FAKE_MODEL_KEY not in r.text)
            setup = (await root.get("/api/factory/setup")).json()
            step = next((s for s in setup.get("steps", []) if s.get("slot") == "runtime"), {})
            ck("setup shows Codex as the runtime in use and offers nothing to switch to",
               step.get("line") == "Codex" and [a["id"] for a in step.get("available", [])] == ["claude_code"]
               or step.get("line") == "Codex" and step.get("available") == [], (step.get("line"), step.get("available")))
            r = await root.put("/api/factory/integrations/runtime", json={"name": "claude_code", "settings": {}})
            r2 = await root.get(URL, params=params)
            ck("set back, the launch is Claude Code's again", r.status_code == 200 and r2.json().get("runtime") == "claude_code", r2.text[:200])
            setup = (await root.get("/api/factory/setup")).json()
            step = next((s for s in setup.get("steps", []) if s.get("slot") == "runtime"), {})
            ck("and setup still does not offer Codex", step.get("line") == "Claude Code" and step.get("available") == [],
               (step.get("line"), step.get("available")))
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
