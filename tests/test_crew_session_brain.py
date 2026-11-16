"""The brain paragraph of the SessionStart hook (TR-1202): a crew session is told to call `search_brain` or
`ask_brain` before it reads code, only when the cell reports a brain with a ready repo (`GET /api/brain`).

Checks: the paragraph is there with a ready repo (a station, a spec session, after a resume), and absent
for a cell with no brain (404, configured false), a brain with no ready repo, a session with no cell address,
and a cell that is down; the crew's token is presented; the spec skill's step 0 names the brain.
It checks the injected text, not what a model then does.

Run: .venv/bin/python tests/test_crew_session_brain.py
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "claude-plugin" / "hooks" / "session_start.py"
PLUGIN = ROOT / "claude-plugin"
SKILL = (PLUGIN / "skills" / "spec-project" / "SKILL.md").read_text(encoding="utf-8")
T = Path(tempfile.mkdtemp(prefix="crew_session_brain_"))
P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


ANSWER = {"status": 200, "body": {"configured": False, "repos": []}, "slow": False}
SEEN = []   # the Authorization header of each request


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        SEEN.append((self.path, self.headers.get("Authorization")))
        body = json.dumps(ANSWER["body"]).encode()
        self.send_response(ANSWER["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if ANSWER["slow"]:
            # a body that arrives a byte every half second: each read is quick, the whole is not
            try:
                for i in range(len(body)):
                    self.wfile.write(body[i:i + 1])
                    self.wfile.flush()
                    time.sleep(0.5)
            except OSError:
                pass
            return
        self.wfile.write(body)

    def log_message(self, *a):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_address[1]}"

CLEAN = {k: v for k, v in os.environ.items()
         if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_")) and k not in ("PYTHONPATH", "PYTHONHOME")}


def hook(extra, source="startup"):
    env = dict(CLEAN, CLAUDE_PLUGIN_ROOT=str(PLUGIN), PYTHONUTF8="1")
    env.update(extra)
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"source": source}).encode(), capture_output=True,
                       env=env, cwd=str(T), timeout=60)
    try:
        ctx = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    except Exception:
        ctx = ""
    return r, ctx


def answer(status, body):
    ANSWER["status"], ANSWER["body"] = status, body


STATION = {"FACTORY_ROLE": "builder", "FACTORY_STATION": "b", "TARES_FACTORY_URL": URL}
SPEC = {"TARES_FACTORY_URL": URL}   # `tares factory spec`: an address, no role, no project
READY = {"configured": True, "repos": [{"repo": "acme/api", "state": "ready"}, {"repo": "acme/web", "state": "indexing"}]}

print("with a ready repo")
answer(200, READY)
r, ctx = hook(STATION)
ck("a station is told to call search_brain and ask_brain before reading code",
   "`search_brain`" in ctx and "`ask_brain`" in ctx and "Before reading code" in ctx, ctx[-300:])
ck("... grep only for repos the tools say are not ready, and to confirm an exact line",
   "only for repos those tools say are not ready" in ctx and "confirm an exact line" in ctx, ctx[-300:])
ck("... it names the ready repo and not the one still indexing", "acme/api" in ctx and "acme/web" not in ctx, ctx[-300:])
ck("... after what the station is told, and the hook does not fail",
   ctx.startswith("You are the builder station `b`") and r.returncode == 0 and r.stderr == b"", (r.returncode, r.stderr))
r, ctx = hook(SPEC)
ck("a spec session (an address, no role, no project) is told too", ctx.startswith("This cell has a code brain") and "`search_brain`" in ctx, ctx)
r, ctx = hook(STATION, source="compact")
ck("a compacted station hears it again", "`ask_brain`" in ctx and "compacted" in ctx, ctx[-300:])

print("without a brain")
answer(200, {"configured": False, "repos": []})
r, ctx = hook(STATION)
ck("configured false: nothing about the brain", "brain" not in ctx and ctx.startswith("You are the builder station"), ctx)
r, ctx = hook(SPEC)
ck("... a spec session hears nothing at all", ctx == "" and r.returncode == 0, (ctx, r.stderr))
answer(200, {"configured": False, "repos": [{"repo": "a/b", "state": "ready"}]})
r, ctx = hook(STATION)
ck("configured false with a ready row: nothing about the brain", "brain" not in ctx, ctx)
answer(200, {"configured": True, "repos": [{"repo": "a/b", "state": "queued"}, {"repo": "a/c", "state": "failed"}]})
r, ctx = hook(STATION)
ck("a brain with no ready repo: nothing about the brain", "brain" not in ctx, ctx)
answer(200, {"configured": True, "repos": []})
r, ctx = hook(STATION)
ck("a brain with no repos: nothing about the brain", "brain" not in ctx, ctx)
answer(200, {"configured": True})
r, ctx = hook(STATION)
ck("a brain answer with no repos list: nothing about the brain", "brain" not in ctx and r.returncode == 0, ctx)
answer(404, {"detail": "Not Found"})
r, ctx = hook(STATION)
ck("a cell with no brain routes (404): nothing about the brain", "brain" not in ctx and r.returncode == 0, ctx)
answer(200, [1, 2])
r, ctx = hook(STATION)
ck("an answer that is not an object: nothing about the brain", "brain" not in ctx and r.returncode == 0, ctx)
answer(200, READY)
before = len(SEEN)
r, ctx = hook({"FACTORY_ROLE": "builder", "FACTORY_STATION": "b"})
ck("a session with no cell address asks nothing and hears nothing about the brain", "brain" not in ctx and len(SEEN) == before, (ctx, len(SEEN) - before))
s = socket.socket()
s.bind(("127.0.0.1", 0))
dead = s.getsockname()[1]
s.close()
r, ctx = hook(dict(STATION, TARES_FACTORY_URL=f"http://127.0.0.1:{dead}"))
ck("a cell that is down: the station is told what it is told, the hook does not fail",
   ctx.startswith("You are the builder station") and "brain" not in ctx and r.returncode == 0 and r.stderr == b"", (ctx, r.returncode, r.stderr))

print("the token")
cred = T / "cred"
cred.write_text("tok-123\n", encoding="utf-8")
del SEEN[:]
r, ctx = hook(dict(STATION, TARES_FACTORY_CRED_FILE=str(cred)))
ck("the crew's token goes to /api/brain as a bearer", SEEN and SEEN[0] == ("/api/brain", "Bearer tok-123"), SEEN)
del SEEN[:]
r, ctx = hook(STATION)
ck("no token file: no Authorization header", SEEN and SEEN[0] == ("/api/brain", None), SEEN)

print("the saved key")
home = T / "home"
home.mkdir()
(home / "login.json").write_text(json.dumps({"cell_url": URL, "key": "key-abc"}), encoding="utf-8")
del SEEN[:]
r, ctx = hook(dict(STATION, TARES_HOME=str(home)))
ck("no token file: the key `tares login` saved for this cell goes as the bearer", SEEN and SEEN[0] == ("/api/brain", "Bearer key-abc"), SEEN)
(home / "login.json").write_text(json.dumps({"cell_url": "http://example.invalid", "key": "key-abc"}), encoding="utf-8")
del SEEN[:]
r, ctx = hook(dict(STATION, TARES_HOME=str(home)))
ck("a key saved for another cell is not presented", SEEN and SEEN[0] == ("/api/brain", None), SEEN)

print("many ready repos")
answer(200, {"configured": True, "repos": [{"repo": f"acme/r{i}", "state": "ready"} for i in range(8)]})
r, ctx = hook(STATION)
ck("five names and a count of the rest", "acme/r0, acme/r1, acme/r2, acme/r3, acme/r4 and 3 more" in ctx and "acme/r5" not in ctx, ctx[-400:])
answer(200, {"configured": True, "repos": [{"repo": f"acme/r{i}", "state": "ready"} for i in range(5)]})
r, ctx = hook(STATION)
ck("exactly five: all five, no count", "acme/r4)" in ctx and "more" not in ctx.split("brain")[-1].split("(")[1], ctx[-400:])

print("a slow cell")
answer(200, READY)
ANSWER["slow"] = True
t0 = time.time()
r, ctx = hook(STATION)
took = time.time() - t0
ANSWER["slow"] = False
ck("a body that trickles in: the station text still comes out within about 3 seconds, without the brain paragraph",
   took < 3.5 and ctx.startswith("You are the builder station") and "brain" not in ctx and r.returncode == 0 and r.stderr == b"",
   (round(took, 1), ctx[-200:], r.returncode))

print("the skill")
step0 = SKILL.split("## 0. Read what is already decided")[1].split("## 1.")[0]
ck("spec-project step 0 says to ask the brain about the repo first, when the session was told of one",
   "`search_brain`" in step0 and "`ask_brain`" in step0 and "before asking the user" in step0, step0[-400:])
ck("no em dash in the hook or the skill", "—" not in HOOK.read_text(encoding="utf-8") and "—" not in step0)

server.shutdown()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
