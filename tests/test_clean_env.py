"""Tests never report to the real Tares (crew-stale-rows M1): run inside a crew station, a test
that hands the plugin's shipper the station's environment, or the saved login, makes it post the
test's fake session to the station's real Tares. clean_env() closes both paths. Each case runs
twice: a control with the leaking environment (the decoy must be reached, so the check can fail)
and a run with clean_env() (the decoy must get nothing, the test's own fake Tares must).
Run: .venv/bin/python tests/test_clean_env.py   (no daemon, no network)
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIP = os.path.join(ROOT, "claude-plugin", "scripts", "ship.py")

PASS = FAIL = 0


def ck(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def recorder():
    """An HTTP server on a free port that records every request path and answers 200 {}."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = _ok

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", seen


def ship(env, tmp):
    transcript = os.path.join(tmp, "session.jsonl")
    open(transcript, "w").close()
    hook = {"session_id": "leak-check", "transcript_path": transcript, "cwd": tmp,
            "hook_event_name": "UserPromptSubmit"}
    subprocess.run([sys.executable, SHIP], input=json.dumps(hook), text=True, env=env,
                   timeout=20, capture_output=True)


class patched_environ:
    """Set keys in os.environ for the duration, as a crew station's shell would have them."""
    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Kept apart from _env.py on purpose: with the helper broken, this test must still reach only
# its decoy, never the Tares of the station or session it runs in.
OWN_PREFIXES = ("FACTORY_", "TARES_FACTORY_", "CLAUDE_PLUGIN_OPTION_", "TARES_SESSION_")
OWN_NAMES = ("TARESD_URL", "TARES_URL", "TARES_AUTH_TOKEN", "TARES_PROJECT", "TARES_HOME")


def drop_inherited():
    for k in [k for k in os.environ if k in OWN_NAMES or k.startswith(OWN_PREFIXES)]:
        del os.environ[k]
    os.environ["TARES_HOME"] = tempfile.mkdtemp(prefix="tares-clean-env-home-")


def main():
    drop_inherited()
    fake, fake_seen = recorder()
    decoy, decoy_seen = recorder()
    mk = lambda: tempfile.mkdtemp(prefix="tares-clean-env-test-")
    station = {"FACTORY_STATION": "leak-builder", "FACTORY_ROLE": "builder", "TARES_FACTORY_URL": decoy}

    def run(env):
        f0, d0 = len(fake_seen), len(decoy_seen)
        ship(env, mk())
        return len(fake_seen) - f0, len(decoy_seen) - d0

    print("== case A: the station's environment ==")
    f, d = run({**os.environ, **station, "CLAUDE_PLUGIN_OPTION_TARES_URL": fake,
                "CLAUDE_PLUGIN_DATA": mk(), "TARES_HOME": mk()})
    print(f"  control: fake {f}, decoy {d}")
    ck("control: the station env reaches the decoy (the check can fail)", d >= 1, f"decoy {d}")
    with patched_environ(**station):
        env = clean_env(CLAUDE_PLUGIN_OPTION_TARES_URL=fake, CLAUDE_PLUGIN_DATA=mk())
    f, d = run(env)
    print(f"  clean:   fake {f}, decoy {d}")
    ck("clean_env: the decoy gets nothing", d == 0, f"decoy {d}")
    ck("clean_env: the test's own Tares gets the session", f >= 1, f"fake {f}")

    print("== case B: the saved login ==")
    home = mk()
    with open(os.path.join(home, "login.json"), "w") as fh:
        json.dump({"cell_url": decoy, "key": "k"}, fh)
    f, d = run({**os.environ, "TARES_HOME": home, "CLAUDE_PLUGIN_DATA": mk()})
    print(f"  control: fake {f}, decoy {d}")
    ck("control: the saved login reaches the decoy (the check can fail)", d >= 1, f"decoy {d}")
    with patched_environ(TARES_HOME=home):
        env = clean_env(CLAUDE_PLUGIN_OPTION_TARES_URL=fake, CLAUDE_PLUGIN_DATA=mk())
    f, d = run(env)
    print(f"  clean:   fake {f}, decoy {d}")
    ck("clean_env: the decoy gets nothing", d == 0, f"decoy {d}")
    ck("clean_env: the test's own Tares gets the session", f >= 1, f"fake {f}")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
