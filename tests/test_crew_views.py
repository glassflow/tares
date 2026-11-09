"""tares.crew.views: status, desk, roster, attach and logs (TR-957), checked against the bash commands.

Every case builds the same throwaway crew twice (tests/_crew_fake.py: stations.json, the sessions the fake
Claude Code lists, a stub Tares on a local port), runs the command once through the bash engine
(claude-plugin/bin/factory) and once through `python -m tares.crew`, and compares exit code, stdout and
stderr (the two together as one pipe too, so the order of the lines counts) and the files the command leaves.
Bash is the reference: where it and the ticket disagree, bash wins. A few cases also check the text itself,
so a change that breaks both engines the same way still fails.

The Claude Code version of a station comes from the `version` field of its session row; the bash suites'
fake-ps stub cannot be seen by psutil, so that is the way to test it here.

Untested on real Windows (TR-946): attach and logs start `claude` as a child process there instead of
replacing this one, and the process reads for the watch and for "who is attached" go through psutil.
Nothing here starts a real Claude Code, reads the real HOME or reaches a real Tares.

Run: python tests/test_crew_views.py [case name ...]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "factory"))

import fake_claude  # noqa: E402
from _crew_fake import fake_crew_env  # noqa: E402

FACTORY = ROOT / "claude-plugin" / "bin" / "factory"
P = F = 0
CASES = []


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail!s}"))


def case(fn):
    CASES.append(fn)
    return fn


if os.name == "nt" or not Path("/bin/bash").exists() or not shutil.which("jq"):
    print("skip: the comparison needs bash and jq")
    print("\n0 passed, 0 failed")
    sys.exit(0)

# ---------- a stub Tares ----------

WORLD = {"desk": [], "projects": [], "settings": {"autonomy": "L4-ship"}}


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = {"/health": {"status": "ok"},
                "/api/crew/settings": {"settings": WORLD["settings"]},
                "/api/crew/projects": {"projects": WORLD["projects"]},
                "/api/desk": {"desk": WORLD["desk"]}}.get(path)
        raw = json.dumps(body if body is not None else {}).encode()
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_POST = do_GET  # noqa: N815

    def log_message(self, *a):
        pass


SRV = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
UP = f"http://127.0.0.1:{PORT}"
DOWN = "http://127.0.0.1:9"

# ---------- a world, and a run in it ----------

TOP = Path(tempfile.mkdtemp(prefix="tares crew views "))
N = [0]

OID, RID, BID, SID = "aaaa0001", "bbbb0002", "cccc0003", "dddd0004"


def station(role, sid, project="", status="active", model="opus", effort="high", **extra):
    return {"role": role, "status": status, "project": project, "repo": "", "cwd": "", "model": model,
            "effort": effort, "id": sid, **extra}


def world(stations=None, agents=None, version="2.1.310", extra_files=None):
    """A new crew in its own folder: returns (env, tmp)."""
    N[0] += 1
    tmp = TOP / f"w{N[0]}"
    tmp.mkdir()
    env = fake_crew_env(tmp)
    env["TARESD_URL"] = UP
    crew = Path(env["FACTORY_CREW_HOME"])
    if stations is not None:
        (crew / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    state = Path(env["FAKE_CLAUDE_STATE"])
    fake_claude.write_agents(state, agents or [])
    (state / "version").write_text(version + "\n", encoding="utf-8")
    for rel, text in (extra_files or {}).items():
        p = crew / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return env, tmp


def command(engine, argv):
    if engine == "bash":
        return ["/bin/bash", str(FACTORY)] + argv
    return [sys.executable, "-m", "tares.crew"] + argv


def run(engine, env, argv, merged=False, extra_env=None):
    e = dict(env, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", TARES_FACTORY_ENGINE=engine, **(extra_env or {}))
    e.pop("PYTHONSTARTUP", None)
    if engine == "auto":  # the dispatcher decides: what is ported runs in Python, the rest in bash
        e.pop("TARES_FACTORY_ENGINE")
    p = subprocess.run(command(engine, argv), env=e, cwd=str(ROOT), capture_output=not merged, text=True,
                       encoding="utf-8", errors="replace", timeout=120,
                       stdout=subprocess.PIPE if merged else None, stderr=subprocess.STDOUT if merged else None)
    return p.returncode, (p.stdout or ""), ("" if merged else (p.stderr or ""))


def norm(text, tmp):
    text = text.replace(str(tmp), "<T>").replace(str(tmp.resolve()), "<T>").replace(f":{PORT}", ":<PORT>")
    return re.sub(r"/private<T>", "<T>", text)


def files(env):
    """The crew home's files, for comparing what a command leaves behind."""
    crew = Path(env["FACTORY_CREW_HOME"])
    out = {}
    for p in sorted(crew.rglob("*")):
        if p.is_file():
            out[p.relative_to(crew).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return out


def _claude_calls(env):
    f = Path(env["FAKE_CLAUDE_STATE"]) / "calls.jsonl"
    return [json.loads(x)["args"] for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def both(label, argv, setup, merged=False, extra_env=None, want=None, expect_rc=None):
    """The same command in two identical worlds; the engines must agree. `want` is text that must be in the
    output, `expect_rc` the exit code both must give."""
    eb, tb = setup()
    ep, tp = setup()
    bash = run("bash", eb, argv, merged, extra_env)
    py = run("python", ep, argv, merged, extra_env)
    bn = (bash[0], norm(bash[1], tb), norm(bash[2], tb))
    pn = (py[0], norm(py[1], tp), norm(py[2], tp))
    ck(f"{label}: same exit code, stdout and stderr as bash", bn == pn,
       f"\n    bash:   {bn!r}\n    python: {pn!r}")
    fb = {k: norm(v, tb) for k, v in files(eb).items()}
    fp = {k: norm(v, tp) for k, v in files(ep).items()}
    ck(f"{label}: leaves the same files", fb == fp, f"\n    only bash: {sorted(set(fb) - set(fp))} only python: {sorted(set(fp) - set(fb))}"
       f" differ: {[k for k in fb if k in fp and fb[k] != fp[k]]}")
    cb, cp = _claude_calls(eb), _claude_calls(ep)
    ck(f"{label}: asks Claude Code the same things in the same order", cb == cp, f"\n    bash:   {cb}\n    python: {cp}")
    if want is not None:
        ck(f"{label}: says {want!r}", want in pn[1] + pn[2], pn)
    if expect_rc is not None:
        ck(f"{label}: exits {expect_rc}", pn[0] == expect_rc, pn[0])
    return pn


def crew_of(*, older=False, failed=False, title=None):
    """Four stations: an orchestrator, a reviewer, a builder and one that is gone."""
    st = {
        "crew-orchestrator": station("orchestrator", OID),
        "crew-reviewer": station("reviewer", RID),
        "shop-build-m1": station("builder", BID, project="shop"),
        "shop-build-m2": station("builder", SID, project="shop"),
        "old-station": station("helper", "eeee0005", status="stopped"),
    }
    if failed:
        st["shop-build-m1"]["cc_failed"] = "2.1.310"
    return st


def sessions(v_old="2.1.310", title="crew-orchestrator"):
    return [
        fake_claude.agent_row("working", OID, title, "/x", version="2.1.310"),
        fake_claude.agent_row("idle", RID, "crew-reviewer", "/x", version="2.1.310"),
        fake_claude.agent_row("waiting", BID, "shop-build-m1", "/x", version=v_old),
        # shop-build-m2 has no session: gone
    ]


# ---------- roster ----------


@case
def roster_empty_crew():
    both("roster, no stations", ["roster"], lambda: world({}, []), want="crew: crew (orchestrator: crew-orchestrator)",
         expect_rc=0)


@case
def roster_lists_active_stations():
    pn = both("roster, four stations", ["roster"], lambda: world(crew_of(), sessions()), expect_rc=0)
    lines = pn[1].splitlines()
    ck("roster: first line names the crew", lines[0] == "crew: crew (orchestrator: crew-orchestrator)", lines[:1])
    ck("roster: a working orchestrator", "crew-orchestrator\torchestrator\t\tworking\t2.1.310" in lines, lines)
    ck("roster: a station with no session is gone", "shop-build-m2\tbuilder\tshop\tgone\t" in lines, lines)
    ck("roster: a stopped station is not listed", not any(ln.startswith("old-station") for ln in lines), lines)


@case
def roster_older_claude_code():
    pn = both("roster, a station on an older Claude Code", ["roster"],
              lambda: world(crew_of(), sessions(v_old="2.1.100")), want="(older than 2.1.310)")
    both("roster, an older one that failed to move", ["roster"],
         lambda: world(crew_of(failed=True), sessions(v_old="2.1.100")),
         want="retrying in")


@case
def roster_wrong_title():
    pn = both("roster, orchestrator under another title", ["roster"],
              lambda: world(crew_of(), sessions(title="old-crew-orchestrator")),
              want="Orchestrator session title is old-crew-orchestrator, expected crew-orchestrator")
    # the warning goes to stderr, the list to stdout, and the order of the two in one pipe is bash's
    both("roster, title warning in one pipe", ["roster"], lambda: world(crew_of(), sessions(title="old-crew-orchestrator")),
         merged=True)


@case
def roster_named_crew_and_unreadable_sessions():
    both("roster, a named crew", ["roster"], lambda: world(crew_of(), sessions()), extra_env={"FACTORY_CREW_NAME": "ops"})
    both("roster, Claude Code cannot list sessions", ["roster"], lambda: world(crew_of(), sessions()),
         extra_env={"CLAUDE_BIN": "/usr/bin/false"}, expect_rc=1)
    both("roster, no stations file", ["roster"], lambda: world(None, []), expect_rc=0)


# ---------- status ----------


@case
def status_without_tares():
    both("status, no Tares answers", ["status"], lambda: _down(world(crew_of(), sessions())), want="no Tares answers at",
         expect_rc=1)
    both("crew status, no Tares answers", ["crew", "status"], lambda: _down(world(crew_of(), sessions())), expect_rc=1)


def _down(w):
    w[0]["TARESD_URL"] = DOWN
    return w


@case
def status_table():
    WORLD["desk"], WORLD["projects"] = [], []
    pn = both("status, a running crew", ["status"], lambda: world(crew_of(), sessions(v_old="2.1.100")), expect_rc=0)
    out = pn[1]
    ck("status: header", out.startswith("STATION"), out[:80])
    ck("status: a gone builder", re.search(r"shop-build-m2\s+builder\s+opus/high\s+gone", out) is not None, out)
    ck("status: claude code behind", "claude code: 1 station(s) run an older Claude Code than 2.1.310" in out, out)
    ck("status: watch not running", "watch: not running;" in out, out)
    ck("status: empty desk", "desk: nothing waits for you" in out, out)
    ck("status: ends with the crew page", out.rstrip().endswith("/crew"), out[-80:])


@case
def status_two_sessions_halted_frozen():
    two = sessions() + [fake_claude.agent_row("working", "ffff0006", "shop-build-m1", "/y", version="2.1.310")]
    pn = both("status, two sessions under one name", ["status"], lambda: world(crew_of(), two),
              want="TWO SESSIONS named shop-build-m1")
    both("status, halted", ["status"],
         lambda: world(crew_of(), sessions(), extra_files={"HALT": "maintenance\n"}), want="HALTED")
    both("status, no stations file", ["status"], lambda: world(None, []), expect_rc=0)
    both("status, no stations running", ["status"], lambda: world({}, []), want="no stations running")


@case
def status_desk_and_projects():
    WORLD["desk"] = [{"id": "d1", "label": "D1", "question": "ship it?", "options": ["yes", "no"],
                      "recommendation": "yes", "why": "tests pass", "blocking": True, "project_name": "shop"}]
    WORLD["projects"] = [{"id": "p1", "name": "shop", "tickets_total": 4, "tickets_done": 1,
                          "milestone": {"name": "M1", "done": 1, "total": 3}},
                         {"id": "p2", "name": "docs", "tickets_total": 2, "tickets_done": 2},
                         {"id": "p3", "name": "new", "tickets_total": 0, "tickets_done": 0}]
    pn = both("status, a question on the desk and three projects", ["status"], lambda: world(crew_of(), sessions()),
              want="desk: 1 waiting for you")
    ck("status: the question in full", "D1 (blocks work) · shop: ship it?" in pn[1] and "recommended: yes, since tests pass" in pn[1], pn[1])
    ck("status: a project on its milestone", "project shop: 1/4 tickets done, on M1 1/3" in pn[1], pn[1])
    ck("status: a finished project", "project docs: 2/2 tickets done, all done" in pn[1], pn[1])
    ck("status: a project without tickets", "project new: 0/0 tickets done, no tickets yet" in pn[1], pn[1])
    WORLD["desk"], WORLD["projects"] = [], []


# ---------- desk ----------


@case
def desk_cases():
    WORLD["desk"] = []
    both("desk, nothing waits", ["desk"], lambda: world(crew_of(), sessions()), want="Nothing waits for you on the desk.", expect_rc=0)
    WORLD["desk"] = [
        {"id": "d1", "label": "D1", "question": "ship it?", "options": ["yes", "no"], "recommendation": "yes",
         "why": "tests pass", "blocking": True, "project_name": "shop", "blocks": "T3", "assumptions": ["A1", "A2"]},
        {"id": "d2", "label": "D2", "question": "May shop-build-m1 run `git push`?", "recommendation": "allow",
         "station": "shop-build-m1", "command": "git push\norigin"},
    ]
    pn = both("desk, two questions", ["desk"], lambda: world(crew_of(), sessions()), expect_rc=0)
    ck("desk: options lettered", "  A) yes" in pn[1] and "  B) no" in pn[1], pn[1])
    ck("desk: blocks and confirms", "  blocks: T3" in pn[1] and "  confirms: A1, A2" in pn[1], pn[1])
    ck("desk: a refused command", "  shop-build-m1 was refused: git push origin" in pn[1], pn[1])
    both("crew desk", ["crew", "desk"], lambda: world(crew_of(), sessions()), expect_rc=0)
    both("desk, no Tares", ["desk"], lambda: _down(world(crew_of(), sessions())), expect_rc=1)
    WORLD["desk"] = []


# ---------- attach and logs ----------


@case
def attach_and_logs():
    for verb in ("attach", "logs"):
        # bash's `${1:?usage}` prints its own script path and line number, so only the exit code and the
        # usage text are compared here
        eb, _ = world(crew_of(), sessions())
        ep, _ = world(crew_of(), sessions())
        b, p = run("bash", eb, [verb]), run("python", ep, [verb])
        usage = f"usage: tares factory crew {verb} <role|station>"
        ck(f"{verb}, no station named: both exit 1 with the usage line", b[0] == p[0] == 1 and usage in b[2] and usage in p[2], (b, p))
        both(f"{verb}, an unknown station", [verb, "nobody"], lambda: world(crew_of(), sessions()), want="unknown station nobody", expect_rc=1)
        both(f"crew {verb}, an unknown station", ["crew", verb, "nobody"], lambda: world(crew_of(), sessions()),
             want="unknown station nobody", expect_rc=1)
        both(f"{verb}, no crew", [verb, "reviewer"], lambda: world(None, []), expect_rc=1)
    # a known station: claude is called with `attach|logs <id>`, as bash exec's it
    for verb in ("attach", "logs"):
        for word, sid in (("reviewer", RID), ("shop-build-m1", BID)):
            eb, _tb = world(crew_of(), sessions())
            ep, _tp = world(crew_of(), sessions())
            run("bash", eb, [verb, word])
            run("python", ep, [verb, word])

            def calls(env):
                f = Path(env["FAKE_CLAUDE_STATE"]) / "calls.jsonl"
                return [json.loads(x)["args"] for x in f.read_text().splitlines() if x.strip()] if f.exists() else []

            cb = [c for c in calls(eb) if c[:1] == [verb]]
            cp = [c for c in calls(ep) if c[:1] == [verb]]
            ck(f"{verb} {word}: claude is called with {verb} {sid}", cp == [[verb, sid]] and cb == cp, (cb, cp))


# ---------- the dispatcher keeps what is not ported ----------


@case
def settings_stays_in_bash():
    eb, _ = world(crew_of(), sessions())
    ep, _ = world(crew_of(), sessions())
    b = run("bash", eb, ["settings"])
    p = run("auto", ep, ["settings"])
    ck("settings: through the dispatcher it still runs in bash (TR-953 ports it)", b[0] == p[0] == 1 and "usage: tares factory settings" in p[2], (b, p))
    pe = run("python", ep, ["settings"])
    ck("settings: forced to Python it says it is not ported", pe[0] == 2 and "not yet ported" in pe[2], pe)


def main(names):
    todo = [fn for fn in CASES if not names or fn.__name__ in names]
    for fn in todo:
        print(f"\n{fn.__name__}")
        fn()
    shutil.rmtree(TOP, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    return 1 if F or not todo else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
