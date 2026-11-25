"""Does a Codex station reach the crew's Tares MCP endpoint as itself, and does it stay down without it? A
by-hand probe (TR-1596), not in CI: it needs a signed-in `codex`, and it starts a real Tares cell.

    TARES_PROBE_CODEX=/path/to/codex .venv/bin/python tests/factory/codex_probe/probe_mcp_station.py [--out DIR] [--only NAME ...]

The cell is real and thrown away (cell.py): `tares up` and `tares mcp` in a folder of their own, a login
required, set to the Codex runtime, with one project handed over to the probe's crew and one ticket assigned to
the station. The station is started by the product's own start (tares.crew.launch.launch), from the launch the
cell builds, with no `claude` on PATH. Nothing of the person's Factory, Codex setup or repositories is touched:
the crew home, the repository and the station's Codex folder are the probe's.

  as_itself                 one turn: the station reads its ticket's brief over MCP and starts the ticket, which
                            only its holder may do; the cell's record of the ticket names the station and its
                            role. The same turn prints its whole environment; the key is then looked for in that
                            output, in every file of the crew home and of the station's Codex folder, in the
                            arguments and environment of every process of the station, and in what the app-server
                            stored of the conversation
  tares_down_keeps_the_conversation
                            the station is stopped and the MCP endpoint ended: a start in its conversation is
                            refused, and the conversation is not given up for a new one; with the endpoint back
                            the station carries on in it (one more turn)
  wrong_key                 the crew's key file holds a key the cell does not take: no turn, and the reason said
  no_key                    the key file is empty: Codex itself refuses the conversation (the server is required)

Two model turns in all.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import psutil  # noqa: E402
from appserver import codex_bin  # noqa: E402
from cell import DROP, Cell  # noqa: E402
from probe_launch import EFFORT, MODEL, finish, path_without_claude  # noqa: E402

from tares.crew import codex as cx  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

CREW, PROJECT = "probe", "p1"
SCENARIOS = {}
WORLD: dict = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def world() -> dict:
    """The cell, started once, and this process made the shell of the probe's crew."""
    if WORLD:
        return WORLD
    root = tempfile.mkdtemp(prefix="tcx-mcp-", dir="/tmp")
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", "-c", "user.name=probe", "-c", "user.email=probe@example.invalid", *argv], cwd=repo, check=True)
    cell = Cell(root)
    for k in [k for k in os.environ if k.startswith(DROP) or k == "TARESD_URL"]:
        del os.environ[k]
    os.environ.update(FACTORY_CREW_NAME=CREW, TARES_FACTORY_URL=cell.url, TARESD_URL=cell.url, FACTORY_NO_WATCH="1",
                      TARES_FACTORY_MCP_URL=cell.mcp_url, TARES_HOME=os.path.join(root, "tares-home"),
                      PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", CODEX_BIN=codex_bin())
    uid = cell.project(PROJECT, repo, CREW)
    WORLD.update(root=root, repo=repo, cell=cell, uid=uid, n=0)
    return WORLD


def crew(w: dict) -> tuple:
    """A crew home of its own for a scenario: (paths, a client primed with the person's key, the station's name)."""
    w["n"] += 1
    station = f"{PROJECT}-build-s{w['n']}"
    home = os.path.join(w["root"], f"crew{w['n']}")
    os.makedirs(os.path.join(home, "stations"))
    with open(os.path.join(home, "stations.json"), "w", encoding="utf-8") as f:
        f.write("{}\n")
    os.environ.update(FACTORY_CREW_HOME=home, FACTORY_HOME=home)
    paths = CrewPaths.at(home)
    client = TaresClient(paths, env={**os.environ, "TARES_AUTH_TOKEN": w["cell"].key}, say=lambda _t: None)
    client.prime()   # keeps the key in the crew home's key file, as `crew up` does
    return paths, client, station


def start(w: dict, client, station: str, prompt: str, resume: str = ""):
    """The product's own start of a Builder; the error text, or None."""
    try:
        L.launch(client, station, "builder", "", PROJECT, w["repo"], w["repo"], MODEL, EFFORT, prompt, resume)
    except CrewError as e:
        return str(e)
    return None


def row(paths, station: str) -> dict:
    try:
        with open(paths.stations, encoding="utf-8") as f:
            return json.load(f).get(station) or {}
    except (OSError, ValueError):
        return {}


def at_rest(paths, station: str, seconds: float = 240) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cs.turn_state(paths, station) == "done":
            return True
        time.sleep(1)
    return False


def last_turn(paths, station: str) -> list:
    thread = row(paths, station).get("sessionId")
    with cs.visit(paths, station) as wire:
        turns = (wire.request("thread/read", {"threadId": thread, "includeTurns": True}).get("thread") or {}).get("turns") or []
    return (turns[-1].get("items") or []) if turns else []


def files_with(folder: str, needle: str, skip=()) -> list:
    """The files under `folder` that hold `needle`. Links are not followed (the station's login is one)."""
    found = []
    for base, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if os.path.join(base, d) not in skip and not os.path.islink(os.path.join(base, d))]
        for name in names:
            path = os.path.join(base, name)
            if path in skip or os.path.islink(path):
                continue
            try:
                with open(path, "rb") as f:
                    if needle.encode() in f.read():
                        found.append(path)
            except OSError:
                pass
    return found


def processes_with(pid: int, needle: str) -> list:
    """Which of the app-server and what runs under it has `needle` in its arguments or its environment."""
    found = []
    try:
        procs = [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]
    except psutil.Error:
        return ["the app-server could not be read"]
    for p in procs:
        try:
            if needle in " ".join(p.cmdline()) or needle in json.dumps(p.environ()):
                found.append(f"{p.pid} {p.name()}")
        except psutil.Error:   # a system program's environment cannot be read on macOS; its arguments can
            pass
    return found


@scenario
def as_itself(out):
    w = world()
    cell, uid = w["cell"], w["uid"]
    paths, client, station = crew(w)
    cell.assign(uid, "T1", station)
    key_file = os.path.join(paths.home, ".tares-token")
    err = start(w, client, station, (
        "Do these steps in order and then answer with the one word done. "
        "Step 1: call the tares MCP tool builder_brief with ticket T1 and start true. "
        "Step 2: call the tares MCP tool get_ticket with ticket T1. "
        "Step 3: run the shell command `printenv`. "
        "Step 4: run the shell command `ps -axeww -o pid,command | grep -c tares`."))
    rec = row(paths, station)
    pid = (rec.get("codex") or {}).get("pid")
    in_turn = processes_with(pid, cell.key) if pid else ["no app-server"]
    rested = at_rest(paths, station)
    items = last_turn(paths, station) if rested else []
    calls = [(i.get("server"), i.get("tool"), i.get("status")) for i in items if i.get("type") == "mcpToolCall"]
    commands = [i for i in items if i.get("type") == "commandExecution"]
    printed = "\n".join(str(i.get("aggregatedOutput") or "") for i in commands)
    ticket = cell.ticket(uid, "T1")
    moved = [h for h in ticket.get("history") or [] if h.get("field") == "stage"]
    settings = json.load(open(paths.station_file(station, "settings.json"), encoding="utf-8"))
    WORLD.update(paths=paths, client=client, station=station, thread=rec.get("sessionId"))
    return finish({
        "the station started and its first turn ended": err is None and rested,
        "it read its ticket's brief and its ticket over MCP": ("tares", "builder_brief", "completed") in calls
            and ("tares", "get_ticket", "completed") in calls,
        "it started the ticket, which only its holder may": ticket.get("stage") == "doing" and ticket.get("holder") == station,
        "the cell's record of that names the station and its role": bool(moved) and moved[-1].get("by") == station
            and moved[-1].get("role") == "builder",
        "the conversation was started with the server required and the key file named by its path":
            settings["mcp_servers"]["tares"].get("required") is True
            and key_file in settings["mcp_servers"]["tares"].get("http_headers_helper", ""),
        "the key is not in the environment the model's commands print": bool(printed) and cell.key not in printed
            and "FACTORY_STATION=" + station in printed,
        "nor in the arguments or environment of the station's processes, during the turn and after":
            in_turn == [] and processes_with(pid, cell.key) == [],
        "nor in any file of the crew home but the key file, the station's Codex folder among them":
            files_with(str(paths.home), cell.key, skip=(key_file,)) == [],
        "nor in what the app-server stored of the conversation": cell.key not in json.dumps(items),
    }, error=err, mcp_calls=calls, commands=[str(i.get("command"))[-60:] for i in commands],
        stage_history=moved[-2:], key_found_in=files_with(str(paths.home), cell.key, skip=(key_file,)),
        token_file_variable_shown="TARES_FACTORY_TOKEN_FILE=" in printed, state_at_the_end=cs.turn_state(paths, station))


@scenario
def tares_down_keeps_the_conversation(out):
    w = world()
    if "station" not in w:
        return {"pass": False, "error": "run as_itself first: this goes on from its station"}
    cell, paths, client, station, thread = w["cell"], w["paths"], w["client"], w["station"], w["thread"]
    cs.stop(paths, station)
    cell.stop_mcp()
    err = start(w, client, station, "Say the one word back.", resume=thread)
    kept = row(paths, station).get("sessionId")
    down_alive = cs.is_alive(paths, station)
    cell.start_mcp()
    err2 = start(w, client, station, "Say the one word back.", resume=thread)
    rested = at_rest(paths, station)
    return finish({
        "with the endpoint down the start is refused, and says it cannot reach the server":
            bool(err) and "cannot reach an MCP server it is started with" in err,
        "the station is not left running and its conversation is the one it had": not down_alive and kept == thread,
        "with the endpoint back it is started in that conversation and takes a turn":
            err2 is None and rested and row(paths, station).get("sessionId") == thread,
    }, refused_with=err, second_start=err2)


@scenario
def wrong_key(out):
    w = world()
    paths, client, station = crew(w)
    with open(os.path.join(paths.home, ".tares-token"), "w", encoding="utf-8") as f:
        f.write("a-key-the-cell-never-made\n")
    err = start(w, client, station, "Say the one word done.")
    return finish({
        "the station gets no turn, and the reason is the cell's refusal of its key":
            bool(err) and "would work without Tares" in err and "error 401" in err,
        "it is not left running, and no process or conversation is recorded for it": not cs.is_alive(paths, station)
            and not row(paths, station).get("codex") and not row(paths, station).get("sessionId"),
    }, refused_with=err)


@scenario
def no_key(out):
    w = world()
    paths, client, station = crew(w)
    open(os.path.join(paths.home, ".tares-token"), "w").close()
    err = start(w, client, station, "Say the one word done.")
    return finish({
        "Codex refuses the conversation: the endpoint wants a key and the server is required":
            bool(err) and "cannot reach an MCP server it is started with" in err and "401" in err,
        "it is not left running, and no process or conversation is recorded for it": not cs.is_alive(paths, station)
            and not row(paths, station).get("codex") and not row(paths, station).get("sessionId"),
    }, refused_with=err)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    os.environ["PATH"] = path_without_claude()
    os.environ.pop("CLAUDE_BIN", None)
    todo = [(n, f) for n, f in SCENARIOS.items() if not args.only or n in args.only]
    results = {}
    try:
        for name, fn in todo:
            try:
                results[name] = fn(args.out)
            except Exception as e:  # noqa: BLE001 - a probe says what broke and goes on
                results[name] = {"pass": False, "error": f"{type(e).__name__}: {e}"}
            r = results[name]
            print("PASS" if r.get("pass") else "FAIL", name, r.get("error") or "")
            for k, v in (r.get("checks") or {}).items():
                print(f"      {'ok ' if v else 'NO '} {k}")
    finally:
        if WORLD:
            for n in range(1, WORLD["n"] + 1):
                home = CrewPaths.at(os.path.join(WORLD["root"], f"crew{n}"))
                for station in list(json.load(open(home.stations, encoding="utf-8"))):
                    try:
                        cs.stop(home, station)
                    except CrewError:
                        pass
            WORLD["cell"].stop()
            shutil.rmtree(WORLD["root"], ignore_errors=True)
    with open(os.path.join(args.out, "mcp_station.json"), "w", encoding="utf-8") as f:
        json.dump({"codex": cx.codex_version(codex_bin()), "results": results}, f, indent=1, ensure_ascii=False)
    return 0 if all(results[name].get("pass") for name, _ in todo) else 1


if __name__ == "__main__":
    sys.exit(main())
