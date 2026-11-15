"""The Claude Code runtime builds a station's launch on the cell (TR-867).

The runtime integration (tares/integrations/builtin/runtime_claude_code.py) gives a LaunchSpec for a station
and a role. A later runtime is one more integration of the slot. Covered:
 - claude_code is installed for the runtime slot and is its default;
 - for each of the five roles (orchestrator, reviewer, releaser, builder, helper) what `tares factory
   launch-plan` prints is the spec the cell gave (the copy the command line keeps) with that crew's values
   put in: the arguments and the settings the same, the environment the same except FACTORY_TARES, which
   only the command line adds (a spec has the keys of monitoring's station_env as well, tested apart);
 - no spec holds a secret: the crew's key stays in its file ({TOKEN_FILE}), TARES_AUTH_TOKEN is blank, and no
   value looks like a key;
 - the placeholders a spec uses are listed with what they stand for, and filling them with fixed values leaves
   none behind and does not change the spec;
 - a monitoring integration on OTLP puts the telemetry variables in the spec's env and the headers helper in
   its settings, and neither a key nor a header value anywhere;
 - a role the runtime cannot run, a helper with no parent role, a crew with no valid autonomy, a station
   with no rules and a blank station are a ValueError;
 - GET /api/factory/stations/{station}/launch on a cell with auth on: an admin and the station itself get the
   spec, a key for another station gets 403, no key 401, a key with no admin scope 403, an unknown role 422;
   /health lists the launch_spec capability; the answer holds no secret;
 - a name that is not a station name (a space, a backslash, a leading dot or hyphen, upper case, two dots, more
   than 120 characters) is a 422 that says the rule (TR-870; tests/test_crew_launch_spec.py covers the command line).

The crew side (launch-plan, the cell answered by a fake Tares that builds the spec with this runtime) runs in a throwaway crew of its own (tests/factory/launch_support.py: a fake
Tares and a copy of the kit), as tests/test_crew_launch.py does.

Run: python tests/test_runtime_launch.py
"""
import asyncio
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

# A fresh HOME, fake keys and no keyring, before anything is imported or run.
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-runtime-launch-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
FAKE_MODEL_KEY = "sk-fake-runtime-launch-test"
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
    ANTHROPIC_API_KEY=FAKE_MODEL_KEY, TARES_HOME=str(SCRATCH / "tares-home"),
    FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
    TARES_DB=str(SCRATCH / "cell.duckdb"), TARES_CATALOG=str(SCRATCH / "none.yaml"),
    TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN="root-token-runtime-launch",   # auth on, for the route
)
(SCRATCH / "home").mkdir()
ROOT_TOKEN = os.environ["TARES_AUTH_TOKEN"]

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

import httpx  # noqa: E402

from tares.envelope import now_utc  # noqa: E402
from tares.integrations import registry, resolve  # noqa: E402
from tares.integrations.builtin.runtime_claude_code import ROLES, RuntimeClaudeCode  # noqa: E402
from tares.integrations.runtime import PLACEHOLDERS, fill_placeholders  # noqa: E402
import parity  # noqa: E402

P = F = 0
FAKE_TOKEN = "tok-fake-runtime-launch-0123456789"
KEY, HDR = "otlp-secret-key-777", "hdr-secret-value-888"
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


def drive(tmp, steps):
    """Run the crew driver once per step in a throwaway crew under tmp; returns [(code, stdout, stderr)]."""
    tmp.mkdir(parents=True, exist_ok=True)
    base = parity.crew_env(tmp)
    out = []
    for op, args, world, setup in steps:
        e = {**base, "LAUNCH_WORLD": json.dumps(world), "LAUNCH_SETUP": json.dumps(setup or {})}
        p = subprocess.run([sys.executable, str(HERE / "factory" / "launch_support.py"), op, *args], cwd=str(ROOT),
                           env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        out.append((p.returncode, p.stdout, p.stderr))
    return out


def world(autonomy="L3-review", projects=None, **settings):
    return {"settings": {"autonomy": autonomy, "releaser": True, "release_profile": "kind", "challenger": False,
                         "builders_max": 3, **settings},
            "projects": projects or [], "grants": {"all": '- "the releaser may restart the staging database" (2026-01-02)\n',
                                                   "projects": {}}}


def secret_findings(spec, extra=()):
    """What in the spec looks like a secret: a value that holds a known secret or a key-shaped word, and a
    token or key variable that is not blank or the file placeholder."""
    text = json.dumps([spec.command, spec.env, spec.settings])
    bad = [s for s in (FAKE_TOKEN, FAKE_MODEL_KEY, ROOT_TOKEN, *extra) if s in text]
    bad += re.findall(r"sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+|eyJ[A-Za-z0-9_-]{10,}", text)
    for k, v in spec.env.items():
        if any(w in k.upper() for w in ("TOKEN", "KEY", "SECRET", "PASSWORD")) and not k.endswith("_FILE") \
                and v not in ("", "{TOKEN_FILE}"):
            bad.append(f"{k}={v}")
        if k.endswith("_FILE") and "TOKEN" in k and v != "{TOKEN_FILE}":
            bad.append(f"{k}={v}")
    return bad


# ---------------------------------------------------------------------------------------------
print("the runtime slot")
registry.load()
ck("the claude_code runtime is installed for the runtime slot", registry.get("runtime", "claude_code") is RuntimeClaudeCode)
ck("it is the default runtime", resolve.DEFAULTS["runtime"] == "claude_code")
ck("it is named Claude Code, runs the five roles and is not an ee integration",
   RuntimeClaudeCode.title == "Claude Code" and RuntimeClaudeCode.slot == "runtime" and not RuntimeClaudeCode.ee
   and set(ROLES) == {"orchestrator", "reviewer", "releaser", "builder", "helper"})

# ---------------------------------------------------------------------------------------------
print("the launch plan is the cell's spec with this machine's values put in")
links = {"id": "uc_links", "name": "Links App", "repos": [{"path": "{TMP}/links"}]}
CASES = [("crew-orchestrator", "orchestrator", "-", "-", "-"), ("crew-reviewer", "reviewer", "-", "-", "-"),
         ("crew-releaser", "releaser", "-", "-", "-"),
         ("links-app-build-m1", "builder", "crew-orchestrator", "Links App", "{TMP}/links"),
         ("links-app-build-m1-h1", "helper", "links-app-build-m1", "Links App", "{TMP}/links")]
SETUP = {"token": FAKE_TOKEN,
         "stations": {"links-app-build-m1": {"autonomy": "L3-review", "project_id": "uc_links"}}}
tmp = Path(tempfile.mkdtemp(prefix="tares-runtime-plan-", dir=SCRATCH)).resolve()
steps = [("launch_plan", list(c), world(projects=[links]), SETUP) for c in CASES]
res = drive(tmp, steps)
ck("every launch-plan run exits 0", all(r[0] == 0 for r in res), str([(r[0], r[2][-160:]) for r in res if r[0]]))
UNKNOWN = ("REPOS", "WT_REPOS", "PLUGIN_DIR_SHELL")   # machine words the test does not spell out: matched, not compared


def same(spec_node, plan_node, values):
    """True when plan_node is spec_node with each {NAME} of `values` put in, and an UNKNOWN one standing for any text."""
    if isinstance(spec_node, dict):
        return (isinstance(plan_node, dict) and spec_node.keys() == plan_node.keys()
                and all(same(spec_node[k], plan_node[k], values) for k in spec_node))
    if isinstance(spec_node, list):
        return (isinstance(plan_node, list) and len(spec_node) == len(plan_node)
                and all(same(a, b, values) for a, b in zip(spec_node, plan_node)))
    if not isinstance(spec_node, str):
        return spec_node == plan_node
    pattern = ""
    for part in re.split(r"(\{[A-Z_]+\})", spec_node):
        name = part[1:-1]
        if part.startswith("{") and name in values:
            pattern += re.escape(values[name])
        elif part.startswith("{") and name in UNKNOWN:
            pattern += ".+?"
        else:
            pattern += re.escape(part)
    return isinstance(plan_node, str) and re.fullmatch(pattern, plan_node, re.S) is not None


specs = {}
for i, (station, role, *_rest) in enumerate(CASES):
    r = res[i]
    copy_file = tmp / "crew" / "launch" / f"{station}.json"
    if r[0] or not copy_file.is_file():
        ck(f"{role}: the crew side ran and kept the spec", False, r[2][-200:])
        continue
    plan = json.loads(r[1])
    raw = json.loads(copy_file.read_text(encoding="utf-8"))
    spec = SimpleNamespace(command=raw["command"], env=raw["env"], settings=raw["settings"], placeholders=raw["placeholders"])
    specs[role] = spec
    before = copy.deepcopy([spec.command, spec.env, spec.settings])
    values = {"CREW_HOME": plan["env"]["FACTORY_HOME"], "PLUGIN_DIR": plan["args"][-1],
              "SETTINGS_FILE": plan["args"][plan["args"].index("--settings") + 1],
              "TOKEN_FILE": plan["env"].get("TARES_FACTORY_TOKEN_FILE", "")}
    filled = fill_placeholders({"command": spec.command, "env": spec.env, "settings": spec.settings}, values)
    ck(f"{role}: filling the placeholders does not change the spec", [spec.command, spec.env, spec.settings] == before)
    ck(f"{role}: the plan has no placeholder left", not NAME.findall(json.dumps(plan)), NAME.findall(json.dumps(plan)))
    ck(f"{role}: the arguments are the spec's command with this machine's values",
       same(spec.command, plan["args"], values), json.dumps([spec.command, plan["args"]])[:600])
    ck(f"{role}: the settings are the spec's settings with this machine's values",
       same(spec.settings, plan["settings"], values),
       [k for k in plan["settings"] if not same(spec.settings.get(k), plan["settings"][k], values)])
    plan_env = {k: v for k, v in plan["env"].items() if k != "FACTORY_TARES"}
    ck(f"{role}: the environment is the spec's, but for FACTORY_TARES", same(spec.env, plan_env, values),
       {k: (spec.env.get(k), plan_env.get(k)) for k in set(spec.env) | set(plan_env) if not same(spec.env.get(k, ""), plan_env.get(k, ""), values)})
    ck(f"{role}: FACTORY_TARES is added by the command line only", "FACTORY_TARES" not in spec.env)
    ck(f"{role}: the spec has no --resume, no boot prompt and no claude binary",
       "--resume" not in spec.command and spec.command[0] == "--bg" and spec.command[-2] == "--plugin-dir"
       and spec.command[-1] == "{PLUGIN_DIR}" and "{SETTINGS_FILE}" in spec.command)
    ck(f"{role}: the settings file the spec stands for has the env first and holds the rest",
       "env" not in spec.settings and {"env": spec.env, **spec.settings}.keys() == {"env", *spec.settings})
    ck(f"{role}: no secret in the spec", not secret_findings(spec), secret_findings(spec))
    ck(f"{role}: the token is the file placeholder",
       spec.env.get("TARES_FACTORY_TOKEN_FILE") == "{TOKEN_FILE}" and spec.env.get("TARES_FACTORY_CRED_FILE") == "{TOKEN_FILE}"
       and spec.env["TARES_AUTH_TOKEN"] == "", {k: v for k, v in spec.env.items() if "TOKEN" in k})
    ck(f"{role}: the crew's key is in no spec, nor in the plan; only the file it is in is known",
       FAKE_TOKEN not in json.dumps(raw) and FAKE_TOKEN not in json.dumps(plan) and values["TOKEN_FILE"].endswith(".tares-token"),
       values["TOKEN_FILE"])
    used = set(NAME.findall(json.dumps([spec.command, spec.env, spec.settings])))
    ck(f"{role}: spec.placeholders names each placeholder the spec uses, with what it stands for",
       set(spec.placeholders) == used and used <= set(PLACEHOLDERS)
       and all(spec.placeholders[k] == PLACEHOLDERS[k] for k in used), (sorted(spec.placeholders), sorted(used)))

if len(specs) == len(CASES):
    ck("the helper's rules and skill are its parent's (a builder's)",
       "station-builder" in specs["helper"].command[specs["helper"].command.index("--append-system-prompt") + 1]
       and "FACTORY_ROLE" in specs["helper"].env and specs["helper"].env["FACTORY_ROLE"] == "helper")
    ck("only the builder and its helper have a project and a repo",
       all(("TARES_PROJECT" in specs[r].env) == (r in ("builder", "helper")) for r in specs)
       and all(("FACTORY_REPO" in specs[r].env) == (r in ("builder", "helper")) for r in specs))
    ck("only the orchestrator may run the crew's commands",
       any("spawn" in a for a in specs["orchestrator"].settings["permissions"]["allow"])
       and not any("spawn" in a for r in ("reviewer", "releaser", "builder", "helper") for a in specs[r].settings["permissions"]["allow"])
       and all("Bash(gh pr merge:*)" in s.settings["permissions"]["deny"] for s in specs.values()))
    ck("every spec leaves some machine values to the command line",
       all({"CREW_HOME", "PLUGIN_DIR", "SETTINGS_FILE", "PLUGIN_DIR_SHELL"} <= set(s.placeholders) for s in specs.values()))

# ---------------------------------------------------------------------------------------------
print("placeholders filled by fixed values")
if "builder" in specs:
    fixed = {k: f"/fixed/{k.lower()}" for k in PLACEHOLDERS}
    spec = specs["builder"]
    out = fill_placeholders({"c": spec.command, "e": spec.env, "s": spec.settings}, fixed)
    text = json.dumps(out)
    ck("fixed values leave no placeholder", not NAME.findall(text), NAME.findall(text))
    ck("each fixed value lands where the spec had its placeholder",
       out["e"]["FACTORY_HOME"] == "/fixed/crew_home" and out["e"]["TARES_FACTORY_TOKEN_FILE"] == "/fixed/token_file"
       and "/fixed/settings_file" in out["c"] and out["c"][-1] == "/fixed/plugin_dir"
       and out["e"]["FACTORY_WORKTREES"].endswith("/.factory/worktrees"))
    ck("the spec itself is not changed by filling it", json.dumps([spec.command, spec.env, spec.settings]).count("{PLUGIN_DIR}") >= 1)
    order = {"A": "{B}", "B": "x"}
    ck("fill_placeholders fills in one pass: a value holding another {NAME} stays, in either order of the values",
       fill_placeholders("{A}/{B}", order) == "{B}/x" and fill_placeholders("{A}/{B}", dict(reversed(order.items()))) == "{B}/x")
    ck("fill_placeholders leaves a name it was not given, and a dict's keys",
       fill_placeholders({"{CREW_HOME}": ["{CREW_HOME}/x {other}"]}, {"CREW_HOME": "/h"}) == {"{CREW_HOME}": ["/h/x {other}"]})

# ---------------------------------------------------------------------------------------------
print("what the runtime cannot run, and what it will not start without")
ctx0 = {"crew": "crew", "crew_autonomy": "L3-review", "model": "sonnet", "effort": "high", "autocompact": "200000",
        "tares_url": "http://t", "mcp_url": "http://t/mcp",
        "rules": {"allow": ["the station may read {home}"], "soft_deny": ["never touch {prod_pattern}"]}}
ck("a baseline ctx builds", raises_value(RuntimeClaudeCode().launch_spec, "builder", "s", ctx0) is None)
for role in ("janitor", "", "Builder", "human"):
    msg = raises_value(RuntimeClaudeCode().launch_spec, role, "s", ctx0)
    ck(f"role {role!r} is a ValueError that says what the roles are",
       msg is not None and "orchestrator" in msg and "helper" in msg, msg)
ck("a helper without a parent role is a ValueError", "parent_role" in (raises_value(RuntimeClaudeCode().launch_spec, "helper", "s", ctx0) or ""))
ck("a helper under a bad parent role is a ValueError",
   raises_value(RuntimeClaudeCode().launch_spec, "helper", "s", {**ctx0, "parent_role": "helper"}) is not None)
ck("a helper under a builder builds",
   raises_value(RuntimeClaudeCode().launch_spec, "helper", "s", {**ctx0, "parent_role": "builder"}) is None)
ck("no crew autonomy is a ValueError", "crew_autonomy" in (raises_value(RuntimeClaudeCode().launch_spec, "builder", "s",
                                                                       {**ctx0, "crew_autonomy": ""}) or ""))
ck("an autonomy that is not a level is a ValueError",
   raises_value(RuntimeClaudeCode().launch_spec, "builder", "s", {**ctx0, "autonomy": "L9"}) is not None)
ck("no rules is a ValueError, never a station without rules",
   "rules" in (raises_value(RuntimeClaudeCode().launch_spec, "builder", "s", {**ctx0, "rules": {"allow": [], "soft_deny": []}}) or "")
   and raises_value(RuntimeClaudeCode().launch_spec, "builder", "s", {k: v for k, v in ctx0.items() if k != "rules"}) is not None)
ck("a blank station is a ValueError", raises_value(RuntimeClaudeCode().launch_spec, "builder", "  ", ctx0) is not None)
ck("a crew lowered below the station's level takes it down",
   "autonomy level L3-review" in json.dumps(RuntimeClaudeCode().launch_spec(
       "builder", "s", {**ctx0, "autonomy": "L4-ship"}).settings["autoMode"]["environment"]))
ck("no token file, no token variables",
   not any("TOKEN_FILE" in k or "CRED_FILE" in k for k in RuntimeClaudeCode().launch_spec("builder", "s", ctx0).env)
   and "TOKEN_FILE" not in RuntimeClaudeCode().launch_spec("builder", "s", ctx0).placeholders)

# ---------------------------------------------------------------------------------------------
print("monitoring on OTLP")


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def mk(token=ROOT_TOKEN, station=None, role="builder", crew=None):
        hdr = {"Authorization": f"Bearer {token}"} if token else {}
        if station:
            hdr.update({"X-Tares-Station": station, "X-Tares-Role": role})
        if crew:
            hdr["X-Tares-Crew"] = crew
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)

    async with app.router.lifespan_context(app):
        store.create_project("p1", "custom", "alpha", {}, lead_person_id=None)
        async with mk() as root:
            r = await root.put("/api/factory/integrations/monitoring", json={
                "name": "otlp",
                "settings": {"endpoint": "https://collector.example.com:4318/", "headers": f"x-token={HDR}", "api_key": KEY}})
            ck("choosing otlp for monitoring is 200", r.status_code == 200, r.text[:300])
            otlp = registry.get("monitoring", "otlp")(store)
            ctx = copy.deepcopy(ctx0)
            plain = RuntimeClaudeCode().launch_spec("builder", "shop-build-api", {**ctx, "project": "My Project"})
            spec = RuntimeClaudeCode().launch_spec("builder", "shop-build-api", {**ctx, "project": "My Project", "monitoring": otlp})
            env = spec.env
            ck("telemetry is on and every exporter is otlp over http/protobuf",
               env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1" and env.get("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA") == "1"
               and all(env.get(f"OTEL_{s}_EXPORTER") == "otlp" for s in ("METRICS", "LOGS", "TRACES"))
               and env.get("OTEL_EXPORTER_OTLP_PROTOCOL") == "http/protobuf", env)
            ck("the endpoint is the stored one, base only", env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "https://collector.example.com:4318", env)
            ck("the resource attributes name the station, role and project",
               env.get("OTEL_RESOURCE_ATTRIBUTES") == "tares.station=shop-build-api,tares.role=builder,tares.project=My%20Project",
               env.get("OTEL_RESOURCE_ATTRIBUTES"))
            ck("the headers come from the helper the settings name, fetched at run time",
               spec.settings.get("otelHeadersHelper") == "tares hook otel-headers" and "OTEL_EXPORTER_OTLP_HEADERS" not in env, spec.settings)
            ck("no plaintext key or header anywhere in the spec", not secret_findings(spec, (KEY, HDR)), secret_findings(spec, (KEY, HDR)))
            ck("monitoring added the keys and nothing else changed",
               {k: v for k, v in env.items() if k not in otlp.station_env({"station": "shop-build-api", "role": "builder",
                                                                          "project": "My Project"})} == plain.env
               and {k: v for k, v in spec.settings.items() if k != "otelHeadersHelper"} == plain.settings
               and spec.command == plain.command)
            ck("without monitoring the spec has no telemetry variable",
               not any(k.startswith(("OTEL_", "CLAUDE_CODE_")) for k in plain.env) and "otelHeadersHelper" not in plain.settings)
            off = await root.put("/api/factory/integrations/monitoring", json={"name": "off", "settings": {}})
            ck("monitoring off is 200", off.status_code == 200, off.text[:200])
            ck("with monitoring off, nothing is added", registry.get("monitoring", "off")(store).station_env({"station": "s"}) == {})
            r = await root.put("/api/factory/integrations/monitoring", json={
                "name": "otlp",
                "settings": {"endpoint": "https://collector.example.com:4318/", "headers": f"x-token={HDR}", "api_key": KEY}})

            print("the route: GET /api/factory/stations/{station}/launch")
            URL = "/api/factory/stations/shop-build-api/launch"
            params = {"role": "builder", "model": "sonnet", "effort": "high", "autocompact": "200000", "token_file": "true",
                      "human": "Pat", "authorized_at": "2026-01-01", "autonomy": "L3-review", "cluster": "factory",
                      "tares_url": "http://cell.test", "mcp_url": "http://cell.test/mcp", "crew": "shop",
                      "orchestrator": "shop-orchestrator", "project": "alpha", "repo": "/work/alpha"}
            h = (await root.get("/health")).json()
            r = await root.get(URL, params=params)
            ck("a cell with no crew autonomy set refuses to build one (422), as the command line does",
               r.status_code == 422 and "crew_autonomy" in r.text, r.text[:200])
            store.set_setting("crew_settings", json.dumps({"autonomy": "L3-review"}))
            ck("/health lists launch_spec", "launch_spec" in h.get("capabilities", []), h.get("capabilities"))
            reader = (await root.post("/api/keys", json={"name": "reader", "scopes": ["read"]})).json()["secret"]
            r = await root.get(URL, params=params)
            ck("an admin gets the spec", r.status_code == 200, r.text[:300])
            body = r.json() if r.status_code == 200 else {}
            ck("it names the runtime, station and role",
               (body.get("runtime"), body.get("station"), body.get("role")) == ("claude_code", "shop-build-api", "builder"), body)
            ck("its command, env and settings are the runtime's for those values",
               body.get("command", [None])[0] == "--bg" and body["command"][-1] == "{PLUGIN_DIR}"
               and body["env"]["FACTORY_STATION"] == "shop-build-api" and body["env"]["FACTORY_CREW"] == "shop"
               and body["env"]["TARES_FACTORY_URL"] == "http://cell.test" and body["env"]["TARES_PROJECT_NAME"] == "alpha"
               and body["env"]["TARES_FACTORY_TOKEN_FILE"] == "{TOKEN_FILE}" and body["env"]["TARES_AUTH_TOKEN"] == ""
               and body["settings"]["autoMode"]["allow"][0] == "$defaults"
               and set(body["placeholders"]) <= set(PLACEHOLDERS), json.dumps(body)[:400])
            ck("the cell's own rules and monitoring are in it",
               len(body["settings"]["autoMode"]["soft_deny"]) > 1 and body["env"].get("OTEL_EXPORTER_OTLP_ENDPOINT")
               and body["settings"].get("otelHeadersHelper") == "tares hook otel-headers")
            ck("the answer holds no secret",
               not any(s in r.text for s in (ROOT_TOKEN, KEY, HDR, FAKE_MODEL_KEY, FAKE_TOKEN)), r.text[:200])
            async with mk(station="shop-build-api") as cx:
                r2 = await cx.get(URL, params=params)
                ck("the station itself gets it", r2.status_code == 200 and r2.json() == body, r2.text[:200])
                r3 = await cx.get(URL, params={k: v for k, v in params.items() if k != "role"})
                ck("a station's role comes from its name when nothing else says", r3.status_code == 200 and r3.json()["role"] == "builder", r3.text[:200])
            async with mk(station="shop-build-other") as cx:
                ck("a key for another station is refused", (await cx.get(URL, params=params)).status_code == 403)
            async with mk(token=None) as cx:
                rr = await cx.get(URL, params=params)
                ck("no key is refused", rr.status_code == 401, (rr.status_code, rr.text[:100]))
            async with mk(token="wrong-key") as cx:
                rr = await cx.get(URL, params=params)
                ck("a wrong key is refused", rr.status_code == 401, (rr.status_code, rr.text[:100]))
            async with mk(token=reader) as cx:
                rr = await cx.get(URL, params=params)
                ck("a key without the admin scope is refused", rr.status_code == 403, (rr.status_code, rr.text[:100]))
            r = await root.get(URL, params={**params, "project": "alpah"})
            ck("a project the caller names that does not exist is 404, as on the rules route",
               r.status_code == 404 and (await root.get("/api/factory/guardrails/rules",
                                                         params={"station": "shop-build-api", "project": "alpah"})).status_code == 404,
               (r.status_code, r.text[:100]))
            ck("a project left out still builds (a station with no project, such as the reviewer)",
               (await root.get("/api/factory/stations/shop-reviewer/launch",
                               params={k: v for k, v in {**params, "role": "reviewer"}.items() if k != "project"})).status_code == 200)
            ck("an unknown role is 422",(await root.get(URL, params={**params, "role": "janitor"})).status_code == 422)
            ck("a helper needs its parent's role (422)",
               (await root.get("/api/factory/stations/shop-build-api-h1/launch", params={**params, "role": "helper"})).status_code == 422)
            r = await root.get("/api/factory/stations/shop-build-api-h1/launch",
                               params={**params, "role": "helper", "parent": "shop-build-api", "parent_role": "builder"})
            ck("a helper with its parent's role builds", r.status_code == 200 and r.json()["role"] == "helper", r.text[:200])

            print("the route answers what the fake cell the crew tests rely on answers (F2)")
            import fake_launch
            hdr_st = {"X-Tares-Station": "shop-build-api", "X-Tares-Role": "builder"}
            for what, q in (("a builder with a project", params),
                            ("a builder with no project", {k: v for k, v in params.items() if k != "project"}),
                            ("a builder with no token file", {**params, "token_file": "false"})):
                rr = await root.get(URL, params=q)
                rules_r = (await root.get("/api/factory/guardrails/rules", params={
                    "station": "shop-build-api", "project": q.get("project", ""), "role": "builder"})).json()
                want = fake_launch.launch_answer(
                    URL + "?" + "&".join(f"{k}={v}" for k, v in q.items()), hdr_st,
                    [{"id": "p1", "name": "alpha"}],
                    lambda pname, wrole, _r=rules_r: {"allow": _r["allow"], "soft_deny": _r["soft_deny"]},
                    "L3-review", "http://t", monitoring=otlp)
                ck(f"the real route and the fake agree: {what}",
                   rr.status_code == 200 and want is not None and want[0] == 200
                   and rr.json() == json.loads(json.dumps(want[1])),
                   (rr.status_code, rr.text[:200], str(want)[:200]))

            print("a station whose recorded project is gone still launches (F6)")
            store.set_session_station("sess-gone", "shop-build-gone", "builder", "shop-orchestrator", "p_gone_id", now_utc())
            no_proj = {k: v for k, v in params.items() if k not in ("project", "crew")}   # a named crew must match the record
            rr = await root.get("/api/factory/stations/shop-build-gone/launch", params=no_proj)
            ck("with no project named, the gone recorded project is not an error (200)", rr.status_code == 200, (rr.status_code, rr.text[:200]))
            store.set_session_station("sess-none", "shop-build-none", "builder", "shop-orchestrator", None, now_utc())
            r_none = await root.get("/api/factory/stations/shop-build-none/launch", params=no_proj)
            if rr.status_code == 200 and r_none.status_code == 200:
                ck("the launch has no project id, and its allow and deny rules are those of a station with no project",
                   not rr.json()["env"].get("TARES_PROJECT") and not rr.json()["env"].get("TARES_PROJECT_NAME")
                   and "p_gone_id" not in json.dumps(rr.json()) and {k: v for k, v in rr.json()["settings"]["autoMode"].items() if k != "environment"}
                   == {k: v for k, v in r_none.json()["settings"]["autoMode"].items() if k != "environment"},
                   (rr.json()["env"].get("TARES_PROJECT"), list(rr.json()["settings"]["autoMode"])))
            store.set_session_station("sess-here", "shop-build-here", "builder", "shop-orchestrator", "p1", now_utc())
            rr = await root.get("/api/factory/stations/shop-build-here/launch", params=no_proj)
            ck("a recorded project that exists is the station's project",
               rr.status_code == 200 and rr.json()["env"].get("TARES_PROJECT") == "p1", (rr.status_code, rr.text[:200]))
            rr = await root.get("/api/factory/stations/shop-build-gone/launch",
                                params={**params, "project": "p_gone_id"})
            ck("naming the gone project is still 404", rr.status_code == 404, (rr.status_code, rr.text[:120]))

            print("the route refuses a name that is not a station name")
            bad = {"a b": "a%20b", "a backslash": "x%5Cy", "a leading dot": ".hidden", "a leading hyphen": "-lead",
                   "upper case": "Shop", "two dots": "a..b", "121 characters": "a" * 121, "a colon": "a%3Ab"}
            for what, enc in bad.items():
                rr = await root.get(f"/api/factory/stations/{enc}/launch", params=params)
                ck(f"{what} is 422 and the answer says the rule",
                   rr.status_code == 422 and "lower-case letters, digits, hyphens and underscores" in rr.text,
                   (rr.status_code, rr.text[:160]))
            ck("a name of 120 characters is a station name", (await root.get(f"/api/factory/stations/{'a' * 120}/launch",
                                                                             params={**params, "project": ""})).status_code == 200)
            async with mk(station=".hidden") as cx:
                rr = await cx.get("/api/factory/stations/.hidden/launch", params=params)
                ck("the station's own key does not let a bad name through", rr.status_code == 422, (rr.status_code, rr.text[:120]))

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
