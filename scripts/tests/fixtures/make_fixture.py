# /// script
# requires-python = ">=3.11"
# dependencies = ["tares==1.61.0", "psycopg[binary]>=3.1"]
# ///
"""Make the small Tares 1.61.0 cell the migration test reads (TR-1821), with Tares 1.61.0 itself.

    uv run scripts/tests/fixtures/make_fixture.py sqlite
    uv run scripts/tests/fixtures/make_fixture.py postgres --dsn postgresql://postgres:pg@127.0.0.1:55499/postgres \\
        --pg-dump "docker exec -i <container> pg_dump -U postgres"

`sqlite` writes cell-1.61.0-sqlite.tar.gz here: a stopped 1.61.0 data directory (tares.sqlite,
tares.duckdb and the archive folder). `postgres` makes the same cell on Postgres, in the schema
`tares161fixture` (dropped first, dropped again after), and writes cell-1.61.0-postgres.tar.gz (the
data directory: tares.duckdb and the archive) and cell-1.61.0-postgres.sql.gz (pg_dump of the
schema, plain INSERTs, no owner). The test restores the dump into a schema of its own by replacing the
schema name. cell-1.61.0.json says what the cell holds for the test (keys and secrets made up here).

What the cell holds, through 1.61.0's own Store and claude_code connector where it can:
- a person, an API key with a known secret, a GitHub App credential with a webhook secret and an
  installation, the github_app catalog source with a known ingest key, a polling github source;
- two factory projects with milestones, tickets, history, a review with findings, a desk item and a
  decision, docs (one shared by both), a skill, members and log lines; the default project (recipe
  default) and a challenger_workflow project (not a factory one); a platform object (kind source);
- sessions posted as Claude Code transcript lines (prompts, replies with usage, Bash calls with
  results, a SendMessage, a station and a project line), one of them under a second claude_code
  source with redact off and thinking on, and one 10 days old that the archive moves into a part;
- the 1.61.0 session move run to the end; then what only a long-lived cell has: a session whose
  lines are partly in DuckDB alone (lines_complete false), a session with the legacy complete flag
  on fewer rows, a challenger line and a model reply only in DuckDB, and GitHub pull_request events.

The output is the same on every run but for the ids 1.61.0 makes itself (docs, reviews, keys).

A full-size cell, for a run by hand (not in the repo, not in CI):

    uv run scripts/tests/fixtures/make_fixture.py seeded DATA_DIR [--dsn DSN_WITH_SEARCH_PATH]

fills DATA_DIR (and, with --dsn, that Postgres schema) with 1.61.0's own factory seed
(ee/tests/factory_seed.py at the tag v1.61.0, read with `git show`, with the licence helpers it
needs: about 1.4 million events, the row counts of a real factory cell), then runs 1.61.0's session
move to the end, as a 1.61.0 cell does after its start.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
SCHEMA = "tares161fixture"
API_SECRET = "tares-fixture-api-secret-0001"
WEBHOOK_SECRET = "fixture-webhook-secret"
INGEST_KEY = "github_app-fixture00000001"
INSTALLATION = 4242
REPO = "example-org/lantern"


def iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def transcript(sid: str, start: datetime, n_turns: int, project: str | None, station: str | None,
               prefix: str) -> list[dict]:
    """A Claude Code transcript of `n_turns` turns: a prompt, a reply with usage, a Bash call and its
    result; a SendMessage at the end."""
    out = []
    t = start
    k = 0

    def line(kind, msg, **kw):
        nonlocal k, t
        k += 1
        t = t + timedelta(seconds=30)
        d = {"type": kind, "uuid": f"{prefix}-{k:04d}", "sessionId": sid, "timestamp": iso(t),
             "cwd": "/work/lantern", "gitBranch": "factory/LAN-1", "message": msg, **kw}
        if project:
            d["tares_project"] = project
        if station:
            d["factory_station"] = station
        out.append(d)
    if station:
        out.append({"type": "session_station", "sessionId": sid, "timestamp": iso(t), "station": station,
                    "role": "builder", "parent": "lantern-orchestrator", "tares_project": project or ""})
    if project:
        out.append({"type": "session_project", "sessionId": sid, "timestamp": iso(t),
                    "tares_project": project, "cwd": "/work/lantern"})
    for i in range(n_turns):
        line("user", {"role": "user", "content": f"please run the checks, round {i + 1}"})
        line("assistant", {"id": f"msg_{prefix}_{i}", "model": "claude-opus-5-5", "role": "assistant",
                           "content": [{"type": "thinking", "thinking": "the tests first"},
                                       {"type": "text", "text": f"I will run the tests (round {i + 1})."}],
                           "usage": {"input_tokens": 100 + i, "output_tokens": 20,
                                     "cache_creation_input_tokens": 5, "cache_read_input_tokens": 50}})
        tool = f"toolu_{prefix}_{i}"
        line("assistant", {"id": f"msg_{prefix}_{i}b", "model": "claude-opus-5-5", "role": "assistant",
                           "content": [{"type": "tool_use", "id": tool, "name": "Bash",
                                        "input": {"command": "pytest -q tests/test_cart.py"}}],
                           "usage": {"input_tokens": 10, "output_tokens": 5}})
        line("user", {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool,
                                                   "content": "12 passed in 3.1s", "is_error": False}]})
    line("assistant", {"id": f"msg_{prefix}_send", "model": "claude-opus-5-5", "role": "assistant",
                       "content": [{"type": "tool_use", "id": f"toolu_{prefix}_send", "name": "SendMessage",
                                    "input": {"to": "lantern-orchestrator",
                                              "message": "[TF:DONE] LAN-1 is ready for review"}}],
                       "usage": {"input_tokens": 10, "output_tokens": 5}})
    return out


def build(data: str, dsn: str | None) -> dict:
    os.environ["TZ"] = "UTC"
    for k in [k for k in os.environ if k.startswith(("TARES_", "FACTORY_"))]:
        del os.environ[k]
    if dsn:
        os.environ["TARES_ARCHIVE_URL"] = "file://" + os.path.join(data, "archive")
    from sqlalchemy import delete, insert, select, update
    from tares import session_archive, session_move
    from tares.config import SourceCfg
    from tares.connectors.claude_code import ClaudeCodeConnector
    from tares.envelope import Envelope
    from tares.session_store import tables as T
    from tares.store import Store

    store = Store(os.path.join(data, "tares.duckdb"), state_dsn=dsn)
    store.set_setting("edition", "factory")
    db = store.sessions_db
    ada = store.create_person("Ada Quill", "ada.quill@example.com", is_admin=True)["id"]
    store.insert_api_key("key_fixture01", "ada-laptop", "nvf_key_fixture01",
                         hashlib.sha256(API_SECRET.encode()).hexdigest(), ["ingest", "read"], person_id=ada)
    # GitHub: an App credential with a webhook secret, its catalog source, and a polling source
    store.upsert_github_credential("fx-app", "", kind="app", account="example-org", config={
        "app_id": 99, "slug": "fixture-app", "webhook_secret": WEBHOOK_SECRET,
        "installations": [{"id": INSTALLATION, "account": "example-org", "account_type": "Organization",
                           "repository_selection": "all"}]})
    store.upsert_catalog_source("github", "event_stream", "github_app", "5s", {"credential": "fx-app"},
                                ingest_key=INGEST_KEY)
    store.upsert_github_credential("fx-token", "ghp_fixture", kind="token")
    store.upsert_catalog_source("gh-poll", "event_stream", "github", "60s", {"credential": "fx-token",
                                                                             "repo": REPO})
    store.upsert_catalog_source("claude_code", "event_stream", "claude_code", "5s", {"push": True})
    store.upsert_catalog_source("cc_old", "event_stream", "claude_code", "5s",
                                {"push": True, "redact": False, "include_thinking": True})
    # projects
    store.create_project("uc_lantern01", "custom", "Lantern billing", {}, goal="Make billing fast.",
                         lead_person_id=ada)
    store.set_project_kind("uc_lantern01", "software_factory")
    store.create_project("uc_harbor002", "custom", "Harbor search", {}, goal="Search that answers.")
    store.set_project_kind("uc_harbor002", "software_factory")
    store.create_project("uc_default03", "default", "Default", {})
    store.create_project("uc_challeng4", "challenger_workflow", "Challenger", {}, goal="Challenge commits.")
    store.add_project_member("uc_harbor002", ada)
    spec = store.create_doc("uc_lantern01", "spec", "Lantern spec", "The spec of billing.")
    store.use_doc("uc_harbor002", spec)
    store.create_doc("uc_harbor002", "plan", "Harbor plan", "The plan.")
    store.upsert_skill("uc_lantern01", "deploy", "How to deploy", "Run make deploy.")
    store.log_project("uc_lantern01", "updated", "goal set")
    with db.begin() as c:
        c.execute(insert(T.usecase_objects).values(usecase_id="uc_lantern01", kind="source",
                                                    key="source:gh-poll", name="gh-poll", customized=False,
                                                    created_at=NOW))
    ms = store.create_milestone("uc_lantern01", "M1: first pages", "The pages are live.",
                                [{"check": "open the home page", "for": "it loads"}])
    tickets = [store.create_ticket("uc_lantern01", "tares", f"Ticket {i}", milestone=ms, repo=REPO)
               for i in range(3)]
    store.update_ticket(tickets[0], stage="merged", status="done")
    store.update_ticket(tickets[1], stage="review", status="in_progress", holder="lantern-build-m1",
                        pr={"url": f"https://github.com/{REPO}/pull/7", "repo": REPO, "number": 7,
                            "head": "a" * 40, "state": "open", "merged": False, "ci": "success"})
    store.add_ticket_history(tickets[1], "uc_lantern01", "stage", "review", None, "lantern-build-m1",
                             "builder", NOW - timedelta(hours=3))
    store.add_review("uc_lantern01", tickets[1], f"https://github.com/{REPO}/pull/7", "a" * 40, "changes",
                     "the tests", "the docs", "lantern-reviewer",
                     [{"severity": "P2", "file": "cart.py", "line": 12, "blocking": True,
                       "text": "The empty list is not handled.", "kind": "null or empty"}],
                     {}, lambda text: "null or empty", NOW - timedelta(hours=2), person_id=ada)
    desk = store.add_desk_item("uc_lantern01", "Which date format?", "exports", ["ISO", "US"], "ISO",
                               "it sorts", "", [tickets[2]], False, "lantern-build-m1", [],
                               NOW - timedelta(hours=1))
    store.add_decision("uc_lantern01", desk["id"], "Which date format?", "ISO please", "ISO", False,
                       "project", True, None, False, [tickets[2]], NOW - timedelta(minutes=30), via="console",
                       person_id=ada)
    # sessions, through 1.61.0's connector
    cc = ClaudeCodeConnector(SourceCfg("claude_code", "event_stream", "claude_code", 5.0, {"push": True}), store)
    old = ClaudeCodeConnector(SourceCfg("cc_old", "event_stream", "claude_code", 5.0,
                                        {"push": True, "redact": False, "include_thinking": True}), store)
    s_main, s_two, s_old, s_arch = ("11111111-0000-4000-a000-000000000001", "11111111-0000-4000-a000-000000000002",
                                    "11111111-0000-4000-a000-000000000003", "11111111-0000-4000-a000-000000000004")
    store.append(cc.map_payload(transcript(s_main, NOW - timedelta(hours=5), 3, "uc_lantern01",
                                           "lantern-build-m1", "a")))
    store.append(cc.map_payload(transcript(s_two, NOW - timedelta(hours=4), 2, "uc_harbor002", None, "b")))
    store.append(old.map_payload(transcript(s_old, NOW - timedelta(hours=3), 2, None, None, "c")))
    store.append(cc.map_payload(transcript(s_arch, NOW - timedelta(days=10), 2, "uc_lantern01", None, "d")))
    # 1.61.0's own session move, to the end, then its archive pass
    session_move.plan(store)
    for _ in range(10):
        session_move.move_recent(store)
        session_move.move_old(store)
        if session_move.complete(store):
            break
    session_archive.archive_pass(store, now=NOW)
    # what a long-lived cell has besides: lines DuckDB alone holds, and older flags
    s_part, s_legacy = "11111111-0000-4000-a000-000000000005", "11111111-0000-4000-a000-000000000006"
    part_lines = cc.map_payload(transcript(s_part, NOW - timedelta(hours=2), 2, None, None, "e"))
    store.append(part_lines)
    store.append(cc.map_payload(transcript(s_legacy, NOW - timedelta(hours=1), 1, None, None, "f")))

    def duck_insert(envs: list[Envelope]) -> None:
        for e in envs:
            store.con.execute(
                "INSERT INTO events (source, source_type, key_value, event_type, text, payload, labels, "
                "event_time, ingest_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [e.source, e.source_type, e.key_value, e.event_type, e.text, json.dumps(e.payload),
                 json.dumps(e.labels), e.event_time, e.event_time])
    with db.begin() as c:
        # s_part: every line is in DuckDB, half of them in the table (a write that failed half way)
        t = T.session_lines
        keep = c.execute(select(t.c.seq).where(t.c.session == s_part).order_by(t.c.seq)).scalars().all()
        c.execute(delete(t).where(t.c.session == s_part, t.c.seq > len(keep) // 2))
        c.execute(update(T.sessions).where(T.sessions.c.session == s_part).values(lines_complete=False))
        # s_legacy: complete on the flag, one row short, nothing in DuckDB
        c.execute(delete(t).where(t.c.session == s_legacy, t.c.seq == 1))
    duck_insert(part_lines)
    ch = Envelope(source="claude_code", source_type="event_stream", key_value=s_main,
                  event_type="challenge_commit", text="challenge PASS",
                  event_time=NOW - timedelta(minutes=50),
                  payload={"challenge": {"verdict": "PASS", "sha": "b" * 40, "findings": []}},
                  labels={"branch": "factory/LAN-1", "tares_project": "uc_lantern01", "verdict": "PASS"})
    reply = Envelope(source="claude_code", source_type="event_stream", key_value=s_main, event_type="assistant",
                     text="a reply DuckDB alone holds", event_time=NOW - timedelta(minutes=40),
                     payload={"type": "assistant", "uuid": "only-duck-0001", "sessionId": s_main,
                              "message": {"id": "msg_only_duck", "model": "claude-sonnet-5-5",
                                          "usage": {"input_tokens": 7, "output_tokens": 3}}},
                     labels={"session": s_main})
    duck_insert([ch])
    with store._lock:
        store.con.execute(
            "INSERT INTO events (source, source_type, key_value, event_type, text, payload, labels, "
            "event_time, ingest_time) VALUES ('claude_code', 'event_stream', ?, 'assistant', ?, ?, ?, ?, ?)",
            [s_main, reply.text, json.dumps(reply.payload), json.dumps(reply.labels), reply.event_time,
             reply.event_time])
    prs = []
    for i, (number, branch, action, days) in enumerate(((7, "factory/LAN-1", "opened", 3),
                                                       (7, "factory/LAN-1", "synchronize", 2),
                                                       (8, "factory/LAN-2", "opened", 1),
                                                       (3, "factory/OLD-1", "closed", 60))):
        at = NOW - timedelta(days=days)
        prs.append(Envelope(source="github", source_type="event_stream", key_value=f"{REPO}#{number}",
                            event_type="pull_request", text=f"PR #{number} {action}", event_time=at,
                            payload={"action": action, "_github_event": "pull_request",
                                     "_github_delivery": f"delivery-{i:04d}",
                                     "pull_request": {"number": number, "head": {"ref": branch}}},
                            labels={"repo": REPO, "number": number, "branch": branch, "action": action}))
    duck_insert(prs)
    store.con.execute("CHECKPOINT")
    store.close()
    store.con.close()
    return {"version": "1.61.0", "api_secret": API_SECRET, "webhook_secret": WEBHOOK_SECRET,
            "ingest_key": INGEST_KEY, "installation": INSTALLATION, "repo": REPO,
            "credential": "fx-app", "now": NOW.isoformat(),
            "projects": {"factory": ["uc_lantern01", "uc_harbor002"], "default": "uc_default03",
                         "non_factory": ["uc_challeng4"]},
            "sessions": {"main": s_main, "two": s_two, "old_source": s_old,
                                               "archived": s_arch, "partial": s_part, "legacy": s_legacy},
            "pr_events_since": (NOW - timedelta(days=30)).isoformat()}


def seeded(data: str, dsn: str | None) -> None:
    """The 1.61.0 factory seed in `data`, then the session move to the end."""
    import asyncio
    import time
    kit = tempfile.mkdtemp(prefix="seed161-")
    repo = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    for path in ("ee/tests/factory_seed.py", "ee/tests/licensed.py", "tests/_env.py", "tests/licence_testkit.py"):
        os.makedirs(os.path.join(kit, os.path.dirname(path)), exist_ok=True)
        with open(os.path.join(kit, path), "wb") as f:
            f.write(subprocess.run(["git", "show", f"v1.61.0:{path}"], cwd=repo, check=True,
                                   capture_output=True).stdout)
    sys.path.insert(0, os.path.join(kit, "ee", "tests"))
    import licensed
    licensed.install_test_root()
    import factory_seed as fs
    os.makedirs(data, exist_ok=True)
    os.environ["TZ"] = "UTC"
    time.tzset()
    os.environ.update({"TARES_DB": os.path.join(data, "tares.duckdb"), "TARES_CATALOG": os.path.join(data, "none.yaml"),
                       "TARES_LICENSE": licensed.test_licence(), "TARES_AUTH_TOKEN": "root-token-seed",
                       "TARES_OTLP_GRPC_PORT": "off", "TARES_EDITION": "factory"})
    if dsn:
        os.environ["TARES_STATE_DSN"] = dsn
        os.environ["TARES_ARCHIVE_URL"] = "file://" + os.path.join(data, "archive")

    async def fill():
        from tares.daemon import make_app
        fs.freeze_clock()
        app = make_app()
        async with app.router.lifespan_context(app):
            fs.seed(app.state.store)
    asyncio.run(fill())
    from tares import session_move
    from tares.store import Store
    store = Store(os.environ["TARES_DB"], state_dsn=dsn)
    session_move.plan(store)
    while not session_move.complete(store):
        if not session_move.move_recent(store, limit=50) + session_move.move_old(store, limit=50) \
                and not session_move.retrying(store):
            break
    print("session move:", session_move.counts(store))
    store.con.execute("CHECKPOINT")
    store.close()
    store.con.close()
    shutil.rmtree(kit, ignore_errors=True)


def tar_dir(data: str, path: str) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name in sorted(os.listdir(data)):
            if name.startswith(".duckdb_tmp"):
                continue
            tf.add(os.path.join(data, name), arcname=name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=("sqlite", "postgres", "seeded"))
    ap.add_argument("data", nargs="?", help="seeded: the data directory to fill")
    ap.add_argument("--dsn", help="a Postgres the postgres fixture is made in")
    ap.add_argument("--pg-dump", default="pg_dump", help="the pg_dump command, with its connection options")
    args = ap.parse_args()
    if args.kind == "seeded":
        if not args.data:
            ap.error("seeded needs DATA_DIR")
        seeded(os.path.abspath(args.data), args.dsn)
        return 0
    data = tempfile.mkdtemp(prefix="tares161-")
    try:
        if args.kind == "sqlite":
            meta = build(data, None)
            tar_dir(data, os.path.join(HERE, "cell-1.61.0-sqlite.tar.gz"))
        else:
            import psycopg
            if not args.dsn:
                ap.error("postgres needs --dsn")
            with psycopg.connect(args.dsn, autocommit=True) as c:
                c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
                c.execute(f"CREATE SCHEMA {SCHEMA}")
            try:
                sep = "&" if "?" in args.dsn else "?"
                meta = build(data, f"{args.dsn}{sep}options=-csearch_path%3D{SCHEMA}")
                dump = subprocess.run(shlex.split(args.pg_dump) + [
                    "--schema", SCHEMA, "--inserts", "--no-owner", "--no-privileges",
                    urllib.parse.urlparse(args.dsn).path.lstrip("/") or "postgres"],
                    capture_output=True)
                if dump.returncode:
                    raise SystemExit(f"pg_dump failed: {dump.stderr.decode()[-2000:]}")
                dump = dump.stdout
                with gzip.GzipFile(os.path.join(HERE, "cell-1.61.0-postgres.sql.gz"), "wb", mtime=0) as f:
                    f.write(dump)
                tar_dir(data, os.path.join(HERE, "cell-1.61.0-postgres.tar.gz"))
            finally:
                with psycopg.connect(args.dsn, autocommit=True) as c:
                    c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        with open(os.path.join(HERE, "cell-1.61.0.json"), "w") as f:
            json.dump({**meta, "schema": SCHEMA}, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"fixture written ({args.kind})")
        return 0
    finally:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
