"""Does a Codex crew's pace step read the real account's limits and get a pace back from a real cell? A by-hand
probe (TR-1597), not in CI: it needs a signed-in `codex`. It asks no model and spends nothing.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_pace_station.py [--out DIR]

The whole path is the product's: a station started by `tares.crew.codex.start` (as probe_launch.py starts one,
with no prompt, so it has no turn), in a throwaway crew home and a Codex folder of its own; a throwaway cell
(`tares.daemon`, its own database and key, on a free port of this machine); then one top of a watch round,
`tares.crew.pace.round_start`. That asks the station's app-server for the account's limits
(`account/rateLimits/read`), sends them to the cell (PUT /api/crew/codex-limits) and reads the pace the cell
works out (GET /api/crew/pace?runtime=codex).

Checked: the reading arrived and is the one the pace came from, the two windows a ChatGPT plan has were told by
their length, nothing that names the account left the station, no conversation was started for it, and the
crew's status line shows the windows. What is saved (pace_station.json) has the windows' percentages and reset
times and the account's type and plan; no account id and no address.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from probe_launch import STATION, Crew  # noqa: E402

from tares.crew import codex_pace, pace, views  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402

TOKEN = "probe-cell-" + os.urandom(8).hex()   # made for this run's cell, good for nothing else


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_cell(folder: str):
    """A cell of this checkout on a free port, with a database and a key of its own. (process, address)."""
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TARES", "FACTORY", "ANTHROPIC"))}
    env.update(TARES_DB=os.path.join(folder, "cell.duckdb"), TARES_CATALOG=os.path.join(folder, "none.yaml"),
               TARES_HOME=os.path.join(folder, "tares-home"), TARES_AUTH_TOKEN=TOKEN, TARES_OTLP_GRPC_PORT="off",
               PYTHONPATH=ROOT)
    log = open(os.path.join(folder, "cell.log"), "ab")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "tares.daemon:make_app", "--host", "127.0.0.1",
                             "--port", str(port), "--log-level", "warning"], env=env, cwd=folder, stdout=log, stderr=log)
    url = f"http://127.0.0.1:{port}"
    for _ in range(300):
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                if r.status < 400:
                    return proc, url
        except OSError:
            time.sleep(0.2)
        if proc.poll() is not None:
            break
    proc.kill()
    raise SystemExit(f"the probe's cell did not come up; see {os.path.join(folder, 'cell.log')}")


def cell(url: str, method: str, path: str, body=None):
    req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    crew = Crew()
    folder = tempfile.mkdtemp(prefix="tares-pace-cell-")
    proc, url = start_cell(folder)
    checks, facts = {}, {}
    try:
        cell(url, "PUT", "/api/crew/settings", {"pace": True})
        before = cell(url, "GET", "/api/crew/pace?runtime=codex")
        err, row = crew.start()
        if err:
            raise SystemExit(f"the station did not start: {err}")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TARES", "FACTORY"))}
        env.update(TARESD_URL=url, TARES_AUTH_TOKEN=TOKEN, FACTORY_CREW_HOME=crew.home)
        taken = codex_pace.take(crew.paths)
        said = io.StringIO()
        with redirect_stderr(said):
            pace.round_start(crew.paths, env)
        reading = pace.last(crew.paths)
        after = cell(url, "GET", "/api/crew/pace?runtime=codex")
        claude = cell(url, "GET", "/api/crew/pace")
        client = TaresClient(crew.paths, env)
        line = views.pace_status_line(client)
        client.close()
        with cs.visit(crew.paths, STATION) as wire:
            threads = wire.request("thread/list", {}).get("data")
        kinds = {x["kind"]: x for x in after.get("limits") or []}
        sent = json.dumps(taken)
        checks = {
            "before any reading the cell says a Codex crew's pace is unknown (none)": before.get("reading") == "none" and before.get("known") is False,
            "a station answered for the account's limits": bool(taken) and taken.get("station") == STATION
                and isinstance((taken.get("limits") or {}).get("rateLimits"), dict),
            "nothing that names the account is in what leaves the station": not any(k in sent for k in ("accountId", "email", "workspaceName", "userId")),
            "the round's start sent it and read the pace back: an active reading from Codex": reading.state == "active"
                and reading.raw.get("source") == "codex" and reading.raw.get("reading") == "ok",
            "the 5-hour window and the week were told by their length": {"session", "weekly_all"} <= set(kinds)
                and all(isinstance(kinds[k]["percent"], (int, float)) and kinds[k]["paces"] for k in ("session", "weekly_all")),
            "the pace's percentages are those windows'": after.get("session_pct") is not None and after.get("weekly_pct") is not None,
            "a Claude Code crew on the same cell is not paced from it": claude.get("known") is False and "source" not in claude,
            "no conversation was started to read the limits": threads == [],
            "the status line shows the windows": line.startswith("pace: ") and "5-hour" in line,
            "the watch had nothing to complain about": said.getvalue() == "",
        }
        facts = {"account": after.get("account"), "reached": after.get("reached"), "read_at": after.get("read_at"),
                 "limits": after.get("limits"), "pace": {k: after.get(k) for k in ("known", "level", "state", "at_limit", "until",
                                                                                   "session_pct", "weekly_pct", "note")},
                 "status_line": line, "buckets_in_the_answer": sorted((taken.get("limits") or {}).keys()),
                 "window_lengths_minutes": sorted({w.get("windowDurationMins") for _, ws in codex_pace.buckets(taken["limits"]) for w in ws},
                                                  key=lambda v: (v is None, v))}
    finally:
        ended = crew.end()
        crew.remove()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(folder, ignore_errors=True)
    result = {"checks": checks, "pass": bool(checks) and all(checks.values()), "station_ended": ended, **facts}
    with open(os.path.join(args.out, "pace_station.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    print("PASS" if result["pass"] else "FAIL", "a Codex crew's pace from the real account's limits, through a real cell")
    for k, v in checks.items():
        print(f"      {'ok ' if v else 'NO '} {k}")
    print("      pace:", json.dumps(facts.get("pace")))
    print("      windows:", json.dumps([(x["kind"], x["percent"], x["resets_at"]) for x in facts.get("limits") or []]))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
