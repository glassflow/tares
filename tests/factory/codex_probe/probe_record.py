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
  turn_read_by_visits  two commands in one turn: in the middle of the turn a visitor reads the one that has
                       ended (a moment after it ended) and not the one that runs; after the turn each
                       command's item has the id the guard wrote down, and every item its times
  recorded_on_a_cell   the product's recorder (`tares.crew.codex_record.step`) against a real cell, started
                       for this run on a port and a data folder of its own: the station's session shows on
                       the crew's API with its state, its messages and its commands; a builder's checks are
                       matched against what it really ran (a true claim, a command that exited 7 claimed as
                       passed, a command that never ran); then the app-server is killed in the middle of a
                       command, and that command is recorded as started with an unknown outcome and backs
                       no claim
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from probe_launch import STATION, Crew, finish  # noqa: E402
from probe_station import running, wait_for  # noqa: E402

from tares.crew import codex_guard  # noqa: E402
from tares.crew import codex_record  # noqa: E402
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
        err, _ = c.start(prompt="Run `echo first-mark-4747` as one shell command and wait for its result. Only after "
                                "that, as a second and separate shell command, run `sleep 47.4747`. Then answer "
                                "with: slept")
        if err:
            return finish({"the start succeeded": False}, error=err)
        thread = c.row().get("sessionId")
        for _ in range(90):
            if running("sleep 47.4747"):
                break
            time.sleep(1)
        began, first = time.time(), []
        while time.time() - began < 15:   # an item is listed a moment after it ends, not at once
            first = shape(read(c.paths, thread))
            if any(i["type"] == "commandExecution" for t in first for i in t["items"]):
                break
            time.sleep(0.5)
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
            "the guard's record holds the start of the command that was running, with the command and its folder":
                len(sleeping) == 2 and sleeping[1].get("command") == "sleep 47.4747" and bool(sleeping[1].get("cwd")),
            "the running command was in no listing": not any("sleep 47.4747" in i.get("command", "") for i in listed.values()),
            "the stored conversation has no item for the command the kill cut off": not err2 and len(cut) == 1
                and cut[0].get("command") == "sleep 47.4747",
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
                                "that, as a second and separate shell command, run `sleep 21.4848`. Then create the "
                                "file probe-note.txt holding the one line hello, by editing the file directly with "
                                "your patch tool, not with a shell command. Then answer with: slept")
        if err:
            return finish({"the start succeeded": False}, error=err)
        thread = c.row().get("sessionId")
        for _ in range(90):
            if running("sleep 21.4848"):
                break
            time.sleep(1)
        began, mid = time.time(), []
        while time.time() - began < 15:   # an item is listed a moment after it ends, not at once
            mid = shape(read(c.paths, thread))
            if any(i["type"] == "commandExecution" for t in mid for i in t["items"]):
                break
            time.sleep(0.5)
        listed_after = round(time.time() - began, 1)
        mid_record = record(c)
        reached, seen, took = wait_for(c.paths, "done")
        found = read(c.paths, thread)
        done = shape(found)
        usage = ask(c.paths, "account/usage/read", {"threadId": thread})
        rec = record(c)
        calls = [r["call"] for r in rec if r.get("call") and r.get("said") == "allow" and r.get("tool") == "Bash"]
        other = [r for r in rec if r.get("call") and r.get("tool") != "Bash"]
        changes = [i for t in (found or {}).get("turns") or [] for i in t.get("items") or [] if i.get("type") == "fileChange"]
        commands = [i for t in done for i in t["items"] if i["type"] == "commandExecution"]
        mid_commands = [i for t in mid for i in t["items"] if i["type"] == "commandExecution"]
        how = cs.stop(c.paths, STATION)
        return finish({
            "in the middle of the turn a visitor reads the command that has finished": any(
                "first-mark-4848" in i.get("command", "") and i.get("exitCode") == 0 for i in mid_commands),
            "the running command is not among the items yet": not any("sleep 21.4848" in i.get("command", "")
                                                                      and i.get("status") == "completed" for i in mid_commands),
            "but the guard's record already holds its start": len([r for r in mid_record if r.get("call")]) == 2
                and mid_record[-1].get("command") == "sleep 21.4848",
            "after the turn each command's item has the id the guard wrote down": reached and len(commands) == 2
                and [i["id"] for i in commands] == calls,
            "items read in the middle of the turn keep their ids": set(ids(mid)) <= set(ids(done)),
            "every item says when it started and ended": all("startedAtMs" in i["keys"] and "completedAtMs" in i["keys"]
                                                             for t in done for i in t["items"]),
        }, thread=thread, seconds_until_the_finished_command_was_listed=listed_after,
            guard_lines_of_other_tools=other, file_change_items=changes, mid_turn=mid, done=done,
            guard_record=rec, usage=usage, tokens=token_counts(found),
            thread_fields={k: found.get(k) for k in ("model", "modelProvider", "cliVersion", "cwd", "createdAt", "updatedAt",
                                                     "source", "reasoningEffort")} if found else None, stop=how)
    finally:
        c.end(wait=3)
        c.remove()


class Cell:
    """A Tares cell of this run's own: its own port, data folder and login, started with `tares up` of this
    checkout and ended with the run. Nothing of the person's Tares is read or written."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="tcx-cell-", dir="/tmp")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.url = f"http://127.0.0.1:{self.port}"
        # A login made for this run, good for nothing else. With TARES_PROBE_KEEP_CELL set the cell asks for
        # none and is left running at the end (its address and process id are printed), for a look at its
        # console; it listens on this machine only.
        self.keep = bool(os.environ.get("TARES_PROBE_KEEP_CELL"))
        self.token = "" if self.keep else "probe-" + secrets.token_urlsafe(18)
        tares = os.path.join(os.path.dirname(sys.executable), "tares")
        env = {**os.environ, "TARES_OTLP_GRPC_PORT": "off", "TARES_HOME": self.dir, "TARES_AUTH_TOKEN": self.token}
        for k in ("TARES_DB", "TARES_CATALOG", "TARES_FACTORY_URL", "TARESD_URL", "FACTORY_STATION", "FACTORY_ROLE"):
            env.pop(k, None)
        self.log = open(os.path.join(self.dir, "cell.log"), "ab")
        self.proc = subprocess.Popen([tares, "up", "--port", str(self.port), "--data-dir", self.dir, "--edition", "factory"],
                                     stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log, env=env, cwd=self.dir)
        for _ in range(240):
            try:
                self.http("GET", "/health")
                return
            except (OSError, RuntimeError):
                if self.proc.poll() is not None:
                    raise RuntimeError(f"the cell ended at once; see {self.dir}/cell.log")
                time.sleep(0.5)
        raise RuntimeError("the cell did not come up in 120 s")

    def http(self, method: str, path: str, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        auth = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            **auth, "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode('utf-8', 'replace')[:200]}") from None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def tares_http(self, method, path, body=None, headers=None):   # what the recorder asks of a crew's client
        return json.dumps(self.http(method, path, body, headers))

    def end(self):
        if self.keep:
            print(f"      the cell is left running at {self.url} (process {self.proc.pid}, data in {self.dir})")
            return
        self.proc.terminate()
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def as_station(station="", role="", parent=""):
    for k, v in (("FACTORY_STATION", station), ("FACTORY_ROLE", role), ("FACTORY_PARENT", parent)):
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


async def ticket_for(cell, station: str) -> dict:
    """A project with one ticket the station holds and has started on, made through the cell's own MCP tools
    (the ones a station calls), pointed at this run's cell."""
    import tares.mcp_factory as mf
    import tares.mcp_server as m
    m.TARESD = mf.TARESD = cell.url
    m.AUTH_TOKEN = cell.token
    as_station()
    project = json.loads(await m.create_project("Probe", "A probe of Codex recording"))["id"]
    await mf.write_milestone("Record", "", [], project="Probe")
    await m.write_ticket("Record a Codex session", milestone="Record", project="Probe")
    as_station("probe-orchestrator", "orchestrator")
    assigned = await mf.assign_ticket("T1", station, project="Probe")
    as_station(station, "builder", "probe-orchestrator")
    staged = await mf.set_stage("T1", "doing", project="Probe")
    return {"project": project, "assigned": str(assigned)[:200], "staged": str(staged)[:200]}


async def claim(station: str, command: str, result: str) -> str:
    import tares.mcp_factory as mf
    as_station(station, "builder", "probe-orchestrator")
    return str(await mf.add_check("T1", command, result, project="Probe"))[:200]


async def checks() -> dict:
    import tares.mcp_server as m
    as_station()
    got = json.loads(await m.get_ticket("T1", project="Probe"))
    return {c["command"]: {"match": c.get("match"), "recorded": c.get("recorded")} for c in got.get("checks") or []}


@scenario
def recorded_on_a_cell(out):
    cell = Cell()
    c = Crew()
    client = type("Client", (), {"paths": c.paths, "env": dict(os.environ), "tares_http": staticmethod(cell.tares_http)})()
    try:
        made = asyncio.run(ticket_for(cell, STATION))
        err, _ = c.start(prompt="Do these as three separate shell commands, one tool call each, waiting for each result "
                                "before the next: 1) `echo rec-a-4949` 2) `sh -c 'echo rec-b-4949; exit 7'` "
                                "3) `sleep 25.4949`. Then answer with: recorded")
        if err:
            return finish({"the start succeeded": False}, error=err, made=made)
        thread = c.row().get("sessionId")
        for _ in range(90):
            if running("sleep 25.4949"):
                break
            time.sleep(1)
        time.sleep(3)   # an item is listed a moment after it ends
        first = codex_record.step(client)
        mid = cell.http("GET", f"/api/crew/{STATION}?recent=50&conversation=50")
        mid_text = " | ".join(x["text"] for x in mid.get("recent") or [])
        reached, seen, took = wait_for(c.paths, "done")
        second = codex_record.step(client)
        third = codex_record.step(client)    # the look after the turn ended closes it
        again = codex_record.step(client)
        done = cell.http("GET", f"/api/crew/{STATION}?recent=50&conversation=50")
        done_text = " | ".join(x["text"] for x in done.get("recent") or [])
        calls = [r["call"] for r in record(c) if r.get("call") and r.get("said") == "allow"]
        tool = cell.http("GET", f"/api/crew/{STATION}/tool/codex:{thread}:{calls[1]}") if len(calls) > 1 else {}
        claims = [asyncio.run(claim(STATION, "echo rec-a-4949", "passed")),
                  asyncio.run(claim(STATION, "sh -c 'echo rec-b-4949; exit 7'", "passed")),
                  asyncio.run(claim(STATION, "pytest -q", "12 passed"))]
        matched = asyncio.run(checks())

        # the app-server is killed in the middle of a command
        got = cs.hand(c.paths, STATION, "Run the shell command `sleep 61.4949` once, then answer with: slept")
        for _ in range(90):
            if running("sleep 61.4949"):
                break
            time.sleep(1)
        before_kill = codex_record.step(client)
        pid = (c.row().get("codex") or {}).get("pid")
        cs.note_children(c.paths, STATION)
        os.kill(pid, 9)
        time.sleep(2)
        cs.end_leftovers(c.paths, STATION, grace=5)
        c.end(wait=3)
        down = codex_record.step(client)
        gone = cell.http("GET", f"/api/crew/{STATION}?recent=5")
        err2, _ = c.start(resume=thread)      # a new app-server, no prompt
        back1 = codex_record.step(client)
        back2 = codex_record.step(client)
        back3 = codex_record.step(client)
        asyncio.run(claim(STATION, "sleep 61.4949", "passed"))
        matched2 = asyncio.run(checks())
        after = cell.http("GET", f"/api/crew/{STATION}?recent=50")
        after_text = " | ".join(x["text"] for x in after.get("recent") or [])
        usage = after.get("usage")
        cs.stop(c.paths, STATION)
        codex_record.step(client)
        ended = cell.http("GET", f"/api/crew/{STATION}?recent=5")
        return finish({
            "the cell took every post": not [r for r in first + second + third + again + before_kill + down + back1 + back2 + back3 if r["error"]],
            "in the middle of the turn the station is on the crew's API, at work, in its conversation":
                mid.get("session") == thread and mid.get("state") == "working" and mid.get("role") == "reviewer",
            "with the two commands that ended and the one that runs": "rec-a-4949" in mid_text and "exit 7" in mid_text
                and "sleep 25.4949" in mid_text,
            "after the turn it is idle, and its answer is there": done.get("state") == "idle" and "recorded" in done_text,
            "a round with nothing new sends nothing": [r["sent"] for r in again] == [0],
            "a command's whole output is read by its call": "rec-b-4949" in str(tool.get("output")) and tool.get("failed") is True,
            "a check the station really ran is matched": (matched.get("echo rec-a-4949") or {}).get("match") == "matched",
            "a command that exited 7 does not back a claim that it passed":
                (matched.get("sh -c 'echo rec-b-4949; exit 7'") or {}).get("match") == "differs",
            "a command that never ran is not found": (matched.get("pytest -q") or {}).get("match") == "not_found",
            "the kill: before it, the running command was recorded as started": [r["sent"] for r in before_kill] != [0],
            "with the app-server gone the session has ended": gone.get("state") == "ended",
            "after a new start the command's outcome is recorded as unknown": not err2 and "outcome unknown" in after_text,
            "and it backs no claim": (matched2.get("sleep 61.4949") or {}).get("match") == "differs"
                and "not known" in str((matched2.get("sleep 61.4949") or {}).get("recorded")),
            "tokens were recorded, and no cost was invented": isinstance(usage, dict)
                and (usage.get("input_tokens") or 0) + (usage.get("cache_read_tokens") or 0) > 0 and not usage.get("cost_usd"),
            "a stopped station's session has ended": ended.get("state") == "ended",
        }, error=err2, made=made, thread=thread, steps={"first": first, "second": second, "third": third, "again": again,
                                                         "before_kill": before_kill, "down": down, "back": [back1, back2, back3]},
            states_seen=seen, claims=claims, matched=matched, matched_after_the_kill=matched2, usage=usage, tool=tool,
            mid_turn_lines=mid_text[:1500], lines_after_the_kill=after_text[-1500:],
            row={k: after.get(k) for k in ("station", "role", "session", "state", "lines", "crew", "machine", "project")})
    finally:
        as_station()
        c.end(wait=3)
        c.remove()
        cell.end()


@scenario
def through_the_watch(out):
    """The recorder as the watch runs it (PR 947): the watch's Codex step, and the last recording of a stop.
    Nothing calls the recorder by hand."""
    from tares.crew import codex_round
    cell = Cell()
    c = Crew()
    kept = {k: os.environ.get(k) for k in ("TARESD_URL", "TARES_FACTORY_URL", "TARES_AUTH_TOKEN", "FACTORY_CREW_HOME")}
    # the watch and a stop reach the cell their process names, with the person's key
    os.environ.update(TARESD_URL=cell.url, TARES_FACTORY_URL=cell.url, TARES_AUTH_TOKEN=cell.token,
                      FACTORY_CREW_HOME=c.home)
    try:
        err, _ = c.start(prompt="Do these as two separate shell commands, one tool call each, waiting for the first "
                                "to end before the second: 1) `echo watch-a-4949` 2) `sleep 61.4949`. Then answer: done")
        if err:
            return finish({"the start succeeded": False}, error=err)
        thread = c.row().get("sessionId")
        for _ in range(90):
            if running("sleep 61.4949"):
                break
            time.sleep(1)
        time.sleep(3)   # an item is listed a moment after it ends
        codex_round.step(c.paths, dict(os.environ))
        mid = cell.http("GET", f"/api/crew/{STATION}?recent=50")
        mid_text = " | ".join(x["text"] for x in mid.get("recent") or [])
        how = cs.stop(c.paths, STATION)   # no round after this: the stop records last
        ended = cell.http("GET", f"/api/crew/{STATION}?recent=50")
        ended_text = " | ".join(x["text"] for x in ended.get("recent") or [])
        codex_round.step(c.paths, dict(os.environ))
        again = cell.http("GET", f"/api/crew/{STATION}?recent=50")
        return finish({
            "a watch round in the middle of the turn put the station on the cell, at work, in its conversation":
                mid.get("session") == thread and mid.get("state") == "working",
            "with the command that ended and the one that runs": "watch-a-4949" in mid_text and "sleep 61.4949" in mid_text,
            "the stop alone, with no round after it, ended the session": ended.get("state") == "ended" and how in ("stopped", "killed"),
            "and recorded the command it cut off as one whose outcome is not known": "outcome unknown" in ended_text,
            "a round after the stop changes nothing": again.get("lines") == ended.get("lines"),
        }, thread=thread, stop=how, mid_turn_lines=mid_text[:1200], lines_after_the_stop=ended_text[-1200:])
    finally:
        for k, v in kept.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        c.end(wait=3)
        c.remove()
        cell.end()


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
