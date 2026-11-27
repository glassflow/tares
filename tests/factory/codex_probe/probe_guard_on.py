"""Is the guard of a Codex station proven to be on, the way the crew's code takes it to be (TR-1594)? A by-hand
probe, not in CI: it needs a signed-in `codex` and spends four small turns.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_guard_on.py [--out DIR] [--only NAME ...]

Run it with the checkout's own interpreter, so that `tares` (the station's guard is `tares hook codex-guard`)
is this checkout's. Everything is done by the product code: `tares.crew.codex.start` starts the station and
hands it its first prompt, `tares.crew.codex_station` compares what it ran with the guard's record. The same
cases against a fake Codex are tests/test_crew_codex_station.py, section j; what the guard itself lets through
and refuses on the real Codex is probe_guard.py.

Scenarios:
  guarded_turn                a turn leaves the guard's record at its start, before the model is asked, and one
                              line per call; a command let through ran with its input closed (one that waits
                              for input ended at once), a refused one never ran; compared with the record
                              nothing is missing; the next prompt, on the conversation the app-server still has
                              open, leaves its record too
  no_trust_no_command         the same station started without the thread's trust for its hooks: Codex skips
                              the hook and says nothing. The start is refused, the turn is interrupted before
                              the model ran anything (the command it was asked for never ran), and the
                              app-server is gone
  repository_setup_stays_out  a repository with a `.codex/config.toml` of its own (a hook and an MCP server).
                              The control, without the mark: Codex takes the folder as trusted by itself and
                              starts the repository's MCP server for the conversation. A station: neither is
                              read, the repository's hook never runs, and Codex leaves the station's own
                              config as the start wrote it
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from appserver import AppServer, codex_bin  # noqa: E402
from probe_launch import MODEL, PERSONAL, STATION, Crew, finish, path_without_claude  # noqa: E402
from probe_station import wait_for  # noqa: E402

from tares.crew import codex as cx  # noqa: E402
from tares.crew import codex_guard as cg  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.integrations.builtin.runtime_codex import LEAN_FLAGS  # noqa: E402

SCENARIOS = {}
ORDERS = ("Run exactly the shell commands you are asked for, each as its own tool call, in the order given, and "
          "nothing else. If one is refused or fails, do not retry it and do not run it another way: go on to the "
          "next one. ")


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def running(needle: str) -> list:
    ps = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, errors="replace").stdout
    return [line.split()[0] for line in ps.splitlines() if needle in line and "probe_guard_on.py" not in line]


def last_items(paths) -> list:
    try:
        with cs.visit(paths, STATION) as wire:
            turn = cs._last_turn(wire, cs._recorded_thread(paths, STATION)) or {}
    except CrewError:
        return []
    return [i for i in turn.get("items") or [] if i.get("type") in cs.AUDITED]


def events(c: Crew, name: str) -> list:
    try:
        with open(os.path.join(c.home, "events.jsonl"), encoding="utf-8") as f:
            return [e for e in map(json.loads, f.read().splitlines()) if e.get("event") == name]
    except OSError:
        return []


@scenario
def guarded_turn(out):
    c = Crew()
    try:
        began = time.time()
        err, row = c.start(prompt=ORDERS + "1. `echo one > one.txt`  2. `gh pr merge 12 --squash`  3. `cat`  "
                                          "Then answer with: done")
        if err:
            return finish({"the start succeeded and handed over the first prompt": False}, error=err)
        turn = (row.get("codex") or {}).get("turn")
        rec0 = cs._record(c.paths, STATION)
        armed = next((r for r in rec0 if r.get("armed") and r.get("turn") == turn), None)
        reached, seen, took = wait_for(c.paths, "done", 240)
        rec, items = cs._record(c.paths, STATION), last_items(c.paths)
        commands = [i for i in items if i["type"] == "commandExecution"]
        allowed = [r for r in rec if r.get("said") == "allow"]
        found = cs.check_guard(c.paths, STATION)
        up = cs.is_alive(c.paths, STATION)
        got = cs.hand(c.paths, STATION, "Answer with the one word: again") if up else {}
        rec2 = cs._record(c.paths, STATION)
        reached2, _, _ = wait_for(c.paths, "done", 120) if up else (False, [], 0)
        how = cs.stop(c.paths, STATION)
        return finish({
            "when the start returned, the guard's record already showed the turn's start": armed is not None,
            "the turn ended": reached,
            "one line per call: the two let through, and the merge refused": len(allowed) >= 2
                and any(r.get("said") == "deny" and r.get("tool") == "Bash" for r in rec),
            "each command that ran has its line, and carries the line that closes its input": bool(commands)
                and all(cg.input_closed(i) for i in commands) and {i["id"] for i in commands} <= {r.get("call") for r in allowed},
            "the refused merge left no command": not any("pr merge" in (i.get("command") or "") for i in commands),
            "the command let through did its work": os.path.exists(os.path.join(c.work, "one.txt")),
            "`cat`, which waits for input, ended by itself": any(cg.asked(i.get("command") or "").strip() == "cat"
                                                                 and i.get("status") == "completed" for i in commands),
            "compared with the record nothing is missing, and the station stays up": found == "" and up,
            "the next prompt, on the conversation still open, left its record too": bool(got)
                and any(r.get("armed") and r.get("turn") == got.get("turn") for r in rec2) and reached2,
            "the refusal is in the crew's events": len(events(c, "guard-deny")) == 1,
            "at rest, the station ended when asked": how == "stopped",
        }, seconds_to_the_record=round(armed["at"] - began, 1) if armed else None, states_seen=seen, seconds_of_the_turn=took,
            record=[{k: v for k, v in r.items() if k != "at"} for r in rec2],
            commands=[{"asked": cg.asked(i.get("command") or ""), "status": i.get("status"), "exit": i.get("exitCode"),
                       "source": i.get("source")} for i in commands])
    finally:
        c.end(wait=3)
        c.remove()


@scenario
def no_trust_no_command(out):
    c = Crew()
    try:
        plan = c.plan()
        del plan["settings"]["thread"]["config"]["bypass_hook_trust"]   # the hook is there; Codex is not told to trust it
        marker = os.path.join(c.work, "ran-without-a-guard")
        sock = str(cx.socket_path(c.paths, STATION))
        began = time.time()
        err, row = c.start(plan=plan, prompt=f"Run the shell command `touch {marker}` once, then answer with: done")
        took = round(time.time() - began, 1)
        time.sleep(10)   # had the turn gone on, the command would have run by now
        off = events(c, "guard-off")
        return finish({
            "the start is refused, and says the conversation does not run its hooks": bool(err)
                and "its guard cannot be relied on" in err and "with no run of the guard before it" in err,
            "the command the model was asked for never ran": not os.path.exists(marker),
            "the guard left no record": cs._record(c.paths, STATION) == [],
            "the app-server is gone, with its socket": not running(sock) and not os.path.lexists(sock),
            "a station that was never up leaves no record of itself": row == {},
            "the event says why": len(off) == 1 and "no run of the guard" in off[0].get("detail", ""),
        }, error_text=err, seconds_the_start_took=took)
    finally:
        c.end(wait=3)
        c.remove()


@scenario
def repository_setup_stays_out(out):
    c = Crew()
    control_home = os.path.join(c.root, "control-codex")
    hook_log = os.path.join(c.root, "repository-hook.log")
    try:
        os.makedirs(os.path.join(c.work, ".codex"))
        with open(os.path.join(c.work, ".codex", "config.toml"), "w", encoding="utf-8") as f:
            f.write(f'[mcp_servers.repo_probe]\ncommand = "{sys.executable}"\nargs = ["{HERE}/slack_probe_server.py"]\n\n'
                    f'[[hooks.PreToolUse]]\nmatcher = ""\n[[hooks.PreToolUse.hooks]]\ntype = "command"\n'
                    f'command = "/bin/sh -c \'echo ran >> {hook_log}\'"\ntimeout = 5\n')
        # the control: the same folder, a Codex folder with only the login, a conversation nobody marked. No turn.
        os.makedirs(control_home, mode=0o700)
        os.symlink(os.path.realpath(os.path.join(PERSONAL, "auth.json")), os.path.join(control_home, "auth.json"))
        app = AppServer.spawn(codex_bin(), args=list(LEAN_FLAGS), cwd=c.work, env={**os.environ, "CODEX_HOME": control_home},
                              transcript_path=os.path.join(out, "repository.control.wire.jsonl"))
        app.initialize()
        app.request("thread/start", {"cwd": c.work, "ephemeral": True, "model": MODEL, "approvalPolicy": "on-request",
                                     "sandbox": "workspace-write", "config": {"bypass_hook_trust": True}})
        time.sleep(4)
        started = sorted({(e["msg"].get("params") or {}).get("name") for e in app.log
                          if e.get("dir") == "in" and (e.get("msg") or {}).get("method") == "mcpServer/startupStatus/updated"})
        listed = [h.get("source") for d in app.request("hooks/list", {"cwds": [c.work]}).get("data") or [] for h in d.get("hooks") or []]
        app.close()
        try:
            with open(os.path.join(control_home, "config.toml"), encoding="utf-8") as f:
                control_config = f.read()
        except OSError:
            control_config = ""

        err, row = c.start(prompt=ORDERS + "1. `echo hi`  Then answer with: done")
        if err:
            return finish({"the station started": False}, error=err, control={"mcp_started": started, "hooks": listed})
        home = (row.get("codex") or {}).get("home", "")
        with open(os.path.join(home, "config.toml"), encoding="utf-8") as f:
            written = f.read()
        reached, seen, _ = wait_for(c.paths, "done", 180)
        with cs.visit(c.paths, STATION) as wire:
            brought = cs._brought_along(wire, c.work, {"tares"})
        items = last_items(c.paths)
        with open(os.path.join(home, "config.toml"), encoding="utf-8") as f:
            after = f.read()
        found = cs.check_guard(c.paths, STATION)
        how = cs.stop(c.paths, STATION)
        return finish({
            "control: with no mark, Codex starts the repository's MCP server for the conversation": "repo_probe" in started,
            "control: and lists the repository's hook": "project" in listed,
            "control: Codex recorded the folder as trusted by itself": 'trust_level = "trusted"' in control_config,
            "the station's own config marks the folder not trusted, and holds nothing else": written == cx.home_config(c.work)
                and 'trust_level = "untrusted"' in written,
            "the station's turn ran a command": reached and any(i["type"] == "commandExecution" for i in items),
            "the repository's hook never ran": not os.path.exists(hook_log),
            "the app-server has no hook and no MCP server of the repository's for the folder": brought == "",
            "Codex left the station's config as the start wrote it": after == written,
            "compared with the record nothing is missing": found == "",
            "the station ended when asked": how == "stopped",
        }, control={"mcp_started": started, "hooks": listed}, states_seen=seen)
    finally:
        c.end(wait=3)
        c.remove()


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
    for name, fn in todo:
        try:
            results[name] = fn(args.out)
        except Exception as e:  # noqa: BLE001 - a probe says what broke and goes on
            results[name] = {"pass": False, "error": f"{type(e).__name__}: {e}"}
        r = results[name]
        print("PASS" if r.get("pass") else "FAIL", name, r.get("error") or "")
        for k, v in (r.get("checks") or {}).items():
            print(f"      {'ok ' if v else 'NO '} {k}")
    with open(os.path.join(args.out, "guard_on.json"), "w", encoding="utf-8") as f:
        json.dump({"codex": cx.codex_version(codex_bin()), "results": results}, f, indent=1, ensure_ascii=False)
    return 0 if all(results[name].get("pass") for name, _ in todo) else 1


if __name__ == "__main__":
    sys.exit(main())
