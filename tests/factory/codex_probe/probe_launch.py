"""Does the crew's own launch code start a Codex station the way TR-1592 asks, on the real Codex? A by-hand
probe, not in CI: it needs a signed-in `codex` and spends two very small turns.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_launch.py [--out DIR] [--only NAME ...]

The station is started by the product code (`tares.crew.codex.start`) from the spec the product's Codex runtime
builds (`RuntimeCodex().launch_spec`), in a throwaway crew home under /tmp (a socket's path holds about a
hundred bytes, and the system's temp folder can be half of that). The cell is not in the loop: the placeholders
are filled here, as `launch.launch_plan` fills them. The tests with a fake Codex are
tests/test_crew_launch_codex.py; this is the same start against the real one.

Scenarios:
  start_without_claude       no `claude` on PATH: the app-server comes up detached, the path leads to a socket
                             that is the owner's alone (on macOS a link to one Codex keeps under /tmp),
                             `initialize` answers from the station's own Codex folder, and a second start is
                             refused while it runs; ended, it exits 0 and takes its socket with it
  stale_socket               killed, the app-server leaves its socket; the next start is not blocked by it
  personal_setup_stays_out   one turn on the started station with the spec's thread settings: no MCP server and
                             no plugin is loaded, the guard's hook runs, the conversation is kept in the
                             station's folder, the person's own Codex files are untouched, and no program is
                             started that carries the prompt
  notify_program_control     the same turn in a Codex folder whose config names a notify program (the probe's
                             own, which writes down what it is handed): it is started at the end of the turn
                             with the prompt, `config/read` shows it, and the watcher above sees it
  account_extras_are_caught  the same start without the flags that turn the account's connectors and plugins
                             off: the product's check names what was loaded and stops the station
  older_codex_is_refused     an older real Codex (TARES_PROBE_CODEX_OLD, else the one inside the ChatGPT app) is
                             refused before anything starts; skipped when there is none
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import standin_mcp  # noqa: E402
from appserver import AppServer, codex_bin, items, notifications  # noqa: E402

from tares.crew import codex as cx  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
from tares.integrations.builtin.runtime_codex import LEAN_FLAGS, RuntimeCodex  # noqa: E402

MODEL = os.environ.get("TARES_PROBE_MODEL", "gpt-5.6-luna")
EFFORT = os.environ.get("TARES_PROBE_EFFORT", "low")
OLD_CODEX = os.environ.get("TARES_PROBE_CODEX_OLD") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
PERSONAL = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
STATION = "probe-reviewer"
SCENARIOS = {}
TARES_KEY = "probe-key-" + os.urandom(6).hex()   # made for this run, good for nothing else
_TARES: list = []


def tares_mcp() -> int:
    """The port of the stand-in for the crew's MCP endpoint, started at its first use."""
    if not _TARES:
        _TARES.append(standin_mcp.serve(TARES_KEY)[0])
    return _TARES[0]


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def path_without_claude() -> str:
    """This PATH without every folder that holds a `claude`, with the folder of the codex under test, and with
    this interpreter's folder first when it holds a `tares` (the checkout's virtual environment): a station's
    guard is `tares hook codex-guard`, found on the station's PATH, and a station that cannot run it is
    stopped at its first turn (TR-1594)."""
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not os.path.exists(os.path.join(d, "claude"))]
    mine = os.path.dirname(sys.executable)
    first = [mine] if os.access(os.path.join(mine, "tares"), os.X_OK) and not os.path.exists(os.path.join(mine, "claude")) else []
    return os.pathsep.join([*first, os.path.dirname(codex_bin()), *keep])


class Crew:
    """A throwaway crew home and a station's folder, and the plan the product builds for one station."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="tcx-probe-", dir="/tmp")
        self.home = os.path.join(self.root, "crew")
        self.work = os.path.join(self.root, "work")
        os.makedirs(os.path.join(self.home, "stations"))
        os.makedirs(self.work)
        subprocess.run(["git", "init", "-q", self.work], check=True)
        with open(os.path.join(self.home, "stations.json"), "w", encoding="utf-8") as f:
            f.write("{}\n")
        self.paths = CrewPaths.at(self.home)
        self.client = SimpleNamespace(paths=self.paths, env=dict(os.environ))
        self.left_behind: list = []

    def plan(self, lean: bool = True) -> dict:
        # a station does not start without the crew's MCP endpoint (TR-1596): a stand-in, and a key made for this run
        key = standin_mcp.key_file(self.home, TARES_KEY)
        spec = RuntimeCodex().launch_spec("reviewer", STATION, {
            "crew": "probe", "crew_autonomy": "L3-review", "human": "the person", "model": MODEL, "effort": EFFORT,
            "tares_url": "http://127.0.0.1:9", "mcp_url": f"http://127.0.0.1:{tares_mcp()}/mcp", "token_file": True,
            "rules": {"allow": ["Work in {where} is expected work for this station."], "soft_deny": []}})
        values = {"CREW_HOME": self.home, "TOKEN_FILE": key, "TOKEN_FILE_SHELL": shlex.quote(key),
                  "PLUGIN_DIR_SHELL": os.path.join(ROOT, "claude-plugin"), "REPOS": "the probe's folder",
                  "WT_REPOS": "the probe's folder", **cx.machine_values(self.paths, STATION)}
        plan = cx.plan({"command": spec.command, "env": spec.env, "settings": spec.settings}, values)
        if not lean:
            plan["args"] = [a for a in plan["args"] if a not in LEAN_FLAGS]
        return plan

    def start(self, plan: dict | None = None, codex: str | None = None, prompt: str = "", resume: str = ""):
        """(error text or None, the station's record). With no prompt the station gets no turn: the scenarios
        here are about the start itself, and probe_station.py is about what follows."""
        plan = plan or self.plan()
        with open(os.path.join(self.home, "stations", f"{STATION}.settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": plan["env"], **plan["settings"]}, f)   # as launch.write_settings leaves it
        try:
            cx.start(self.client, STATION, plan, self.work, prompt,
                     {"role": "reviewer", "model": MODEL, "effort": EFFORT, "cwd": self.work, "sessionId": resume},
                     codex=codex or codex_bin())
            err = None
        except CrewError as e:
            err = str(e)
        return err, self.row()

    def row(self) -> dict:
        try:
            with open(os.path.join(self.home, "stations.json"), encoding="utf-8") as f:
                return json.load(f).get(STATION) or {}
        except (OSError, ValueError):
            return {}

    def end(self, sig=signal.SIGTERM, wait: float = 15):
        """End the recorded app-server; returns its exit code as this process saw it, or how it went."""
        info = self.row().get("codex") or {}
        pid = info.get("pid")
        if not pid:
            return "not started"
        if sig == signal.SIGKILL and os.path.islink(info.get("socket", "")):
            self.left_behind.append(os.path.realpath(info["socket"]))   # a kill leaves Codex's own socket in /tmp
        try:
            os.kill(pid, sig)
        except OSError:
            return "already gone"
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                got, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return "gone"
            if got:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        return f"killed after {wait:g} s"

    def remove(self):
        for path in self.left_behind:
            for left in (path, path + ".lock"):
                if not os.path.exists(os.path.realpath(left)) or not self._listening(left):
                    try:
                        os.unlink(left)
                    except OSError:
                        pass
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _listening(path: str) -> bool:
        import socket
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(path)
            return True
        except OSError:
            return False
        finally:
            s.close()


def digest(path: str):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return None


def personal_state() -> dict:
    n = sum(len(files) for _, _, files in os.walk(os.path.join(PERSONAL, "sessions")))
    return {"config.toml": digest(os.path.join(PERSONAL, "config.toml")), "auth.json": digest(os.path.join(PERSONAL, "auth.json")),
            "session files": n}


class PromptWatch:
    """Looks, a few times a second, for any process whose command line carries `marker` (the prompt does)."""

    def __init__(self, marker: str):
        self.marker, self.seen, self._stop = marker, {}, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            ps = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, errors="replace").stdout
            for line in ps.splitlines():
                if self.marker in line and "probe_launch.py" not in line:
                    pid = line.strip().partition(" ")[0]
                    if pid not in self.seen:   # the program's own name, without its arguments
                        comm = subprocess.run(["ps", "-o", "comm=", "-p", pid], capture_output=True, text=True, errors="replace").stdout
                        self.seen[pid] = os.path.basename(comm.strip()) or "(ended before it could be named)"
            time.sleep(0.15)

    def stop(self, linger: float = 4) -> list:
        time.sleep(linger)   # a notify program starts at the end of the turn
        self._stop.set()
        self._t.join(2)
        return sorted(set(self.seen.values()))


def one_turn(app: AppServer, thread_params: dict, cwd: str, marker: str) -> dict:
    """Start a thread and run one small turn whose prompt carries `marker`. Approvals are accepted and counted."""
    asked = []

    def on_request(msg):
        if str(msg.get("method", "")).endswith("requestApproval"):
            asked.append(msg["method"])
            return {"decision": "accept"}
        return None
    app.on_server_request = on_request
    started = app.request("thread/start", {**thread_params, "cwd": cwd})
    thread = started["thread"]
    listed = app.request("hooks/list", {"threadId": thread["id"]})
    watch = PromptWatch(marker)
    turn = app.run_turn(thread["id"], f"Run the shell command `echo launch-probe` once, then answer with: done {marker}",
                        timeout=180, effort=EFFORT)
    programs = watch.stop()
    return {"thread": thread, "turn": turn["status"], "approvals_asked": asked,
            "hooks_listed": [h.get("eventName") for d in listed.get("data", []) for h in d.get("hooks", [])],
            "commands": [{"command": i.get("command"), "exit": i.get("exitCode")} for i in items(app.log, kind="commandExecution")],
            "pre_tool_hook_runs": [n["run"].get("status") for n in notifications(app.log, "hook/completed")
                                   if n.get("run", {}).get("eventName") == "preToolUse"],
            "programs_that_carried_the_prompt": programs}


def finish(checks: dict, **facts) -> dict:
    return {"pass": all(checks.values()), "checks": checks, **facts}


# ---------------------------------------------------------------------------------------------


@scenario
def start_without_claude(out):
    c = Crew()
    try:
        err, row = c.start()
        info = row.get("codex") or {}
        sock = info.get("socket", "")
        found = os.stat(sock) if sock and os.path.exists(sock) else None   # through the link Codex makes of the path
        mode = oct(stat.S_IMODE(found.st_mode)) if found else None
        is_link, kept_in = os.path.islink(sock), os.path.dirname(os.path.realpath(sock)) if sock else ""
        folder_mode = oct(stat.S_IMODE(os.stat(kept_in).st_mode)) if is_link else None
        hello = {}
        if sock and os.path.exists(sock):
            app = AppServer.connect(sock, os.path.join(out, "start.wire.jsonl"))
            hello = app.initialize()
            app.close()
        again, _ = c.start()
        same = (c.row().get("codex") or {}).get("pid") == info.get("pid")
        try:
            sid = os.getsid(info["pid"])
        except (KeyError, OSError):
            sid = None
        code = c.end()
        return finish({
            "there is no claude on the PATH of this run": shutil.which("claude") is None,
            "the start succeeded": err is None,
            "the path leads to a socket, the owner's alone": bool(found) and stat.S_ISSOCK(found.st_mode)
                and found.st_uid == os.getuid() and mode == "0o600",
            "where the path is a link, the folder that holds the socket is the owner's alone": folder_mode in (None, "0o700"),
            "initialize answers, from the station's own Codex folder":
                bool(hello) and os.path.realpath(hello.get("codexHome", "")) == os.path.realpath(info.get("home", "-")),
            "the app-server runs in a session of its own": sid is not None and sid == info.get("pid") and sid != os.getsid(0),
            "a second start while it runs is refused and leaves it alone": bool(again) and "is running" in again and same,
            "asked to end, it exits 0 and takes its socket with it": code == 0 and not os.path.exists(sock),
        }, error=err, version=info.get("version"), socket_mode=mode, path_is_a_link=is_link,
            socket_kept_in=kept_in.replace(str(os.getuid()), "<uid>") if is_link else None, folder_mode=folder_mode,
            second_start=again.replace(c.root, "<crew root>") if again else again, exit_code=code)
    finally:
        c.end(signal.SIGKILL, 3)
        c.remove()


@scenario
def stale_socket(out):
    c = Crew()
    try:
        err, row = c.start()
        first = (row.get("codex") or {}).get("pid")
        sock = (row.get("codex") or {}).get("socket", "")
        target = os.path.realpath(sock)
        c.end(signal.SIGKILL)
        left = os.path.lexists(sock) and os.path.exists(target)
        err2, row2 = c.start()
        second = (row2.get("codex") or {}).get("pid")
        answers = False
        if err2 is None:
            app = AppServer.connect(sock, os.path.join(out, "stale.wire.jsonl"))
            answers = bool(app.initialize().get("codexHome"))
            app.close()
        code = c.end()
        return finish({
            "the first start succeeded": err is None,
            "killed, the app-server left its socket behind": left,
            "the next start is not blocked by it": err2 is None and bool(second) and second != first,
            "the new app-server answers on the socket": answers,
        }, first_error=err, second_error=err2, exit_code_of_the_second=code, path_is_a_link=target != sock)
    finally:
        c.end(signal.SIGKILL, 3)
        c.remove()


@scenario
def personal_setup_stays_out(out):
    c = Crew()
    before = personal_state()
    marker = "promptmark" + os.urandom(4).hex()
    try:
        plan = c.plan()
        err, row = c.start(plan)
        info = row.get("codex") or {}
        if err:
            return finish({"the start succeeded": False}, error=err)
        app = AppServer.connect(info["socket"], os.path.join(out, "personal_out.wire.jsonl"))
        app.initialize()
        servers = [d.get("name") for d in app.request("mcpServerStatus/list", {}).get("data", [])]
        plugins = app.request("plugin/installed", {}).get("marketplaces", [])
        config = app.request("config/read", {}).get("config") or {}
        res = one_turn(app, plan["settings"]["thread"], c.work, marker)
        app.close()
        home = info["home"]
        wrote = []   # what Codex itself put in the station's config during the run (the key names, never a value)
        try:
            import tomllib
            with open(os.path.join(home, "config.toml"), "rb") as f:
                wrote = sorted(tomllib.load(f).keys())
        except (OSError, ValueError):
            pass
        rollout = str(res["thread"].get("path") or "")
        link = os.path.join(home, "auth.json")
        after = personal_state()
        try:
            import tomllib
            with open(os.path.join(PERSONAL, "config.toml"), "rb") as f:
                personal_keys = set(tomllib.load(f).keys())
        except (OSError, ValueError):
            personal_keys = set()
        code = c.end()
        return finish({
            "the turn completed": res["turn"] == "completed",
            "no MCP server is loaded": servers == [],
            "no plugin is installed": plugins == [],
            "no notify program is configured": not config.get("notify"),
            "the guard is the thread's only hook (every tool call, the start of every turn), and it ran before the command":
                sorted(res["hooks_listed"]) == ["preToolUse", "userPromptSubmit"] and "completed" in res["pre_tool_hook_runs"],
            "the command ran": any("launch-probe" in (x["command"] or "") and x["exit"] == 0 for x in res["commands"]),
            "no program was started that carries the prompt": res["programs_that_carried_the_prompt"] == [],
            "the conversation is kept in the station's own folder": os.path.realpath(rollout).startswith(os.path.realpath(home) + os.sep),
            "the person's config, login and conversations are as they were": before == after,
            "the login is still a link to the person's file": os.path.islink(link)
                and os.path.realpath(link) == os.path.realpath(os.path.join(PERSONAL, "auth.json")),
        }, turn=res["turn"], approvals_asked=res["approvals_asked"], commands=res["commands"],
            hook_runs=res["pre_tool_hook_runs"], mcp_servers=servers,
            programs_that_carried_the_prompt=res["programs_that_carried_the_prompt"],
            station_folder_entries=sorted(os.listdir(home)), config_keys_codex_wrote_during_the_run=wrote,
            the_persons_own_config_has={k: k in personal_keys for k in ("notify", "mcp_servers", "plugins")}, exit_code=code)
    finally:
        c.end(signal.SIGKILL, 3)
        c.remove()


NOTIFY = r'''
import json, os, sys, time
with open(os.environ["PROBE_NOTIFY_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
time.sleep(2)   # long enough for the watcher, which looks a few times a second
'''


@scenario
def notify_program_control(out):
    """Not the product's start: a Codex folder that has a notify program in its config, as a person's own may
    (TR-1576 met one). The program is the probe's own and writes down what it is handed."""
    root = tempfile.mkdtemp(prefix="tcx-probe-", dir="/tmp")
    work, home = os.path.join(root, "work"), os.path.join(root, "codex")
    os.makedirs(work)
    os.makedirs(home, mode=0o700)
    subprocess.run(["git", "init", "-q", work], check=True)
    os.symlink(os.path.realpath(os.path.join(PERSONAL, "auth.json")), os.path.join(home, "auth.json"))
    script, handed = os.path.join(root, "notify_probe.py"), os.path.join(root, "handed.jsonl")
    with open(script, "w", encoding="utf-8") as f:
        f.write(NOTIFY)
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as f:
        f.write(f"notify = [{json.dumps(sys.executable)}, {json.dumps(script)}]\n")
    marker = "promptmark" + os.urandom(4).hex()
    env = {**os.environ, "CODEX_HOME": home, "PROBE_NOTIFY_LOG": handed}
    app = AppServer.spawn(codex_bin(), args=list(LEAN_FLAGS), cwd=work, env=env,
                          transcript_path=os.path.join(out, "notify_control.wire.jsonl"),
                          stderr_path=os.path.join(out, "notify_control.stderr.log"))
    try:
        app.initialize()
        seen_in_config = bool((app.request("config/read", {}).get("config") or {}).get("notify"))
        res = one_turn(app, {"model": MODEL, "approvalPolicy": "on-request", "sandbox": "workspace-write", "ephemeral": True}, work, marker)
        try:
            with open(handed, encoding="utf-8") as f:
                calls = [json.loads(line) for line in f if line.strip()]
        except OSError:
            calls = []
        return finish({
            "the turn completed": res["turn"] == "completed",
            "config/read shows the notify program (what the product's check reads)": seen_in_config,
            "the program was started at the end of the turn and handed the prompt": any(marker in json.dumps(c) for c in calls),
            "the watcher saw a program that carries the prompt": bool(res["programs_that_carried_the_prompt"]),
        }, times_started=len(calls), what_it_was_handed=sorted({k for c in calls for a in c for k in _keys(a)}),
            programs_that_carried_the_prompt=res["programs_that_carried_the_prompt"])
    finally:
        app.close()
        shutil.rmtree(root, ignore_errors=True)


def _keys(arg: str) -> list:
    try:
        doc = json.loads(arg)
    except ValueError:
        return []
    return list(doc) if isinstance(doc, dict) else []


@scenario
def account_extras_are_caught(out):
    c = Crew()
    try:
        err, row = c.start(c.plan(lean=False))
        sock = cx.socket_path(c.paths, STATION)
        log = os.path.join(c.home, "stations", f"{STATION}.codex.log")
        return finish({
            "the start is refused": err is not None,
            "the refusal names an MCP server the launch did not name": bool(err) and "loaded an MCP server its launch does not name" in err,
            "the station is not recorded": not row,
            "the app-server is stopped and its socket is gone": not os.path.exists(sock),
        }, error=err, log_exists=os.path.exists(log))   # the refusal is the product's own sentence
    finally:
        c.end(signal.SIGKILL, 3)
        c.remove()


@scenario
def older_codex_is_refused(out):
    if not os.path.exists(OLD_CODEX):
        return {"pass": True, "skipped": f"no older codex at {OLD_CODEX}"}
    c = Crew()
    try:
        said = cx.codex_version(OLD_CODEX)
        err, row = c.start(codex=OLD_CODEX)
        return finish({
            "the start is refused as older, with the version it found": bool(err) and "is older than 0.171.0" in err,
            "nothing was started or recorded": not row and not os.path.exists(cx.socket_path(c.paths, STATION)),
        }, version_line=said, error=err)
    finally:
        c.end(signal.SIGKILL, 3)
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
        print("SKIP" if r.get("skipped") else "PASS" if r.get("pass") else "FAIL", name, r.get("skipped") or r.get("error") or "")
        for k, v in (r.get("checks") or {}).items():
            print(f"      {'ok ' if v else 'NO '} {k}")
    with open(os.path.join(args.out, "launch.json"), "w", encoding="utf-8") as f:
        json.dump({"codex": cx.codex_version(codex_bin()), "model": MODEL, "results": results}, f, indent=1, ensure_ascii=False)
    return 0 if all(results[name].get("pass") for name, _ in todo) else 1


if __name__ == "__main__":
    sys.exit(main())
