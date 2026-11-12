# Tares enterprise licence, see ee/LICENSE
"""A crew's name on a Tares shared by several people (TR-541), the Python crew engine.

With one person besides local nothing is asked; with two the name is asked once, suggesting
<first name>-<short hostname>; a typed name is used, a name that is not valid is asked again,
FACTORY_CREW_NAME skips the question, and a default crew that is already running keeps its name when
Tares gains a second person. The Tares is a daemon started the way `tares up` starts it, with a test
licence (ee/tests/licensed.py serve), and its MCP endpoint. Uses a fake Claude Code and throwaway homes.

Run: .venv/bin/python ee/tests/test_crewname_team.py
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path

REPO = Path(licensed.REPO)
sys.path.insert(0, str(REPO / "tests" / "factory"))
import parity   # noqa: E402  (crew_env: a throwaway crew with the fake Claude Code)

P = F = 0
PROCS = []


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode() or "null")


def wait_for(fn, what, log):
    for _ in range(150):
        try:
            if fn():
                return
        except Exception:
            pass
        time.sleep(0.2)
    print("FAIL " + what)
    print(Path(log).read_text(errors="replace") if Path(log).exists() else "")
    sys.exit(1)


def start_licensed_tares(d: Path, env: dict):
    """The daemon the way `tares up` starts it, with a test licence, and the MCP endpoint stations reach it through."""
    d.mkdir(parents=True, exist_ok=True)
    port = free_port()
    # keep a PYTHONPATH the machine runs the test with (a checkout whose installed metadata lacks the ee entry point)
    pp = os.pathsep.join(x for x in (env.get("PYTHONPATH"), os.environ.get("PYTHONPATH")) if x)
    denv = {**env, "PYTHONPATH": pp, "TARES_CATALOG": str(d / "none.yaml"), "TARES_OTLP_GRPC_PORT": "off", "HOME": str(d)}
    log = d / "taresd.log"
    PROCS.append(subprocess.Popen([sys.executable, str(REPO / "ee" / "tests" / "licensed.py"), "serve", "--port", str(port),
                                   "--data-dir", str(d)], env=denv, stdout=open(log, "w"), stderr=subprocess.STDOUT))
    url = f"http://127.0.0.1:{port}"
    wait_for(lambda: http("GET", url + "/health"), f"Tares did not start on {url}", log)
    mport = free_port()
    menv = {**denv, "TARES_MCP_TRANSPORT": "streamable-http", "TARES_MCP_HOST": "127.0.0.1", "TARES_MCP_PORT": str(mport),
            "TARESD_URL": url, "TARES_FACTORY_URL": "", "TARES_AUTH_TOKEN": ""}
    mlog = d / "mcp.log"
    PROCS.append(subprocess.Popen([sys.executable, "-c", "from tares.cli import run_mcp; run_mcp()"], env=menv,
                                  stdout=open(mlog, "w"), stderr=subprocess.STDOUT))
    mcp = f"http://127.0.0.1:{mport}/mcp"

    def mcp_up():
        req = urllib.request.Request(mcp, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}).encode())
        with urllib.request.urlopen(req, timeout=2) as r:
            return b"serverInfo" in r.read()
    wait_for(mcp_up, f"the MCP endpoint did not start on {mcp}", mlog)
    return url, mcp


def run(T: Path):
    env = parity.crew_env(T)
    (T / "gitconfig").write_text("[user]\n\tname = Ashish Bagri\n")
    for k in ("FACTORY_CREW_NAME", "FACTORY_CREW", "FACTORY_ORCHESTRATOR", "TARES_URL", "TARES_AUTH_TOKEN"):
        env.pop(k, None)
    state = Path(env["FAKE_CLAUDE_STATE"])
    home = Path(env["HOME"])
    D = Path(env["FACTORY_CREW_HOME"])
    # Never write outside this test's own folder: the real crew home and factory home must not be reachable.
    if not all(str(p.resolve()).startswith(str(T)) for p in (D, home, state)):
        print("FAIL the test would write outside " + str(T))
        sys.exit(1)

    def trust(*folders):
        f = home / ".claude.json"
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            doc = {}
        for d in folders:
            Path(d).mkdir(parents=True, exist_ok=True)
            doc.setdefault("projects", {}).setdefault(str(d), {})["hasTrustDialogAccepted"] = True
        f.write_text(json.dumps(doc))

    def st(h, station, key):
        """A field of a station in one crew's stations.json."""
        try:
            v = json.loads((Path(h) / "stations.json").read_text()).get(station, {}).get(key)
        except (OSError, ValueError):
            return ""
        return "" if v is None or v is False else v

    def envof(h, station, key):
        try:
            v = json.loads((Path(h) / "stations" / f"{station}.settings.json").read_text()).get("env", {}).get(key)
        except (OSError, ValueError):
            return ""
        return "" if v is None or v is False else v

    def crew_json(h):
        try:
            return json.loads((Path(h) / "crew.json").read_text())
        except (OSError, ValueError):
            return {}

    def launches():
        try:
            lines = (state / "calls.jsonl").read_text().splitlines()
        except OSError:
            return 0
        return sum(1 for x in lines if x.strip() and "--bg" in json.loads(x)["args"])

    def up(out_name, *args, stdin="", crew_home=None, **extra_env):
        """The crew-up command of the Python engine; returns (exit code, stdout and stderr together)."""
        e = {**env, "TARESD_URL": URL, "TARES_FACTORY_MCP_URL": MCP, **extra_env}
        if crew_home is not None:
            e["FACTORY_CREW_HOME"] = str(crew_home)
        p = subprocess.run([sys.executable, "-m", "tares.crew", "crew", "up", *args], cwd=str(REPO), env=e, input=stdin,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        out = p.stdout + p.stderr
        (T / out_name).write_text(out)
        return p.returncode, out

    URL, MCP = start_licensed_tares(T / "tares", env)
    lic = http("GET", URL + "/api/licence")
    ck("the Tares runs under a valid test licence",
       [lic.get("valid"), lic.get("ee_allowed"), lic.get("source")] == [True, True, "env"], str(lic))

    def others():
        return [p for p in http("GET", URL + "/api/people")["people"] if p["id"] != "local"]

    trust(D)
    rc, out = up("up-default.out", "--yes")
    ck("default crew: up exits 0", f"{URL}/crew" in out, out)
    ck("default crew: crew-orchestrator is recorded", st(D, "crew-orchestrator", "status") == "active")

    # A Tares shared by several people asks for the name once, suggesting <first name>-<short hostname>.
    http("POST", URL + "/api/people", {"name": "Lena", "email": "lena@example.com"})
    ck("one person besides local: nothing is asked", len(others()) == 1)
    C0 = T / "crew-one"
    trust(C0)
    rc, out = up("up-one.out", stdin="", crew_home=C0)
    ck("one person: crew up does not ask for a name and keeps crew",
       "What should this crew be called" not in out and st(C0, "crew-orchestrator", "status") == "active"
       and "crew_name" not in crew_json(C0), out)
    http("POST", URL + "/api/people", {"name": "Ravi", "email": "ravi@example.com"})
    C1, C2, C3, C4 = (T / n for n in ("crew-two", "crew-three", "crew-four", "crew-five"))
    trust(C1, C2, C3)
    rc, out = up("up-two.out", stdin="", crew_home=C1)
    sugg = crew_json(C1).get("crew_name") or ""
    ck("two people: the name is asked, suggesting <first name>-<short hostname>",
       "What should this crew be called" in out and re.fullmatch(r"ashish(-[a-z0-9-]+)?", sugg) is not None
       and f"recommended: {sugg} " in out, out)
    ck("two people: enter takes the suggestion and names the stations with it",
       st(C1, f"{sugg}-orchestrator", "status") == "active" and envof(C1, f"{sugg}-reviewer", "FACTORY_CREW") == sugg)
    rc, out = up("up-three.out", stdin="ana-laptop\n", crew_home=C2)
    ck("two people: a typed name is used",
       crew_json(C2).get("crew_name") == "ana-laptop" and st(C2, "ana-laptop-orchestrator", "status") == "active", out)
    rc, out = up("up-four.out", stdin="Bad Name\nana-desk\n", crew_home=C3)
    ck("two people: a name that is not valid is asked again",
       ("is not one of the choices" in out or "is not a valid crew name" in out) and crew_json(C3).get("crew_name") == "ana-desk", out)
    trust(C4)
    rc, out = up("up-five.out", stdin="", crew_home=C4, FACTORY_CREW_NAME="ravi-pc")
    ck("two people: FACTORY_CREW_NAME skips the question",
       "What should this crew be called" not in out and crew_json(C4).get("crew_name") == "ravi-pc", out)
    # A default crew that is already running keeps its name when Tares gains a second person: crew up must not
    # start a second orchestrator and reviewer under a new name beside the running ones.
    n = launches()
    _, o1 = up("up-d1.out", "--yes", crew_home=D)
    _, o2 = up("up-d2.out", stdin="", crew_home=D)
    stations = json.loads((D / "stations.json").read_text())
    ck("running default crew: crew up does not ask, rename or start anything",
       "What should this crew be called" not in o1 + o2 and "crew_name" not in crew_json(D) and launches() == n
       and sum(1 for s in stations.values() if s.get("role") == "orchestrator") == 1, o1 + o2)
    rc, out = up("up-d3.out", "--yes", crew_home=D, FACTORY_CREW_NAME="zzz")
    ck("running default crew: another FACTORY_CREW_NAME is refused and nothing is stored",
       rc != 0 and "already running under the name crew" in out and "crew_name" not in crew_json(D) and launches() == n, out)


def main():
    T = Path(tempfile.mkdtemp(prefix="crewname-team-")).resolve()
    try:
        run(T)
    finally:
        for p in PROCS:
            p.terminate()
        for p in PROCS:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(T, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
