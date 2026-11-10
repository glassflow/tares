"""The plugin keeps recording a station after a restart in the same conversation (TR-1102).

`claude --resume` starts a new session id and a new transcript file that opens with the whole
conversation so far, so the first hook that finds the file has a backlog from byte 0. When that
first request cannot be delivered (the ingress in front of a cell refuses a body over 1 MiB, a busy
daemon outlasts the 5 s timeout), every later hook retried the same request and no line, not even
the session's state, reached Tares: `lines` stopped at 2 while the station kept working.

Run: PYTHONPATH=.:ee .venv/bin/python tests/test_plugin_resume_ship.py   (no daemon, no network)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env, scrub_os_environ

scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "claude-plugin", "scripts")
SHIP = os.path.join(SCRIPTS, "ship.py")
sys.path.insert(0, SCRIPTS)
import ship  # noqa: E402

PASS = FAIL = 0
MIB = 1024 * 1024


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


class FakeTares:
    """/ingest/claude_code with a request size limit and, optionally, a status for transcript posts."""

    def __init__(self, limit=None):
        self.limit, self.refuse_transcript, self.lines, self.posts = limit, None, [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                code = 201 if self.path == "/api/sources" else 202
                if self.path == "/ingest/claude_code":
                    outer.posts.append(len(body))
                    objs = [json.loads(ln) for ln in body.decode().splitlines() if ln.strip()]
                    real = any(o.get("type") in ("user", "assistant") for o in objs)
                    if outer.limit and len(body) > outer.limit:
                        code = 413
                    elif outer.refuse_transcript and real:
                        code = outer.refuse_transcript
                    else:
                        outer.lines.extend(objs)
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"{}")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def uuids(self, sid):
        return {o["uuid"] for o in self.lines if o.get("uuid") and o.get("sessionId") == sid}

    def states(self, sid):
        return [o["state"] for o in self.lines
                if o.get("type") == "session_state" and o.get("sessionId") == sid]

    def close(self):
        self.srv.shutdown()


def line(sid, n, size=2000):
    return json.dumps({"type": "assistant" if n % 2 else "user", "uuid": f"u{n}", "sessionId": sid,
                       "timestamp": "2026-11-09T12:00:00.000Z", "cwd": "/tmp",
                       "message": {"role": "user", "content": "x" * size}}) + "\n"


def write(path, sid, first, last, size=2000):
    with open(path, "a") as f:
        for n in range(first, last):
            f.write(line(sid, n, size))


class Box:
    """A plugin data dir and a station's environment, with the shipper run as Claude Code runs it."""

    def __init__(self, url):
        self.dir = tempfile.mkdtemp(prefix="tr1102-")
        self.env = clean_env(CLAUDE_PLUGIN_DATA=os.path.join(self.dir, "data"),
                             CLAUDE_PLUGIN_OPTION_tares_url=url, FACTORY_STATION="crew-reviewer",
                             FACTORY_ROLE="reviewer")

    def path(self, sid):
        return os.path.join(self.dir, sid + ".jsonl")

    def hook(self, event, sid, transcript=None, **extra):
        payload = {"hook_event_name": event, "session_id": sid, "cwd": self.dir,
                   "transcript_path": transcript or self.path(sid), **extra}
        return subprocess.run([sys.executable, SHIP], input=json.dumps(payload), text=True,
                              env=self.env, capture_output=True, timeout=60)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def resume_with_a_new_file():
    print("resumed under a new session id, in a new file holding the whole conversation")
    t = FakeTares(limit=MIB)
    b = Box(t.url)
    old, new = "old-session", "new-session"
    b.hook("SessionStart", old)
    write(b.path(old), old, 0, 40)
    b.hook("PostToolUse", old)
    check("the first session ships as it goes", len(t.uuids(old)) == 40, len(t.uuids(old)))

    write(b.path(old), old, 40, 1500)      # about 3 MB, written while nothing shipped
    b.hook("SessionStart", new, source="resume")
    b.hook("UserPromptSubmit", new)        # the file is not there yet
    check("the station and working lines arrive first", t.states(new) == ["working"], t.states(new))
    write(b.path(new), new, 0, 1500)       # the copy, with the new session id on every line
    for _ in range(12):
        b.hook("PostToolUse", new)
    check("the copied conversation all arrives", len(t.uuids(new)) == 1500, len(t.uuids(new)))
    check("no request was over the limit", max(t.posts) <= MIB, max(t.posts))

    write(b.path(new), new, 1500, 1503)
    b.hook("Stop", new)
    check("lines written after the restart arrive", len(t.uuids(new)) == 1503, len(t.uuids(new)))
    check("the turn's end arrives", t.states(new)[-1] == "idle", t.states(new))
    t.close()
    b.close()


def resume_in_the_same_file():
    print("resumed under a new session id, writing on in the same file")
    t = FakeTares(limit=MIB)
    b = Box(t.url)
    old, new = "old-session", "new-session"
    transcript = b.path(old)
    write(transcript, old, 0, 20)
    b.hook("PostToolUse", old, transcript)
    b.hook("SessionStart", new, transcript, source="resume")
    write(transcript, new, 20, 25)
    b.hook("PostToolUse", new, transcript)
    b.hook("Stop", new, transcript)
    check("the earlier lines are not sent again", len(t.uuids(old)) == 20, len(t.uuids(old)))
    check("the new lines arrive under the new id", len(t.uuids(new)) == 5, len(t.uuids(new)))
    check("the state arrives under the new id", t.states(new) == ["working", "idle"], t.states(new))
    t.close()
    b.close()


def in_process(limit_error, transcript_lines, size=2000):
    """ship_main with _post replaced: a request over 256 KiB fails with `limit_error`."""
    d = tempfile.mkdtemp(prefix="tr1102-")
    path = os.path.join(d, "s.jsonl")
    write(path, "s", 0, transcript_lines, size)
    sent, real = [], ship._post

    def fake(url, data, headers, timeout=5):
        sent.append(len(data))
        if len(data) > 256 * 1024:
            raise limit_error
        return None

    ship._post = fake
    cfg = {"base": "http://x", "headers": {}, "data_dir": d}
    hook = {"session_id": "s", "cwd": d}
    try:
        result = [ship.ship_main(cfg, hook, path, "") for _ in range(30)]
        return sent, result, ship._main_offset(cfg, path), os.path.getsize(path)
    finally:
        ship._post = real
        shutil.rmtree(d, ignore_errors=True)


def a_slow_tares():
    print("a request too big to answer within the timeout")
    sent, result, offset, size = in_process(TimeoutError("timed out"), 1000)
    check("a smaller request gets through and the whole backlog drains", offset == size, (offset, size))
    check("every hook ended well", all(ok for ok, _ in result))
    sent2, result2, offset2, _ = in_process(ConnectionRefusedError("down"), 1000)
    check("Tares being down is one try per hook, not a cascade", len(sent2) == 30, len(sent2))
    check("the offset waits while Tares is down", offset2 == 0 and not any(ok for ok, _ in result2), offset2)


def a_line_over_the_limit():
    print("a single line the ingress never takes")
    t = FakeTares(limit=MIB)
    b = Box(t.url)
    write(b.path("s"), "s", 0, 2)
    write(b.path("s"), "s", 2, 3, size=2 * MIB)
    write(b.path("s"), "s", 3, 6)
    for _ in range(4):
        b.hook("PostToolUse", "s")
    check("the lines around it arrive", t.uuids("s") == {"u0", "u1", "u3", "u4", "u5"}, t.uuids("s"))
    check("it is named on the timeline",
          any(o.get("type") == "session_warning" and "too large" in o.get("warning", "") for o in t.lines))
    t.close()
    b.close()


def state_without_the_transcript():
    print("a transcript that cannot be delivered does not hide the session's state")
    t = FakeTares()
    t.refuse_transcript = 500
    b = Box(t.url)
    write(b.path("s"), "s", 0, 5)
    b.hook("PostToolUse", "s")
    b.hook("Stop", "s")
    check("working and idle still arrive", t.states("s") == ["working", "idle"], t.states("s"))
    check("the transcript is retried, not skipped", not t.uuids("s"))
    t.refuse_transcript = None
    b.hook("PostToolUse", "s")
    check("it catches up once Tares takes it", len(t.uuids("s")) == 5, len(t.uuids("s")))
    t.close()
    b.close()


if __name__ == "__main__":
    resume_with_a_new_file()
    resume_in_the_same_file()
    a_slow_tares()
    a_line_over_the_limit()
    state_without_the_transcript()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
