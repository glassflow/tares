# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1", "sqlalchemy>=2.0", "psycopg[binary]>=3.1", "alembic>=1.13", "httpx>=0.27"]
# ///
"""scripts/migrate_to_2.py moves a 1.61.0 cell to the 2.0 baseline (TR-1821).

The input is the small 1.61.0 cell under fixtures/ (make_fixture.py made it with Tares 1.61.0), on
SQLite and, with TARES_TEST_PG, on Postgres. For each:
- `--check` passes, prints the plan and leaves the input as it was;
- `--apply` into a new data directory (or a new schema) passes its own verification, stamps
  0001_baseline, and `--verify` passes again;
- the output holds what the cell held, renamed (projects, project_docs, project_skills, project_log,
  integration_keys, pr_events, the sessions under claude_code with the default render flags, the
  partial session completed from DuckDB, the challenger line and the model reply DuckDB alone held);
- a 2.0 daemon (this checkout's tares) started on the output serves a project, a ticket and a
  session's lines; a GitHub webhook delivery signed with the copied secret on the copied key answers
  202 and its pull request lands in pr_events; a plugin post with the copied API key lands.
Then, on SQLite copies of the cell: what the script refuses (a pending or failed move, another
revision, an output that exists), the skills merge for a cell that never shared them,
--drop-non-factory, and a broken rename and a changed session line, which the verification catches by
table name.

Run (the 2.0 daemon needs this checkout installed beside the script's own dependencies):
    uv run --with-editable . scripts/tests/test_migrate_to_2.py
    TARES_TEST_PG=postgresql://postgres:pg@127.0.0.1:55499/postgres uv run --with-editable . scripts/tests/test_migrate_to_2.py
The script itself runs as it would on a cell: `uv run scripts/migrate_to_2.py`, in its own environment.
"""
from __future__ import annotations

import ast
import contextlib
import gzip
import hashlib
import hmac
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
REPO = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(REPO, "scripts", "migrate_to_2.py")
META = json.load(open(os.path.join(FIX, "cell-1.61.0.json")))
PG = os.environ.get("TARES_TEST_PG")
SINCE = META["pr_events_since"]
TOKEN = "root-token-migrate"
FAILS: list[str] = []
SCRATCH = tempfile.mkdtemp(prefix="test-migrate-to-2-")
SCHEMAS: list[str] = []

for k in [k for k in os.environ if k.startswith(("TARES_", "FACTORY_")) and k != "TARES_TEST_PG"]:
    del os.environ[k]


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  {detail}"), flush=True)
    if not ok:
        FAILS.append(name)


def run(*args, expect=None):
    """The script as a cell runs it: uv run, its own environment."""
    p = subprocess.run(["uv", "run", "-q", SCRIPT, *args], capture_output=True, text=True, cwd=REPO)
    out = p.stdout + p.stderr
    if expect is not None and p.returncode != expect:
        print(out[-4000:])
    return p.returncode, out


def unpack(name: str) -> str:
    d = tempfile.mkdtemp(dir=SCRATCH)
    with tarfile.open(os.path.join(FIX, name)) as tf:
        tf.extractall(d, filter="data")
    return d


def files_md5(d: str) -> dict:
    out = {}
    for root, _dirs, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, d)] = hashlib.md5(open(p, "rb").read()).hexdigest()
    return out


def pg_dsn(schema: str) -> str:
    sep = "&" if "?" in PG else "?"
    return f"{PG}{sep}options=-csearch_path%3D{schema}"


def new_schema(prefix: str) -> str:
    s = f"{prefix}_{uuid.uuid4().hex[:10]}"
    SCHEMAS.append(s)
    return s


def restore_pg(schema: str) -> None:
    import psycopg
    sql = gzip.open(os.path.join(FIX, "cell-1.61.0-postgres.sql.gz")).read().decode()
    sql = sql.replace(META["schema"], schema)
    sql = "\n".join(ln for ln in sql.splitlines() if not ln.startswith("\\"))   # psql meta commands
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(sql)


def drop_schemas() -> None:
    if not PG or not SCHEMAS:
        return
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c:
        for s in SCHEMAS:
            c.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')


def engine_of(out: str):
    if out.startswith("postgresql"):
        return sa.create_engine(out.replace("postgresql://", "postgresql+psycopg://", 1))
    return sa.create_engine("sqlite:///" + os.path.join(out, "tares.sqlite"))


def q(engine, sql, **kw):
    with engine.connect() as c:
        return c.execute(sa.text(sql), kw).all()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── the checks ───────────────────────────────────────────────────────────────────────────────
def static_checks():
    print("== the script and the baseline ==")
    tree = ast.parse(open(os.path.join(REPO, "tests", "test_baseline.py")).read())
    baseline = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == "TABLES" for t in n.targets))
    mod = load_script()
    ck("the script's 2.0 table list is tests/test_baseline.py's", sorted(mod.TABLES_2) == sorted(baseline),
       sorted(set(mod.TABLES_2) ^ set(baseline)))
    py = open(os.path.join(REPO, "pyproject.toml")).read()
    ck("scripts/ is not in the wheel (packages are tares and ee/tares_ee)",
       'packages = ["tares", "ee/tares_ee"]' in py and "scripts" not in py.split("[tool.hatch.build.targets.wheel]")[1]
       .split("[tool.hatch.build.targets.wheel.hooks.custom]")[0])
    src = open(SCRIPT).read()
    head = src.split('"""', 2)[2]
    imports = [ln for ln in head.splitlines() if ln.startswith(("import ", "from "))]
    ck("the script imports nothing from tares at the top", not [i for i in imports if "tares" in i], imports)
    ck("the script carries its dependencies (PEP 723)", "# /// script" in src and "duckdb" in src.split("# ///")[1])
    md = sa.MetaData()
    old = sa.Table("t_old", md, sa.Column("id", sa.Text, primary_key=True), sa.Column("gone", sa.Text))
    new = sa.Table("t_new", md, sa.Column("id", sa.Text, primary_key=True),
                   sa.Column("opt", sa.Text), sa.Column("dflt", sa.Integer, nullable=False, server_default="0"),
                   sa.Column("must", sa.Text, nullable=False))
    note, blocked = mod.column_notes(old, new)
    ck("the plan names a dropped column, a new one with its default, and refuses a NOT NULL one with no source",
       "columns dropped: gone" in note and "opt (NULL)" in note and "dflt (its default)" in note
       and blocked == ["must"], (note, blocked))


def load_script():
    spec = importlib.util.spec_from_file_location("migrate_to_2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def content_checks(label: str, out: str, pg: bool):
    e = engine_of(out)
    s = META["sessions"]
    ck(f"{label}: stamped {'0001_baseline'}",
       [r[0] for r in q(e, "SELECT version_num FROM session_store_version")] == ["0001_baseline"])
    ck(f"{label}: 4 projects, the non-factory one kept",
       sorted(r[0] for r in q(e, "SELECT id FROM projects")) ==
       sorted(META["projects"]["factory"] + [META["projects"]["default"]] + META["projects"]["non_factory"]))
    ck(f"{label}: project_docs from usecase_objects kind doc (one doc in two projects)",
       len(q(e, "SELECT project, doc FROM project_docs")) == 3)
    ck(f"{label}: project_skills from kind skill",
       q(e, "SELECT project, skill FROM project_skills") == [("uc_lantern01", "deploy")])
    ck(f"{label}: project_log from usecase_log",
       ("uc_lantern01", "updated") in [tuple(r) for r in q(e, "SELECT project, action FROM project_log")])
    ck(f"{label}: default_project is the recipe default project",
       q(e, "SELECT value FROM settings WHERE key = 'default_project'") == [(META["projects"]["default"],)])
    ck(f"{label}: every session is under claude_code",
       q(e, "SELECT DISTINCT source FROM sessions") == [("claude_code",)])
    ck(f"{label}: every table line has the default render flags",
       q(e, "SELECT DISTINCT render FROM session_lines") == [('{"include_thinking": false, "redact": true}',)])
    ck(f"{label}: the facts of the old source's session are under claude_code",
       q(e, "SELECT DISTINCT source FROM session_commands") == [("claude_code",)])
    part = q(e, "SELECT line_count, lines_complete, lines_origin FROM sessions WHERE session = :s", s=s["partial"])
    held = q(e, "SELECT count(*) FROM session_lines WHERE session = :s", s=s["partial"])[0][0]
    ck(f"{label}: the partial session is completed from DuckDB", part and part[0][0] == held
       and bool(part[0][1]) and part[0][2] == "table" and held > 0, (part, held))
    ck(f"{label}: its facts were rebuilt from DuckDB's lines",
       q(e, "SELECT count(*) FROM session_commands WHERE session = :s", s=s["partial"])[0][0] == 2)
    leg = q(e, "SELECT lines_complete FROM sessions WHERE session = :s", s=s["legacy"])
    ck(f"{label}: the legacy session short of a line is marked incomplete", leg and not leg[0][0], leg)
    ck(f"{label}: integration_keys holds the github_app key, byte for byte",
       [tuple(r) for r in q(e, "SELECT key, integration, credential, source FROM integration_keys")]
       == [(META["ingest_key"], "ci_github", META["credential"], "github")])
    ck(f"{label}: pr_events holds the 3 pull request events of the window",
       sorted(r[0] for r in q(e, "SELECT delivery_id FROM pr_events")) == ["delivery-0000", "delivery-0001", "delivery-0002"])
    ck(f"{label}: the challenger line DuckDB alone held is in challenges",
       len(q(e, "SELECT 1 FROM challenges WHERE event_type = 'challenge_commit'")) == 1)
    u = q(e, "SELECT cost_usd FROM session_usage WHERE message = 'msg_only_duck'")
    ck(f"{label}: the reply DuckDB alone held is in session_usage, priced", u and u[0][0] is not None, u)
    ready = q(e, "SELECT ready_at FROM session_rollup_state WHERE name = 'usage_hourly'")
    ck(f"{label}: session_usage_hourly is built and ready",
       q(e, "SELECT count(*) FROM session_usage_hourly")[0][0] > 0 and ready and ready[0][0] is not None, ready)
    ck(f"{label}: no 1.x table is left",
       not [t for t in sa.inspect(e).get_table_names() if t in ("usecases", "catalog_sources", "skills",
                                                                "session_moves", "session_store_copies")])
    e.dispose()


@contextlib.contextmanager
def daemon(env_extra: dict):
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TARES_", "FACTORY_"))}
    env.update({"TARES_PORT": str(port), "TARES_HOST": "127.0.0.1", "TARES_AUTH_TOKEN": TOKEN,
                "TZ": "UTC", **env_extra})
    log = open(os.path.join(SCRATCH, f"daemon-{port}.log"), "w")
    p = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                         env=env, stdout=log, stderr=subprocess.STDOUT, cwd=SCRATCH)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(base + "/health", timeout=2).status_code in (200, 503):
                    break
            time.sleep(0.5)
        yield base
    finally:
        p.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(20)
        if p.poll() is None:
            p.kill()
        log.close()


def daemon_checks(label: str, env_extra: dict, out: str):
    root = {"Authorization": f"Bearer {TOKEN}"}
    s = META["sessions"]
    with daemon(env_extra) as base:
        h = httpx.get(base + "/health", timeout=10)
        ck(f"{label}: a 2.0 daemon starts on the output", h.status_code == 200, f"{h.status_code} {h.text[:300]}")
        r = httpx.get(f"{base}/api/projects/uc_lantern01", headers=root, timeout=30)
        ck(f"{label}: it serves a project", r.status_code == 200 and "Lantern" in r.text, f"{r.status_code} {r.text[:200]}")
        r = httpx.get(f"{base}/api/projects/uc_lantern01/tickets", headers=root, timeout=30)
        plain = [t for t in (r.json().get("tickets") or []) if not t.get("pr")] if r.status_code == 200 else []
        r = httpx.get(f"{base}/api/projects/uc_lantern01/tickets/{plain[0]['id']}", headers=root, timeout=30) \
            if plain else r
        ck(f"{label}: it serves a ticket", r.status_code == 200 and plain and r.json().get("id") == plain[0]["id"],
           f"{r.status_code} {r.text[:200]}")
        r = httpx.get(f"{base}/api/projects/uc_lantern01/sessions/{s['main']}", headers=root, timeout=30)
        lines = r.json().get("lines") if r.status_code == 200 else None
        ck(f"{label}: it serves a session's lines", bool(lines), f"{r.status_code} {r.text[:200]}")
        r = httpx.get(f"{base}/api/projects/uc_lantern01/sessions/{s['archived']}", headers=root, timeout=30)
        ck(f"{label}: it serves an archived session's lines", r.status_code == 200 and bool(r.json().get("lines")),
           f"{r.status_code} {r.text[:200]}")
        # a GitHub webhook delivery, signed with the copied App secret, on the copied key
        body = json.dumps({"action": "opened", "installation": {"id": META["installation"]},
                           "repository": {"full_name": META["repo"]},
                           "pull_request": {"number": 21, "title": "Replay", "html_url": "https://x/21",
                                            "head": {"ref": "factory/LAN-9", "sha": "c" * 40},
                                            "base": {"ref": "main"}, "user": {"login": "ada"},
                                            "state": "open", "merged": False,
                                            "updated_at": datetime.now(timezone.utc).isoformat()}}).encode()
        sig = "sha256=" + hmac.new(META["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
        r = httpx.post(f"{base}/ingest/{META['ingest_key']}", content=body, timeout=30,
                       headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "replay-0001",
                                "X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        ck(f"{label}: a signed webhook delivery on the copied key answers 202", r.status_code == 202,
           f"{r.status_code} {r.text[:300]}")
        bad = httpx.post(f"{base}/ingest/{META['ingest_key']}", content=body, timeout=30,
                         headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "replay-0002",
                                  "X-Hub-Signature-256": "sha256=" + "0" * 64})
        ck(f"{label}: a delivery signed with another secret answers 401", bad.status_code == 401, bad.status_code)
        # a plugin post with the copied API key
        sid = "22222222-0000-4000-a000-000000000001"
        line = {"type": "user", "uuid": "after-migration-1", "sessionId": sid,
                "timestamp": datetime.now(timezone.utc).isoformat(), "cwd": "/work/lantern",
                "message": {"role": "user", "content": "hello from 2.0"}}
        r = httpx.post(f"{base}/ingest/claude_code", content=json.dumps(line), timeout=30,
                       headers={"Authorization": f"Bearer {META['api_secret']}"})
        ck(f"{label}: a plugin post with the copied key answers 202", r.status_code == 202, f"{r.status_code} {r.text[:300]}")
    e = engine_of(out)
    ck(f"{label}: the replayed pull request is in pr_events",
       q(e, "SELECT repo, number, branch FROM pr_events WHERE delivery_id = 'replay-0001'")
       == [(META["repo"], 21, "factory/LAN-9")])
    ck(f"{label}: the posted line landed under claude_code",
       q(e, "SELECT source, line_count FROM sessions WHERE session = :s", s=sid) == [("claude_code", 1)])
    e.dispose()


def migrate(label: str, args_in: list[str], out: str, input_dir: str, pg: bool):
    before = files_md5(input_dir)
    rc, text = run("--check", *args_in, "--pr-events-since", SINCE, expect=0)
    ck(f"{label}: --check passes", rc == 0, text[-1500:])
    for row in ("projects                  rename   usecases", "project_docs              rename   usecase_objects",
                "integration_keys          build    catalog_sources", "pr_events                 build    DuckDB events",
                "session_usage_hourly      rebuild"):
        ck(f"{label}: --check prints {row.split()[0]} ({row.split()[1]})", row in text, row)
    ck(f"{label}: --check lists what is left behind",
       "catalog_sources" in text.split("Left behind")[1] and "session_moves" in text.split("Left behind")[1])
    if not pg:
        ck(f"{label}: --check writes nothing", not os.path.exists(out) and files_md5(input_dir) == before)
    rc, text = run("--apply", *args_in, "--to", out, "--pr-events-since", SINCE, expect=0)
    ck(f"{label}: --apply passes its verification and stamps", rc == 0 and "stamped 0001_baseline" in text, text[-2500:])
    ck(f"{label}: the input is as it was", files_md5(input_dir) == before)
    rc, text = run("--verify", *args_in, "--to", out, "--pr-events-since", SINCE, expect=0)
    ck(f"{label}: --verify passes on the output", rc == 0 and "Verified" in text, text[-1500:])
    return text


def sqlite_suite():
    print("== a SQLite cell ==")
    src = unpack("cell-1.61.0-sqlite.tar.gz")
    out = os.path.join(SCRATCH, "out-sqlite")
    migrate("SQLite", [src], out, src, False)
    content_checks("SQLite", out, False)
    ck("SQLite: the archive files went along, byte for byte",
       files_md5(os.path.join(out, "archive")) == files_md5(os.path.join(src, "archive")))
    daemon_checks("SQLite", {"TARES_HOME": out}, out)


def postgres_suite():
    print("== a Postgres cell ==")
    if not PG:
        print("  skip  TARES_TEST_PG is not set")
        return
    src_schema, out_schema = new_schema("m2in"), new_schema("m2out")
    restore_pg(src_schema)
    data = unpack("cell-1.61.0-postgres.tar.gz")
    archive = "file://" + os.path.join(data, "archive")
    args_in = [data, "--state-dsn", pg_dsn(src_schema), "--archive-url", archive]
    out = pg_dsn(out_schema)
    migrate("Postgres", args_in, out, data, True)
    content_checks("Postgres", out, True)
    daemon_checks("Postgres", {"TARES_STATE_DSN": out, "TARES_HOME": os.path.join(SCRATCH, "pg-home"),
                               "TARES_ARCHIVE_URL": archive}, out)


def variant(edit_sql: list[str] | None = None) -> str:
    d = unpack("cell-1.61.0-sqlite.tar.gz")
    if edit_sql:
        c = sqlite3.connect(os.path.join(d, "tares.sqlite"))
        for stmt in edit_sql:
            c.execute(stmt)
        c.commit()
        c.close()
    return d


def refusals():
    print("== what the script refuses ==")
    s = META["sessions"]
    d = variant([f"INSERT INTO session_moves (session, state, target) VALUES ('{s['main']}', 'pending', 'table')"])
    rc, text = run("--check", d)
    ck("a pending session move is refused", rc == 2 and "pending" in text.split("Refused")[-1], text[-800:])
    d = variant([f"INSERT INTO session_moves (session, state, target) VALUES ('{s['partial']}', 'failed', 'table')"])
    rc, text = run("--check", d)
    ck("a failed session move is refused", rc == 2 and "--rebuild-failed-moves" in text, text[-800:])
    out = os.path.join(SCRATCH, "out-failed")
    rc, text = run("--apply", d, "--to", out, "--rebuild-failed-moves", "--pr-events-since", SINCE, expect=0)
    ck("with --rebuild-failed-moves it is rebuilt from DuckDB and verified", rc == 0, text[-1500:])
    d = variant(["UPDATE session_store_version SET version_num = '0023_brain_tables'"])
    rc, text = run("--check", d)
    ck("another revision is refused", rc == 2 and "0023_brain_tables" in text, text[-800:])
    d = variant(["DELETE FROM session_store_copies WHERE table_name = 'tickets'"])
    rc, text = run("--check", d)
    ck("a table 1.61.0 has not copied from DuckDB is refused", rc == 2 and "tickets" in text.split("Refused")[-1],
       text[-800:])
    d = variant(["CREATE TABLE stray (x TEXT)"])
    rc, text = run("--check", d)
    ck("a table 1.61.0 does not have is refused", rc == 2 and "stray" in text, text[-800:])
    d = variant()
    taken = os.path.join(SCRATCH, "taken")
    os.makedirs(taken)
    open(os.path.join(taken, "x"), "w").write("x")
    rc, text = run("--apply", d, "--to", taken)
    ck("an output folder that is not empty is refused", rc == 2 and "not empty" in text, text[-800:])
    if PG:
        sch = new_schema("m2taken")
        import psycopg
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f'CREATE SCHEMA "{sch}"')
            c.execute(f'CREATE TABLE "{sch}".x (a int)')
        rc, text = run("--apply", d, "--to", pg_dsn(sch))
        ck("an output schema that holds a table is refused", rc == 2 and "new schema" in text, text[-800:])


def skills_and_drop():
    print("== skills never shared, and --drop-non-factory ==")
    d = variant(["DELETE FROM settings WHERE key = 'skills_shared'",
                 "INSERT INTO skills (project, name, description, body, created_at, updated_at) VALUES "
                 "('uc_harbor002', 'deploy', 'Harbor deploy', 'Run harbor deploy.', '2026-11-01 10:00:00.000000', "
                 "'2026-11-01 10:00:00.000000')",
                 "INSERT INTO skills (project, name, description, body, created_at, updated_at) VALUES "
                 "('uc_lantern01', 'review', 'How to review', 'Read the diff.', '2026-11-02 10:00:00.000000', "
                 "'2026-11-02 10:00:00.000000')"])
    out = os.path.join(SCRATCH, "out-skills")
    rc, text = run("--apply", d, "--to", out, "--pr-events-since", SINCE, expect=0)
    ck("a cell that never shared its skills is moved and verified", rc == 0, text[-1500:])
    if rc == 0:
        e = engine_of(out)
        ck("the old skills join shared_skills, the clash as deploy-2",
           sorted(r[0] for r in q(e, "SELECT name FROM shared_skills")) == ["deploy", "deploy-2", "review"],
           q(e, "SELECT name FROM shared_skills"))
        ck("each project uses its own skill",
           sorted(tuple(r) for r in q(e, "SELECT project, skill FROM project_skills")) ==
           [("uc_harbor002", "deploy-2"), ("uc_lantern01", "deploy"), ("uc_lantern01", "review")])
        e.dispose()
    out = os.path.join(SCRATCH, "out-drop")
    rc, text = run("--apply", variant(), "--to", out, "--drop-non-factory", "--pr-events-since", SINCE, expect=0)
    ck("--drop-non-factory is moved and verified", rc == 0, text[-1500:])
    if rc == 0:
        e = engine_of(out)
        ck("the challenger project is dropped, the default one kept",
           sorted(r[0] for r in q(e, "SELECT id FROM projects")) ==
           sorted(META["projects"]["factory"] + [META["projects"]["default"]]))
        e.dispose()


def broken():
    print("== a broken rename fails, naming the table ==")
    mod = load_script()
    orig = mod.Copier.projects

    def lossy(self):
        orig(self)
        with self.out.engine.begin() as c:
            c.execute(self.out.t("projects").delete().where(self.out.t("projects").c.id == "uc_harbor002"))
    mod.Copier.projects = lossy
    out = os.path.join(SCRATCH, "out-broken")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["--apply", variant(), "--to", out, "--pr-events-since", SINCE])
    text = buf.getvalue()
    mod.Copier.projects = orig
    ck("a project lost in the rename fails the run", rc == 1 and "FAIL  projects:" in text, text[-1500:])
    ck("the failed output is not stamped and not renamed",
       not os.path.exists(out) and os.path.exists(out + ".migrating"))

    orig_s = mod.Copier.sessions

    def changed(self):
        orig_s(self)
        lt = self.out.t("session_lines")
        with self.out.engine.begin() as c:
            c.execute(lt.update().where(lt.c.session == META["sessions"]["main"], lt.c.seq == 2)
                      .values(payload='{"changed": true}'))
    mod.Copier.sessions = changed
    out = os.path.join(SCRATCH, "out-broken-lines")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["--apply", variant(), "--to", out, "--pr-events-since", SINCE])
    mod.Copier.sessions = orig_s
    text = buf.getvalue()
    ck("a changed session line fails the run", rc == 1 and "FAIL  session_lines:" in text, text[-1500:])


def duck_edit(d: str, stmts: list[tuple]) -> None:
    import duckdb
    c = duckdb.connect(os.path.join(d, "tares.duckdb"))
    for stmt in stmts:
        c.execute(*stmt)
    c.execute("CHECKPOINT")
    c.close()


def review_fixes():
    print("== NULs, coordination calls, dropped projects, archived sessions, a file as output ==")
    s = META["sessions"]
    # a session completed from DuckDB whose Bash command holds a NUL, into Postgres
    if PG:
        d = variant()
        # in a command, a tool_use id, a prompt and a reply (2.0's own fact rules drop NULs from the
        # text they keep too, so this guards the script's own handling as well as theirs)
        duck_edit(d, [("UPDATE events SET payload = CAST(replace(CAST(payload AS VARCHAR), ?, ?) AS JSON) "
                       "WHERE key_value = ?", [a, b, s["partial"]])
                      for a, b in (("pytest -q", "pytest\\u0000 -q"), ("toolu_e_", "toolu\\u0000_e_"),
                                   ("please run", "please\\u0000 run"), ("I will run", "I\\u0000 will run"))])
        out = pg_dsn(new_schema("m2nul"))
        rc, text = run("--apply", d, "--to", out, "--pr-events-since", SINCE, expect=0)
        ck("a session completed from DuckDB with a NUL in a command moves to Postgres", rc == 0, text[-1500:])
        if rc == 0:
            e = engine_of(out)
            cmds = [r[0] for r in q(e, "SELECT command FROM session_commands WHERE session = :s", s=s["partial"])]
            ck("its commands are kept without the NUL", cmds and all(c == "pytest -q tests/test_cart.py" for c in cmds),
               cmds)
            e.dispose()
    # the sessions table never caught up: coordination_calls counts every line, not DuckDB's alone
    d = variant(["DELETE FROM session_ingest_marks WHERE name = 'sessions_caught_up'"])
    # DuckDB holds one more tool_use line of the main session, whose text looks like coordination and
    # whose tool is not: counted from DuckDB alone, the session would be left with 0
    at = datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc)
    line = {"type": "assistant", "uuid": "duck-bash-0001", "sessionId": s["main"],
            "message": {"content": [{"type": "tool_use", "id": "toolu_duck_1", "name": "Bash",
                                     "input": {"command": "echo"}}]}}
    duck_edit(d, [("INSERT INTO events (source, source_type, key_value, event_type, text, payload, labels, "
                   "event_time, ingest_time) VALUES ('claude_code', 'event_stream', ?, 'tool_use', "
                   "'→ Skill(nothing) → Bash(echo)', ?, '{}', ?, ?)", [s["main"], json.dumps(line), at, at])])
    c = sqlite3.connect(os.path.join(d, "tares.sqlite"))
    before = dict(c.execute("SELECT session, coordination_calls FROM sessions").fetchall())
    c.close()
    out = os.path.join(SCRATCH, "out-coord")
    rc, text = run("--apply", d, "--to", out, "--pr-events-since", SINCE, expect=0)
    ck("a cell whose sessions never caught up is moved and verified", rc == 0, text[-1500:])
    if rc == 0:
        e = engine_of(out)
        after = dict(q(e, "SELECT session, coordination_calls FROM sessions"))
        ck("coordination_calls counts the lines only the table holds", before[s["main"]] > 0
           and after.get(s["main"]) == before[s["main"]], (before.get(s["main"]), after.get(s["main"])))
        e.dispose()
    # --drop-non-factory leaves out every row of the dropped project
    nf = META["projects"]["non_factory"][0]
    d = variant([
        f"INSERT INTO tickets (id, project, owner, title, status) VALUES ('tk_nf1', '{nf}', 'tares', 'x', 'todo')",
        f"INSERT INTO ticket_history (ticket, project, field, value) VALUES ('tk_nf1', '{nf}', 'stage', 'doing')",
        f"INSERT INTO milestones (id, project, owner, name) VALUES ('ms_nf1', '{nf}', 'tares', 'M1')",
        f"INSERT INTO reviews (id, project, ticket, round, verdict) VALUES ('rv_nf1', '{nf}', 'tk_nf1', 1, 'pass')",
        f"INSERT INTO decisions (id, project, question) VALUES ('dc_nf1', '{nf}', 'q')",
        f"INSERT INTO project_sessions (project, session) VALUES ('{nf}', '{s['two']}')",
        f"INSERT INTO merge_queue (id, project_uid, repo) VALUES ('mq_nf1', '{nf}', 'r')",
        f"UPDATE session_usage SET project = '{nf}' WHERE session = '{s['two']}'"])
    rc, text = run("--check", d, "--drop-non-factory", "--pr-events-since", SINCE)
    ck("the plan counts what --drop-non-factory leaves out",
       rc == 0 and "--drop-non-factory leaves out the rows of uc_challeng4" in text and "tickets 1" in text
       and "merge_queue 1" in text and "session_usage" in text.split("empties the project of")[-1], text[-1200:])
    out = os.path.join(SCRATCH, "out-drop-all")
    rc, text = run("--apply", d, "--to", out, "--drop-non-factory", "--pr-events-since", SINCE, expect=0)
    ck("--drop-non-factory with the project's tickets, reviews and the rest is moved and verified", rc == 0, text[-1500:])
    if rc == 0:
        e = engine_of(out)
        left = {t: q(e, f"SELECT count(*) FROM {t} WHERE {c} = :p", p=nf)[0][0]
                for t, c in (("tickets", "project"), ("ticket_history", "project"), ("milestones", "project"),
                             ("reviews", "project"), ("decisions", "project"), ("project_sessions", "project"),
                             ("merge_queue", "project_uid"), ("session_usage", "project"))}
        ck("no row of the dropped project is left", not any(left.values()), left)
        ck("its sessions' replies stay, with no project",
           q(e, "SELECT count(*) FROM session_usage WHERE session = :s AND project IS NULL", s=s["two"])[0][0] > 0)
        e.dispose()
    # an archived session gets an older line DuckDB alone holds
    d = variant([f"UPDATE sessions SET lines_complete = 0 WHERE session = '{s['archived']}'"])
    old = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    payload = {"type": "user", "uuid": "older-0001", "sessionId": s["archived"], "timestamp": old.isoformat(),
               "message": {"role": "user", "content": "a line from before"}}
    duck_edit(d, [("INSERT INTO events (source, source_type, key_value, event_type, text, payload, labels, "
                   "event_time, ingest_time) VALUES ('claude_code', 'event_stream', ?, 'user', 'a line from before', "
                   "?, '{}', ?, ?)", [s["archived"], json.dumps(payload), old, old])])
    rc, text = run("--check", d, "--pr-events-since", SINCE)
    ck("the plan names the archived session whose added line is older than its newest",
       rc == 0 and f"session {s['archived']} has archived parts" in text and "older than its newest line" in text,
       text[-1200:])
    out = os.path.join(SCRATCH, "out-older")
    rc, text = run("--apply", d, "--to", out, "--pr-events-since", SINCE, expect=0)
    ck("it is moved and verified", rc == 0, text[-1500:])
    if rc == 0:
        e = engine_of(out)
        top = q(e, "SELECT max(last_seq) FROM session_archive_parts WHERE session = :s", s=s["archived"])[0][0]
        row = q(e, "SELECT seq FROM session_lines WHERE session = :s AND line_key = 'older-0001'", s=s["archived"])
        meta = q(e, "SELECT line_count, lines_complete FROM sessions WHERE session = :s", s=s["archived"])
        ck("the line goes after the archived part, and the session is complete",
           row == [(top + 1,)] and meta and meta[0][0] == top + 1 and bool(meta[0][1]), (row, top, meta))
        e.dispose()
        env_was = dict(os.environ)
        try:
            from tares.store import Store
            st = Store(out)
            lines, where = st.key_events_from("claude_code", s["archived"], limit=1000)
            st.close()
        finally:
            os.environ.clear()
            os.environ.update(env_was)
        times = [ln.get("at") or ln.get("event_time") for ln in lines]
        ck("2.0 reads it first, in time order", where == "both" and lines and times == sorted(times)
           and "a line from before" in json.dumps(lines[0], default=str), (where, len(lines)))
    # an output that is a file
    f = os.path.join(SCRATCH, "a-file")
    open(f, "w").write("x")
    rc, text = run("--apply", variant(), "--to", f)
    ck("an output that is a file is refused", rc == 2 and "Refused" in text and "is a file" in text, text[-600:])


def main() -> int:
    try:
        static_checks()
        sqlite_suite()
        postgres_suite()
        refusals()
        skills_and_drop()
        review_fixes()
        broken()
    finally:
        drop_schemas()
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
