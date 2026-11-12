"""A crew message is listed once on its ticket, not two to four times.

Two causes, both seen on one ticket: the session's async hooks (PostToolUse, Stop, ...) ran the
shipper side by side, both read the same offset and posted the same lines; and Claude Code
(2.1.3xx) writes an `api-request-blob` line holding a copy of the previous assistant message with no
timestamp, which Tares stored as another tool call at the moment it arrived.

Run: PYTHONPATH=.:ee .venv/bin/python tests/test_plugin_ship_once.py   (no daemon, no network)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env, scrub_os_environ

scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIP = os.path.join(ROOT, "claude-plugin", "scripts", "ship.py")
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


class FakeTares:
    """/ingest/claude_code that answers slowly, as a busy cell does, so shippers overlap."""

    def __init__(self):
        self.lines, self.lock = [], threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.path == "/ingest/claude_code":
                    objs = [json.loads(ln) for ln in body.decode().splitlines() if ln.strip()]
                    if any(o.get("uuid") for o in objs):
                        threading.Event().wait(0.5)
                    with outer.lock:
                        outer.lines.extend(objs)
                self.send_response(201 if self.path == "/api/sources" else 202)
                self.end_headers()
                self.wfile.write(b"{}")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def call(sid, n, text, mid="msg_1", ts="2026-11-12T13:03:59.353Z"):
    return {"type": "assistant", "uuid": f"u{n}", "sessionId": sid, "timestamp": ts, "cwd": "/tmp",
            "message": {"id": mid, "role": "assistant", "content": [
                {"type": "tool_use", "id": f"toolu_{n}", "name": "SendMessage",
                 "input": {"to": "crew-orchestrator", "message": text}}]}}


def blob(sid, *calls):
    return {"type": "api-request-blob", "sessionId": sid, "hash": "bb3e",
            "message": {"role": "assistant",
                        "content": [b for c in calls for b in c["message"]["content"]]}}


def hooks_side_by_side():
    print("the async hooks of one turn ship side by side")
    t = FakeTares()
    d = tempfile.mkdtemp(prefix="ship-once-")
    env = clean_env(CLAUDE_PLUGIN_DATA=os.path.join(d, "data"), CLAUDE_PLUGIN_OPTION_tares_url=t.url)
    path = os.path.join(d, "s.jsonl")
    v = call("s", 1, "[TF:VERDICT] ok")
    f = call("s", 2, "[TF:FYI] note", ts="2026-11-12T13:04:03.148Z")
    with open(path, "w", newline="\n") as fh:
        for o in (v, f, blob("s", v, f)):
            fh.write(json.dumps(o) + "\n")
    procs = [subprocess.Popen([sys.executable, SHIP], stdin=subprocess.PIPE, env=env, text=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for _ in range(4)]
    for ev, p in zip(("PostToolUse", "Stop", "SubagentStop", "Notification"), procs):
        p.stdin.write(json.dumps({"hook_event_name": ev, "session_id": "s", "cwd": d,
                                  "transcript_path": path}))
        p.stdin.close()
    for p in procs:
        p.wait(timeout=60)
    sent = Counter(o["uuid"] for o in t.lines if o.get("uuid"))
    check("each transcript line is posted once", sent == Counter({"u1": 1, "u2": 1}), dict(sent))
    check("the api-request-blob copy is not posted",
          not any(o.get("type") == "api-request-blob" for o in t.lines))
    with open(path, "a", newline="\n") as fh:
        fh.write(json.dumps(call("s", 3, "[TF:REVIEW] pr", mid="msg_2")) + "\n")
    subprocess.run([sys.executable, SHIP], input=json.dumps(
        {"hook_event_name": "Stop", "session_id": "s", "cwd": d, "transcript_path": path}),
        text=True, env=env, capture_output=True, timeout=60)
    sent = Counter(o["uuid"] for o in t.lines if o.get("uuid"))
    check("the lock is let go: a later hook ships the next line", sent.get("u3") == 1, dict(sent))
    # a killed holder left a fresh lock: the session's end ships its last line anyway (no hook
    # comes after it) and leaves the lock that is not its own
    lock = [os.path.join(r, f)[:-len(".off")] + ".lock" for r, _, fs in os.walk(os.path.join(d, "data"))
            for f in fs if f.endswith(".off")]
    check("the transcript has an offset file to find its lock by", len(lock) == 1, lock)
    if lock:
        open(lock[0], "w").close()
        with open(path, "a", newline="\n") as fh:
            fh.write(json.dumps(call("s", 4, "[TF:DONE] bye", mid="msg_3")) + "\n")
        subprocess.run([sys.executable, SHIP], input=json.dumps(
            {"hook_event_name": "SessionEnd", "session_id": "s", "cwd": d, "transcript_path": path}),
            text=True, env=env, capture_output=True, timeout=60)
        sent = Counter(o["uuid"] for o in t.lines if o.get("uuid"))
        check("a SessionEnd that cannot get the lock still ships the last line", sent.get("u4") == 1, dict(sent))
        check("...and leaves the lock that is not its own", os.path.exists(lock[0]))
    t.close()
    shutil.rmtree(d, ignore_errors=True)


def tares_lists_each_call_once():
    print("Tares drops request copies and lists a call stored twice once")
    from tares.connectors.claude_code import ClaudeCodeConnector
    from tares.store import Store
    d = tempfile.mkdtemp(prefix="ship-once-")
    store = Store(os.path.join(d, "t.duckdb"))
    c = ClaudeCodeConnector(SimpleNamespace(name="claude_code", type="claude_code", config={}),
                            store=store)
    v = call("s", 1, "[TF:VERDICT] ok")
    f = call("s", 2, "[TF:FYI] note", ts="2026-11-12T13:04:03.148Z")
    envs = c.map_payload([v, f, blob("s", v, f)])
    check("an older plugin's api-request-blob is not stored", len(envs) == 2, len(envs))
    # what is already stored: each line twice (the race) and the copy, as an older Tares kept it
    store.append(c.map_payload([v, f]) + c.map_payload([v, f]))
    store.append([_raw_envelope(c, blob("s", v, f))])
    got = [(m["type"], m["first_line"]) for m in store.protocol_messages("claude_code")]
    check("each message is listed once, in order",
          got == [("VERDICT", "ok"), ("FYI", "note")], got)
    # a request copy of another call, stored by an older Tares: skipped by its type, not by an id seen before
    store.append([_raw_envelope(c, blob("s", call("s", 9, "[TF:FYI] only in a copy", mid="msg_9")))])
    got = [(m["type"], m["first_line"]) for m in store.protocol_messages("claude_code")]
    check("a request copy holding a call not seen elsewhere is not listed", ("FYI", "only in a copy") not in got, got)
    # a resumed session copies the old lines into its own file under a new session id: same call ids
    resumed = [dict(o, sessionId="s2", uuid="r" + o["uuid"]) for o in (v, f)]
    store.append(c.map_payload(resumed))
    got = [(m["type"], m["first_line"]) for m in store.protocol_messages("claude_code")]
    check("a resumed session's copy of a call is listed once",
          got == [("VERDICT", "ok"), ("FYI", "note")], got)
    store.con.close()
    shutil.rmtree(d, ignore_errors=True)


def _raw_envelope(c, o):
    """The envelope an older Tares made from a line it did not drop."""
    return c._obj_to_envelope(o, True, False)


def main():
    hooks_side_by_side()
    tares_lists_each_call_once()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
