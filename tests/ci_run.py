"""Run CI's test list, or one shard of it.

    python tests/ci_run.py                       # everything, in order
    python tests/ci_run.py --shard 2 --of 5      # the 2nd of 5 shards (1-based)
    python tests/ci_run.py --suite postgres ...  # the list for the Postgres run

Every test file CI runs is listed here; add a new one or it never runs. Shards are balanced by
WEIGHT (seconds a test took on a CI runner); anything unlisted counts as DEFAULT_WEIGHT. A test
that becomes slow should get a weight here so its shard does not finish last. Stops at the first
failure, like the shell loop it replaced."""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

PYTHON = """
test_labels test_normalize test_auth test_api_keys test_pricing test_agents test_catalog_sync
test_cli test_status test_usage test_slack test_slack_verify test_slack_finding_post test_slack_ask
test_degraded test_loki test_projects test_project_timeline test_rius_rca test_rca_callback_anchor
test_ingest_bypass_cooldown test_agent_stats test_agent_conclude test_schedule_trigger
test_watch_escalate test_run_results test_model_provider test_openai_provider test_model_providers
test_metrics test_ai_sre_demo test_seed test_session_flow test_challenger_workflow
test_plugin_challenger test_clean_env test_tracing test_agent_runs_filter test_delete_cascade
test_agent_build test_builder_flow test_http_poll test_agent_daily_cap test_skipped_write_back
test_projects_activity test_projects_unit_upgrade test_upgrade_hard_stop test_startup_fill
test_duckdb_memory test_skills test_handoffs test_project_keys test_goal_first test_setup_flow
test_resources test_wiring test_github_credentials test_github_app test_cloud_connect test_docs
test_linear test_github_tickets test_factory_mcp test_session_project test_pickup test_factory_plan
test_factory_crew test_factory_ledger test_crew_standup test_factory_build test_factory_review
test_factory_builder test_factory_repomap test_factory_desk test_factory_repos test_factory_setup
test_factory_takeover test_mcp_station_headers test_login test_homebrew_formula test_claude_usage
test_factory_batching test_state_postgres test_crews test_my_work test_desk_person test_people
test_recording_person test_person_attribution test_project_access test_areas test_grants_teams
test_private_routes test_private_reads test_personal_docs test_personal_limits test_ticket_assignee
test_teams_e2e test_spec_signoff
""".split()

# tests/factory/<name>_test.sh: the factory CLI, in bash, against a real Tares
FACTORY = """
parse guard heavy tidy spec session tares crew usage freeze idle_wake pace claude_update lsp crewname
""".split()

# Not in the Postgres run: they are about the DuckDB file itself.
DUCKDB_ONLY = {"test_duckdb_memory", "test_state_postgres"}

DEFAULT_WEIGHT = 4
WEIGHT = {
    "factory:claude_update": 240, "factory:idle_wake": 235, "factory:pace": 225,
    "factory:crew": 100, "test_watch_escalate": 70, "test_slack": 65, "factory:freeze": 60,
    "test_project_keys": 19, "test_agents": 15, "test_plugin_challenger": 11, "test_tracing": 11,
    "factory:lsp": 11, "test_seed": 10, "factory:tares": 10, "test_login": 9, "test_usage": 9,
    "test_slack_ask": 9, "factory:usage": 9, "test_github_tickets": 8, "test_degraded": 8,
}


def items(suite):
    py = [t for t in PYTHON if suite == "duckdb" or t not in DUCKDB_ONLY]
    return py + [f"factory:{t}" for t in FACTORY]


def shard(all_items, n, of):
    """Longest first, each onto the lightest shard; then back in list order within the shard."""
    order = {t: i for i, t in enumerate(all_items)}
    bins = [[0, []] for _ in range(of)]
    for t in sorted(all_items, key=lambda t: -WEIGHT.get(t, DEFAULT_WEIGHT)):
        b = min(bins, key=lambda b: b[0])
        b[0] += WEIGHT.get(t, DEFAULT_WEIGHT)
        b[1].append(t)
    return sorted(bins[n - 1][1], key=order.get)


def run(t):
    if t.startswith("factory:"):
        name = t.split(":", 1)[1]
        print(f"== factory {name} ==", flush=True)
        return subprocess.call(["bash", os.path.join(HERE, "factory", f"{name}_test.sh")])
    print(f"== {t} ==", flush=True)
    return subprocess.call([sys.executable, os.path.join(HERE, f"{t}.py")])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--suite", choices=["duckdb", "postgres"], default="duckdb")
    p.add_argument("--shard", type=int, default=1)
    p.add_argument("--of", type=int, default=1)
    p.add_argument("--list", action="store_true", help="print the shard's tests, run nothing")
    a = p.parse_args()
    if not 1 <= a.shard <= a.of:
        p.error("--shard must be between 1 and --of")
    mine = shard(items(a.suite), a.shard, a.of)
    if a.list:
        print("\n".join(mine))
        return 0
    for t in mine:
        rc = run(t)
        if rc:
            print(f"FAILED: {t} (exit {rc})", flush=True)
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
