"""scripts/crew_smoke.py: the isolation rules, the scratch environment, the trust-file edit, and one dry run
(fake Claude Code, scratch HOME, real scratch Tares) that must leave nothing behind and never touch the live
crew home or the real ~/.claude.json. The real-Claude run is by hand (TR-977)."""
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "crew_smoke.py"
spec = importlib.util.spec_from_file_location("crew_smoke", SCRIPT)
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

fails = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f": {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def refused(home, name, live):
    try:
        smoke.check_isolation(home, name, live)
    except smoke.Refused as e:
        return str(e)
    return None


tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="smoke-test-")))
try:
    live = [str(tmp / "me" / "factory")]
    scratch = tmp / "scratch" / "crew"
    check("a scratch home and smoke- name pass", refused(scratch, "smoke-1", live) is None)
    check("the live crew home is refused", refused(live[0], "smoke-1", live) is not None)
    check("a folder inside the live home is refused", refused(Path(live[0]) / "sub", "smoke-1", live) is not None)
    check("a folder holding the live home is refused", refused(tmp / "me", "smoke-1", live) is not None)
    os.makedirs(live[0])
    os.symlink(live[0], tmp / "link")
    check("a link to the live home is refused", refused(tmp / "link", "smoke-1", live) is not None)
    check('the crew name "crew" is refused', refused(scratch, "crew", live) is not None)
    check("a name without smoke- is refused", refused(scratch, "other", live) is not None)
    check("the real ~/factory is among the live homes", os.path.realpath(os.path.expanduser("~/factory")) in smoke.live_crew_homes())

    # the refusal reaches the exit code, before anything starts
    r = subprocess.run([sys.executable, str(SCRIPT), "--crew-home", os.path.expanduser("~/factory")], capture_output=True, text=True, timeout=60)
    check("main refuses ~/factory with exit 2", r.returncode == 2 and "refusing to start" in r.stderr, f"{r.returncode} {r.stderr}")
    r = subprocess.run([sys.executable, str(SCRIPT), "--crew-name", "crew"], capture_output=True, text=True, timeout=60)
    check('main refuses the crew name "crew" with exit 2', r.returncode == 2 and "refusing to start" in r.stderr, f"{r.returncode} {r.stderr}")

    # the scratch environment drops what a station carries and spells out the scratch values
    base = {"PATH": "/bin", "FACTORY_HOME": "/Users/x/factory", "FACTORY_CREW_NAME": "crew", "TARES_AUTH_TOKEN": "secret",
            "TARES_FACTORY_MCP_URL": "http://live/mcp", "TARESD_URL": "http://live", "TARES_KEYRING": "on",
            "TARES_FACTORY_TOKEN_FILE": "/Users/x/factory/.tares-token", "HOME": "/Users/x"}
    env = smoke.scratch_env(base, crew_home=scratch, crew_name="smoke-9", tares_home=tmp / "th", port=4001, mcp_port=4002)
    check("the live crew home is not inherited", env["FACTORY_HOME"] == str(scratch) == env["FACTORY_CREW_HOME"])
    check("the crew name is the smoke one", env["FACTORY_CREW_NAME"] == "smoke-9")
    check("no token, keyring switch or token file is inherited",
          not any(k in env for k in ("TARES_AUTH_TOKEN", "TARES_KEYRING", "TARES_FACTORY_TOKEN_FILE")))
    check("Tares and MCP point at the scratch ports", env["TARESD_URL"] == env["TARES_FACTORY_URL"] == "http://127.0.0.1:4001"
          and env["TARES_FACTORY_MCP_URL"] == "http://127.0.0.1:4002/mcp")
    check("the keyring is pinned to the fail backend", env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring")
    check("the watch is off", env["FACTORY_NO_WATCH"] == "1")

    # the trust entry: added, then removed, the rest of the file kept
    cj = tmp / "claude.json"
    cj.write_text(json.dumps({"theme": "dark", "projects": {"/a": {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    smoke.trust_folder(cj, "/scratch/crew")
    doc = json.loads(cj.read_text(encoding="utf-8"))
    check("trust_folder adds the entry", doc["projects"]["/scratch/crew"]["hasTrustDialogAccepted"] is True)
    smoke.untrust_folder(cj, "/scratch/crew")
    check("untrust_folder restores the file", json.loads(cj.read_text(encoding="utf-8")) ==
          {"theme": "dark", "projects": {"/a": {"hasTrustDialogAccepted": True}}})
    # the folders a real run trusts: crew home, the clone's parent (the builder runs there) and the worktrees
    sm = smoke.Smoke(types.SimpleNamespace(dry_run=False, repo="o/r", crew_name="smoke-9", workdir=str(tmp / "w"),
                                           crew_home=None))
    sm.hdir = tmp / "h"
    sm.hdir.mkdir()
    sm.trust_folders()
    got = set(json.loads((sm.hdir / ".claude.json").read_text(encoding="utf-8"))["projects"])
    check("trust covers the crew home, the clone itself and the worktrees",
          got == {str(sm.crew_home), str(sm.work / "repos" / "r"), str(sm.crew_home / "worktrees")}, str(got))
    for entry in sm.trusted:
        smoke.untrust_folder(*entry)
    check("untrusting every entry empties the file", json.loads((sm.hdir / ".claude.json").read_text(encoding="utf-8"))["projects"] == {})
    smoke.trust_folder(tmp / "missing.json", "/x")
    check("trust_folder makes a missing file", (tmp / "missing.json").is_file())

    # F1: what a station carries is dropped even when the scratch values do not overwrite it
    station = {"PATH": "/bin", "FACTORY_STATION": "crew-reviewer", "FACTORY_ORCHESTRATOR": "crew-orchestrator",
               "FACTORY_PARENT": "crew-orchestrator", "CLAUDE_PLUGIN_OPTION_TOKEN": "x", "NAVFLOW_DB": "/db", "TARES_PROJECT": "uc_1"}
    env = smoke.scratch_env(station, crew_home=scratch, crew_name="smoke-9", tares_home=tmp / "th", port=4001, mcp_port=4002)
    check("an inherited FACTORY_STATION, FACTORY_ORCHESTRATOR and FACTORY_PARENT are gone",
          not any(k in env for k in ("FACTORY_STATION", "FACTORY_ORCHESTRATOR", "FACTORY_PARENT")), str(sorted(env)))
    check("an inherited plugin option, NAVFLOW_DB and TARES_PROJECT are gone",
          not any(k in env for k in ("CLAUDE_PLUGIN_OPTION_TOKEN", "NAVFLOW_DB", "TARES_PROJECT")), str(sorted(env)))
    check("PATH is kept", env["PATH"] == "/bin")

    def new_smoke(name):
        x = smoke.Smoke(types.SimpleNamespace(dry_run=False, repo="o/r", crew_name="smoke-9", workdir=str(tmp / name),
                                              crew_home=None))
        x.hdir = tmp / (name + "-home")
        x.hdir.mkdir()
        x.poll = 0
        return x

    # F1: teardown removes the trust entries it added, and only those
    sm = new_smoke("td")
    (sm.hdir / ".claude.json").write_text(json.dumps({"theme": "dark", "projects": {"/a": {"x": 1}}}), encoding="utf-8")
    sm.trust_folders()
    sm.teardown()
    check("teardown removes every trust entry and keeps the rest of the file",
          json.loads((sm.hdir / ".claude.json").read_text(encoding="utf-8")) == {"theme": "dark", "projects": {"/a": {"x": 1}}})

    # F2: a failed poll is tried again; five in a row end the run (a lasting error, not a 30 minute wait)
    sm = new_smoke("w1")
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise smoke.Failed("gh hiccup")
        return {"number": 1, "url": "u"}
    sm.github_pr, sm.roster, sm.tickets, sm.desk = flaky, lambda: [], lambda: [], lambda: []
    def waited(x, secs=5):
        try:
            return x.wait_for_pr(secs)
        except (smoke.Failed, TimeoutError) as e:
            return f"{type(e).__name__}: {e}"
    got = waited(sm)
    check("two failed polls do not end the wait", got == {"number": 1, "url": "u"} and calls["n"] == 3, f"{got} {calls}")
    sm = new_smoke("w2")
    ok_pr = [None, None]
    n = {"roster": 0}

    def roster_flaky():
        n["roster"] += 1
        if n["roster"] == 1:
            raise smoke.Failed("roster timed out")
        return []

    def pr_later():
        ok_pr.append({"number": 2, "url": "u2"}) if len(ok_pr) < 4 else None
        return ok_pr.pop(0)
    sm.github_pr, sm.roster, sm.tickets, sm.desk = pr_later, roster_flaky, lambda: [], lambda: []
    got = waited(sm)
    check("one roster timeout does not end the wait", isinstance(got, dict) and got["number"] == 2, str(got))
    sm = new_smoke("w3")
    seen = {"n": 0}

    def always():
        seen["n"] += 1
        raise smoke.Failed("gh auth login")
    sm.github_pr, sm.roster, sm.tickets, sm.desk = always, lambda: [], lambda: [], lambda: []
    try:
        sm.wait_for_pr(3)
        got = "returned"
    except smoke.Failed as e:
        got = str(e)
    except TimeoutError:
        got = "timed out"
    check("a lasting error fails after MAX_POLL_ERRORS polls, not at the timeout",
          seen["n"] == smoke.MAX_POLL_ERRORS and "gh auth login" in got, f"{seen['n']} {got}")
    sm = new_smoke("w4")
    sm.github_pr, sm.roster, sm.tickets, sm.desk = flaky, lambda: [], lambda: [], lambda: []
    calls["n"] = -1000
    seq = iter([False, True] * 10)

    def alternate():
        if next(seq):
            return {"number": 3, "url": "u3"} if calls.setdefault("good", 0) >= 6 else (calls.__setitem__("good", calls["good"] + 1) or None)
        raise smoke.Failed("every other poll fails")
    sm.github_pr = alternate
    got = waited(sm)
    check("failures that never come five in a row never end the wait", isinstance(got, dict) and got["number"] == 3, str(got))

    # F3: only this run's pull request counts
    real_run = smoke.run
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5))
    old = "2020-01-01T00:00:00Z"

    def fake_gh(prs, diffs, list_rc=0, diff_rc=0):
        """diff_rc: one exit code for every diff, or {pr number as text: code} for some of them."""
        def _run(cmd, env, timeout, cwd=None):
            if cmd[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(cmd, list_rc, json.dumps(prs), "boom")
            if cmd[:3] == ["gh", "pr", "diff"]:
                rc = diff_rc.get(cmd[3], 0) if isinstance(diff_rc, dict) else diff_rc
                return subprocess.CompletedProcess(cmd, rc, diffs.get(cmd[3], ""), "boom")
            raise AssertionError(cmd)
        return _run
    sm = new_smoke("gh")
    sm.started = time.time()
    mine = f"diff --git a/SMOKE.md b/SMOKE.md\n--- /dev/null\n+++ b/SMOKE.md\n@@ -0,0 +1 @@\n+smoke run {sm.pid}\n"
    other = mine.replace(f"smoke run {sm.pid}", f"smoke run {sm.pid + 1}")
    longer = mine.replace(f"smoke run {sm.pid}", f"smoke run {sm.pid}0")
    ticks = mine.replace(f"+smoke run {sm.pid}", f"+`smoke run {sm.pid}`")
    pr = lambda n, branch="factory/T1", at=now: {"number": n, "url": f"u{n}", "headRefName": branch, "createdAt": at, "state": "OPEN"}
    try:
        smoke.run = fake_gh([pr(1), pr(2)], {"1": other, "2": mine})
        check("the pull request that adds this run's line is found among others", (sm.github_pr() or {}).get("number") == 2)
        smoke.run = fake_gh([pr(1)], {"1": other})
        check("a factory/ pull request of another run is not this run's", sm.github_pr() is None)
        smoke.run = fake_gh([pr(1)], {"1": longer})
        check("a run whose pid starts with ours is not ours", sm.github_pr() is None)
        smoke.run = fake_gh([pr(1)], {"1": ticks})
        check("the line in backticks still counts", (sm.github_pr() or {}).get("number") == 1)
        smoke.run = fake_gh([pr(1, at=old)], {"1": mine})
        check("a pull request from before the run does not count", sm.github_pr() is None)
        smoke.run = fake_gh([pr(1, branch="main-fix")], {"1": mine})
        check("a pull request from a branch outside factory/ does not count", sm.github_pr() is None)
        smoke.run = fake_gh([pr(1)], {"1": mine}, list_rc=1)
        try:
            sm.github_pr()
            got = "returned"
        except smoke.Failed:
            got = "failed"
        check("a gh error on the list raises Failed, it is not 'no pull request yet'", got == "failed")
        # TR-1227 gap 4: one candidate whose diff cannot be read is skipped, the others are still looked at
        def found():
            try:
                return (sm.github_pr() or {}).get("number")
            except smoke.Failed as e:
                return f"Failed: {e}"
        smoke.run = fake_gh([pr(1), pr(2)], {"1": mine, "2": mine}, diff_rc={"1": 1})
        got = found()
        check("a candidate whose diff fails is skipped and the next one is found", got == 2, str(got))
        smoke.run = fake_gh([pr(1), pr(2)], {"1": other, "2": mine}, diff_rc={"2": 1})
        got = found()
        check("a candidate whose diff fails does not end the run when another was read", got is None, str(got))
        smoke.run = fake_gh([pr(1)], {"1": mine}, diff_rc=1)
        got = found()
        check("a lone candidate whose diff cannot be read is 'no pull request yet', not an error (PR 616 F4)",
              got is None, str(got))
        smoke.run = fake_gh([pr(1), pr(2)], {"1": mine, "2": mine}, diff_rc=1)
        got = found()
        check("every candidate diff unreadable is still 'no pull request yet'", got is None, str(got))
        # the sessions a run stops at the end are this crew's, not a longer crew name that starts with it
        sm.env = {"CLAUDE_BIN": "/bin/echo"}
        names = ["smoke-9-orchestrator", "smoke-9-reviewer", "smoke-9-hello-smoke-build-m1", "smoke-9-1-orchestrator",
                 "smoke-9-foo-build-x", "smoke-90-orchestrator", "crew-orchestrator"]
        smoke.run = lambda cmd, env, timeout, cwd=None: subprocess.CompletedProcess(
            cmd, 0, json.dumps([{"name": x, "id": x} for x in names]), "")
        left = sorted(x["name"] for x in sm.leftover_sessions())
        check("leftover sessions are this crew's own stations only",
              left == ["smoke-9-hello-smoke-build-m1", "smoke-9-orchestrator", "smoke-9-reviewer"], str(left))
    finally:
        smoke.run = real_run

    # TR-1227 F2 (PR 645 review): a command run() starts is in its own session, so a terminal Ctrl-C (which goes to
    # the whole foreground process group) does not reach the teardown's children
    if os.name == "posix":
        r = smoke.run([sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"], dict(os.environ), 30)
        check("a command run() starts leads its own session", r.stdout.strip() == "True", r.stdout + r.stderr)

    # F4: a second SIGTERM or Ctrl-C during the teardown does not cut it short
    sm = new_smoke("sig")
    sm.trust_folders()
    sm.env = {"CLAUDE_BIN": "/bin/echo"}

    def raising(signum, _frame=None):
        raise KeyboardInterrupt(f"signal {signum}")
    before = {sig: signal.signal(sig, raising) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        def term_twice():
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
            return []
        sm.leftover_sessions = term_twice
        try:
            sm.teardown()
            got = "finished"
        except KeyboardInterrupt:
            got = "interrupted"
        left = json.loads((sm.hdir / ".claude.json").read_text(encoding="utf-8"))["projects"]
        check("a TERM and a Ctrl-C during teardown do not stop it", got == "finished" and left == {}, f"{got} {left}")
        check("the handlers are back after the teardown",
              signal.getsignal(signal.SIGTERM) is raising and signal.getsignal(signal.SIGINT) is raising)
        # the watchdog stops the main thread and leaves the one teardown to it
        sm2 = new_smoke("dog")
        t = threading.Timer(0.2, smoke.fire_watchdog, [sm2])
        t0 = time.time()
        t.start()
        try:
            time.sleep(20)
            got = "slept"
        except KeyboardInterrupt:
            got = "interrupted"
        t.join()
        check("the watchdog interrupts the main thread's wait", got == "interrupted" and time.time() - t0 < 10, f"{got} {time.time() - t0:.1f}s")
        check("the watchdog marks the run and does not tear down itself", sm2.watchdog_fired and not sm2.torn_down)
    finally:
        for sig, h in before.items():
            signal.signal(sig, h)

    # TR-1227 gaps 1 to 3, each in a child Python so a signal never reaches this test
    # gap 3: the teardown's own commands start with the default signals, not an inherited SIG_IGN
    # (Python itself gives a fresh process its own SIGINT handler unless SIGINT was ignored, so test for "not ignored")
    probe = ("import signal; print(signal.getsignal(signal.SIGTERM) == signal.SIG_DFL and "
             "signal.getsignal(signal.SIGINT) != signal.SIG_IGN)")
    with smoke.ignoring_signals():
        inside = signal.getsignal(signal.SIGTERM)
        child = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    check("signals are not SIG_IGN while ignoring_signals runs", inside != signal.SIG_IGN, str(inside))
    check("a command started inside ignoring_signals can still be stopped by SIGTERM and SIGINT",
          child.stdout.strip() == "True", f"{child.stdout!r} {child.stderr[-300:]!r}")

    driver = tmp / "driver.py"
    driver.write_text('''
import importlib.util, os, signal, sys, threading, time
spec = importlib.util.spec_from_file_location("crew_smoke", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
scenario, marker, work = sys.argv[2:5]

def start_tares(self):
    if scenario == "watchdog":
        threading.Timer(0.2, m.fire_watchdog, [self]).start()
        time.sleep(30)
    raise m.Failed("the scenario fails the run")

def teardown(self):
    if scenario == "finally":
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
    open(marker, "w").write("teardown finished")

m.Smoke.start_tares = start_tares
m.Smoke.teardown = teardown
sys.exit(m.main(["--workdir", work, "--crew-home", work + "/crew", "--timeout", "60"]))
''', encoding="utf-8")

    def drive(scenario):
        marker = tmp / f"marker-{scenario}"
        r = subprocess.run([sys.executable, str(driver), str(SCRIPT), scenario, str(marker), str(tmp / f"drv-{scenario}")],
                           capture_output=True, text=True, timeout=120)
        return r, marker

    # gap 1: a watchdog stop exits 124, not the 1 of a plain failure
    r, marker = drive("watchdog")
    check("a watchdog stop exits 124", r.returncode == smoke.EXIT_WATCHDOG == 124, f"{r.returncode}\n{r.stdout[-400:]}{r.stderr[-400:]}")
    check("the watchdog run still ran its teardown", marker.is_file())
    r, marker = drive("fail")
    check("a plain failure exits 1, not 124", r.returncode == 1, f"{r.returncode}\n{r.stdout[-400:]}{r.stderr[-400:]}")
    # gap 2: a TERM and a Ctrl-C that land inside main's finally do not cut it short
    r, marker = drive("finally")
    check("a TERM and a Ctrl-C inside main's finally do not stop it",
          marker.is_file() and r.returncode == 1 and "Traceback" not in r.stderr,
          f"{r.returncode} marker={marker.is_file()}\n{r.stdout[-400:]}{r.stderr[-400:]}")

    # F5: the trust edit keeps a change made under it, and follows a symlinked file
    cj = tmp / "race.json"
    cj.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    fired = {"n": 0}

    def add_and_race(doc):
        doc.setdefault("projects", {})["/ours"] = {"hasTrustDialogAccepted": True}
        fired["n"] += 1
        if fired["n"] == 1:   # Claude Code writes the file between our read and our replace
            cj.write_text(json.dumps({"projects": {}, "theirs": 1}), encoding="utf-8")
        return True
    smoke.edit_claude_json(cj, add_and_race)
    got = json.loads(cj.read_text(encoding="utf-8"))
    check("a write that lands between the read and the replace is kept", got.get("theirs") == 1 and "/ours" in got["projects"], str(got))
    target = tmp / "real-claude.json"
    target.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    link = tmp / "linked.json"
    os.symlink(target, link)
    smoke.trust_folder(link, "/x")
    check("a symlinked file stays a symlink", link.is_symlink() and os.path.realpath(link) == str(target))
    check("the symlink's target got the entry", json.loads(target.read_text(encoding="utf-8"))["projects"]["/x"]["hasTrustDialogAccepted"] is True)
    smoke.untrust_folder(link, "/x")
    check("untrust follows the symlink too", "/x" not in json.loads(target.read_text(encoding="utf-8"))["projects"] and link.is_symlink())
    constant = tmp / "busy.json"
    constant.write_text("{}", encoding="utf-8")

    def always_races(doc):
        doc["a"] = 1
        constant.write_text(json.dumps({"n": time.time_ns()}), encoding="utf-8")
        return True
    try:
        smoke.edit_claude_json(constant, always_races)
        got = "returned"
    except OSError:
        got = "gave up"
    check("a file that never stops changing is given up on, not overwritten", got == "gave up")
    check("no temp file is left behind", not [x for x in os.listdir(tmp) if ".smoke" in x], str(os.listdir(tmp)))

    # one dry run, end to end
    if not (shutil.which("jq") and shutil.which("git")):
        print("skip the dry run: it needs jq and git")
    else:
        real_cj = Path(os.path.expanduser("~/.claude.json"))
        work = tmp / "dry"
        r = subprocess.run([sys.executable, str(SCRIPT), "--dry-run", "--workdir", str(work), "--timeout", "120"],
                           capture_output=True, text=True, timeout=300)
        out = r.stdout + r.stderr
        check("the dry run exits 10, never 0 (no PR was checked)", r.returncode == 10, f"{r.returncode}\n{out[-1500:]}")
        check("the dry run started both crew stations under the smoke name",
              "started smoke-" in out and "-orchestrator" in out and "-reviewer" in out, out[-800:])
        check("the dry run never names the live crew's stations", "crew-orchestrator" not in out and "crew-reviewer" not in out)
        check("crew down ran", "all stations stopped" in out, out[-800:])
        # a live Claude Code rewrites the real file all the time, so look for our folder, not for a byte-equal file
        leaked = real_cj.is_file() and str(work) in real_cj.read_text(encoding="utf-8", errors="replace")
        check("the dry run leaves no entry in the real ~/.claude.json", not leaked)
        left = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
        check("no scratch daemon is left running", str(work) not in left)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} failed: {', '.join(fails)}")
    sys.exit(1)
print("\nall passed")
