"""Time a cell's endpoints on a copy of its database (TR-654), so every speed change can show
before and after numbers.

The database is copied into a scratch directory and the copy is neutralized (every source and
trigger paused, credentials, keys, subscriptions and settings deleted) before a daemon starts on
it under a macOS sandbox that denies every outbound connection except localhost. The original
file is never opened; its sha256 is printed at start and end and must not change.

Rows: the orchestrator's calls (review queue, projects, tickets, milestones, the MCP list_tickets
path) and every poll of the factory project page, each timed alone (sequential requests, median
and max) and together (the page's polls fired at once, each one's finish time from the start).
--slow-scan times /health while a heavy events read runs in a loop.

Run:
  .venv/bin/python scripts/bench_cell.py --db ~/tares-perf/factory.duckdb
  .venv/bin/python scripts/bench_cell.py --db ~/tares-perf/factory.duckdb --slow-scan
  .venv/bin/python scripts/bench_cell.py --db ~/tares-perf/factory.duckdb --serve
  .venv/bin/python scripts/bench_cell.py --db ~/tares-perf/factory.duckdb --docker   # cell-like speed

--docker builds the production image from --src (default: the current directory) and runs the
daemon in it with a CPU and memory limit, on a Docker network that reaches nothing outside, with
the timing client in a second container. Numbers then sit near a live cell's instead of 10 to 30
times below them.
"""
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import duckdb
import httpx

# Everything outbound is denied except localhost: the copy came from a live cell, and a daemon on
# it must not reach GitHub, Linear, Slack or a model even if neutralize() missed something.
SANDBOX_PROFILE = ('(version 1)(allow default)(deny network-outbound)'
                   '(allow network-outbound (remote ip "localhost:*"))'
                   '(allow network-bind)(allow network-inbound)')

# settings keys kept on the copy. Every other row is deleted, so a secret nobody listed goes too.
# Add a key only when a bench row breaks without it, and say which row.
SETTINGS_ALLOW: tuple[str, ...] = ()

# Env the daemon must not inherit: auth, a catalog file, tracing exporters, model credentials.
_ENV_DROP = ("TARES_AUTH_TOKEN", "TARES_CATALOG")
_ENV_DROP_PREFIXES = ("TARES_TRACING_", "TARES_PLATFORM_PROVIDER_", "ANTHROPIC_", "OPENAI_",
                      "TARES_FACTORY_", "FACTORY_")

# Columns the credential scan matches by name that hold no secret.
_NOT_SECRET = {("catalog_agents", "slack_webhook"), ("events", "key_value"),
               ("catalog_triggers", "key_field"), ("model_usage", "key_source"),
               ("usecase_objects", "key")}

PAGE_POLLS = ("docs", "tickets", "sessions", "milestones", "crew", "assumptions", "decisions",
              "repos")
EVENTS_LIMIT = 500   # the most /api/sources/{name}/events returns (daemon.py caps `limit` there)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(con, sql: str, params=None) -> bool:
    """One neutralizing statement; False when its table is not in this (older) database."""
    try:
        con.execute(sql, params or [])
        return True
    except duckdb.CatalogException:
        return False


def neutralize(path: str) -> list[str]:
    """Make a database copy safe to run a daemon on: pause every source and trigger, delete
    credentials, MCP servers, subscriptions, API keys, Slack webhooks and every settings row not
    in SETTINGS_ALLOW. Returns warnings naming credential-like columns this did not clear, for a
    person to judge. Never call it on an original."""
    con = duckdb.connect(path)
    try:
        _run(con, "UPDATE catalog_sources SET paused = TRUE")
        _run(con, "UPDATE catalog_triggers SET paused = TRUE")
        for table in ("github_credentials", "mcp_servers", "subscriptions", "api_keys"):
            _run(con, f"DELETE FROM {table}")
        _run(con, "UPDATE catalog_agents SET slack_webhook = NULL")
        if SETTINGS_ALLOW:
            marks = ", ".join("?" for _ in SETTINGS_ALLOW)
            _run(con, f"DELETE FROM settings WHERE key NOT IN ({marks})", list(SETTINGS_ALLOW))
        else:
            _run(con, "DELETE FROM settings")
        cleared = {"github_credentials", "mcp_servers", "subscriptions", "api_keys", "settings"}
        # text columns only: the *_tokens counts in the usage tables are numbers, not secrets
        rows = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND data_type IN ('VARCHAR', 'JSON') "
            "AND (column_name ILIKE '%token%' "
            "OR column_name ILIKE '%secret%' OR column_name ILIKE '%key%' "
            "OR column_name ILIKE 'auth\\_%' ESCAPE '\\') ORDER BY 1, 2").fetchall()
        warnings = []
        for table, column in rows:
            if table in cleared or (table, column) in _NOT_SECRET:
                continue
            n = con.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NOT NULL').fetchone()[0]
            if n:
                warnings.append(f"{table}.{column}: {n} non-null rows left (not cleared)")
        con.execute("CHECKPOINT")
        return warnings
    finally:
        con.close()


def stage_review(path: str, n: int) -> int:
    """Put the `n` most recently updated open tickets into the review stage, on the scratch copy
    only. The review queue's cost grows with the tickets in review (a challenger events scan
    each), and a copy taken while none was in review would time an empty queue. Returns how
    many it moved."""
    if n <= 0:
        return 0
    con = duckdb.connect(path)
    try:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM tickets WHERE status NOT IN ('done', 'canceled') "
            "ORDER BY updated_at DESC LIMIT ?", [n]).fetchall()]
        if ids:
            con.execute(f"UPDATE tickets SET stage = 'review' WHERE id IN "
                        f"({', '.join('?' for _ in ids)})", ids)
        return len(ids)
    finally:
        con.close()


def pick_project(path: str, want: str | None) -> tuple[str, str] | None:
    """(id, name) of the project to bench: `want` by id or name, else the software_factory project
    with the most tickets."""
    con = duckdb.connect(path, read_only=True)
    try:
        rows = con.execute(
            "SELECT u.id, u.name, count(t.id) AS n FROM usecases u "
            "LEFT JOIN tickets t ON t.project = u.id "
            "WHERE u.kind = 'software_factory' GROUP BY 1, 2 ORDER BY n DESC, u.id").fetchall()
        if want:
            rows = [r for r in rows if want in (r[0], r[1])]
            if not rows:
                rows = con.execute("SELECT id, name, 0 FROM usecases WHERE id = ? OR name = ?",
                                   [want, want]).fetchall()
        return (rows[0][0], rows[0][1]) if rows else None
    finally:
        con.close()


def busiest_source(path: str) -> tuple[str, int] | None:
    con = duckdb.connect(path, read_only=True)
    try:
        row = con.execute("SELECT source, count(*) FROM events WHERE source IS NOT NULL "
                          "GROUP BY 1 ORDER BY 2 DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else None
    finally:
        con.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sandboxed(cmd: list[str]) -> list[str]:
    """`cmd` under the no-network sandbox. Refuses to run without it: there is no fallback that
    has network."""
    exe = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not exe:
        sys.exit("bench_cell: needs macOS sandbox-exec to run the daemon without network; "
                 "refusing to start a daemon on a cell's data without it")
    return [exe, "-p", SANDBOX_PROFILE, *cmd]


def daemon_env(db: str, port: int, home: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in _ENV_DROP and not k.startswith(_ENV_DROP_PREFIXES)}
    env.update(TARES_DB=db, TARES_PORT=str(port), TARES_HOST="127.0.0.1",
               TARES_OTLP_GRPC_PORT="off", TARES_HOME=home)
    return env


def start_daemon(db: str, port: int, scratch: Path) -> tuple[subprocess.Popen, Path]:
    taresd = Path(sys.executable).parent / "taresd"
    if not taresd.exists():
        sys.exit(f"bench_cell: {taresd} not found; install the repo into this venv first")
    home = scratch / "home"
    home.mkdir(exist_ok=True)
    log = scratch / "daemon.log"
    proc = subprocess.Popen(sandboxed([str(taresd)]), env=daemon_env(db, port, str(home)),
                            stdout=open(log, "wb"), stderr=subprocess.STDOUT,
                            start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 120   # the first open of an older file runs upgrades
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"bench_cell: the daemon exited ({proc.returncode}); log: {log}\n"
                     + log.read_text(errors="replace")[-2000:])
        try:
            if httpx.get(base + "/health", timeout=2).status_code == 200:
                return proc, log
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    stop_daemon(proc)
    sys.exit(f"bench_cell: the daemon did not answer /health within 120 s; log: {log}")


def stop_daemon(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def rows_for(uid: str, ticket_ref: str | None) -> list[tuple[str, list[str], dict]]:
    """(row name, paths timed as one, headers). The snapshot rows light up once TR-649 adds the
    route; a row whose route answers 404 is skipped."""
    p = f"/api/projects/{uid}"
    out = [("review-queue", ["/api/review-queue"], {}),
           ("projects", ["/api/projects"], {}),
           ("tickets", [f"{p}/tickets"], {}),
           ("milestones", [f"{p}/milestones"], {}),
           # what tares/mcp_server.py list_tickets does: whoami, resolve the project, then tickets
           ("mcp-list-tickets", ["/api/whoami", "/api/projects", f"{p}/tickets"], {})]
    out += [(f"page-{name}", [f"{p}/{name}"], {}) for name in PAGE_POLLS]
    if ticket_ref:
        out.append(("page-ticket", [f"{p}/tickets/{ticket_ref}"], {}))
    out.append(("snapshot", [f"{p}/factory-snapshot"], {}))
    return out


def timed(client: httpx.Client, paths: list[str], headers: dict) -> tuple[float, int]:
    t0 = time.perf_counter()
    status = 0
    for path in paths:
        status = client.get(path, headers=headers).status_code
    return (time.perf_counter() - t0) * 1000, status


def bench_alone(client, rows, runs: int) -> dict:
    results = {}
    for name, paths, headers in rows:
        _, status = timed(client, paths, headers)          # warm-up, discarded
        if status == 404 and name.startswith("snapshot"):
            continue
        times, statuses = [], set()
        for _ in range(runs):
            ms, st = timed(client, paths, headers)
            times.append(ms)
            statuses.add(st)
        results[name] = {"alone_median": statistics.median(times), "alone_max": max(times),
                         "status": sorted(statuses)}
        if name == "snapshot":
            etag = client.get(paths[0]).headers.get("etag")
            if etag:
                h = {"If-None-Match": etag}
                times, statuses = [], set()
                for _ in range(runs):
                    ms, st = timed(client, paths, h)
                    times.append(ms)
                    statuses.add(st)
                results["snapshot-304"] = {"alone_median": statistics.median(times),
                                           "alone_max": max(times), "status": sorted(statuses)}
    return results


async def _together_once(base: str, page_rows) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        t0 = time.perf_counter()

        async def one(name, path):
            await client.get(path)
            return name, (time.perf_counter() - t0) * 1000

        done = await asyncio.gather(*(one(n, paths[0]) for n, paths, _ in page_rows))
    return dict(done)


def bench_together(base: str, rows, runs: int) -> dict:
    page_rows = [r for r in rows if r[0].startswith("page-")]
    samples: dict[str, list[float]] = {n: [] for n, _, _ in page_rows}
    asyncio.run(_together_once(base, page_rows))         # warm-up, discarded
    for _ in range(runs):
        for name, ms in asyncio.run(_together_once(base, page_rows)).items():
            samples[name].append(ms)
    return {n: statistics.median(v) for n, v in samples.items()}


def bench_slow_scan(base: str, source: str, seconds: float = 10.0) -> dict:
    """/health every 50 ms while a thread reads the busiest source's events in a loop."""
    stop = threading.Event()
    scans = []

    def scan():
        with httpx.Client(base_url=base, timeout=120) as c:
            while not stop.is_set():
                t0 = time.perf_counter()
                c.get(f"/api/sources/{source}/events", params={"limit": EVENTS_LIMIT})
                scans.append((time.perf_counter() - t0) * 1000)

    t = threading.Thread(target=scan, daemon=True)
    t.start()
    health = []
    with httpx.Client(base_url=base, timeout=120) as c:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            t0 = time.perf_counter()
            c.get("/health")
            health.append((time.perf_counter() - t0) * 1000)
            time.sleep(0.05)
        stop.set()
        t.join(120)
        lag = None
        for line in c.get("/metrics").text.splitlines():
            if line.startswith("tares_event_loop_lag_seconds "):
                lag = float(line.split()[1])
    health.sort()
    return {"source": source, "limit": EVENTS_LIMIT, "scans": len(scans),
            "scan_median": statistics.median(scans) if scans else None,
            "health_p50": health[len(health) // 2],
            "health_p95": health[min(len(health) - 1, int(len(health) * 0.95))],
            "health_max": health[-1], "health_n": len(health), "event_loop_lag": lag}


def fmt(v) -> str:
    return "-" if v is None else f"{v:.0f}"


def print_table(alone: dict, together: dict, scan: dict | None) -> None:
    print(f"\n{'row':<20}{'alone med ms':>14}{'alone max ms':>14}{'together ms':>13}  status")
    for name, r in alone.items():
        print(f"{name:<20}{fmt(r['alone_median']):>14}{fmt(r['alone_max']):>14}"
              f"{fmt(together.get(name)):>13}  {','.join(map(str, r['status']))}")
    if scan:
        print(f"\nslow scan: GET /api/sources/{scan['source']}/events?limit={scan['limit']} "
              f"in a loop ({scan['scans']} scans, median {fmt(scan['scan_median'])} ms)")
        print(f"/health over {scan['health_n']} requests: p50 {fmt(scan['health_p50'])} ms, "
              f"p95 {fmt(scan['health_p95'])} ms, max {fmt(scan['health_max'])} ms; "
              f"event loop lag {scan['event_loop_lag']} s")


def run_timings(base: str, uid: str | None, source: str | None, runs: int,
                slow_scan: bool) -> tuple[dict, dict, dict | None]:
    """Every timing against a daemon at `base`: alone, together and (slow_scan) /health under an
    events read. Runs on the host, or in the client container in --docker mode."""
    with httpx.Client(base_url=base, timeout=120) as client:
        if uid:
            tickets = client.get(f"/api/projects/{uid}/tickets").json().get("tickets") or []
            rows = rows_for(uid, tickets[0]["id"] if tickets else None)
        else:
            rows = [("projects", ["/api/projects"], {}), ("review-queue", ["/api/review-queue"], {})]
        alone = bench_alone(client, rows, runs)
    together = bench_together(base, rows, runs) if uid else {}
    scan = None
    if slow_scan:
        if source:
            scan = bench_slow_scan(base, source)
        else:
            print("slow scan skipped: no events in this database")
    return alone, together, scan


def wait_health(base: str, timeout: float = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(base + "/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


# ── --docker: the daemon in a container with the cell's CPU and memory limits ──────────────────
# The Mac runs the daemon 10 to 30 times faster than a cell does. In --docker mode the daemon runs
# in the production image (deploy/docker/Dockerfile, built from --src) with --cpus and --memory
# like a cell's pod, on an internal Docker network with no way out. The timing client runs in a
# second container on that network, so it does not take the daemon's CPU.

# A cell's pod may use 1 CPU, but a droplet core is much slower than an Apple one, so --cpus 1 on a
# Mac times close to the bare Mac. 0.25 is calibrated, not a real limit: on 2026-10-22 it put the
# factory copy at projects 394 ms, tickets 404 ms, the MCP list_tickets path 1.1 s, against 0.8,
# 0.7 and 1.4 s live. Memory is factory's real limit, and it matters: at 1g the review queue's
# events scans no longer fit and it took 7.6 s instead of 0.9 s.
DOCKER_CPUS = "0.25"
DOCKER_MEMORY = "2g"


def docker(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def docker_build(src: Path) -> str:
    """Build the production image from the worktree being timed; returns its tag."""
    if not (src / "deploy/docker/Dockerfile").exists():
        sys.exit(f"bench_cell: {src} has no deploy/docker/Dockerfile; pass --src <tares worktree>")
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
                          text=True, capture_output=True).stdout.strip() or "dev"
    tag = f"tares-bench:{head}"
    print(f"building {tag} from {src} (deploy/docker/Dockerfile)", flush=True)
    r = subprocess.run(["docker", "build", "-q", "-f", "deploy/docker/Dockerfile", "-t", tag, "."],
                       cwd=src, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"bench_cell: docker build failed\n{r.stderr[-3000:]}")
    return tag


def internal_network(name: str) -> None:
    """A Docker network whose containers reach each other and nothing else, not even the host."""
    docker("network", "create", "--internal", name, capture=True)


class DockerRun:
    """The daemon container and its internal network; stop() removes both."""

    def __init__(self, tag: str, scratch: Path, cpus: str, memory: str):
        self.id = os.urandom(4).hex()
        self.net = f"bench-cell-{self.id}"
        self.daemon = f"bench-cell-daemon-{self.id}"
        self.proxy = None
        self.tag = tag
        internal_network(self.net)
        docker("run", "-d", "--name", self.daemon, "--network", self.net,
               "--cpus", cpus, "--memory", memory, "--memory-swap", memory,
               "--user", "0", "-v", f"{scratch}:/bench",
               "-e", "TARES_DB=/bench/cell.duckdb", "-e", "TARES_HOME=/bench/home",
               "-e", "TARES_HOST=0.0.0.0", "-e", "TARES_PORT=8787",
               "-e", "TARES_OTLP_GRPC_PORT=off", tag, "taresd", capture=True)
        self.base = f"http://{self.daemon}:8787"

    def publish(self, port: int) -> str:
        """For --serve: a forwarder on the internal network and the default bridge publishes the
        daemon on 127.0.0.1:<port>. The daemon itself stays without a way out."""
        self.proxy = f"bench-cell-proxy-{self.id}"
        # created on the default bridge: Docker publishes no port of a container that starts on
        # an internal network; the internal one is attached after
        docker("create", "--name", self.proxy, "-p", f"127.0.0.1:{port}:8787",
               "alpine/socat:1.8.0.0", "TCP-LISTEN:8787,fork,reuseaddr",
               f"TCP:{self.daemon}:8787", capture=True)
        docker("network", "connect", self.net, self.proxy, capture=True)
        docker("start", self.proxy, capture=True)
        return f"http://127.0.0.1:{port}"

    def client(self, script_dir: Path, scratch: Path, args: list[str]) -> int:
        """Run this script's --client mode in a container on the same network; it writes
        /out/result.json into the scratch dir."""
        return docker("run", "--rm", "--network", self.net, "-v", f"{script_dir}:/tool:ro",
                      "-v", f"{scratch}:/out", "--user", "0", "--entrypoint", "python",
                      self.tag, "/tool/bench_cell.py", "--client-url", self.base, *args,
                      check=False).returncode

    def logs(self) -> str:
        return docker("logs", "--tail", "60", self.daemon, check=False, capture=True).stderr or ""

    def stop(self) -> None:
        for c in (self.proxy, self.daemon):
            if c:
                docker("rm", "-f", c, check=False, capture=True)
        docker("network", "rm", self.net, check=False, capture=True)


def client_main(args) -> int:
    """--client-url: only the timings, against a daemon this process did not start (the client
    container of --docker mode). Writes /out/result.json."""
    if not wait_health(args.client_url):
        sys.exit(f"bench_cell: {args.client_url} did not answer /health within 180 s")
    alone, together, scan = run_timings(args.client_url, args.client_project or None,
                                        args.client_source or None, args.runs, args.slow_scan)
    Path("/out/result.json").write_text(json.dumps(
        {"alone": alone, "together": together, "slow_scan": scan}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="the cell database to copy (never opened)")
    ap.add_argument("--project", help="project id or name (default: the software_factory "
                                      "project with the most tickets)")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--in-review", type=int, default=5,
                    help="tickets to put in review on the copy, so the review queue has work "
                         "(default 5; 0 keeps the copy's own stages)")
    ap.add_argument("--slow-scan", action="store_true", help="time /health under a heavy events read")
    ap.add_argument("--port", type=int, default=0, help="daemon port (default: a free one)")
    ap.add_argument("--serve", action="store_true", help="skip the timings; keep the daemon up "
                                                          "until Ctrl-C")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    ap.add_argument("--json", action="store_true", help="also write bench.json here")
    ap.add_argument("--docker", action="store_true",
                    help="run the daemon in the production image with a cell's CPU and memory "
                         "limits, on a Docker network with no way out")
    ap.add_argument("--src", default=".", help="--docker: the tares worktree to build the image "
                                               "from (default: the current directory)")
    ap.add_argument("--cpus", default=DOCKER_CPUS, help=f"--docker: daemon CPU limit (default "
                                                        f"{DOCKER_CPUS}, calibrated to the live cell)")
    ap.add_argument("--memory", default=DOCKER_MEMORY, help=f"--docker: daemon memory limit "
                                                             f"(default {DOCKER_MEMORY}, factory's)")
    ap.add_argument("--client-url", help=argparse.SUPPRESS)
    ap.add_argument("--client-project", help=argparse.SUPPRESS)
    ap.add_argument("--client-source", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.client_url:
        return client_main(args)

    original = Path(args.db).expanduser()
    if not original.exists():
        sys.exit(f"bench_cell: {original} does not exist")
    if args.docker:
        if docker("info", check=False, capture=True).returncode:
            sys.exit("bench_cell: --docker needs a running Docker")
        tag = docker_build(Path(args.src).resolve())
    else:
        sandboxed([])   # fail before copying gigabytes when the sandbox is missing
    digest = sha256(original)
    print(f"original sha256 {digest}  {original}")

    scratch = Path(tempfile.mkdtemp(prefix="bench-cell-"))
    proc = run = None
    try:
        copy = scratch / "cell.duckdb"
        shutil.copyfile(original, copy)
        wal = original.with_name(original.name + ".wal")
        if wal.exists():
            shutil.copyfile(wal, scratch / "cell.duckdb.wal")
        for w in neutralize(str(copy)):
            print(f"warning: {w}")
        moved = stage_review(str(copy), args.in_review)
        if moved:
            print(f"put {moved} tickets in review on the copy (--in-review)")
        project = pick_project(str(copy), args.project)
        source = busiest_source(str(copy)) if args.slow_scan else None
        port = args.port or free_port()
        if args.docker:
            (scratch / "home").mkdir(exist_ok=True)
            run = DockerRun(tag, scratch, args.cpus, args.memory)
            base = run.publish(port) if args.serve else run.base
            if args.serve and not wait_health(base):
                sys.exit(f"bench_cell: the daemon container did not start\n{run.logs()}")
            print(f"daemon {base} in {tag}, --cpus {args.cpus} --memory {args.memory}, "
                  f"internal network {run.net} (no way out)", flush=True)
        else:
            proc, log = start_daemon(str(copy), port, scratch)
            base = f"http://127.0.0.1:{port}"
            print(f"daemon {base} (sandboxed, no network) on {copy}; log {log}", flush=True)
        if project:
            print(f"project {project[1]} ({project[0]})")
        else:
            print("no software_factory project in this database: project rows skipped")

        if args.serve:
            # SIGTERM stops it like Ctrl-C, so a script that started it in the background (where
            # SIGINT is ignored) can stop it and still get the cleanup below
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
            print("serving; Ctrl-C or SIGTERM to stop", flush=True)
            try:
                while proc.poll() is None if proc else True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            return 0

        if args.docker:
            client_args = ["--db", "-", "--runs", str(args.runs)]
            client_args += ["--client-project", project[0]] if project else []
            client_args += ["--client-source", source[0]] if source else []
            client_args += ["--slow-scan"] if args.slow_scan else []
            if run.client(Path(__file__).resolve().parent, scratch, client_args):
                sys.exit(f"bench_cell: the client container failed; daemon log:\n{run.logs()}")
            res = json.loads((scratch / "result.json").read_text())
            alone, together, scan = res["alone"], res["together"], res["slow_scan"]
        else:
            alone, together, scan = run_timings(base, project and project[0],
                                                source and source[0], args.runs, args.slow_scan)
        print_table(alone, together, scan)
        if args.json:
            Path("bench.json").write_text(json.dumps(
                {"project": project, "runs": args.runs, "in_review": moved,
                 "docker": {"cpus": args.cpus, "memory": args.memory} if args.docker else None, "alone": alone, "together": together,
                 "slow_scan": scan}, indent=2))
            print("wrote bench.json")
        return 0
    finally:
        if proc is not None:
            stop_daemon(proc)
        if run is not None:
            run.stop()
        if args.keep:
            print(f"kept {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
        after = sha256(original)
        print(f"original sha256 {after}  {'unchanged' if after == digest else 'CHANGED'}")
        if after != digest:
            sys.exit("bench_cell: the original database changed")


if __name__ == "__main__":
    sys.exit(main())
