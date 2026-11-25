"""Can a Codex station's session be recorded by visits, with nothing connected to it (TR-1595)? A by-hand
probe, not in CI: it needs a signed-in `codex` and spends two small turns.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_record.py [--out DIR] [--only NAME ...]

The station is started by the product (`tares.crew.codex.start`) and read by visits, as the watch would.

Scenarios:
  killed_mid_command   the app-server is killed in the middle of a command. Before the kill a visitor reads the
                       turn's items so far, twice (the same ids both times), and the guard's record holds the
                       running command's start. After a new start the stored conversation has no item for that
                       command: its start is in the guard's record alone, so the recorder must show it as
                       started with an unknown outcome
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from probe_launch import STATION, Crew, finish  # noqa: E402
from probe_station import running, wait_for  # noqa: E402

from tares.crew import codex_guard  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew.codex_wire import WireError  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def read(paths, thread: str):
    """The conversation with its turns, by one visit; None when it cannot be read."""
    try:
        with cs.visit(paths, STATION) as wire:
            return cs._thread(wire, thread, turns=True)[0]
    except CrewError:
        return None


def shape(found) -> list:
    """Per turn: its status, times and items, each item cut to what tells it apart."""
    out = []
    for turn in (found or {}).get("turns") or []:
        items = []
        for i in turn.get("items") or []:
            row = {"id": i.get("id"), "type": i.get("type"), "keys": sorted(i)}
            for k in ("status", "exitCode", "cwd", "durationMs", "processId", "source", "server", "tool", "phase"):
                if i.get(k) is not None:
                    row[k] = i.get(k)
            if i.get("type") == "commandExecution":
                row["command"] = str(i.get("command"))[:200]
                row["actions"] = i.get("commandActions")
                row["output"] = str(i.get("aggregatedOutput") or "")[:80]
            if i.get("type") in ("agentMessage", "userMessage"):
                row["text"] = str(i.get("text") or i.get("content"))[:120]
            items.append(row)
        out.append({"id": turn.get("id"), "status": turn.get("status"), "itemsView": turn.get("itemsView"),
                    "startedAt": turn.get("startedAt"), "completedAt": turn.get("completedAt"),
                    "durationMs": turn.get("durationMs"), "error": turn.get("error"), "items": items})
    return out


def ids(turns: list) -> list:
    return [i["id"] for t in turns for i in t["items"]]


def record(c) -> list:
    return codex_guard.read_record(codex_guard.record_file(c.home, STATION))


def ask(paths, method: str, params: dict):
    try:
        with cs.visit(paths, STATION) as wire:
            return wire.request(method, params)
    except (CrewError, WireError) as e:
        return {"error": str(e)[:300]}


@scenario
def killed_mid_command(out):
    c = Crew()
    try:
        err, _ = c.start(prompt="Run the shell command `echo first-mark-4747` once. After it, run the shell command "
                                "`sleep 47.4747` once, then answer with: slept")
        if err:
            return finish({"the start succeeded": False}, error=err)
        thread = c.row().get("sessionId")
        for _ in range(90):
            if running("sleep 47.4747"):
                break
            time.sleep(1)
        time.sleep(1)
        first = shape(read(c.paths, thread))
        again = shape(read(c.paths, thread))
        status = (read(c.paths, thread) or {}).get("status")
        usage = ask(c.paths, "account/usage/read", {"threadId": thread})
        before = record(c)
        sleeping = [r for r in before if r.get("call") and r.get("said") == "allow"]
        listed = {i["id"]: i for t in first for i in t["items"] if i["type"] == "commandExecution"}
        pid = (c.row().get("codex") or {}).get("pid")
        cs.note_children(c.paths, STATION)
        os.kill(pid, 9)
        time.sleep(2)
        cs.end_leftovers(c.paths, STATION, grace=5)
        c.end(wait=3)
        err2, _ = c.start(resume=thread)   # a new app-server, no prompt: nothing is asked of the model
        stored = shape(read(c.paths, thread)) if not err2 else []
        stored2 = shape(read(c.paths, thread)) if not err2 else []
        usage_after = ask(c.paths, "account/usage/read", {"threadId": thread}) if not err2 else None
        after_ids = set(ids(stored))
        cut = [r for r in sleeping if r["call"] not in after_ids]
        echo = [i for i in listed.values() if "first-mark-4747" in i.get("command", "")]
        return finish({
            "a visitor reads the turn's items so far, in the middle of the turn": bool(echo)
                and echo[0].get("exitCode") == 0,
            "a second read gives the same ids": bool(ids(first)) and ids(first) == ids(again),
            "the guard's record holds the start of the command that was running": len(sleeping) >= 2,
            "the stored conversation has no item for the command the kill cut off": not err2 and len(cut) == 1,
            "what did finish before the kill is still there, under the same ids": bool(echo)
                and echo[0]["id"] in after_ids,
            "two reads of the stored conversation give the same ids": ids(stored) == ids(stored2),
        }, error=err2, thread=thread, status_mid_turn=status, mid_turn=first, guard_record=before, stored=stored,
            cut_off=cut, usage_mid_turn=usage, usage_after=usage_after)
    finally:
        c.end(wait=3)
        c.remove()


def token_counts(found) -> dict:
    """What the conversation's own file says of tokens (looked at, never relied on: the file's format is
    Codex's own business): how many `token_count` lines, and the last one's shape."""
    path = (found or {}).get("path")
    n, last = 0, None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                body = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
                if body.get("type") == "token_count":
                    n, last = n + 1, body
    except (OSError, TypeError):
        return {"file": bool(path), "lines": 0}
    return {"file": True, "lines": n, "last": last}


@scenario
def turn_read_by_visits(out):
    c = Crew()
    try:
        err, _ = c.start(prompt="Run `echo first-mark-4848` as one shell command and wait for its result. Only after "
                                "that, as a second and separate shell command, run `sleep 21.4848`. Then answer "
                                "with: slept")
        if err:
            return finish({"the start succeeded": False}, error=err)
        thread = c.row().get("sessionId")
        for _ in range(90):
            if running("sleep 21.4848"):
                break
            time.sleep(1)
        time.sleep(1)
        mid = shape(read(c.paths, thread))
        mid_record = record(c)
        reached, seen, took = wait_for(c.paths, "done")
        found = read(c.paths, thread)
        done = shape(found)
        usage = ask(c.paths, "account/usage/read", {"threadId": thread})
        rec = record(c)
        calls = [r["call"] for r in rec if r.get("call") and r.get("said") == "allow"]
        commands = [i for t in done for i in t["items"] if i["type"] == "commandExecution"]
        mid_commands = [i for t in mid for i in t["items"] if i["type"] == "commandExecution"]
        how = cs.stop(c.paths, STATION)
        return finish({
            "in the middle of the turn a visitor reads the command that has finished": any(
                "first-mark-4848" in i.get("command", "") and i.get("exitCode") == 0 for i in mid_commands),
            "the running command is not among the items yet": not any("sleep 21.4848" in i.get("command", "")
                                                                      and i.get("status") == "completed" for i in mid_commands),
            "but the guard's record already holds its start": len([r for r in mid_record if r.get("call")]) == 2,
            "after the turn each command's item has the id the guard wrote down": reached and len(commands) == 2
                and [i["id"] for i in commands] == calls,
            "items read in the middle of the turn keep their ids": set(ids(mid)) <= set(ids(done)),
            "every item says when it started and ended": all("startedAtMs" in i["keys"] and "completedAtMs" in i["keys"]
                                                             for t in done for i in t["items"]),
        }, thread=thread, mid_turn=mid, done=done, guard_record=rec, usage=usage, tokens=token_counts(found),
            thread_fields={k: found.get(k) for k in ("model", "modelProvider", "cliVersion", "cwd", "createdAt", "updatedAt",
                                                     "source", "reasoningEffort")} if found else None, stop=how)
    finally:
        c.end(wait=3)
        c.remove()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    results = {}
    for name, fn in SCENARIOS.items():
        if args.only and name not in args.only:
            continue
        try:
            results[name] = fn(args.out)
        except Exception as e:   # a probe says what broke and goes on
            results[name] = {"pass": False, "checks": {}, "error": f"{type(e).__name__}: {e}"}
        r = results[name]
        print("PASS" if r["pass"] else "FAIL", name)
        for k, v in r.get("checks", {}).items():
            print(f"      {'ok ' if v else 'NO '} {k}")
        if r.get("error"):
            print("      error:", r["error"])
    with open(os.path.join(args.out, "record.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1, ensure_ascii=False, default=str)
    return 0 if results and all(r["pass"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
