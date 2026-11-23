"""Who has to stay alive for a Codex station to keep working? A by-hand probe (TR-1576), not in CI: it needs a
signed-in `codex` and spends a little of that account's quota.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_transport.py [--out DIR] [--only NAME ...]

Two ways to run the app-server are compared. On a Unix socket (`--listen unix://PATH`) it is its own detached
process and a controller connects to it; on stdio it is the child of whoever started it. Each scenario breaks
one thing in the middle of a turn (the controller hangs up, the app-server is killed, the crew goes down) and
records what the turn, its command and its events did. Threads here are stored, not ephemeral, so they can be
resumed; each is deleted at the end.

The result is `results.json` in the out folder, with the wire transcripts next to it.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appserver import AppServer, AppServerError, codex_bin, items, notifications  # noqa: E402
from fixture import HERE, World, guard_hooks  # noqa: E402
from probe_guard import EFFORT, MODEL, QUIET, RULES  # noqa: E402

GUARD_HOOKS = guard_hooks(f"{sys.executable} {os.path.join(HERE, 'codex_guard.py')}")


def pids_of(marker: str) -> list[int]:
    r = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split() if p.isdigit() and int(p) != os.getpid()]


def wait_until(pred, timeout: float, step: float = 0.25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


class Runtime:
    """A detached app-server on a private Unix socket, as a station's runtime would be."""

    def __init__(self, out: str, name: str, world: World, role: str = "builder"):
        self.out, self.name, self.world, self.role = out, name, world, role
        self.sock = os.path.join(world.root, "app.sock")
        self.proc = None
        self.n = 0
        self.start()

    def start(self) -> None:
        log = open(os.path.join(self.out, f"{self.name}.server.log"), "ab")
        self.proc = subprocess.Popen([codex_bin(), "app-server", "--listen", "unix://" + self.sock, *QUIET],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=self.world.work,
                                     env=self.world.env(self.role), start_new_session=True)
        if not wait_until(lambda: os.path.exists(self.sock), 20):
            raise AppServerError("the app-server did not open its socket")

    def controller(self, label: str) -> AppServer:
        self.n += 1
        a = AppServer.connect(self.sock, os.path.join(self.out, f"{self.name}.{self.n}-{label}.wire.jsonl"), name=label)
        a.initialize()
        return a

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, sig=signal.SIGTERM, wait: float = 15):
        if self.alive():
            self.proc.send_signal(sig)
        try:
            return self.proc.wait(wait)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return "killed after %ss" % wait


def start_thread(app: AppServer, world: World, approval: str = "never", sandbox: str = "danger-full-access") -> str:
    started = app.request("thread/start", {
        "cwd": world.work, "config": {"hooks": GUARD_HOOKS, "bypass_hook_trust": True},
        "approvalPolicy": approval, "sandbox": sandbox, "model": MODEL, "developerInstructions": RULES})
    return started["thread"]["id"]


def resume(app: AppServer, world: World, thread: str) -> dict:
    return app.request("thread/resume", {"threadId": thread, "cwd": world.work,
                                         "config": {"hooks": GUARD_HOOKS, "bypass_hook_trust": True}})


def begin(app: AppServer, thread: str, text: str) -> str:
    started = app.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": text}], "effort": EFFORT})
    return (started.get("turn") or {}).get("id")


def saw_command(app: AppServer, needle: str, timeout: float = 90) -> dict | None:
    """Wait for the commandExecution item whose command holds `needle` to start."""
    ev = app.wait_for(lambda e: e.get("method") == "item/started"
                      and ((e.get("params") or {}).get("item") or {}).get("type") == "commandExecution"
                      and needle in (((e.get("params") or {}).get("item") or {}).get("command") or ""), timeout=timeout)
    return ((ev or {}).get("params") or {}).get("item") if ev else None


def history(app: AppServer, thread: str) -> list[dict]:
    """The stored turns of a thread: [{id, status, items: [{type, command, status, exit}]}]."""
    read = app.request("thread/read", {"threadId": thread, "includeTurns": True})
    out = []
    for turn in (read.get("thread") or {}).get("turns") or []:
        out.append({"id": turn.get("id"), "status": turn.get("status"),
                    "items": [{"type": i.get("type"), "command": i.get("command"), "status": i.get("status"),
                               "exit": i.get("exitCode")} for i in turn.get("items") or []
                              if i.get("type") in ("commandExecution", "mcpToolCall", "agentMessage")]})
    return out


def forget(app: AppServer, thread: str) -> None:
    for method in ("thread/archive", "thread/delete"):
        try:
            app.request(method, {"threadId": thread}, timeout=20)
        except AppServerError:
            pass


def scenario(fn):
    fn.is_scenario = True
    return fn


# -- the scenarios ------------------------------------------------------------------------------------------

@scenario
def socket_basics(out):
    """The socket is private (owner only), two controllers can be connected at once, and the app-server
    outlives them both."""
    w = World()
    rt = Runtime(out, "socket_basics", w)
    mode = stat.filemode(os.stat(rt.sock).st_mode)
    a, b = rt.controller("A"), rt.controller("B")
    a.close(), b.close()
    time.sleep(1)
    alive = rt.alive()
    code = rt.stop()
    return {"pass": mode == "srw-------" and alive and code == 0, "socket_mode": mode,
            "alive_with_no_controller": alive, "exit_on_sigterm": code, "socket_removed": not os.path.exists(rt.sock)}


@scenario
def controller_disconnect_mid_turn(out):
    """The controller hangs up while a command of the turn is running. Does the turn go on with nobody
    listening, and can a new controller get back what it missed?"""
    w = World()
    rt = Runtime(out, "controller_disconnect", w)
    a = rt.controller("A")
    thread = start_thread(a, w)
    turn = begin(a, thread, "Step 1: run `sleep 11.3131`. Step 2: run `echo two > step2.txt`. Step 3: run `echo three`.")
    started = saw_command(a, "sleep 11.3131")
    a.close()                                   # the controller is gone, mid-command
    gone_at = time.time()
    step2 = wait_until(lambda: os.path.exists(os.path.join(w.work, "step2.txt")), 60)
    b = rt.controller("B")
    loaded = b.request("thread/loaded/list", {}).get("data")
    resumed = resume(b, w, thread)
    status_on_resume = ((resumed.get("thread") or {}).get("status") or {}).get("type")
    done = b.wait_for(lambda e: e.get("method") == "turn/completed", timeout=45)
    hist = history(b, thread)
    live_after = sorted({(e["msg"].get("method")) for e in b.log if e["dir"] == "in" and "method" in e["msg"]})
    cmds = [i for t in hist for i in t["items"] if i["type"] == "commandExecution"]
    forget(b, thread)
    b.close()
    rt.stop()
    return {"pass": bool(started) and step2 and len(cmds) >= 3,
            "turn_went_on_without_a_controller": step2, "thread_still_loaded": thread in (loaded or []),
            "thread_status_on_resume": status_on_resume,
            "new_controller_saw_turn_completed_live": bool(done),
            "stored_turns": [{"status": t["status"], "commands": [(i["command"], i["status"], i["exit"])
                                                                   for i in t["items"] if i["type"] == "commandExecution"]}
                             for t in hist],
            "live_methods_after_reconnect": live_after, "turn_id": turn}


@scenario
def pending_approval_across_disconnect(out):
    """A command waits for approval; the controller that was asked hangs up without answering. Nothing may
    run on its own. Is the question put again to the next controller?"""
    w = World()
    rt = Runtime(out, "pending_approval", w)
    a = rt.controller("A")
    thread = start_thread(a, w, approval="untrusted", sandbox="workspace-write")
    begin(a, thread, "Step 1: run `touch approval-probe.txt`. Step 2: run `echo after`.")
    asked = a.wait_for(lambda e: e.get("method") == "item/commandExecution/requestApproval", timeout=90)
    asked_params = (asked or {}).get("params") or {}
    a.close()
    time.sleep(4)
    ran_unapproved = os.path.exists(os.path.join(w.work, "approval-probe.txt"))
    b = rt.controller("B")
    resumed = resume(b, w, thread)
    status = (resumed.get("thread") or {}).get("status")
    again = b.wait_for(lambda e: e.get("method") == "item/commandExecution/requestApproval", timeout=20)
    outcome = "not asked again"
    if again:
        same = (again.get("params") or {}).get("itemId") == asked_params.get("itemId")
        outcome = "asked again (same item)" if same else "asked again (another item)"
        # what the turn asks after this one (step 2) is allowed, so the turn can end by itself
        b.on_server_request = lambda m: {"decision": "accept"} if m["method"].endswith("requestApproval") else None
        b.respond(again["id"], {"decision": "decline"})
    done = b.wait_for(lambda e: e.get("method") == "turn/completed", timeout=60)
    turn_status = ((((done or {}).get("params") or {}).get("turn")) or {}).get("status")
    hist = history(b, thread)
    resolved = len(notifications(b.log, "serverRequest/resolved"))
    forget(b, thread)
    b.close()
    rt.stop()
    exists = os.path.exists(os.path.join(w.work, "approval-probe.txt"))
    return {"pass": bool(asked) and bool(again) and not ran_unapproved and not exists and turn_status == "completed",
            "turn_status_after_the_decline": turn_status, "approval_was_requested": bool(asked), "request": {k: asked_params.get(k) for k in ("command", "kind", "reason")},
            "ran_while_nobody_answered": ran_unapproved, "thread_status_on_resume": status, "after_reconnect": outcome,
            "file_exists_at_end": exists, "resolved_notifications_seen_by_B": resolved,
            "stored_turns": [{"status": t["status"]} for t in hist]}


@scenario
def two_controllers(out):
    """Two controllers on one thread. Do both get the events and the approval question, whose answer counts,
    and what does a second turn/start do while a turn is running? (Codex does not fence controllers: if
    this shows both can act, the station must allow one.)"""
    w = World()
    rt = Runtime(out, "two_controllers", w)
    a, b = rt.controller("A"), rt.controller("B")
    thread = start_thread(a, w, approval="untrusted", sandbox="workspace-write")
    try:
        resume(b, w, thread)
        resume_before_first_turn = "ok"
    except AppServerError as e:
        resume_before_first_turn = str(e)[:160]
    turn = begin(a, thread, "Step 1: run `touch two-controllers.txt`. Step 2: run `sleep 6.4141`. Step 3: run `echo end`.")
    pa = a.wait_for(lambda e: e.get("method") == "item/commandExecution/requestApproval", timeout=90)
    resume(b, w, thread)                          # B joins while the question is open
    pb = b.wait_for(lambda e: e.get("method") == "item/commandExecution/requestApproval", timeout=15)
    second_answer = None
    if pb:
        b.respond(pb["id"], {"decision": "decline"})         # B answers first: no
    time.sleep(1.5)
    if pa:
        try:
            a.respond(pa["id"], {"decision": "accept"})      # A answers late: yes
            second_answer = "sent"
        except AppServerError as e:
            second_answer = f"refused: {e}"
    time.sleep(2)
    created = os.path.exists(os.path.join(w.work, "two-controllers.txt"))
    # approve what else the turn asks, from A, so it reaches the sleep
    a.on_server_request = lambda m: {"decision": "accept"} if m["method"].endswith("requestApproval") else None
    second_turn = None
    if saw_command(a, "sleep 6.4141", timeout=60):
        try:
            r = b.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Step 1: run `echo from-B`."}],
                                         "effort": EFFORT}, timeout=30)
            second_turn = {"accepted": True, "turn_id": (r.get("turn") or {}).get("id"), "same_as_running": (r.get("turn") or {}).get("id") == turn}
        except AppServerError as e:
            second_turn = {"accepted": False, "error": str(e)[:300]}
    a.wait_for(lambda e: e.get("method") == "turn/completed", timeout=90)
    time.sleep(3)
    b_methods = sorted({e["msg"].get("method") for e in b.log if e["dir"] == "in" and "method" in e["msg"]})
    hist = history(a, thread)
    forget(a, thread)
    a.close(), b.close()
    rt.stop()
    return {"pass": bool(pa) and bool(pb) and not created, "resume_before_the_first_turn": resume_before_first_turn,
            "both_were_asked": bool(pa) and bool(pb), "first_answer_decline_then_late_accept": second_answer,
            "file_created_despite_first_decline": created, "second_turn_start_while_running": second_turn,
            "B_received": [m for m in b_methods if m.startswith(("turn/", "item/", "serverRequest/"))],
            "stored_turns": [{"status": t["status"], "commands": [(i["command"], i["status"]) for i in t["items"] if i["type"] == "commandExecution"]} for t in hist]}


def _crash(out, name: str, how: str) -> dict:
    """Kill the runtime in the middle of a command, start it again, resume the thread."""
    w = World()
    marker = {"socket_sigkill": "37.5151", "socket_sigterm": "37.6161"}[how]
    rt = Runtime(out, name, w)
    a = rt.controller("A")
    thread = start_thread(a, w)
    turn = begin(a, thread, f"Step 1: run `sleep {marker} && echo late > late.txt`. Step 2: run `echo two`.")
    started = saw_command(a, f"sleep {marker}")
    before = pids_of(f"sleep {marker}")
    event_pid = (started or {}).get("processId")
    def ids(pid):
        try:
            return {"pid": pid, "pgid": os.getpgid(pid), "sid": os.getsid(pid)}
        except OSError:
            return {"pid": pid}
    runtime_ids, command_ids = ids(rt.proc.pid), [ids(p) for p in before]
    t0 = time.time()
    code = rt.stop(signal.SIGKILL if how == "socket_sigkill" else signal.SIGTERM, wait=20)
    took = round(time.time() - t0, 1)
    time.sleep(2)
    orphans = pids_of(f"sleep {marker}")
    for pid in orphans:                           # the probe cleans up what the runtime left
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    a.close()
    stale_socket = os.path.exists(rt.sock)
    if stale_socket:
        os.remove(rt.sock)
    rt.start()
    b = rt.controller("B")
    try:
        resumed = resume(b, w, thread)
        resume_ok, status = True, (resumed.get("thread") or {}).get("status")
    except AppServerError as e:
        resume_ok, status = False, str(e)[:300]
    hist = history(b, thread) if resume_ok else []
    follow = None
    if resume_ok:
        r = b.run_turn(thread, "Step 1: run `echo resumed`. Then say in one line which commands you ran before this message.",
                       timeout=120, effort=EFFORT)
        final = [i.get("text") for i in items(b.log, kind="agentMessage") if i.get("phase") == "final_answer"]
        follow = {"status": r["status"], "answer": (final[-1] if final else "")[:300]}
    forget(b, thread)
    b.close()
    rt.stop()
    return {"pass": bool(started) and resume_ok and not os.path.exists(os.path.join(w.work, "late.txt")),
            "runtime_exit": code, "seconds_to_exit": took, "command_pids_before": before, "command_left_running": orphans,
            "runtime_process": runtime_ids, "command_processes": command_ids,
            "commands_share_the_runtime_session": bool(command_ids) and all(c.get("sid") == runtime_ids.get("sid") for c in command_ids),
            "pid_in_the_item_started_event": event_pid, "that_pid_is_one_of_the_orphans": str(event_pid) in [str(p) for p in orphans],
            "socket_file_left_behind": stale_socket, "late_side_effect_happened": os.path.exists(os.path.join(w.work, "late.txt")),
            "resume_after_restart": resume_ok, "thread_status": status,
            "stored_turns": [{"status": t["status"], "commands": [(i["command"], i["status"], i["exit"]) for i in t["items"] if i["type"] == "commandExecution"]} for t in hist],
            "follow_up_turn": follow, "turn_id": turn}


@scenario
def runtime_killed_mid_command(out):
    """SIGKILL to the app-server while a command runs (a crash). Is the command left running, what does the
    stored thread say about the turn, and can a new app-server resume the conversation?"""
    return _crash(out, "runtime_killed", "socket_sigkill")


@scenario
def runtime_terminated_mid_command(out):
    """SIGTERM to the app-server while a command runs (a `crew down` that does not interrupt first)."""
    return _crash(out, "runtime_terminated", "socket_sigterm")


@scenario
def down_during_a_turn(out):
    """The orderly `crew down`: interrupt the turn, wait for its end, read what is left, then stop the
    app-server. The command must be gone, the turn must say interrupted, nothing may be left running."""
    w = World()
    rt = Runtime(out, "down_during_a_turn", w)
    a = rt.controller("A")
    thread = start_thread(a, w)
    turn = begin(a, thread, "Step 1: run `sleep 38.7171 && echo late > late.txt`. Step 2: run `echo two`.")
    started = saw_command(a, "sleep 38.7171")
    t0 = time.time()
    a.request("turn/interrupt", {"threadId": thread, "turnId": turn})
    done = a.wait_for(lambda e: e.get("method") == "turn/completed", timeout=30)
    status = ((((done or {}).get("params") or {}).get("turn")) or {}).get("status")
    interrupted_in = round(time.time() - t0, 1)
    left_at_once = pids_of("sleep 38.7171")
    gone = wait_until(lambda: not pids_of("sleep 38.7171"), 15)
    gone_in = round(time.time() - t0, 1)
    left = pids_of("sleep 38.7171")
    hist = history(a, thread)
    forget(a, thread)
    a.close()
    code = rt.stop()
    time.sleep(1)
    after = pids_of("sleep 38.7171")
    for pid in after:
        os.kill(pid, signal.SIGKILL)
    return {"pass": bool(started) and status == "interrupted" and not after and code == 0
            and not os.path.exists(os.path.join(w.work, "late.txt")),
            "note": "the interrupt ends the turn but not the command; stopping the app-server afterwards does",
            "turn_status": status, "seconds_to_interrupt": interrupted_in, "command_alive_right_after_interrupt": left_at_once,
            "command_gone": gone, "seconds_until_command_gone": gone_in, "command_left_after_interrupt": left,
            "runtime_exit": code, "command_left_after_stop": after,
            "stored_turns": [{"status": t["status"], "commands": [(i["command"], i["status"], i["exit"]) for i in t["items"] if i["type"] == "commandExecution"]} for t in hist]}


@scenario
def interrupt_leaves_the_command_running(out):
    """An interrupt without a stop: the turn says interrupted, the app-server stays up. Does the command the
    turn started still finish and do its work afterwards?"""
    w = World()
    rt = Runtime(out, "interrupt_only", w)
    a = rt.controller("A")
    thread = start_thread(a, w)
    turn = begin(a, thread, "Step 1: run `sleep 12.9191 && echo late > late.txt`. Step 2: run `echo two`.")
    started = saw_command(a, "sleep 12.9191")
    a.request("turn/interrupt", {"threadId": thread, "turnId": turn})
    done = a.wait_for(lambda e: e.get("method") == "turn/completed", timeout=30)
    status = ((((done or {}).get("params") or {}).get("turn")) or {}).get("status")
    late = wait_until(lambda: os.path.exists(os.path.join(w.work, "late.txt")), 25)
    forget(a, thread)
    a.close()
    rt.stop()
    return {"pass": bool(started) and status == "interrupted", "turn_status": status,
            "the_command_finished_its_work_after_the_interrupt": late,
            "note": "true means an interrupted turn is not a stopped command: a halt or a steer that only "
                    "interrupts must also end the app-server, or wait for the command"}


@scenario
def stdio_owner_dies(out):
    """The stdio alternative: the app-server is the child of its controller. The controller dies (its pipes
    close) in the middle of a command. What happens to the app-server and to the command?"""
    w = World()
    name = "stdio_owner_dies"
    app = AppServer.spawn(codex_bin(), args=QUIET, cwd=w.work, env=w.env("builder"), new_session=True,
                          transcript_path=os.path.join(out, f"{name}.1.wire.jsonl"), stderr_path=os.path.join(out, f"{name}.stderr.log"))
    app.initialize()
    thread = start_thread(app, w)
    begin(app, thread, "Step 1: run `sleep 39.8181 && echo late > late.txt`. Step 2: run `echo two`.")
    started = saw_command(app, "sleep 39.8181")
    pid = app.proc.pid
    app.proc.stdin.close()
    app.proc.stdout.close()                       # both pipes gone, as when the owner process dies
    t0 = time.time()
    exited = wait_until(lambda: app.proc.poll() is not None, 20)
    took = round(time.time() - t0, 1)
    time.sleep(2)
    left = pids_of("sleep 39.8181")
    for p in left:
        os.kill(p, signal.SIGKILL)
    if not exited:
        app.proc.kill()
    app2 = AppServer.spawn(codex_bin(), args=QUIET, cwd=w.work, env=w.env("builder"),
                           transcript_path=os.path.join(out, f"{name}.2.wire.jsonl"), stderr_path=os.path.join(out, f"{name}.stderr.log"))
    app2.initialize()
    try:
        resumed = resume(app2, w, thread)
        resume_ok, status = True, (resumed.get("thread") or {}).get("status")
        hist = history(app2, thread)
    except AppServerError as e:
        resume_ok, status, hist = False, str(e)[:300], []
    forget(app2, thread)
    app2.close()
    return {"pass": bool(started) and resume_ok, "app_server_pid": pid, "app_server_exited_when_pipes_closed": exited,
            "seconds_to_exit": took, "command_left_running": left,
            "late_side_effect_happened": os.path.exists(os.path.join(w.work, "late.txt")),
            "resume_in_a_new_app_server": resume_ok, "thread_status": status,
            "stored_turns": [{"status": t["status"], "commands": [(i["command"], i["status"], i["exit"]) for i in t["items"] if i["type"] == "commandExecution"]} for t in hist]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    todo = [(n, f) for n, f in globals().items() if getattr(f, "is_scenario", False)]
    if args.only:
        todo = [(n, f) for n, f in todo if n in args.only]
    path = os.path.join(args.out, "results.json")
    try:
        with open(path, encoding="utf-8") as f:
            results = json.load(f)
    except (OSError, ValueError):
        results = {}
    for name, fn in todo:
        t0 = time.time()
        try:
            res = fn(args.out)
        except Exception as e:
            res = {"pass": False, "error": repr(e)[:800]}
        res["seconds"] = round(time.time() - t0, 1)
        res["what"] = " ".join((fn.__doc__ or "").split())
        results[name] = res
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1, ensure_ascii=False)
        print(("PASS " if res.get("pass") else "FAIL ") + name, f"({res['seconds']} s)", flush=True)
        for k, v in res.items():
            if k not in ("what", "seconds", "pass"):
                print(f"      {k}: {json.dumps(v, ensure_ascii=False)[:420]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
