"""The MCP tools a factory cell lists (Tares 2.0, TR-1811): `tools/list` from the MCP server, against a
running factory daemon with auth on and no licence, is exactly the list below. The source catalog's
tools (read, stats, catalog_list, catalog_describe, list_connectors, discover_source, discover_docker,
test_source, create_source, list_sources, source_fields) are gone; a tool added or removed later
changes this list in the same change.

Run: python tests/test_mcp_factory_tools.py
"""
import asyncio, os, signal, subprocess, sys, tempfile
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DPORT, MPORT, TOKEN = "8858", "8859", "root-mcp-factory-tools"
URL = f"http://127.0.0.1:{MPORT}/mcp"
D = f"http://127.0.0.1:{DPORT}"

EXPECTED = [
    "add_check", "add_grant", "add_to_global", "assign_ticket", "assume", "attach_working_doc",
    "builder_brief", "create_project", "crew_projects", "decide_assumption", "desk_add",
    "desk_answer", "desk_list", "desk_withdraw", "finish_project", "get_crew_settings", "get_doc",
    "get_integrations", "get_station", "get_ticket", "hand_over", "hand_project_to_crew",
    "how_is_project", "link_tickets", "list_assumptions", "list_crew", "list_decisions",
    "list_docs", "list_milestones", "list_people", "list_projects", "list_secrets", "list_tickets",
    "merge_ready", "my_work", "pick_up", "project_timeline", "read_global_docs", "read_grants",
    "recall", "record_milestone_check", "record_review", "recurring_findings", "release_project",
    "release_ready", "remember_for_project", "review_brief", "review_queue", "send_to_station",
    "set_crew_settings", "set_later", "set_required_checks", "set_session_flow", "set_stage",
    "start_project", "what_needs_me", "write_doc", "write_milestone", "write_repo_map",
    "write_ticket",
]


async def _wait(url, ok=lambda r: r.status_code == 200):
    for _ in range(120):
        try:
            async with httpx.AsyncClient() as cx:
                if ok(await cx.get(url, timeout=1)):
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def _spawn(code, env):
    return subprocess.Popen([sys.executable, "-c", code], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(*procs):
    for p in procs:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


async def names() -> list[str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(URL, headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return [t.name for t in (await s.list_tools()).tools]


async def main():
    tmp = tempfile.mkdtemp(prefix="mcp-factory-tools-")
    base = clean_env(TARES_AUTH_TOKEN=TOKEN, TARES_EDITION="factory")
    d = _spawn("from tares.cli import run_daemon; run_daemon()",
               {**base, "TARES_DB": os.path.join(tmp, "t.duckdb"), "TARES_PORT": DPORT})
    m = _spawn("from tares.cli import run_mcp; run_mcp()",
               {**base, "TARES_MCP_TRANSPORT": "streamable-http", "TARES_MCP_HOST": "127.0.0.1",
                "TARES_MCP_PORT": MPORT, "TARES_MCP_SURFACE_TTL": "0", "TARESD_URL": D})
    try:
        up = await _wait(f"{D}/health") and await _wait(URL, ok=lambda r: r.status_code < 500)
        ck("daemon and MCP server up", up)
        if up:
            got = sorted(await names())
            ck("tools/list on a factory cell is exactly the expected list", got == EXPECTED,
               f"added {sorted(set(got) - set(EXPECTED))}, missing {sorted(set(EXPECTED) - set(got))}")
    finally:
        _stop(m, d)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
