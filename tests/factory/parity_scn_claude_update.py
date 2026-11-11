"""Parity scenarios for tests/factory/claude_update_test.sh (TR-1172): every `check "<label>"` of that suite that
parity.py itself does not cover. They run on the bash engine and on the Python engine and must print and write the
same. The suite is one long chain on one crew; here it is cut into groups, one scenario each, so they run side by side.

The crew of a group is written straight into the throwaway folder (`_cc_setup` of parity.py: three idle stations
whose sessions show the Claude Code they run, and `claude --version` the installed one). The fake Tares is a route
table (`tares=`) whose answers a step can change (`run_before`), or the World of fake_tares.py where `status` and
`doctor` are asked too. A station "going idle in four seconds" or "the plan filling in the middle" is a timer that
edits the fake's files while the command waits (`_later`).
"""
from parity import *  # noqa
import parity as P

fake_tares = P.fake_tares
_R, _L, _O = P._R, P._L, P._O
_cc, _seen, _runs, _stn, _bg_of, _moved = P._cc, P._seen, P._runs, P._stn, P._bg_of, P._moved
_cc_setup, _cc_tares, _all_idle, _cc_busy = P._cc_setup, P._cc_tares, P._all_idle, P._cc_busy
_cc_attach, _cc_detach, _cc_reap, _cc_marks = P._cc_attach, P._cc_detach, P._cc_reap, P._cc_marks
_launched, _events = P._launched, P._events
_WIN = P._WIN_NO_EXEC_A

# What a `crew update` needs from the environment: a poll of a second and a short wait for a stop.
UPD = {"FACTORY_UPDATE_POLL": "1", "FACTORY_STOP_WAIT": "2"}
CALLER = {**UPD, "FACTORY_STATION": "crew-orchestrator"}
NOW = {**UPD, "FACTORY_NOW": "1700000000"}   # a clock that stands still, for the times a station's wake file keeps
CU_RETRY = P.CU_RETRY
HALTED = '{"at":1700000000,"reason":"testing"}\n'


# --- helpers -----------------------------------------------------------------------------------------------------

def _install(version: str, *more_idle: bool):
    """Before a step: this Claude Code is installed, and every session is between turns."""
    return P._cc_installed(version)


def _later(seconds: float, fn):
    """A `before` hook part: run fn(tmp) after this many seconds, while the step waits (errors are not the step's)."""
    import threading

    def start(tmp: Path) -> None:
        def go() -> None:
            try:
                fn(tmp)
            except Exception:  # the folder may be gone when a run ended early
                pass
        t = threading.Timer(seconds, go)
        t.daemon = True
        t.start()
    return start


def _after_launches(n: int, delay: float, fn):
    """A `before` hook part: once the command has started n sessions (a slow machine starts it late), wait `delay`
    seconds and run fn(tmp): a station going idle, or a halt, while crew update waits."""
    import threading
    import time

    def count(tmp: Path) -> int:
        calls = tmp / "claude" / "calls.jsonl"
        return sum(1 for x in calls.read_text(encoding="utf-8").splitlines() if '"--bg"' in x) if calls.exists() else 0

    def start(tmp: Path) -> None:
        base = count(tmp)

        def go() -> None:
            try:
                for _ in range(600):
                    if count(tmp) >= base + n:
                        break
                    time.sleep(0.2)
                time.sleep(delay)
                fn(tmp)
            except Exception:  # the folder may be gone when a run ended early
                pass
        t = threading.Thread(target=go, daemon=True)
        t.start()
    return start


def _both(*fns):
    def before(tmp: Path) -> None:
        for f in fns:
            f(tmp)
    return before


def _rows(tmp: Path) -> list:
    return json.loads((tmp / "claude" / "agents.json").read_text(encoding="utf-8"))


def _put_rows(tmp: Path, rows: list) -> None:
    p = tmp / "claude" / "agents.json"
    t = p.with_name("agents.json.tmp")
    t.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    os.replace(t, p)


def _status_of(name: str, status: str):
    def run(tmp: Path) -> None:
        sid = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))[name]["id"]
        rows = _rows(tmp)
        for row in rows:
            if row.get("id") == sid:
                row["state"], row["status"] = "working", status
        _put_rows(tmp, rows)
    return run


def _versions(version: str):
    """Before a step: every session shows this Claude Code."""
    def run(tmp: Path) -> None:
        rows = _rows(tmp)
        for row in rows:
            row["version"] = version
        _put_rows(tmp, rows)
    return run


def _file(name: str, text: str):
    def run(tmp: Path) -> None:
        (tmp / "claude" / name).write_text(text, encoding="utf-8")
    return run


def _halt(tmp: Path) -> None:
    (tmp / "crew" / "HALT").write_text(HALTED, encoding="utf-8")


def _unhalt(tmp: Path) -> None:
    (tmp / "crew" / "HALT").unlink(missing_ok=True)


def _routes(**docs):
    """run_before: change what the fake Tares answers (route -> (status, document))."""
    def run(r: Run) -> None:
        r.tares.routes.update(docs)
    return run


def _settings_doc(**settings):
    return (200, {"settings": dict(_cc_tares()["GET /api/crew/settings"][1]["settings"], **settings)})


def _settings(**settings):
    return _routes(**{"GET /api/crew/settings": _settings_doc(**settings)})


def _state(tmp: Path) -> dict:
    """An `after`: the Claude Code each station's session shows, and the session ids (compared and checked)."""
    ids = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))
    rows = _rows(tmp)
    out = {"runs": {}, "ids": {}, **P._cc_after()(tmp)}
    for name, v in ids.items():
        out["ids"][name] = v.get("id")
        out["runs"][name] = next((x.get("version") for x in rows if x.get("id") == v.get("id")), None)
    return out


def _watch(before=None, env=None, after=_state) -> Step:
    """One `crew watch --once`."""
    return P._watch_once(before=before, env=env, after=after)


def _bg(r: Run, i: int) -> list:
    """The sessions started up to the end of step i, in order."""
    return r.snap(i)["launched"]


def _new(r: Run, i: int) -> list:
    """The sessions started by step i alone."""
    return _bg(r, i)[len(_bg(r, i - 1)) if i else 0:]


def _every_runs(r: Run, version: str, names=(_O, _R, _L)) -> bool:
    return all(_runs(r, n) == version for n in names)


def _prompt(r: Run, name: str) -> str:
    return _bg_of(r, name)[-1][-1]


def _ids(setup, names: dict):
    def run(tmp: Path, env: dict) -> None:
        setup(tmp, env)
        for rel in ("crew/stations.json", "claude/agents.json"):
            f = tmp / rel
            t = f.read_text(encoding="utf-8")
            for old, new in names.items():
                t = t.replace(old, new)
            f.write_text(t, encoding="utf-8")
    return run


def _setup(**kw):
    """`_cc_setup` with ids of this file's own. An attached station is a process named `claude attach <id>` on the
    machine's process table, and the scenarios of parity.py, run beside these, attach the ids bbbb0002 and so on."""
    return _ids(_cc_setup(**kw), CUIDS)


CUIDS = {"aaaa0001": "aaaacu01", "bbbb0002": "bbbbcu02", "cccc0003": "cccccu03"}


def _worlds_setup(world: "fake_tares.World", **kw):
    """A World (status and doctor are asked) and the crew of `_cc_setup`."""
    def setup(tmp: Path, env: dict) -> None:
        P._WORLDS[str(tmp)] = world
        P._doctor_world(world)(tmp, env)
        _setup(**kw)(tmp, env)
    return setup


def _line(text: str, start: str) -> str:
    return next((x for x in text.splitlines() if x.startswith(start)), "")


def _lines(text: str, start: str) -> list:
    return [x for x in text.splitlines() if x.startswith(start)]


# --- crew up ---------------------------------------------------------------------------------------------------

scenario(Scenario(
    name="claude_update_up",
    suite="claude_update",
    about="crew up on the answers of the suite (L3-review, releaser, kind, no challenger, three builders) starts three stations",
    stand_in={},
    prepare=P.trust_crew,
    steps=[Step(["crew", "up"], stdin="L3-review\nyes\n\nno\n3\n\n")],
    checks=[("crew up starts three stations", lambda r: r.code(0) == 0 and r.launches() == 3)],
    covers=["crew up starts three stations"],
))


# --- the views: roster, doctor and status say what the stations run -----------------------------------------------

DROP = P.DOCTOR_DROP

scenario(Scenario(
    name="claude_update_views",
    suite="claude_update",
    about="roster, doctor and status show the Claude Code each station runs, flag an older one with how to update, and drop the flag",
    setup=_worlds_setup(fake_tares.World(), have="1.0.0", installed="1.0.0"),
    steps=[Step(["roster"]), Step(["doctor"], drop=DROP), Step(["crew", "status"]),                  # 0 1 2: all on the installed one
           Step(["roster"], before=_install("1.1.0")), Step(["crew", "status"]), Step(["doctor"], drop=DROP),   # 3 4 5: 1.1.0 installed
           Step(["roster"], before=_both(_install("1.1.0"), _versions("1.1.0")))],                  # 6: they all run it
    checks=[
        ("roster shows each station's Claude Code",
         lambda r: "1.0.0" in _line(r.out(0), "crew-reviewer") and "older than" not in r.out(0)),
        ("doctor names it", lambda r: "crew-reviewer runs Claude Code 1.0.0" in r.out(1)),
        ("status says nothing is behind", lambda r: not _lines(r.out(2), "claude code:")),
        ("roster flags an older station", lambda r: "1.0.0 (older than 1.1.0)" in _line(r.out(3), "crew-reviewer")),
        ("status counts them",
         lambda r: bool(_line(r.out(4), "claude code: 3 station(s) run an older Claude Code than 1.1.0"))),
        ("doctor warns, and says how to update",
         lambda r: any("crew-reviewer runs Claude Code 1.0.0, older than the 1.1.0 installed" in x and "crew update" in x
                       for x in r.out(5).splitlines())),
        ("roster has no flag any more", lambda r: "older than" not in r.out(6)),
    ],
    covers=["roster shows each station's Claude Code", "doctor names it", "status says nothing is behind",
            "roster flags an older station", "status counts them", "doctor warns, and says how to update",
            "roster has no flag any more"],
))


# --- crew update: arguments, the caller, a halt, a wait -----------------------------------------------------------

scenario(Scenario(
    name="claude_update_wait_args",
    suite="claude_update",
    about="--wait with a word, 1e3, a minus, nothing or twenty digits is refused before a station is touched",
    setup=_setup(installed="1.5.7"),
    tares=_cc_tares(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[Step(["crew", "update", "--wait", w], env=UPD) for w in ("abc", "1e3", "-5", "", "99999999999999999999")],
    checks=[("--wait '$w' is refused, nothing restarted",
             lambda r: all(r.code(i) != 0 and "takes a number of seconds" in r.out(i) for i in range(5)) and r.launches() == 0)],
    covers=["--wait '$w' is refused, nothing restarted"],
))

scenario(Scenario(
    name="claude_update_wait_num",
    suite="claude_update",
    about="--wait 00 is 0 (no deadline): the busy reviewer is waited for; --wait 08 is 8 seconds, not an octal error",
    setup=_setup(installed="1.5.7"),
    tares=_cc_tares(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[Step(["crew", "update", "--wait", "00"], env=UPD, after=_state,
                before=_both(_cc_busy(_R), _after_launches(1, 1.5, _status_of(_R, "idle")))),
           Step(["crew", "update", "--wait", "08"], env=UPD, after=_state,
                before=_both(P._cc_installed("1.5.7.1"), _cc_busy(_R), _after_launches(1, 1.5, _status_of(_R, "idle"))))],
    checks=[
        ("--wait 00 is 0: it waits for the busy reviewer, then every station moves",
         lambda r: r.code(0) == 0 and "waiting for crew-reviewer to be idle" in r.out(0) and "gave up waiting" not in r.out(0)
         and all(v == "1.5.7" for v in r.snap(0)["runs"].values())),
        ("--wait 08 is 8 seconds, not an octal error: the busy reviewer is waited for",
         lambda r: r.code(1) == 0 and "value too great" not in r.out(1) and "waiting for crew-reviewer to be idle" in r.out(1)
         and r.snap(1)["runs"][_R] == "1.5.7.1"),
    ],
    covers=["--wait 00 is 0: it waits for the busy reviewer, then every station moves",
            "--wait 08 is 8 seconds, not an octal error: the busy reviewer is waited for"],
))


scenario(Scenario(
    name="claude_update_caller",
    suite="claude_update",
    about="crew update run from the orchestrator: a current caller is not moved or promised a move; --force says to restart it elsewhere; "
          "another station failing still says the caller's way out",
    setup=_setup(have="1.5.7", installed="1.5.7"),
    tares=_cc_tares(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[Step(["crew", "update"], env=CALLER, after=_state, before=_all_idle),
           Step(["crew", "update", "--force"], env=CALLER, after=_state, before=_all_idle),
           Step(["crew", "update", "--wait", "30"], env=CALLER, after=_state,
                before=_both(P._cc_installed("1.5.8"), _file("spawn-version", "1.1.0\n"), _file("stale-left", "all\n")))],
    checks=[
        ("the caller already runs the version: nothing restarted, nothing said about its move",
         lambda r: r.code(0) == 0 and not _new(r, 0) and "crew-orchestrator already runs Claude Code 1.5.7" in r.out(0)
         and "which ran this command" not in r.out(0)),
        ("a caller that is not behind: it is told to restart itself from another session, not that the watch moves it",
         lambda r: r.code(1) == 0 and bool(re.search(
             r"crew-orchestrator, which ran this command, can only be restarted from another session: run .* restart crew-orchestrator there",
             r.out(1))) and "moves when" not in r.out(1)),
        ("another station fails: the caller's way out is still said",
         lambda r: r.code(2) not in (0, 124) and "crew-orchestrator, which ran this command, moves when" in r.out(2)
         and "not every station runs Claude Code 1.5.8" in r.out(2)),
    ],
    covers=["the caller already runs the version: nothing restarted, nothing said about its move",
            "a caller that is not behind: it is told to restart itself from another session, not that the watch moves it",
            "another station fails: the caller's way out is still said"],
))

scenario(Scenario(
    name="claude_update_self",
    suite="claude_update",
    about="crew update run from a busy orchestrator finishes without waiting on itself and says how the orchestrator moves",
    setup=_setup(have="1.5.0", installed="1.5.5"),
    tares=_cc_tares(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[Step(["crew", "update"], env=CALLER, after=_state, before=_cc_busy(_O))],
    checks=[
        ("from the orchestrator: it finishes, the others move, the orchestrator is left",
         lambda r: r.code(0) == 0 and sorted(_new(r, 0)) == sorted([_R, _L]) and r.snap(0)["ids"][_O] == CUIDS["aaaa0001"]
         and r.snap(0)["runs"][_R] == "1.5.5" and r.snap(0)["runs"][_L] == "1.5.5"),
        ("it says how the orchestrator moves", lambda r: "crew-orchestrator, which ran this command, moves when" in r.out(0)),
    ],
    covers=["from the orchestrator: it finishes, the others move, the orchestrator is left",
            "it says how the orchestrator moves"],
))

scenario(Scenario(
    name="claude_update_halt_mid",
    suite="claude_update",
    about="a halt in the middle of crew update --wait 0 stops it and says so",
    setup=_setup(have="1.5.0", installed="1.5.6"),
    tares=_cc_tares(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[Step(["crew", "update", "--wait", "0"], env=UPD, after=_state,
                before=_both(_cc_busy(_R), _after_launches(1, 1.5, _halt)))],
    checks=[("halted in the middle: crew update stops and says so",
             lambda r: r.code(0) not in (0, 124) and "the crew was halted" in r.out(0))],
    covers=["halted in the middle: crew update stops and says so"],
))

scenario(Scenario(
    name="claude_update_wait_restart",
    suite="claude_update",
    about="crew update waits for a busy station and then moves every station, the orchestrator last; crew restart restarts all three anyway",
    setup=_setup(have="1.2.0", installed="1.3.0"),
    tares=_cc_tares(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[Step(["crew", "update", "--wait", "30"], env=UPD, after=_state, before=_both(_cc_busy(_R), _after_launches(1, 1.5, _status_of(_R, "idle")))),
           Step(["crew", "restart"], env=UPD, after=_state, before=_all_idle)],
    checks=[
        ("crew update waits for a busy station and then does it",
         lambda r: r.code(0) == 0 and "waiting for crew-reviewer to be idle" in r.out(0) and r.snap(0)["runs"][_R] == "1.3.0"),
        ("orchestrator last", lambda r: _new(r, 0)[-1] == _O),
        ("crew restart restarts every station anyway, the orchestrator last",
         lambda r: r.code(1) == 0 and len(_new(r, 1)) == 3 and _new(r, 1)[-1] == _O),
    ],
    covers=["crew update waits for a busy station and then does it", "orchestrator last",
            "crew restart restarts every station anyway, the orchestrator last"],
))

def _own_reviewer(setup):
    """The setup with the reviewer under an id of its own (see _setup)."""
    return _ids(setup, {"bbbbcu02": "bbbbcu0a"})


scenario(Scenario(
    name="claude_update_attached",
    suite="claude_update",
    about="an attached station is not restarted: crew update gives up at --wait, says who it waited for, and moves the others",
    setup=_own_reviewer(_setup(have="1.3.0", installed="1.4.0")),
    tares=_cc_tares(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    not_windows=_WIN,
    steps=[Step(["crew", "update", "--wait", "6"], env=UPD, before=_cc_attach(_R),
                after=lambda tmp: {**_state(tmp), "alive": _cc_reap(tmp)})],
    checks=[
        ("an attached station is not restarted; crew update says who it waited for",
         lambda r: r.code(0) != 0 and r.snap(0)["ids"][_R] == "bbbbcu0a" and "gave up waiting for crew-reviewer" in r.out(0)
         and "someone has it open" in r.out(0)),
        ("the others were done", lambda r: r.snap(0)["runs"][_L] == "1.4.0" and r.snap(0)["runs"][_O] == "1.4.0"),
    ],
    covers=["an attached station is not restarted; crew update says who it waited for", "the others were done"],
))

scenario(Scenario(
    name="claude_update_dup",
    suite="claude_update",
    about="two sessions under one station name: the watch reports it after a move too",
    setup=_setup(have="1.4.0", installed="1.5.0"),
    tares=_cc_tares(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[_watch(before=_both(P._cc_installed("1.5.0"), P._cu_add_dup))],
    checks=[("the watch reports it after a move too",
             lambda r: "TWO SESSIONS named crew-reviewer" in r.out(0) or "two-sessions" in _events(r))],
    covers=["the watch reports it after a move too"],
))

REARM_OFF = {"idle_wake": False}


def _rearm_ok(r: Run, i: int, on: bool) -> bool:
    for n in (_O, _R, _L):
        sent = [c["args"][-1] for c in bg_calls(r) if c["args"][2] == n]
        text = sent[i] if len(sent) > i else ""
        has_re = "re-arm your schedule if you have one" in text
        if on and (has_re or "arm no tick of your own" not in text):
            return False
        if not on and not has_re:
            return False
    return True


scenario(Scenario(
    name="claude_update_rearm",
    suite="claude_update",
    about="what a Claude Code restart says about the schedule follows idle_wake: off, re-arm; on, arm no tick",
    setup=_setup(have="1.5.0", installed="1.5.9"),
    tares=_cc_tares(idle_wake=False),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[Step(["crew", "update", "--wait", "30"], env=NOW, after=_state, before=_all_idle),
           Step(["crew", "update", "--wait", "30"], env=NOW, after=_state, before=P._cc_installed("1.6.0"),
                run_before=_settings(idle_wake=True))],
    checks=[
        ("idle_wake off: the Claude Code restart says to re-arm the schedule", lambda r: _rearm_ok(r, 0, False)),
        ("idle_wake on: the Claude Code restart says to arm no tick", lambda r: _rearm_ok(r, 1, True)),
    ],
    covers=["idle_wake off: the Claude Code restart says to re-arm the schedule",
            "idle_wake on: the Claude Code restart says to arm no tick"],
))


# --- the retry chain: a station whose restarts keep landing on an old spare ----------------------------------------
# The World is built in the setup, one for each engine: it keeps the list the watch PUTs to /api/crew/claude-retry.

def _crew_world(**settings):
    def make() -> "fake_tares.World":
        w = fake_tares.World(settings={"releaser": True, "release_profile": "kind", "builders_max": 3, **settings})
        w.routes["/api/grants"] = {"all": ""}
        return w
    return make


def _world_setup(make, **kw):
    """A World made for this run (status and doctor ask it; the retry list goes to it) and the crew of `_setup`."""
    def setup(tmp: Path, env: dict) -> None:
        w = make()
        P._WORLDS[str(tmp)] = w
        P._doctor_world(w)(tmp, env)
        _setup(**kw)(tmp, env)
    return setup


def _wait_bucket(at):
    """The minutes a retry time is away, for the two waits the suite checks (10 and 20), else 'other'."""
    import time
    if not at:
        return None
    d = int(at) - time.time()
    return 10 if 540 < d <= 600 else 20 if 1140 < d <= 1200 else "other"


def _rstate(tmp: Path) -> dict:
    """An `after`: _state, the marks of every station (failed version, failed moves, new conversation done, minutes to
    the retry) and the list Tares was last given."""
    out = _state(tmp)
    rows = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))
    out["marks"] = {n: [v.get("cc_failed"), v.get("cc_fails"), v.get("cc_fresh"), _wait_bucket(v.get("cc_retry_at"))]
                    for n, v in rows.items()}
    out["retry"] = {k: [v.get("target"), v.get("fails")] for k, v in sorted(P._WORLDS[str(tmp)].retry.items())}
    return out


def _retry_due(tmp: Path) -> None:
    """The backoff of the marked station that is not the orchestrator is over."""
    p = tmp / "crew" / "stations.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    for n, v in rows.items():
        if n != _O and v.get("cc_failed"):
            v["cc_retry_at"] = 1
    t = p.with_name("stations.json.tmp")
    t.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    os.replace(t, p)


def _unmarked(r: Run, i: int, name: str) -> bool:
    return r.snap(i)["marks"][name] == [None, None, None, None]


def _resumed_of(r: Run, name: str) -> list:
    return ["--resume" in c["args"] for c in bg_calls(r) if c["args"][2] == name]


NEW_SPARES = {"spawn-version": "1.1.0\n", "stale-left": "3\n"}

scenario(Scenario(
    name="claude_update_retry",
    suite="claude_update",
    about="restarts that land on old spares: marked, shown by roster, status and doctor, listed on Tares, the station backing off "
          "is skipped and does not hold the orchestrator back, retried after the backoff with the wait doubled, cleared on success",
    setup=_world_setup(_crew_world(), have="1.2.0", installed="1.2.1", files=NEW_SPARES),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[
        _watch(after=_rstate),                                                                           # 0 three old spares, then it gets there
        _watch(before=_both(_all_idle, _file("stale-left", "all\n")), after=_rstate),                     # 1 old spares every time
        Step(["roster"], after=_rstate), Step(["crew", "status"], after=_rstate),                         # 2 3
        Step(["doctor"], drop=DROP, after=_rstate),                                                      # 4
        _watch(before=_all_idle, after=_rstate),                                                          # 5 the orchestrator, y backs off
        _watch(before=_all_idle, after=_rstate),                                                          # 6 everyone backs off
        _watch(before=_both(_retry_due, _all_idle), after=_rstate),                                       # 7 y's backoff is over
        _watch(before=_both(_retry_due, _file("stale-left", "0\n"), _all_idle), after=_rstate),           # 8 no spare in the way
        Step(["crew", "update", "--wait", "5"], env=UPD, after=_rstate,
             before=_both(_file("stale-left", "all\n"), _all_idle)),                                      # 9 the orchestrator, at once
        Step(["crew", "update", "--wait", "5"], env=UPD, after=_rstate,
             before=_both(_file("stale-left", "0\n"), _all_idle)),                                        # 10 the spares are gone
    ],
    checks=[
        ("roster flags it as retrying",
         lambda r: "1.1.0 (older than 1.2.1, retrying in 10 min)" in _line(r.out(2), _new(r, 1)[-1])),
        ("status counts it as retrying",
         lambda r: bool(re.search(r"^claude code: 2 station\(s\) run an older Claude Code than 1\.2\.1.*1 retrying after coming back on an old spare",
                                  r.out(3), re.M))),
        ("doctor says it keeps coming back and when the watch retries",
         lambda r: any(f"{_new(r, 1)[-1]} runs Claude Code 1.1.0" in x and "keeps coming back on an old spare; the watch retries in 10 min" in x
                       for x in r.out(4).splitlines())),
        ("Tares has it, for the Crew page", lambda r: r.snap(1)["retry"].get(_new(r, 1)[-1]) == ["1.2.1", 1]),
        ("the station backing off is not tried again, and does not hold the orchestrator back",
         lambda r: _new(r, 5) == [_O] * 4 and r.snap(5)["marks"][_O][0] == "1.2.1" and r.snap(5)["marks"][_new(r, 1)[-1]][1] == 1),
        ("everyone is backing off: nothing more is tried", lambda r: _new(r, 6) == []),
        ("Tares lists both", lambda r: sorted(r.snap(6)["retry"]) == sorted([_new(r, 1)[-1], _O])),
        ("after the backoff: three more tries, no second new conversation, the wait doubles",
         lambda r: _new(r, 7) == [_new(r, 1)[-1]] * 3 and _resumed_of(r, _new(r, 1)[-1])[-3:] == [True] * 3
         and r.snap(7)["marks"][_new(r, 1)[-1]][1] == 2 and r.snap(7)["marks"][_new(r, 1)[-1]][3] == 20),
        ("once a spare is not in the way it moves, and every mark is cleared",
         lambda r: len(_new(r, 8)) == 1 and r.snap(8)["runs"][_new(r, 1)[-1]] == "1.2.1" and _unmarked(r, 8, _new(r, 1)[-1])),
        ("crew update ignores the backoff, tries the orchestrator again and says the watch retries",
         lambda r: r.code(9) != 0 and "not every station runs Claude Code 1.2.1" in r.out(9) and "the watch tries again in" in r.out(9)
         and len(_new(r, 9)) >= 3 and r.snap(9)["marks"][_O][1] == 2),
        ("crew update, once the spares are gone: every station on 1.2.1",
         lambda r: r.code(10) == 0 and all(v == "1.2.1" for v in r.snap(10)["runs"].values())
         and "every station runs Claude Code 1.2.1, one session each" in r.out(10)),
        ("the failure marks are cleared, here and on Tares",
         lambda r: all(_unmarked(r, 10, n) for n in (_O, _R, _L)) and r.snap(10)["retry"] == {}),
    ],
    covers=["roster flags it as retrying", "status counts it as retrying",
            "doctor says it keeps coming back and when the watch retries", "Tares has it, for the Crew page",
            "the station backing off is not tried again, and does not hold the orchestrator back",
            "everyone is backing off: nothing more is tried", "Tares lists both",
            "after the backoff: three more tries, no second new conversation, the wait doubles",
            "once a spare is not in the way it moves, and every mark is cleared",
            "crew update, once the spares are gone: every station on 1.2.1"],
))


# --- the retries look again at what the first restart left -------------------------------------------------------------

def _on_bg(command: str):
    """Before a step: a command the fake Claude Code runs once, after the next launch (FAKE_ID is the new session)."""
    def before(tmp: Path) -> None:
        (tmp / "claude" / "on-bg").write_text(command.replace("{tmp}", str(tmp)) + "\n", encoding="utf-8")
    return before


def _kill_attach(tmp: Path) -> None:
    import signal
    try:
        os.kill(int((tmp / "attach.pid").read_text(encoding="utf-8").strip()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def _after_halt(tmp: Path) -> dict:
    _unhalt(tmp)
    return _rstate(tmp)


def _after_attach(tmp: Path) -> dict:
    import time
    time.sleep(1)
    _kill_attach(tmp)
    return _rstate(tmp)


scenario(Scenario(
    name="claude_update_retry_stop",
    suite="claude_update",
    about="a halt, or someone opening the station, after the first restart stops the retries: one launch, no failure mark; "
          "then crew update puts every station on the new version",
    setup=_world_setup(_crew_world(), have="1.2.0", installed="1.2.5", files={"spawn-version": "1.1.0\n", "stale-left": "all\n"}),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    not_windows=_WIN,
    steps=[
        _watch(before=_on_bg('printf \'{"at":1700000000,"reason":"retry-test"}\\n\' > "$FACTORY_CREW_HOME/HALT"'),
               after=_after_halt),                                           # 0 halted after the first try
        _watch(before=_both(_all_idle, _on_bg('bash -c "exec -a \'claude attach $FAKE_ID\' sleep 120" & echo $! > {tmp}/attach.pid')),
               after=_after_attach),        # 1 opened after the first try
        Step(["crew", "update", "--wait", "5"], env=UPD, after=_rstate,
             before=_both(_file("stale-left", "0\n"), _all_idle)),                                           # 2 the spares are gone
    ],
    checks=[
        ("halted after the first try: no second restart",
         lambda r: len(_new(r, 0)) == 1 and "came back on Claude Code 1.1.0" in r.out(0)
         and r.snap(0)["marks"][_R][0] is None),
        ("opened after the first try: no second restart",
         lambda r: len(_new(r, 1)) == 1 and "came back on Claude Code 1.1.0" in r.out(1)
         and r.snap(1)["marks"][_R][0] is None),
        ("the stations all run 1.2.5 after it", lambda r: all(v == "1.2.5" for v in r.snap(2)["runs"].values())),
    ],
    covers=["halted after the first try: no second restart", "opened after the first try: no second restart",
            "the stations all run 1.2.5 after it"],
))


# --- a spare claimed after the listing was read -------------------------------------------------------------------------

def _raced_setup(tmp: Path, env: dict) -> None:
    """The process listing and `sort -V` are stand-ins on PATH, as the .sh has them (and the Python engine reads the
    listing from TARES_FAKE_PS_DIR): a spare whose claim socket is removed when its version is sorted."""
    _world_setup(_crew_world(), have="1.2.5", installed="1.2.6")(tmp, env)
    b = tmp / "bin"
    b.mkdir(exist_ok=True)
    (b / "ps").symlink_to(P.HERE / "fake-ps")
    (b / "sort").write_text(
        '#!/usr/bin/env bash\n'
        'case " $* " in *" -V "*) ;; *) exec /usr/bin/sort "$@" ;; esac\n'
        'in=$(cat)\n'
        'if [ -f "$FAKE_CLAUDE_STATE/sort-hook" ] && grep -q \'0\\.9\\.9\' <<<"$in"; then hook=$(cat "$FAKE_CLAUDE_STATE/sort-hook"); '
        'rm -f "$FAKE_CLAUDE_STATE/sort-hook"; bash -c "$hook"; fi\n'
        'printf \'%s\\n\' "$in" | /usr/bin/sort "$@"\n', encoding="utf-8")
    (b / "sort").chmod(0o755)
    env["TARES_FAKE_PS_DIR"] = str(tmp / "claude")


def _raced_spare(tmp: Path) -> None:
    p = subprocess.Popen(["sleep", "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    P._CC_PROCS[(str(tmp), "raced")] = p
    (tmp / "raced.claim.sock").write_text("", encoding="utf-8")
    (tmp / "claude" / "ps-list").write_text(
        f"{p.pid} claude bg-pty-host --bg-pty-host {tmp}/raced.pty.sock 200 50 -- /x/versions/0.9.9 --bg-spare {tmp}/raced.claim.sock\n",
        encoding="utf-8")
    (tmp / "claude" / "sort-hook").write_text(f"rm -f {tmp}/raced.claim.sock\n", encoding="utf-8")


scenario(Scenario(
    name="claude_update_raced",
    suite="claude_update",
    about="a spare that is claimed after the process listing was read (its claim socket is gone) is not stopped",
    setup=_raced_setup,
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    not_windows=_WIN,
    steps=[Step(["crew", "update", "--wait", "5"], env=UPD, before=_both(_all_idle, _raced_spare),
                after=lambda tmp: {**_state(tmp), "alive": _cc_reap(tmp), "hook_left": (tmp / "claude" / "sort-hook").exists()})],
    checks=[("a spare claimed since the listing is left alone",
             lambda r: r.code(0) == 0 and r.snap(0)["alive"].get("raced") is True and r.snap(0)["hook_left"] is False)],
    covers=["a spare claimed since the listing is left alone"],
))


# --- a frozen crew: the watch hears of a newer Claude Code and moves nothing ----------------------------------------------

def _fstate(tmp: Path) -> dict:
    """An `after`: _state, the claude-newer events so far, and the note of the version already said."""
    out = _state(tmp)
    ev = tmp / "crew" / "events.jsonl"
    out["newer"] = sum(1 for x in ev.read_text(encoding="utf-8").splitlines() if '"claude-newer"' in x.replace(" ", "")) if ev.exists() else 0
    note = tmp / "crew" / "claude-newer"
    out["note"] = note.read_text(encoding="utf-8").strip() if note.exists() else None
    out["frozen"] = bool(P._frozen_of(tmp))
    return out


def _note(version: str):
    def run(tmp: Path) -> None:
        (tmp / "crew" / "claude-newer").write_text(version + "\n", encoding="utf-8")
    return run


def _frozen_world():
    return lambda tmp, env: _world_setup(_crew_world(), have="1.10.0", installed="1.10.1")(tmp, env)


scenario(Scenario(
    name="claude_update_frozen",
    suite="claude_update",
    about="a frozen crew: the watch moves no station onto a newer Claude Code and says so once for each version; status says why; "
          "crew update still moves every station; unfreezing lets the watch move one again",
    setup=_frozen_world(),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[
        Step(["crew", "freeze"], after=_fstate),                                                         # 0
        _watch(before=_all_idle, after=_fstate), _watch(before=_all_idle, after=_fstate),                # 1 2
        Step(["crew", "status"], after=_fstate),                                                         # 3
        Step(["crew", "update", "--wait", "5"], env=UPD, after=_fstate),                                 # 4
        _watch(before=_install("1.10.2"), after=_fstate),                                                # 5 the next version
        Step(["crew", "unfreeze"], after=_fstate),                                                       # 6
        _watch(before=_all_idle, after=_fstate),                                                         # 7
    ],
    checks=[
        ("frozen: the watch moves no station onto a newer Claude Code", lambda r: r.code(0) == 0 and _bg(r, 1) == []),
        ("frozen: it says there is a newer Claude Code",
         lambda r: bool(re.search(r"Claude Code 1\.10\.1 is newer than the one [0-9]* station\(s\) run; not moving stations while the kit is frozen",
                                  r.out(1)))),
        ("frozen: the same version is said once",
         lambda r: r.snap(2)["newer"] == 1 and "is newer than" not in r.out(2) and _bg(r, 2) == []),
        ("frozen: status says the freeze stops the watch moving them",
         lambda r: bool(re.search(r"^claude code: [0-9]* station\(s\) run an older Claude Code than 1\.10\.1; the freeze stops the watch moving them",
                                  r.out(3), re.M))),
        ("frozen: crew update still moves every station",
         lambda r: r.code(4) == 0 and all(v == "1.10.1" for v in r.snap(4)["runs"].values())),
        ("frozen: the next version is said once more",
         lambda r: r.snap(5)["newer"] == 2 and "Claude Code 1.10.2 is newer" in r.out(5)),
        ("unfrozen: the watch moves a station again", lambda r: r.code(6) == 0 and len(_new(r, 7)) == 1),
    ],
    covers=["frozen: the watch moves no station onto a newer Claude Code", "frozen: it says there is a newer Claude Code",
            "frozen: the same version is said once", "frozen: status says the freeze stops the watch moving them",
            "frozen: crew update still moves every station", "frozen: the next version is said once more",
            "unfrozen: the watch moves a station again"],
))

scenario(Scenario(
    name="claude_update_frozen_note",
    suite="claude_update",
    about="the note of the version already said goes when nobody is behind any more, and with a freeze or an unfreeze",
    setup=_world_setup(_crew_world(), have="1.10.3", installed="1.10.4"),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[
        Step(["crew", "freeze"], after=_fstate),                                                         # 0
        _watch(before=_all_idle, after=_fstate),                                                         # 1 the note: 1.10.4
        Step(["crew", "update", "--wait", "5"], env=UPD, after=_fstate),                                 # 2 nobody is behind
        _watch(before=_all_idle, after=_fstate),                                                         # 3 the note is gone
        Step(["crew", "unfreeze"], before=_note("9.9.9"), after=_fstate),                                # 4
        Step(["crew", "freeze"], before=_note("9.9.9"), after=_fstate),                                  # 5
    ],
    checks=[
        ("frozen: the version said is noted", lambda r: r.snap(1)["note"] == "1.10.4"),
        ("frozen, nobody behind: the note is cleared", lambda r: r.snap(3)["note"] is None and "is newer than" not in r.out(3)),
        ("unfreeze clears the note", lambda r: r.code(4) == 0 and r.snap(4)["note"] is None and r.snap(4)["frozen"] is False),
        ("freeze clears the note", lambda r: r.code(5) == 0 and r.snap(5)["note"] is None and r.snap(5)["frozen"] is True),
    ],
    covers=["frozen: the version said is noted", "frozen, nobody behind: the note is cleared",
            "unfreeze clears the note", "freeze clears the note"],
))

def _freeze_on_agents(tmp: Path) -> None:
    """The fake Claude Code freezes the crew (the setting `crew freeze` keeps, with the kit as it is) once the watch has
    read the session list."""
    doc = json.dumps({"kit": {"frozen": {"at": 1700000000, "by": "test", "version": P._cc_kit(tmp / "kit" / "claude-plugin")}}})
    (tmp / "claude" / "on-agents").write_text(
        f"curl -sS -X PUT -H 'Content-Type: application/json' --data '{doc}' \"$TARESD_URL/api/crew/settings\"\n", encoding="utf-8")
    (tmp / "claude" / "on-agents-at").write_text("1\n", encoding="utf-8")


scenario(Scenario(
    name="claude_update_frozen_mid",
    suite="claude_update",
    about="a freeze that lands in the middle of a round (after the watch read the session list) wins: nothing is restarted",
    setup=_world_setup(_crew_world(), have="1.10.4", installed="1.10.5"),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[_watch(before=_both(_all_idle, _freeze_on_agents), after=_fstate),
           Step(["crew", "status"], after=_fstate)],
    checks=[("frozen in the middle of the round: nothing moves onto the newer Claude Code",
             lambda r: _bg(r, 0) == [] and r.snap(0)["frozen"] and bool(re.search(r"^FROZEN", r.out(1), re.M)))],
    covers=["frozen in the middle of the round: nothing moves onto the newer Claude Code"],
))


# --- pace: a Claude Code move is a restart, a model turn, so it never goes into a plan limit that is full ------------------
# The plan is the document the fake Tares answers GET /api/crew/pace with (the daemon's own pace_now, as pace.py of the
# parity file does); a step swaps it, a timer or the fake Claude Code swaps it while a command runs.

PACE = {"FACTORY_NOW": str(P.PC_N0)}
PACE_ROUTE = "/api/crew/pace"


def _pace_world(session, weekly):
    def make() -> "fake_tares.World":
        w = _crew_world(pace=True, idle_wake=False)()
        w.routes[PACE_ROUTE] = P._pc_doc(session, weekly)
        return w
    return make


def _plan(session, weekly):
    """Before a step: this is the plan's reading."""
    def run(tmp: Path) -> None:
        P._WORLDS[str(tmp)].routes[PACE_ROUTE] = P._pc_doc(session, weekly)
    return run


def _plan_when_listed(session, weekly):
    """Before a step: the plan fills (a PUT to the fake Tares) once the watch has read the session list."""
    def run(tmp: Path) -> None:
        body = tmp / "plan.json"
        body.write_text(json.dumps({"path": PACE_ROUTE, "answer": P._pc_doc(session, weekly)}), encoding="utf-8")
        (tmp / "claude" / "on-agents").write_text(
            f"curl -sS -X PUT -H 'Content-Type: application/json' --data-binary @{body} \"$TARESD_URL/fake/routes\"\n", encoding="utf-8")
        (tmp / "claude" / "on-agents-at").write_text("1\n", encoding="utf-8")
    return run


def _plan_state(tmp: Path) -> dict:
    return {**_state(tmp), "at_limit": P._WORLDS[str(tmp)].routes[PACE_ROUTE].get("at_limit")}


def _pwatch(before=None) -> Step:
    return _watch(before=before, env=PACE, after=_plan_state)


def _prompt_at(r: Run, i: int, name: str) -> str:
    """The prompt of the last restart of this station up to the end of step i."""
    sent = [c["args"][-1] for c in bg_calls(r) if c["args"][2] == name]
    n = _bg(r, i).count(name)
    return sent[n - 1] if 0 < n <= len(sent) else ""


def _last_name(r: Run, i: int) -> str:
    return (_bg(r, i) or [""])[-1]


LIMIT_FULL = r"the plan.s limit is full until 20"

scenario(Scenario(
    name="claude_update_pace_full",
    suite="claude_update",
    about="a plan limit that is full holds a Claude Code move back (the watch moves nothing, crew update says why and restarts nothing); "
          "once the limit has reset the watch moves a station again",
    setup=_world_setup(_pace_world(100, 10), have="1.0.0", installed="1.0.0"),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[
        _pwatch(_all_idle), _pwatch(_all_idle), _pwatch(_all_idle),                                      # 0 1 2 the notes pace sends to a full plan
        _pwatch(_install("1.11.0")), _pwatch(_all_idle), _pwatch(_all_idle),                              # 3 4 5 a newer Claude Code
        Step(["crew", "update", "--wait", "5"], env={**UPD, **PACE}, after=_plan_state, before=_all_idle),   # 6
        _pwatch(_both(_plan(10, 10), _all_idle)), _pwatch(_all_idle), _pwatch(_all_idle),                 # 7 8 9 the limit has reset
    ],
    checks=[
        ("limit full: the watch moves no station onto the newer Claude Code",
         lambda r: _bg(r, 5) == _bg(r, 2) and r.snap(5)["runs"] == r.snap(2)["runs"] and r.snap(5)["at_limit"] is True),
        ("limit full: crew update says so and restarts nothing",
         lambda r: r.code(6) != 0 and bool(re.search(LIMIT_FULL, r.out(6))) and _bg(r, 6) == _bg(r, 5)),
        ("the limit has reset: the watch moves a station again",
         lambda r: len(_bg(r, 9)) > len(_bg(r, 6)) and r.snap(9)["runs"].get(_last_name(r, 9)) == "1.11.0"),
    ],
    covers=["limit full: the watch moves no station onto the newer Claude Code", "limit full: crew update says so and restarts nothing",
            "the limit has reset: the watch moves a station again"],
))

scenario(Scenario(
    name="claude_update_pace_fill",
    suite="claude_update",
    about="the limit fills in the middle of a round: nothing is restarted onto the newer Claude Code; below the limit crew update moves "
          "the stations and the restart says to re-arm at 45 minutes; the limit fills while crew update waits: it stops and says until when",
    setup=_world_setup(_pace_world(10, 10), have="1.11.0", installed="1.11.0"),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[
        _pwatch(_both(_install("1.11.1"), _plan_when_listed(100, 10))),                                  # 0 it fills after the list was read
        _pwatch(_both(_plan(90, 10), _all_idle)), _pwatch(_all_idle), _pwatch(_all_idle),                 # 1 2 3 slow, not the limit
        Step(["crew", "update", "--wait", "5"], env={**UPD, **PACE}, after=_plan_state,
             before=_both(_install("1.11.2"), _plan(90, 10))),                                           # 4
        Step(["crew", "update", "--wait", "30"], env={**UPD, **PACE}, after=_plan_state,
             before=_both(_install("1.11.3"), _cc_busy(_R), _plan(10, 10),
                          _later(3, lambda tmp: (_plan(100, 10)(tmp), _status_of(_R, "idle")(tmp))))),   # 5 it fills while it waits
    ],
    checks=[
        ("the limit fills in the middle of the round: nothing is restarted onto the newer Claude Code",
         lambda r: r.snap(0)["at_limit"] is True and "restarting" not in r.out(0) and _bg(r, 0) == []),
        ("below the limit, crew update moves them",
         lambda r: r.code(4) == 0 and all(v == "1.11.2" for v in r.snap(4)["runs"].values())),
        ("and the restart says to re-arm at 45 minutes",
         lambda r: all("re-arm your schedule if you have one, at 45 minutes while the factory paces the crew" in _prompt_at(r, 4, n)
                       for n in (_O, _R, _L))),
        ("the limit fills while crew update waits: it stops, says until when, and restarts nothing more",
         lambda r: r.code(5) not in (0, 124) and bool(re.search(LIMIT_FULL, r.out(5))) and "nothing more was restarted" in r.out(5)
         and r.snap(5)["ids"][_R] == r.snap(4)["ids"][_R] and r.snap(5)["ids"][_O] == r.snap(4)["ids"][_O]),
    ],
    covers=["the limit fills in the middle of the round: nothing is restarted onto the newer Claude Code",
            "below the limit, crew update moves them", "and the restart says to re-arm at 45 minutes",
            "the limit fills while crew update waits: it stops, says until when, and restarts nothing more"],
))


# --- a helper whose restarts land on old spares, or fail ------------------------------------------------------------------
# A helper's task lives only in its spawn prompt, so it is never started in a new conversation: where every try lands on an
# old spare it is marked and retried after a backoff, resumed each time. A helper that cannot be restarted is not revived by
# the watch, and the message does not say it is.

_HELPER, _H = P._HELPER, P._HELPER[0]
RESUME_FAILS = {"FAKE_CLAUDE_FAIL_RESUME": "1"}


def _due(name: str):
    """Before a step: this station's backoff is over."""
    def run(tmp: Path) -> None:
        p = tmp / "crew" / "stations.json"
        rows = json.loads(p.read_text(encoding="utf-8"))
        rows[name]["cc_retry_at"] = 1
        t = p.with_name("stations.json.tmp")
        t.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        os.replace(t, p)
    return run


def _could_not(name: str, version: str) -> str:
    return f"could not restart {name} on Claude Code {version}; the watch does not revive helpers, so ask crew-reviewer to spawn it again"


scenario(Scenario(
    name="claude_update_helper",
    suite="claude_update",
    about="a helper behind on old spares: three resumed tries, no new conversation, marked for a retry; crew update promises it no new "
          "conversation; a helper that cannot be restarted: the watch is not said to revive it",
    setup=_world_setup(_crew_world(), have="1.12.2", installed="1.12.2", extra=(_HELPER,)),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[
        Step(["roster"], after=_rstate),                                                                 # 0 on the installed version
        Step(["roster"], before=_install("1.12.3"), after=_rstate),                                      # 1 a newer one
        _watch(before=_both(_file("spawn-version", "1.12.2\n"), _file("stale-left", "all\n"), _all_idle), after=_rstate),   # 2 the reviewer
        _watch(before=_all_idle, after=_rstate),                                                         # 3 the releaser
        _watch(before=_all_idle, after=_rstate),                                                         # 4 the helper
        Step(["crew", "update", "--wait", "5"], env=UPD, after=_rstate, before=_both(_due(_H), _all_idle)),   # 5
        _watch(before=_both(_due(_H), _all_idle), env=RESUME_FAILS, after=_rstate),                      # 6 the restart fails
    ],
    checks=[
        ("a helper is running, on the installed version", lambda r: r.snap(0)["runs"].get(_H) == "1.12.2" and bool(r.snap(0)["ids"].get(_H))),
        ("the roster flags an old helper", lambda r: "older than 1.12.3" in _line(r.out(1), _H)),
        ("a helper behind on old spares: three resumed tries, no new conversation",
         lambda r: _new(r, 4) == [_H] * 3 and _resumed_of(r, _H)[-3:] == [True] * 3 and "started you again" not in _prompt_at(r, 4, _H)),
        ("it is marked for a retry, with no new conversation recorded",
         lambda r: r.snap(4)["marks"][_H][0] == "1.12.3" and r.snap(4)["marks"][_H][2] is None
         and "a helper keeps its conversation" in r.out(4)),
        ("crew update on a helper that keeps landing on old spares: no new conversation promised",
         lambda r: any(f"{_H} came back" in x and "a helper keeps its conversation" in x for x in r.out(5).splitlines())
         and not any(f"{_H} came back" in x and "even in a new conversation" in x for x in r.out(5).splitlines())),
        ("a helper that could not be restarted: the message says the watch does not revive helpers",
         lambda r: _could_not(_H, "1.12.3") in r.out(6) and not any(_H in x and "revives it as before" in x for x in r.out(6).splitlines())),
    ],
    covers=["a helper is running, on the installed version", "the roster flags an old helper",
            "a helper behind on old spares: three resumed tries, no new conversation",
            "it is marked for a retry, with no new conversation recorded",
            "crew update on a helper that keeps landing on old spares: no new conversation promised",
            "a helper that could not be restarted: the message says the watch does not revive helpers"],
))

scenario(Scenario(
    name="claude_update_helper_down",
    suite="claude_update",
    about="crew update on a helper whose restart fails: it says the watch does not revive helpers, not that it revives it",
    setup=_world_setup(_crew_world(), have="1.12.7", installed="1.12.8", extra=(_HELPER,)),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[Step(["crew", "update", "--wait", "5"], env={**UPD, **RESUME_FAILS}, before=_all_idle, after=_rstate)],
    checks=[("crew update, a helper that could not be restarted: the watch is not said to revive it",
             lambda r: _could_not(_H, "1.12.8") in r.out(0) and not any(_H in x and "revives it" in x for x in r.out(0).splitlines()))],
    covers=["crew update, a helper that could not be restarted: the watch is not said to revive it"],
))


# --- the new conversation is recorded only once its restart has returned ------------------------------------------------

scenario(Scenario(
    name="claude_update_fresh",
    suite="claude_update",
    about="a new conversation that dies is not recorded as done, nor marked as failed; a later round puts every station on the new version",
    setup=_world_setup(_crew_world(), have="1.12.2", installed="1.12.3", files={"spawn-version": "1.12.0\n", "stale-left": "all\n"}),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[_watch(before=_all_idle, env={"FAKE_CLAUDE_FAIL_FRESH": "1"}, after=_rstate)]
          + [_watch(before=_both(_file("stale-left", "0\n"), _all_idle), after=_rstate) for _ in range(4)],
    checks=[
        ("the new conversation could not be started: it is not recorded as done, nor marked as failed",
         lambda r: bool(_new(r, 0)) and r.snap(0)["marks"][_new(r, 0)[-1]][2] != "1.12.3" and r.snap(0)["marks"][_new(r, 0)[-1]][0] != "1.12.3"),
        ("the stations all run 1.12.3 after it", lambda r: all(r.snap(4)["runs"][n] == "1.12.3" for n in (_O, _R, _L))),
    ],
    covers=["the new conversation could not be started: it is not recorded as done, nor marked as failed",
            "the stations all run 1.12.3 after it"],
))


# --- what Tares is told of the marks, and the note for the caller --------------------------------------------------------

def _set(**settings):
    """Before a step: these settings are kept in Tares."""
    def run(tmp: Path) -> None:
        P._WORLDS[str(tmp)].settings.update(settings)
    return run


def _old_mark(tmp: Path) -> None:
    """The reviewer is marked for 1.12.6, a version older than the installed one, and Tares was told so."""
    p = tmp / "crew" / "stations.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    rows[_R].update({"cc_failed": "1.12.6", "cc_fails": 2, "cc_retry_at": 4102444800})
    t = p.with_name("stations.json.tmp")
    t.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    os.replace(t, p)
    P._WORLDS[str(tmp)].retry = {_R: {"target": "1.12.6", "fails": 2, "next_at": 4102444800}}


SELF_OFF = "crew-orchestrator, which ran this command, can only be restarted from another session"

scenario(Scenario(
    name="claude_update_marks",
    suite="claude_update",
    about="Tares hears only of stations still retrying the version now installed (one that moved some other way, or a mark for an older "
          "version, drops off at the next round); the caller is told to restart itself elsewhere when the watch would not move it",
    setup=_world_setup(_crew_world(), have="1.12.3", installed="1.12.4", files={"spawn-version": "1.12.0\n", "stale-left": "all\n"}),
    ignore_asked=P._CC_NOT_YET,
    ignore_calls=P._CC_READS,
    steps=[
        _watch(before=_all_idle, after=_rstate),                                                         # 0 the reviewer exhausts its tries
        Step(["restart", _R], before=_both(_file("stale-left", "0\n"), _all_idle), after=_rstate),        # 1 moved by a manual restart
        _watch(before=_all_idle, after=_rstate),                                                         # 2 the next round
        Step(["crew", "update", "--wait", "5"], env=CALLER, after=_rstate,
             before=_both(_install("1.12.5"), _set(claude_update=False))),                               # 3 claude_update off
        Step(["crew", "freeze"], before=_set(claude_update=True), after=_rstate),                        # 4
        Step(["crew", "update", "--wait", "5"], env=CALLER, after=_rstate, before=_install("1.12.6")),   # 5 frozen
        Step(["crew", "unfreeze"], after=_rstate),                                                       # 6
        Step(["roster"], after=_rstate,
             before=_both(_install("1.12.7"), _file("stale-left", "all\n"), _cc_busy(_R), _old_mark)),   # 7 an old mark, listed
        _watch(after=_rstate), _watch(after=_rstate),                                                    # 8 9 settled rounds
    ],
    checks=[
        ("a station that exhausted its tries is listed on Tares",
         lambda r: r.snap(0)["marks"][_new(r, 0)[-1]][0] == "1.12.4" and r.snap(0)["retry"].get(_new(r, 0)[-1], [None])[0] == "1.12.4"),
        ("it moved by a manual restart and the mark is still there",
         lambda r: r.snap(1)["runs"][_R] == "1.12.4" and r.snap(1)["marks"][_R][0] == "1.12.4"),
        ("the next round drops it from Tares", lambda r: _R not in r.snap(2)["retry"]),
        ("claude_update off: the caller is told to restart itself from another session, not that the watch moves it",
         lambda r: r.code(3) == 0 and SELF_OFF in r.out(3) and "moves when" not in r.out(3)),
        ("a frozen crew: the same", lambda r: r.code(5) == 0 and SELF_OFF in r.out(5) and "moves when" not in r.out(5)),
        ("a mark for an older version is listed on Tares before the round", lambda r: r.snap(7)["retry"].get(_R, [None])[0] == "1.12.6"),
        ("a mark for an older version: the next settled round drops it from Tares", lambda r: _R not in r.snap(9)["retry"]),
    ],
    covers=["a station that exhausted its tries is listed on Tares", "it moved by a manual restart and the mark is still there",
            "the next round drops it from Tares",
            "claude_update off: the caller is told to restart itself from another session, not that the watch moves it",
            "a frozen crew: the same", "a mark for an older version is listed on Tares before the round",
            "a mark for an older version: the next settled round drops it from Tares"],
))
