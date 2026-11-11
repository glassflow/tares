"""`tares factory with-secrets` and `tares factory secrets` (TR-775), against a stand-in for Tares.

  - the project is --project, else TARES_PROJECT; without either it says so (exit 2);
  - the command runs with the secrets in its environment, and only names are printed;
  - Tares refusing (not a member, secrets off) means the command does not run (exit 1);
  - --only names some; one the project lacks stops the command;
  - the command's exit code is passed on;
  - `secrets` lists names and how to use them.

Run: python tests/test_crew_with_secrets.py
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares.crew import dispatch, with_secrets as ws  # noqa: E402
from tares.crew.client import TaresHTTPError  # noqa: E402

P = F = 0
VALUE = "secret-value-0123456789"


def check(label, ok, detail=""):
    global P, F
    if ok:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}  {detail}")


class Client:
    calls = []
    answer = {"values": {"CELL_KEY": VALUE}, "unreadable": []}
    refuse = None

    def __init__(self, paths, env, say=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def tares_http(self, method, path, body=None):
        Client.calls.append((method, path, body))
        if Client.refuse:
            raise TaresHTTPError("curl: (22) The requested URL returned error", Client.refuse)
        if method == "GET":
            return json.dumps({"enabled": True, "off": "", "secrets": [
                {"name": "CELL_KEY", "set_by": "Ann", "set_at": "2026-11-11T10:00:00Z", "readable": True}]})
        return json.dumps(Client.answer)


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    saved = ws.TaresClient
    ws.TaresClient = Client
    Client.calls = []
    try:
        with redirect_stdout(out), redirect_stderr(err):
            fn = ws.cmd_secrets if argv[0] == "secrets" else ws.cmd_with_secrets
            rc = fn(argv, env if env is not None else {"PATH": os.environ.get("PATH", ""), "TARES_PROJECT": "uc_1"})
    finally:
        ws.TaresClient = saved
    return rc, out.getvalue(), err.getvalue()


tmp = Path(tempfile.mkdtemp())
probe = tmp / "probe.py"
probe.write_text("import os, sys\nopen(sys.argv[1], 'w').write(os.environ.get('CELL_KEY', '<unset>'))\n"
                 "sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)\n")
seen = tmp / "seen"

check("both are crew commands", dispatch.PORTED.get(("with-secrets",)) is not None
      and dispatch.PORTED.get(("secrets",)) is not None)

rc, out, err = run(["with-secrets", "--", sys.executable, str(probe), str(seen)])
check("the command runs with the secret in its environment", rc == 0 and seen.read_text() == VALUE, err)
check("the project comes from TARES_PROJECT", Client.calls[0][1] == "/api/projects/uc_1/secrets/use", Client.calls)
check("only the name is printed, never the value", "CELL_KEY" in err and VALUE not in err + out, err)

rc, out, err = run(["with-secrets", "--project", "my ui", "--", sys.executable, str(probe), str(seen)])
check("--project names it (quoted in the path)", Client.calls[0][1] == "/api/projects/my%20ui/secrets/use", Client.calls)

rc, out, err = run(["with-secrets", "--", sys.executable, str(probe), str(seen), "3"])
check("the command's exit code is passed on", rc == 3)

seen.write_text("not run")
Client.refuse = 403
rc, out, err = run(["with-secrets", "--", sys.executable, str(probe), str(seen)])
check("not a member: the command does not run", rc == 1 and seen.read_text() == "not run" and "members" in err, err)
Client.refuse = 409
rc, out, err = run(["with-secrets", "--", sys.executable, str(probe), str(seen)])
check("secrets off on Tares: it says so and does not run", rc == 1 and "TARES_SECRETS_KEY" in err
      and seen.read_text() == "not run", err)
Client.refuse = None

rc, out, err = run(["with-secrets", "--only", "CELL_KEY", "--", sys.executable, str(probe), str(seen)])
check("--only asks Tares for those names", Client.calls[0][2] == {"names": ["CELL_KEY"]} and rc == 0, Client.calls)
seen.write_text("not run")
rc, out, err = run(["with-secrets", "--only", "CELL_KEY,MISSING", "--", sys.executable, str(probe), str(seen)])
check("a name the project lacks stops the command", rc == 1 and "MISSING" in err and seen.read_text() == "not run", err)

rc, out, err = run(["with-secrets", "--", sys.executable, str(probe), str(seen)], {"PATH": os.environ.get("PATH", "")})
check("no project anywhere: it says how to name one", rc == 2 and "--project" in err, err)
rc, out, err = run(["with-secrets"])
check("no command: usage", rc == 2 and "usage" in err, err)
rc, out, err = run(["with-secrets", "--", "/nonexistent/cmd"])
check("a command that is not there: 127", rc == 127, err)

rc, out, err = run(["secrets"])
check("secrets lists the names and how to use them", rc == 0 and "CELL_KEY" in out and "set by Ann" in out
      and "with-secrets" in out and VALUE not in out, out + err)

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
