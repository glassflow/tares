"""The frozen cell routes (Tares 2.0, TR-1803): every route the Claude Code plugin, the crew engine
and the tares-cloud control plane call on a cell, so a deletion in 2.0 cannot break a caller that
runs a different version.

Two parts:
  routes  every (method, path) in FROZEN is served by a real route of the app, not the console's
          catch-all. A route that 2.0 removes on purpose goes in RETIRED with the ticket that
          retires it, and its caller stops using it in the same change.
  shapes  a running daemon answers the routes that cross a version boundary (control plane,
          ingest, health) with the fields their callers read.

Change FROZEN only together with the caller, and say why in the PR.

Run: python tests/test_cell_contract.py
"""
import asyncio, json, os, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import clean_env, scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-cell-contract-")
AUTH = "root-token-contract"
PORT = "8893"
os.environ.update({"TARES_DB": os.path.join(TMP, "contract.duckdb"),
                   "TARES_CATALOG": os.path.join(TMP, "none.yaml"),
                   "TARES_AUTH_TOKEN": AUTH, "TARES_OTLP_GRPC_PORT": "off",
                   "TARES_EDITION": "factory"})

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


# (method, path, caller). {x} is any path segment. One row per method a caller sends.
FROZEN = [
    # the Claude Code plugin (claude-plugin/scripts)
    ("POST", "/ingest/claude_code", "plugin ship.py, crew"),
    ("POST", "/api/factory/challenger/stopped", "plugin challenger.py"),
    ("GET", "/api/factory/monitoring/headers", "plugin otel_headers.py"),
    # the crew engine (tares/crew)
    ("POST", "/ingest/codex", "crew codex_record.py"),
    ("GET", "/api/whoami", "crew"),
    ("GET", "/api/home", "crew"),
    ("GET", "/api/people", "crew, control plane"),
    ("GET", "/api/answers", "crew"),
    ("PUT", "/api/crew/codex-limits", "crew codex_pace.py"),
    ("GET", "/api/crew/context/asks", "crew readings.py"),
    ("POST", "/api/crew/machine", "crew"),
    ("GET", "/api/crew/my-work", "crew"),
    ("GET", "/api/crew/pace", "crew pace.py"),
    ("GET", "/api/crew/projects", "crew"),
    ("GET", "/api/crew/settings", "crew"),
    ("PUT", "/api/crew/settings", "crew"),
    ("GET", "/api/desk", "crew"),
    ("POST", "/api/desk", "crew"),
    ("GET", "/api/desk/answered", "crew"),
    ("POST", "/api/desk/{x}/withdraw", "crew codex_asks.py"),
    ("GET", "/api/factory/guardrails/rules", "crew"),
    ("GET", "/api/factory/integrations/effective", "crew"),
    ("PUT", "/api/factory/integrations/runtime", "crew runtime_cmd.py"),
    ("PUT", "/api/factory/integrations/{x}", "crew person.py setup save"),
    ("PUT", "/api/factory/integrations/{x}/default", "crew person.py setup default"),
    ("GET", "/api/factory/setup", "crew person.py"),
    ("GET", "/api/factory/setup/github/repos", "crew"),
    ("GET", "/api/factory/setup/progress", "crew"),
    ("PUT", "/api/factory/setup/{x}/keep", "crew person.py setup keep"),
    ("GET", "/api/factory/stations/{x}/launch", "crew"),
    ("GET", "/api/grants", "crew"),
    ("POST", "/api/grants", "crew"),
    ("GET", "/api/projects", "crew"),
    ("GET", "/api/projects/{x}/crew", "crew person.py"),
    ("GET", "/api/projects/{x}/tickets", "crew idle_wake.py"),
    ("GET", "/api/projects/{x}/secrets", "crew with_secrets.py"),
    ("POST", "/api/projects/{x}/secrets/use", "crew with_secrets.py"),
    ("GET", "/api/review-queue", "crew"),
    # the tares-cloud control plane (control-plane/app)
    ("GET", "/health", "control plane usage.py, monitoring"),
    ("GET", "/api/capabilities", "ops: version"),
    ("GET", "/api/usage", "control plane usage.py: db_bytes, wal_bytes"),
    ("GET", "/api/usage/model", "control plane trial.py, userlist.py: by_key_source"),
    ("POST", "/api/keys", "control plane cells.py mint_key"),
    ("DELETE", "/api/keys/{x}", "control plane cells.py delete_key"),
    ("PUT", "/api/keys/{x}/person", "control plane cells.py"),
    ("POST", "/api/people", "control plane cells.py"),
    ("DELETE", "/api/people/{x}", "control plane cells.py"),
    ("POST", "/api/integrations/github", "control plane cells.py: returns ingest_key"),
    ("PUT", "/api/integrations/github/{x}", "control plane cells.py"),
    ("DELETE", "/api/integrations/github/{x}", "control plane cells.py"),
    ("PUT", "/api/settings/slack-bot-token", "control plane cells.py"),
    ("DELETE", "/api/settings/slack-bot-token", "control plane cells.py"),
    ("PUT", "/api/settings/slack-signing-secret", "control plane cells.py"),
    ("DELETE", "/api/settings/slack-signing-secret", "control plane cells.py"),
    ("PUT", "/api/settings/slack-team", "control plane cells.py"),
    ("POST", "/api/slack/channels/changed", "control plane cells.py"),
    ("POST", "/api/slack/events", "control plane slack_routes.py (proxy)"),
    ("PUT", "/api/settings/linear-connection", "control plane cells.py"),
    ("DELETE", "/api/settings/linear-connection", "control plane cells.py"),
    ("GET", "/api/settings/providers", "control plane trial.py"),
    ("GET", "/api/settings/anthropic-key", "control plane trial.py (older cells)"),
    ("GET", "/api/licence", "ops ee_fleet_plan.py"),
    ("POST", "/ingest/{x}", "control plane github.py: forwarded GitHub App deliveries"),
]

# Routes a caller still uses today that 2.0 removes; the caller drops them in the named ticket.
RETIRED = [
    ("GET", "/api/settings/tracing", "control plane, TR-1818"),
    ("PUT", "/api/settings/tracing", "control plane, TR-1818"),
    ("GET", "/api/settings/edition", "control plane, TR-1818"),
    ("PUT", "/api/settings/edition", "control plane, TR-1818"),
    ("GET", "/api/catalog/export", "control plane workshop seeding, TR-1824"),
    ("POST", "/api/catalog/import", "control plane workshop seeding, TR-1824"),
]


def _concrete(path: str) -> str:
    return path.replace("{x}", "contract-x")


def flat_routes(routes):
    """Every route of the app in matching order. A router the app includes is one entry of
    app.routes in newer FastAPI (its routes stay on the router), and its routes copied in older."""
    for r in routes:
        if hasattr(r, "original_router"):
            yield from flat_routes(r.original_router.routes)
        else:
            yield r


def check_routes():
    from starlette.routing import Match
    from tares.daemon import make_app
    app = make_app()
    ck("make_app built the full app, not the degraded one", "degraded" not in (app.title or ""), app.title)
    catch_all = {"/{path:path}", "/{full_path:path}", "/api/{rest:path}", "/ingest/{rest:path}",
                 "/catalog/{rest:path}"}
    routes = [r for r in flat_routes(app.routes) if getattr(r, "path", "") not in catch_all]
    for method, path, caller in FROZEN:
        want = _concrete(path)
        hit = False
        for r in routes:
            methods = getattr(r, "methods", None) or set()
            scope = {"type": "http", "path": want, "method": method, "root_path": ""}
            match, _ = r.matches(scope)
            if match == Match.FULL and method in methods:
                hit = True
                break
        ck(f"route {method} {path} ({caller})", hit, "no route serves it")


async def _wait(url):
    for _ in range(120):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def check_shapes():
    data = tempfile.mkdtemp(prefix="tares-cell-contract-d-")
    env = clean_env(TARES_DB=os.path.join(data, "d.duckdb"), TARES_CATALOG=os.path.join(data, "n.yaml"),
                    TARES_PORT=PORT, TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN=AUTH,
                    TARES_EDITION="factory")
    log = open(os.path.join(data, "daemon.log"), "w")
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    B = f"http://127.0.0.1:{PORT}"
    H = {"Authorization": f"Bearer {AUTH}"}
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False, open(os.path.join(data, "daemon.log")).read()[-500:])
            return
        async with httpx.AsyncClient(base_url=B, timeout=20) as cx:
            r = await cx.get("/health")
            ck("/health answers a status", r.status_code == 200 and "status" in r.json(), r.text[:200])
            r = await cx.get("/api/capabilities", headers=H)
            ck("/api/capabilities has a version", r.status_code == 200 and r.json().get("version"), r.text[:200])
            r = await cx.get("/api/whoami", headers=H)
            ck("/api/whoami answers", r.status_code == 200, r.text[:200])
            r = await cx.get("/api/usage", headers=H)
            ck("/api/usage has db_bytes", r.status_code == 200 and "db_bytes" in r.json(), r.text[:200])
            r = await cx.get("/api/usage/model", headers=H)
            ck("/api/usage/model has by_key_source", r.status_code == 200
               and isinstance(r.json().get("by_key_source"), dict), r.text[:200])
            r = await cx.get("/api/settings/providers", headers=H)
            ck("/api/settings/providers lists providers", r.status_code == 200
               and isinstance(r.json().get("providers"), list), r.text[:200])
            r = await cx.get("/api/people", headers=H)
            ck("/api/people lists people", r.status_code == 200 and isinstance(r.json().get("people"), list),
               r.text[:200])
            r = await cx.post("/api/keys", headers=H, json={"name": "contract", "scopes": ["ingest"]})
            body = r.json() if r.status_code == 201 else {}
            ck("POST /api/keys answers 201 with id and secret", r.status_code == 201 and body.get("id")
               and body.get("secret"), r.text[:200])
            key = body.get("secret") or ""
            line = {"type": "user", "sessionId": "contract-session", "uuid": "contract-1",
                    "timestamp": "2026-11-27T12:00:00Z", "cwd": "/tmp/contract",
                    "message": {"role": "user", "content": "hello"}}
            r = await cx.post("/ingest/claude_code", content=json.dumps(line) + "\n",
                              headers={"Authorization": f"Bearer {key}", "content-type": "application/x-ndjson"})
            ck("POST /ingest/claude_code with an ingest key is accepted", r.status_code == 202, r.text[:200])
            r = await cx.get("/api/factory/monitoring/headers", headers=H)
            ck("/api/factory/monitoring/headers answers", r.status_code == 200, r.text[:200])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    print("== routes ==")
    check_routes()
    print("== shapes ==")
    asyncio.run(check_shapes())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
