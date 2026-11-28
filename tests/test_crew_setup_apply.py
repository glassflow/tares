"""`tares factory setup apply -` (TR-1421): saving setup choices from one line of JSON on stdin.

The real command runs as a subprocess against a scratch cell with auth on, served on a local port from this process,
in a clean environment (tests/_env.py) with a throwaway home (tests/_crew_guard.py). What is checked:

  - save, keep and default are applied and the cell holds the result; `default` left out makes it the default;
  - an input that cannot be run is `bad_input`, exit 2, and the cell is asked nothing, a good operation before the
    bad one included; the input is never quoted back;
  - a failed operation does not stop the rest; a station and a key that is not an admin's get `admin_required`; an
    integration that needs a licence gets `licence_required`;
  - a settings value never reaches stdout, stderr or a file of the crew: a failed row shows `***` where the cell's
    reason quotes one (each such request is first sent straight to the cell, to show its reason holds the value);
  - against a stub that answers what the cell would not: an error body that is not the route's `detail` string is
    never printed, an old cell, a refused key, no answer and an answer that confirms nothing.

Run: python tests/test_crew_setup_apply.py
"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)
from _env import clean_env, scrub_os_environ  # noqa: E402

scrub_os_environ()
_crew_guard.scratch_home()   # this process runs the command too: not on the real home (TR-1164)
_crew_guard.install()

TMP = Path(tempfile.mkdtemp(prefix="tares-setup-apply-"))
ROOT_KEY = "root-token-setup-apply"
os.environ.update(TARES_HOME=str(TMP / "cell"), TARES_CATALOG=str(TMP / "none.yaml"),
                  TARES_AUTH_TOKEN=ROOT_KEY, TARES_OTLP_GRPC_PORT="off")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from tares import licence  # noqa: E402
from tares.crew import person  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.daemon import make_app  # noqa: E402
from tares.integrations import registry  # noqa: E402
from tares.integrations.tickets import TicketBackend  # noqa: E402
from tares.integrations.types import Field  # noqa: E402

try:
    from jsonschema import Draft202012Validator  # noqa: E402
except ImportError:
    print("FAIL  jsonschema is not installed (a test-only dependency: pip install jsonschema)")
    print("\n0 passed, 1 failed")
    sys.exit(1)

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail!s}"[:600]))


SCHEMAS = ROOT / "tares" / "crew" / "schemas"
VALID = {n: Draft202012Validator(json.loads((SCHEMAS / f"{n}.json").read_text(encoding="utf-8")))
         for n in ("setup_apply", "error")}

# Made-up secrets. They hold what an error text escapes (a backslash, both quotes, a letter outside ASCII).
SECRET = "placeholder-not-real-\\apply\"it's-é"
PADDED = "  placeholder-not-real-padded  "
NESTED = "placeholder-not-real-nested"
SECRETS = (SECRET, PADDED, NESTED, "placeholder-not-real-saved")
MARKS = ("placeholder-not-real", "not-real-", "apply\\")   # a piece of any of them, however it is escaped
PRINTED = []   # everything the command printed in this run


class Fenced(TicketBackend):
    """An integration that needs a licence."""
    name = "fenced"
    title = "Fenced"
    ee = True
    settings_schema = [Field("key", "Key", "secret")]


registry.load()
registry._loaded[(Fenced.slot, Fenced.name)] = Fenced
licence.ee_allowed = lambda feature="enterprise": False


# ---------- running the command ----------

N = [0]


def crew_env(url, key=ROOT_KEY, **more):
    """A clean environment with a crew home of its own, pointed at `url` only."""
    N[0] += 1
    home = TMP / f"home{N[0]}"
    crew = home / "crew"
    crew.mkdir(parents=True)
    return clean_env(**{"HOME": str(home), "USERPROFILE": str(home), "FACTORY_HOME": str(crew),
                        "FACTORY_CREW_HOME": str(crew), "TARES_HOME": str(home / "tares"), "TARESD_URL": url,
                        "TARES_FACTORY_URL": url, "TARES_AUTH_TOKEN": key, "TARES_URL_LOG": str(home / "urls.log"),
                        "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8", "PYTHONSTARTUP": None,
                        "TARES_CATALOG": None, "TARES_OTLP_GRPC_PORT": None, **more})


def asked(env):
    log = Path(env["TARES_URL_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


MODULE = ("-m", "tares.crew")                                           # python -m tares.crew setup ...
CLI = ("-c", "from tares.cli import main; main()", "factory")           # tares factory setup ...


def run(env, stdin, argv=("setup", "apply", "-"), entry=MODULE):
    """(exit code, stdout, stderr) of the command with `stdin` (a document, text or bytes) on its stdin."""
    if isinstance(stdin, (dict, list)):
        stdin = json.dumps(stdin, ensure_ascii=False) + "\n"
    if isinstance(stdin, str):
        stdin = stdin.encode("utf-8")
    p = subprocess.run([sys.executable, *entry, *argv], input=stdin, env=env, cwd=str(ROOT), capture_output=True,
                       timeout=120)
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    PRINTED.extend((out, err))
    return p.returncode, out, err


def document(label, got, name="setup_apply", want=0):
    """Check the exit code, one valid line on stdout and nothing on stderr; return the document."""
    code, out, err = got
    ck(f"{label}: exit {want}", code == want, f"{code} {out[:300]} {err[-300:]}")
    try:
        doc = json.loads(out)
    except ValueError:
        ck(f"{label}: stdout is JSON", False, f"{out[:300]!r} {err[-300:]}")
        return {}
    bad = [e.message for e in VALID[name].iter_errors(doc)]
    ck(f"{label}: one line that validates against {name}.json", out.count("\n") == 1 and not bad, bad or out[:300])
    ck(f"{label}: nothing on stderr", err == "", err[-300:])
    return doc


def rows(doc):
    return [(r["ok"], r["status"], r["code"]) for r in doc.get("results", [])]


def ops(*items):
    return {"ops": list(items)}


def op(kind, slot, name, **more):
    return {"op": kind, "slot": slot, "name": name, **more}


def clean(text):
    return not any(m in text for m in MARKS)


# ---------- the scratch cell ----------

app = make_app()
SOCK = socket.socket()
SOCK.bind(("127.0.0.1", 0))
CELL = f"http://127.0.0.1:{SOCK.getsockname()[1]}"
SERVER = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
THREAD = threading.Thread(target=SERVER.run, kwargs={"sockets": [SOCK]}, daemon=True)
THREAD.start()
for _ in range(600):
    if SERVER.started or not THREAD.is_alive():
        break
    time.sleep(0.05)
if not SERVER.started:
    print("FAIL  the scratch cell did not start\n\n0 passed, 1 failed")
    sys.exit(1)

ROOT_HTTP = httpx.Client(base_url=CELL, headers={"Authorization": f"Bearer {ROOT_KEY}"}, timeout=30)


def cell_state():
    """What the cell holds for the setup: its integrations and which steps are set."""
    return (ROOT_HTTP.get("/api/factory/integrations").json()["slots"],
            [(s["slot"], s["state"]) for s in ROOT_HTTP.get("/api/factory/setup").json()["steps"]])


def slot(name):
    return next(s for s in cell_state()[0] if s["slot"] == name)


def default_of(name):
    return next((c["name"] for c in slot(name)["connections"] if c["default"]), None)


print("== an input that cannot be run")
env = crew_env(CELL)
before = cell_state()
GOOD = op("save", "tickets", "linear", settings={"api_key": SECRET})
code, out, err = run(env, '{"ops":[{"op":"nope"}]}\n', entry=CLI)
doc = document("an unknown op, through `tares factory`", (code, out, err), "error", 2)
ck("an unknown op: bad_input", doc.get("error", {}).get("code") == "bad_input", doc)
quoted = json.dumps(SECRET)[1:-1]
BAD = (
    ("empty stdin", b""),
    ("not JSON", f"api_key={SECRET}\n"),
    ("not UTF-8", b'{"ops": [{"name": "\xff\xfe"}]}\n'),
    ("NaN", '{"ops": [{"op": "save", "slot": "tickets", "name": "linear", "settings": {"api_key": NaN}}]}\n'),
    ("a lone surrogate", '{"ops": [{"op": "save", "slot": "tickets", "name": "\\ud800"}]}\n'),
    ("JSON nested too deep", "[" * 200_000 + "\n"),
    ("a document over two lines", '{"ops":\n[]}\n'),
    ("a list", [GOOD]),
    ("no ops", {}),
    ("ops not a list", {"ops": {"0": GOOD}}),
    ("ops null", {"ops": None}),
    ("a key beside ops", {"ops": [GOOD], quoted: 1}),
    ("an empty op", ops(GOOD, {})),
    ("an op that is null", ops(GOOD, None)),
    ("slot null", ops(GOOD, op("keep", None, "tares"))),
    ("name null", ops(GOOD, op("keep", "review", None))),
    ("default null", ops(GOOD, op("save", "tickets", "linear", default=None))),
    ("an op that is not an object", ops(GOOD, SECRET)),
    ("an op that is not a string", ops(GOOD, op(["save"], "tickets", "linear"))),
    ("an unknown op after a good one", ops(GOOD, op(SECRET, "tickets", "linear"))),
    ("an unknown slot", ops(GOOD, op("keep", SECRET, "tares"))),
    ("a slot that is not a string", ops(GOOD, op("keep", ["review"], "tares"))),
    ("no name", ops(GOOD, {"op": "keep", "slot": "review"})),
    ("an empty name", ops(GOOD, op("keep", "review", "  "))),
    ("a name that is not a string", ops(GOOD, op("keep", "review", 7))),
    ("settings not an object", ops(GOOD, op("save", "tickets", "linear", settings=SECRET))),
    ("settings null", ops(GOOD, op("save", "tickets", "linear", settings=None))),
    ("default not a boolean", ops(GOOD, op("save", "tickets", "linear", default="yes"))),
    ("a misspelt key", ops(GOOD, op("save", "tickets", "linear", setings={"api_key": SECRET}))),
    ("settings on keep", ops(GOOD, op("keep", "review", "tares", settings={}))),
)
for why, text in BAD:
    code, out, err = run(env, text)
    got = json.loads(out) if out.startswith("{") else {}
    ck(f"{why}: bad_input, exit 2, nothing on stderr",
       code == 2 and err == "" and got.get("error", {}).get("code") == "bad_input"
       and not list(VALID["error"].iter_errors(got)), (code, out[:300], err[-300:]))
    ck(f"{why}: the input is not quoted", clean(out + err), out[:300])
for argv in (("setup", "apply"), ("setup", "apply", "ops.json"), ("setup", "apply", "-", "extra"),
             ("setup", "--apply"), ("setup", "--apply", "-", "--progress")):
    doc = document(f"`{' '.join(argv)}`", run(env, ops(GOOD), argv), "error", 2)
    ck(f"`{' '.join(argv)}`: usage", doc.get("error", {}).get("code") == "usage", doc)
ck("none of these asked the cell anything", asked(env) == [], asked(env))
ck("and the cell holds what it held", cell_state() == before)

print("== save, keep and default")
env = crew_env(CELL)
ck("before: Tares is the tickets default and review is not set",
   default_of("tickets") in (None, "tares") and dict(cell_state()[1])["review"] == "not_set", cell_state()[1])
good = ops(op("save", "tickets", "linear", settings={"api_key": "placeholder-not-real-saved"}),
           op("keep", "review", "tares"),
           op("save", "ci", "github", settings={"no_ci": False}, default=False),
           op("default", "ci", "github"))
doc = document("four operations", run(env, good))
ck("four operations: ok, a row for each in order, 200 and no code", doc.get("ok") is True
   and rows(doc) == [(True, 200, None)] * 4
   and [(r["index"], r["op"], r["slot"], r["name"], r["detail"]) for r in doc["results"]]
   == [(0, "save", "tickets", "linear", None), (1, "keep", "review", "tares", None),
       (2, "save", "ci", "github", None), (3, "default", "ci", "github", None)], doc)
ck("save without `default`: linear is the tickets default", default_of("tickets") == "linear", slot("tickets"))
ck("save: the cell holds the secret", app.state.store.stored_settings("tickets", "linear").get("api_key")
   == "placeholder-not-real-saved")
ck("keep: the review step is set", dict(cell_state()[1])["review"] == "set", cell_state()[1])
ck("default: github is the ci default", default_of("ci") == "github", slot("ci"))
doc = document("save with default false", run(env, ops(op("save", "tickets", "tares", default=False))))
ck("save with default false: linear stays the default", doc.get("ok") is True and default_of("tickets") == "linear",
   slot("tickets"))
for argv, entry in ((("setup", "apply", "-", "--json"), CLI), (("setup", "--apply", "-"), CLI),
                    (("setup", "--apply", "-", "--json"), CLI), (("setup", "--apply", "-", "--json"), MODULE)):
    label = f"`{'tares factory ' if entry == CLI else ''}{' '.join(argv)}`"
    doc = document(label, run(env, ops(op("keep", "review", "tares")), argv, entry))
    ck(f"{label}: applied", rows(doc) == [(True, 200, None)], doc)
doc = document("a last line with no newline", run(env, json.dumps(ops(op("keep", "review", "tares")))))
ck("a last line with no newline: applied", rows(doc) == [(True, 200, None)], doc)
doc = document("a BOM, CR LF and a second line",
               run(env, b'\xef\xbb\xbf{"ops": [{"op": "keep", "slot": "review", "name": "tares"}]}\r\nnot read\n'))
ck("only the first line is read", rows(doc) == [(True, 200, None)], doc)
doc = document("no operations", run(env, ops()))
ck("no operations: ok, no rows", doc == {"schema_version": 1, "ok": True, "results": []}, doc)

print("== a failed operation does not stop the rest")
doc = document("three operations, two fail", run(env, ops(
    op("default", "monitoring", "rius"), op("save", "tickets", "fenced"), op("default", "tickets", "tares"))), want=1)
ck("rows in order: refused, licence_required, applied", doc.get("ok") is False and rows(doc) == [
    (False, 422, "tares_error"), (False, 402, "licence_required"), (True, 200, None)], doc)
said = ROOT_HTTP.put("/api/factory/integrations/monitoring/default", json={"name": "rius"}).json().get("detail", "")
ck("the cell's status and reason are the detail", said
   and doc["results"][0]["detail"] == f"Tares did not save this (HTTP 422): {said}", (said, doc))
ck("the one after them was applied", default_of("tickets") == "tares", slot("tickets"))
doc = document("keep what is not the default", run(env, ops(op("keep", "tickets", "linear"))), want=1)
ck("keep what is not the default: tares_error", rows(doc) == [(False, 422, "tares_error")]
   and "not the default" in doc["results"][0]["detail"], doc)

print("== a station and a key that is not an admin's")
REFUSED = ops(op("save", "tickets", "linear", settings={"api_key": SECRET}), op("keep", "ci", "github"),
              op("default", "tickets", "linear"))
before = cell_state()
key_file = TMP / "crew-key"
key_file.write_text(ROOT_KEY + "\n", encoding="utf-8")
env = crew_env(CELL, key=None, FACTORY_STATION="northwind-build-1", FACTORY_ROLE="builder",
               TARES_FACTORY_TOKEN_FILE=str(key_file))
reader = ROOT_HTTP.post("/api/keys", json={"name": "writer", "scopes": ["read", "ingest"]}).json()["secret"]
for who, env in (("a station", env), ("a key that is not an admin's", crew_env(CELL, key=reader))):
    doc = document(who, run(env, REFUSED), want=1)
    ck(f"{who}: admin_required on every operation", rows(doc) == [(False, 403, "admin_required")] * 3, doc)
    ck(f"{who}: the sentence", {r["detail"] for r in doc.get("results", [])}
       == {"Saving setup needs a person's admin key; a station key cannot."}, doc)
    ck(f"{who}: nothing changed", cell_state() == before)

print("== no settings value is repeated")
env = crew_env(CELL)
# each pair: the operations, and which of them the cell answers with a value of the settings in its detail
ECHO = (
    ("a secret posted as a setting name", ops(op("save", "tickets", "linear", settings={"api_key": SECRET, SECRET: "x"})), 0),
    ("a secret given as the name", ops(op("save", "tickets", SECRET, settings={"api_key": SECRET})), 0),
    ("a padded secret the cell strips", ops(op("save", "tickets", "linear", settings={"api_key": PADDED, "oops": 1}),
                                           op("default", "tickets", PADDED)), 1),
    ("a value nested in the settings", ops(op("save", "tickets", "linear", settings={"api_key": {"in": [NESTED]}}),
                                          op("default", "tickets", NESTED)), 1),
)
for why, body, index in ECHO:
    one = body["ops"][index]
    path = f"/api/factory/integrations/{one['slot']}" + ("/default" if one["op"] == "default" else "")
    said = ROOT_HTTP.put(path, json={k: v for k, v in one.items() if k not in ("op", "slot")}).json().get("detail", "")
    ck(f"{why}: sent straight to the cell, the reason holds the value", not clean(said), said)
    code, out, err = run(env, body)
    doc = document(why, (code, out, err), want=1)
    ck(f"{why}: neither stdout nor stderr holds it", clean(out + err), out)
    ck(f"{why}: the detail has *** in its place",
       "***" in doc.get("results", [{}] * (index + 1))[index].get("detail", ""), doc)
doc = document("a value inside another", run(env, ops(
    op("save", "tickets", "linear", settings={"api_key": "not-real", "oops": "placeholder-not-real-whole"}),
    op("default", "tickets", "placeholder-not-real-whole"))), want=1)
ck("a value that holds another one goes as a whole", doc["results"][1]["name"] == "***"
   and "placeholder" not in doc["results"][1]["detail"], doc)
doc = document("values under 4 characters", run(env, ops(
    op("save", "tickets", "linear", settings={"api_key": "abc", "oops": "x"}))), want=1)
ck("a value under 4 characters is left alone, and the reason stays readable",
   "***" not in doc["results"][0]["detail"] and "oops" in doc["results"][0]["detail"], doc)
ck("overlapping values leave no end of either", person._redact("ab-abcdef-ghij", {"abcd", "cdef", "ghij"}) == "ab-***-***",
   person._redact("ab-abcdef-ghij", {"abcd", "cdef", "ghij"}))


# ---------- a stub: answers the scratch cell does not give ----------

WORLD = {"answers": [], "puts": []}


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _send(self, status, raw, kind="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        body = {"/health": {"status": "ok"}, "/api/crew/settings": {"settings": {}}}.get(self.path)
        self._send(200 if body is not None else 404, json.dumps(body or {}).encode())

    def do_PUT(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        WORLD["puts"].append((self.path, json.loads(raw), self.headers.get("X-Tares-Station")))
        answer = WORLD["answers"].pop(0) if WORLD["answers"] else "applied"
        if answer == "applied":
            self._send(200, json.dumps({"slot": self.path.split("/")[4]}).encode())
        elif answer == "hang up":
            self.close_connection = True
        else:
            status, body = answer
            text = body(raw) if callable(body) else body
            self._send(status, text.encode() if isinstance(text, str) else json.dumps(text).encode())

    def log_message(self, *a):
        pass


SRV = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=SRV.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{SRV.server_address[1]}"

print("== what is sent")
env = crew_env(STUB)
document("a person's run", run(env, ops(op("save", "tickets", "linear"), op("keep", "review", "tares"),
                                        op("save", "ci", "github", settings={}, default=False),
                                        op("default", "ci", "github"))))
ck("each operation is one PUT to its route, with only what the operation held, and no station header",
   WORLD["puts"] == [("/api/factory/integrations/tickets", {"name": "linear"}, None),
                     ("/api/factory/setup/review/keep", {"name": "tares"}, None),
                     ("/api/factory/integrations/ci", {"name": "github", "settings": {}, "default": False}, None),
                     ("/api/factory/integrations/ci/default", {"name": "github"}, None)], WORLD["puts"])

print("== answers that are not an applied operation")
env = crew_env(STUB)
ONE = op("save", "tickets", "linear", settings={"api_key": SECRET})
echo_list = (422, lambda raw: {"detail": [{"msg": "bad", "input": raw.decode()}]})
ANSWERS = (
    ("a validation list that repeats the body", echo_list, 422, "tares_error"),
    ("a body that is not JSON and repeats it", (500, lambda raw: "boom " + raw.decode()), 500, "tares_error"),
    ("a detail that is an object", (409, lambda raw: {"detail": {"body": raw.decode()}}), 409, "tares_error"),
    ("a refused key", (401, {"detail": "no such key"}), 401, "key_refused"),
    ("a 403 with no reason", (403, "<html>blocked</html>"), 403, "forbidden"),
    ("no such route", (404, {"detail": "Not Found"}), 404, "cell_too_old"),
    ("a path that takes no PUT", (405, {"detail": "Method Not Allowed"}), 405, "cell_too_old"),
    ("a redirect", (302, ""), None, "tares_error"),
    ("a page that is not Tares", (200, "<html>sign in</html>"), None, "tares_error"),
    ("JSON that confirms nothing", (200, {"ok": True}), None, "tares_error"),
    ("no answer", "hang up", None, "tares_error"),
)
WORLD["answers"] = [a for _, a, _, _ in ANSWERS]
code, out, err = run(env, ops(*[ONE] * len(ANSWERS), ONE))
doc = document("a run through all of them", (code, out, err), want=1)
got = doc.get("results", [])
for (why, _, status, kind), row in zip(ANSWERS, got):
    ck(f"{why}: {kind}, status {status}", (row["ok"], row["status"], row["code"]) == (False, status, kind), row)
ck("the operation after all of them is applied", len(got) == len(ANSWERS) + 1 and got[-1]["ok"] is True, got[-1:])
ck("a body that is not the route's detail string is not printed: only the status",
   [r["detail"] for r in got[:3]] == [f"Tares did not save this (HTTP {n})" for n in (422, 500, 409)], got[:3])
ck("nothing of the secret is printed", clean(out + err), out)
ck("no answer: the row says it may or may not be saved", "not known" in got[len(ANSWERS) - 1]["detail"], got)

print("== a Tares that does not answer at the start")
env = crew_env("http://127.0.0.1:9")
doc = document("Tares down", run(env, ops(ONE)), "error", 1)
ck("Tares down: the error shape", doc.get("error", {}).get("code") == "error", doc)

print("== a bug in the command tells its type only")


def in_process(env, body):
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO((json.dumps(body) + "\n").encode("utf-8")), encoding="utf-8")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = person.cmd_setup_apply(["setup", "apply", "-"], env)
    finally:
        sys.stdin = saved
    PRINTED.extend((out.getvalue(), err.getvalue()))
    return code, out.getvalue(), err.getvalue()


REAL_HTTP, REAL_PRELUDE = TaresClient.tares_http, person.prelude
calls = []


def breaks_once(self, method, path, body=None, headers=None):
    calls.append(path)
    if len(calls) == 1:
        raise RuntimeError(f"could not send {body}")
    return REAL_HTTP(self, method, path, body, headers)


TaresClient.tares_http = breaks_once
code, out, err = in_process(crew_env(STUB), ops(ONE, ONE))
TaresClient.tares_http = REAL_HTTP
doc = json.loads(out)
ck("while sending: that row is tares_error with the type, the next is applied", code == 1
   and rows(doc) == [(False, None, "tares_error"), (True, 200, None)] and "RuntimeError" in doc["results"][0]["detail"],
   (code, out, err))
ck("while sending: the secret is not printed", clean(out + err), out + err)


def breaks(argv, env):
    raise RuntimeError(f"broke on {SECRET}")


person.prelude = breaks
code, out, err = in_process(crew_env(STUB), ops(ONE))
person.prelude = REAL_PRELUDE
ck("before sending: the error shape with the type only", code == 1 and json.loads(out)["error"]
   == {"code": "error", "message": "setup apply failed: RuntimeError"} and err == "", (code, out, err))

print("== nowhere")
ck("no run printed a settings value", all(clean(t) for t in PRINTED) and len(PRINTED) > 80, len(PRINTED))
held = [str(f) for home in TMP.glob("home*") for f in home.rglob("*")
        if f.is_file() and any(m.encode() in f.read_bytes() for m in (*MARKS, *SECRETS))]
ck("no file of any crew home (events, logs, the request log) holds one",
   not held and any(TMP.glob("home*/urls.log")), held)
reached = {line for log in TMP.glob("home*/urls.log") for line in log.read_text(encoding="utf-8").splitlines()}
ck("the command asked only this test's own addresses", reached
   and all(line.startswith((CELL + "/", STUB + "/", "http://127.0.0.1:9/")) for line in reached), sorted(reached)[:10])

SRV.shutdown()
SERVER.should_exit = True
THREAD.join(timeout=30)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
