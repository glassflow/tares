"""tares.crew.views: status, desk, roster, attach and logs (TR-957).

Every case builds a throwaway crew (tests/_crew_fake.py: stations.json, the sessions the fake Claude Code
lists, a stub Tares on a local port), runs the command through `python -m tares.crew`, and checks the exit
code, the text it prints (stdout and stderr, and the two together as one pipe where the order of the lines
counts) and that a command that only reads leaves the crew's files as they were. (Until TR-975 each case also
ran the bash `factory` and compared the two; the bash crew is gone, so the expectations are the literals below.)

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


def run(env, argv, merged=False, extra_env=None):
    e = dict(env, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(extra_env or {}))
    e.pop("PYTHONSTARTUP", None)
    p = subprocess.run([sys.executable, "-m", "tares.crew"] + argv, env=e, cwd=str(ROOT), capture_output=not merged,
                       text=True, encoding="utf-8", errors="replace", timeout=120,
                       stdout=subprocess.PIPE if merged else None, stderr=subprocess.STDOUT if merged else None)
    return p.returncode, (p.stdout or ""), ("" if merged else (p.stderr or ""))


def norm(text, tmp):
    text = text.replace(str(tmp), "<T>").replace(str(tmp.resolve()), "<T>").replace(f":{PORT}", ":<PORT>")
    return re.sub(r"/private<T>", "<T>", text)


def files(env):
    """The crew home's files, for comparing what a command leaves behind (a .json file by what it holds: a command
    that reads crew.json may write it back with other spacing)."""
    crew = Path(env["FACTORY_CREW_HOME"])
    out = {}
    for p in sorted(crew.rglob("*")):
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            if p.suffix == ".json":
                try:
                    text = json.loads(text)
                except ValueError:
                    pass
            out[p.relative_to(crew).as_posix()] = text
    return out


def _claude_calls(env):
    f = Path(env["FAKE_CLAUDE_STATE"]) / "calls.jsonl"
    return [json.loads(x)["args"] for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def check(label, argv, setup, merged=False, extra_env=None, want=None, expect_rc=None, reads_only=True):
    """Run the command in a new world. `want` is text that must be in the output, `expect_rc` the exit code, and
    `reads_only` that the command leaves the crew home's files as they were."""
    env, tmp = setup()
    before = files(env)
    out = run(env, argv, merged, extra_env)
    pn = (out[0], norm(out[1], tmp), norm(out[2], tmp))
    if reads_only:
        after = files(env)
        ck(f"{label}: leaves the crew home's files as they were", before == after,
           f"only before: {sorted(set(before) - set(after))} only after: {sorted(set(after) - set(before))}"
           f" differ: {[k for k in before if k in after and before[k] != after[k]]}")
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
    check("roster, no stations", ["roster"], lambda: world({}, []), want="crew: crew (orchestrator: crew-orchestrator)",
         expect_rc=0)


@case
def roster_lists_active_stations():
    pn = check("roster, four stations", ["roster"], lambda: world(crew_of(), sessions()), expect_rc=0)
    lines = pn[1].splitlines()
    ck("roster: first line names the crew", lines[0] == "crew: crew (orchestrator: crew-orchestrator)", lines[:1])
    ck("roster: a working orchestrator", "crew-orchestrator\torchestrator\t\tworking\t2.1.310" in lines, lines)
    ck("roster: a station with no session is gone", "shop-build-m2\tbuilder\tshop\tgone\t" in lines, lines)
    ck("roster: a stopped station is not listed", not any(ln.startswith("old-station") for ln in lines), lines)


@case
def roster_older_claude_code():
    pn = check("roster, a station on an older Claude Code", ["roster"],
              lambda: world(crew_of(), sessions(v_old="2.1.100")), want="(older than 2.1.310)")
    check("roster, an older one that failed to move", ["roster"],
         lambda: world(crew_of(failed=True), sessions(v_old="2.1.100")),
         want="retrying in")


@case
def roster_wrong_title():
    pn = check("roster, orchestrator under another title", ["roster"],
              lambda: world(crew_of(), sessions(title="old-crew-orchestrator")),
              want="Orchestrator session title is old-crew-orchestrator, expected crew-orchestrator")
    # the warning goes to stderr, the list to stdout; in one pipe the warning comes after the header, before the stations
    pn = check("roster, title warning in one pipe", ["roster"], lambda: world(crew_of(), sessions(title="old-crew-orchestrator")),
               merged=True, expect_rc=0)
    lines = pn[1].splitlines()
    ck("roster: header, then the warning, then the stations", len(lines) >= 3 and lines[0].startswith("crew: crew")
       and lines[1].startswith("tares factory: Orchestrator session title is") and lines[2].startswith("crew-orchestrator\t"), lines[:3])


@case
def roster_named_crew_and_unreadable_sessions():
    check("roster, a named crew", ["roster"], lambda: world(crew_of(), sessions()), extra_env={"FACTORY_CREW_NAME": "ops"},
          want="crew: ops (orchestrator: ops-orchestrator)", expect_rc=0)
    check("roster, Claude Code cannot list sessions", ["roster"], lambda: world(crew_of(), sessions()),
          extra_env={"CLAUDE_BIN": "/usr/bin/false"}, want="could not read Claude Code's session list", expect_rc=1)
    check("roster, no stations file", ["roster"], lambda: world(None, []), want="crew: crew (orchestrator: crew-orchestrator)",
          expect_rc=0)


# ---------- status ----------


@case
def status_without_tares():
    check("status, no Tares answers", ["status"], lambda: _down(world(crew_of(), sessions())), want="no Tares answers at",
         expect_rc=1)
    check("crew status, no Tares answers", ["crew", "status"], lambda: _down(world(crew_of(), sessions())),
          want="no Tares answers at", expect_rc=1)


def _down(w):
    w[0]["TARESD_URL"] = DOWN
    return w


@case
def status_table():
    WORLD["desk"], WORLD["projects"] = [], []
    pn = check("status, a running crew", ["status"], lambda: world(crew_of(), sessions(v_old="2.1.100")), expect_rc=0)
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
    pn = check("status, two sessions under one name", ["status"], lambda: world(crew_of(), two),
              want="TWO SESSIONS named shop-build-m1")
    ck("status: the hint names the command people type", "(tares factory roster)" in pn[1] + pn[2], pn)
    check("status, halted", ["status"],
         lambda: world(crew_of(), sessions(), extra_files={"HALT": "maintenance\n"}), want="HALTED")
    check("status, no stations file", ["status"], lambda: world(None, []),
          want="no stations running; tares factory crew up starts them", expect_rc=0)
    check("status, no stations running", ["status"], lambda: world({}, []), want="no stations running")


@case
def status_desk_and_projects():
    WORLD["desk"] = [{"id": "d1", "label": "D1", "question": "ship it?", "options": ["yes", "no"],
                      "recommendation": "yes", "why": "tests pass", "blocking": True, "project_name": "shop"}]
    WORLD["projects"] = [{"id": "p1", "name": "shop", "tickets_total": 4, "tickets_done": 1,
                          "milestone": {"name": "M1", "done": 1, "total": 3}},
                         {"id": "p2", "name": "docs", "tickets_total": 2, "tickets_done": 2},
                         {"id": "p3", "name": "new", "tickets_total": 0, "tickets_done": 0}]
    pn = check("status, a question on the desk and three projects", ["status"], lambda: world(crew_of(), sessions()),
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
    check("desk, nothing waits", ["desk"], lambda: world(crew_of(), sessions()), want="Nothing waits for you on the desk.", expect_rc=0)
    WORLD["desk"] = [
        {"id": "d1", "label": "D1", "question": "ship it?", "options": ["yes", "no"], "recommendation": "yes",
         "why": "tests pass", "blocking": True, "project_name": "shop", "blocks": "T3", "assumptions": ["A1", "A2"]},
        {"id": "d2", "label": "D2", "question": "May shop-build-m1 run `git push`?", "recommendation": "allow",
         "station": "shop-build-m1", "command": "git push\norigin"},
    ]
    pn = check("desk, two questions", ["desk"], lambda: world(crew_of(), sessions()), expect_rc=0)
    ck("desk: options lettered", "  A) yes" in pn[1] and "  B) no" in pn[1], pn[1])
    ck("desk: blocks and confirms", "  blocks: T3" in pn[1] and "  confirms: A1, A2" in pn[1], pn[1])
    ck("desk: a refused command", "  shop-build-m1 was refused: git push origin" in pn[1], pn[1])
    check("crew desk", ["crew", "desk"], lambda: world(crew_of(), sessions()), want="D1", expect_rc=0)
    check("desk, no Tares", ["desk"], lambda: _down(world(crew_of(), sessions())), want="no Tares answers at", expect_rc=1)
    WORLD["desk"] = []


# ---------- attach and logs ----------


@case
def attach_and_logs():
    for verb in ("attach", "logs"):
        env, _ = world(crew_of(), sessions())
        code, out, err = run(env, [verb])
        usage = f"usage: tares factory crew {verb} <role|station>"
        ck(f"{verb}, no station named: exits 1 with the usage line", code == 1 and usage in err, (code, out, err))
        check(f"{verb}, an unknown station", [verb, "nobody"], lambda: world(crew_of(), sessions()),
              want="unknown station nobody", expect_rc=1)
        check(f"crew {verb}, an unknown station", ["crew", verb, "nobody"], lambda: world(crew_of(), sessions()),
              want="unknown station nobody", expect_rc=1)
        check(f"{verb}, no crew", [verb, "reviewer"], lambda: world(None, []), expect_rc=1)
    # a known station: claude is called with `attach|logs <id>`
    for verb in ("attach", "logs"):
        for word, sid in (("reviewer", RID), ("shop-build-m1", BID)):
            env, _ = world(crew_of(), sessions())
            run(env, [verb, word])
            made = [c for c in _claude_calls(env) if c[:1] == [verb]]
            ck(f"{verb} {word}: claude is called with {verb} {sid}", made == [[verb, sid]], made)


# ---------- a word the dispatcher does not know ----------


@case
def unknown_word_prints_usage():
    env, _ = world(crew_of(), sessions())
    code, out, err = run(env, ["unported-probe"])
    ck("an unknown command: exits 1 and prints the usage", code == 1 and out.startswith("tares factory: one crew"),
       (code, out[:120], err))
    ck("...which names the commands as `tares factory ...`", "  tares factory roster" in out, out[:200])


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
