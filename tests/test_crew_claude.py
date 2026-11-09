"""tares.crew.claude, the process layer: finding Claude Code, reading its sessions, telling a quiet station.

It is checked against the Python fake Claude Code (tests/factory/fake_claude.py) in a throwaway HOME, and,
where bash, jq and a `sort -V` are there (not on Windows), against the real bash functions of
claude-plugin/bin/factory, cut out of the script by text and run on the same fake data: turn_state,
find_live_job, is_alive, quiet and between_turns on a matrix of sessions and job files, parse_bg_id
on the same inputs, older_than against `sort -V`, station_updated against jq.

Untested on real Windows: the tests named windows_branch_* pass the platform in ("win32") and so run on any
machine. They check which file is chosen (claude.exe, claude.cmd), the install folders and the command line
built for a .cmd; they do not start claude.exe or cmd.exe. On the Windows job the fake runs through a small
claude.cmd; if that launcher does not run there, the cases that need it say `skip` and why.

Never touched: the real HOME, ~/.claude, ~/.tares, ~/factory, the OS keychain, a real Claude Code (CLAUDE_BIN
is always the fake) and any real key. Run: python tests/test_crew_claude.py [test name ...]
"""
import ast
import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
FACTORY_DIR = ROOT / "tests" / "factory"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FACTORY_DIR))

import fake_claude  # noqa: E402
from tares.crew import CrewError, CrewPaths, state  # noqa: E402
from tares.crew import claude as cc  # noqa: E402
from tares.crew.errors import (ClaudeNotFoundError, JobStateUnreadableError,  # noqa: E402
                               SessionListUnreadableError)

FACTORY = ROOT / "claude-plugin" / "bin" / "factory"
WIN = sys.platform == "win32"

P = F = 0
TESTS = []


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def short(x, n=300):
    s = repr(x)
    return s if len(s) <= n else s[:n] + "..."


def skip(what, why):
    print(f"  skip {what}: {why}")


def test(fn):
    TESTS.append(fn)
    return fn


# ---------- the sandbox: one temp folder, its name with a space; HOME and the crew home inside it ----------

T = Path(tempfile.mkdtemp(prefix="tares crew claude "))
HOME = T / "home dir"
CREW = T / "crew home"
STATE = T / "fake state"
BINDIR = T / "bin dir"
for d in (HOME, CREW, STATE, BINDIR):
    d.mkdir()
PATHS = CrewPaths.at(CREW)

for k in list(os.environ):
    if k.startswith(("FACTORY_", "FAKE_CLAUDE_", "CLAUDE_")) or k in ("TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_URL"):
        del os.environ[k]
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), FACTORY_CREW_HOME=str(CREW), FAKE_CLAUDE_STATE=str(STATE),
                  TMPDIR=str(T), PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")


@contextlib.contextmanager
def env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def make_launcher(folder, name, script):
    """A `claude` that runs `python script args`: a sh script, or a .cmd on Windows. Returns its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if WIN:
        f = folder / (name + ".cmd")
        f.write_bytes(f'@"{sys.executable}" "{script}" %*\r\n'.encode("utf-8"))
    else:
        f = folder / name
        f.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        f.chmod(0o755)
    return str(f)


FAKE = make_launcher(BINDIR, "claude", FACTORY_DIR / "fake_claude.py")
os.environ["CLAUDE_BIN"] = FAKE


def probe_launcher():
    try:
        return cc.run_claude(["--version"]).stdout.strip() == "0.0.0 (Claude Code)"
    except Exception:
        return False


FAKE_OK = probe_launcher()
if not FAKE_OK:
    print("  note the fake claude launcher does not run here; the cases that need it are skipped")


def needs_fake(what):
    """True when the fake Claude runs here; else says which cases are skipped."""
    if not FAKE_OK:
        skip(what, "the fake claude launcher does not run on this machine")
    return FAKE_OK


def reset():
    """A fresh fake Claude Code, job folder and crew home."""
    for d in (STATE, HOME / ".claude", CREW):
        shutil.rmtree(d, ignore_errors=True)
    STATE.mkdir()
    CREW.mkdir()
    fake_claude.write_agents(STATE, [])


def agents(*rows):
    fake_claude.write_agents(STATE, list(rows))


def sess(id="0000000a", name="s1", cwd="/w", state_="working", status="idle", pid=True, **kw):
    """One row of `claude agents --json`: a live session by default (state working, status idle, a pid)."""
    r = {"id": id, "name": name, "cwd": cwd, "state": state_}
    if status is not None:
        r["status"] = status
    if pid:
        r["pid"] = 40000 + int(id[-4:], 16) if pid is True else pid
    r.update(kw)
    return r


def job(id, value):
    """~/.claude/jobs/<id>/state.json; a str is written as given (to make a broken file)."""
    d = HOME / ".claude" / "jobs" / id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def station(name, **kw):
    state.stations_put(name, kw, paths=PATHS)


def iso(secs_ago, ms=True):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=secs_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + (".123Z" if ms else "Z")


def attempt(fn, *a, **k):
    """The result, or the name of the error it raised."""
    try:
        return fn(*a, **k)
    except Exception as e:
        return type(e).__name__


# ============================================================ parse_bg_id


BG_CASES = [
    ("backgrounded · feb401c6 · tfspike-alpha\n", "feb401c6"),
    ("backgrounded · \033[36m3bd5cd67\033[39m · vetme-build-t1\n\033[2m  claude agents   list sessions\033[22m\n", "3bd5cd67"),
    ("Workspace not trusted. Run `claude` in /x once\n", ""),
    ("", ""),
    ("\033[1m\033[32mbackgrounded\033[0m · \033[36m0a1b2c3d\033[39m · x\n", "0a1b2c3d"),
    ("noise\nmore\nbackgrounded - deadbeef - name\ntail\n", "deadbeef"),
    ("backgrounded · feb401c6aaaa · long id\n", "feb401c6"),
    ("backgrounded · FEB401C6 · upper case is not an id\n", ""),
    ("backgrounded · feb401 · too short\n", ""),
    ("backgrounded · 11111111 · first\nbackgrounded · 22222222 · second\n", "11111111"),
    ("backgrounded\n12345678\n", ""),
    ("\033[?25lbackgrounded · \033[38;5;81m9f8e7d6c\033[m · n\n", "9f8e7d6c"),
]


@test
def parse_bg_id():
    for text, want in BG_CASES:
        got = cc.parse_bg_id(text)
        ck(f"parse_bg_id({text[:46]!r}) = {want!r}", got == want, short(got))
    ck("parse_bg_id gives a str, never None", all(isinstance(cc.parse_bg_id(t), str) for t, _ in BG_CASES))
    if not (shutil.which("bash") and shutil.which("jq")) or WIN:
        skip("parse_bg_id against bash", "bash or jq is missing")
        return
    bad = []
    for text, _ in BG_CASES:
        r = subprocess.run(["bash", str(FACTORY), "_parse-bg-id"], input=text, capture_output=True, text=True,
                           encoding="utf-8", cwd=str(T), env={**os.environ, "CLAUDE_BIN": "claude"})
        if r.stdout.strip() != cc.parse_bg_id(text):
            bad.append((text, r.stdout, cc.parse_bg_id(text)))
    ck("parse_bg_id answers what bash's _parse-bg-id answers on every input", not bad, short(bad))


# ============================================================ claude_bin and the install folders


def script(folder, name, body):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / name
    f.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    f.chmod(0o755)
    return str(f)


@test
def claude_bin_override_and_errors():
    ck("CLAUDE_BIN wins as typed, even for a file that is not there",
       cc.claude_bin({"CLAUDE_BIN": "/no/such claude", "PATH": ""}, "linux") == "/no/such claude")
    real = script(T / "ov" / "a", "claude", "#!/bin/sh\necho real\n")
    if not WIN:  # the POSIX branch splits PATH on ':', which cuts a drive letter
        ck("CLAUDE_BIN beats a good claude on PATH",
           cc.claude_bin({"CLAUDE_BIN": "/mine/claude", "PATH": str(Path(real).parent)}, "linux") == "/mine/claude")
        ck("an empty CLAUDE_BIN counts as unset (${CLAUDE_BIN:-})",
           cc.claude_bin({"CLAUDE_BIN": "", "HOME": str(T / "nohome"), "PATH": str(Path(real).parent)}, "linux") == real)
    try:
        cc.claude_bin({"PATH": str(T / "empty-path"), "HOME": str(T / "nohome")}, "win32")
        e = None
    except ClaudeNotFoundError as err:
        e = err
    ck("nothing found: ClaudeNotFoundError", e is not None)
    ck("...with bash's text", e is not None and str(e) == "claude not found on PATH (set CLAUDE_BIN to its path)", short(str(e)))
    ck("...which is a CrewError and prints as `tares factory: <text>`",
       isinstance(e, CrewError) and e.line == "tares factory: claude not found on PATH (set CLAUDE_BIN to its path)")


@test
def claude_bin_skips_terminal_shims():
    if WIN:
        skip("claude_bin on the POSIX branch", "PATH is split on ':' there; the Windows branch has its own tests")
        return
    nohome = str(T / "nohome")
    d1, d2, d3 = T / "shim" / "one", T / "shim" / "two", T / "shim" / "three"
    shim = script(d1, "claude", "#!/bin/bash\n# cmux claude wrapper\nexec /real/claude --settings /tmp/cmux-claude-settings.json \"$@\"\n")
    real = script(d2, "claude", "#!/bin/sh\necho the real one\n")
    other = script(d3, "claude", "#!/bin/sh\necho another\n")
    e = {"PATH": f"{d1}:{d2}:{d3}", "HOME": nohome}
    ck("a `#!` file that mentions cmux is skipped and the next claude on PATH is chosen",
       cc.claude_bin(e, "linux") == real, short(cc.claude_bin(e, "linux")))
    ck("is_terminal_shim says so for the shim and not for the real one", cc.is_terminal_shim(shim) and not cc.is_terminal_shim(real))
    e2 = {"PATH": f"{d2}:{d1}:{d3}", "HOME": nohome}
    ck("the first real claude on PATH wins over later ones", cc.claude_bin(e2, "linux") == real)
    plain = script(T / "shim" / "plain", "claude", "#!/bin/sh\necho no m-u-x here\n")
    ck("a `#!` script that does not mention cmux is not a shim", cc.claude_bin({"PATH": str(Path(plain).parent), "HOME": nohome}, "linux") == plain)
    binary = script(T / "shim" / "bin", "claude", b"\x7fELF\x00\x01cmux\x00\x02")
    ck("a file that mentions cmux but does not start with `#!` (a real binary) is not a shim",
       cc.claude_bin({"PATH": str(Path(binary).parent), "HOME": nohome}, "linux") == binary and not cc.is_terminal_shim(binary))
    ck("only a shim on PATH: claude is not found", attempt(cc.claude_bin, {"PATH": str(d1), "HOME": nohome}, "linux") == "ClaudeNotFoundError")
    ck("the same path twice on PATH is listed once (awk '!seen[$0]++')",
       cc.claude_candidates({"PATH": f"{d2}:{d2}:{d3}", "HOME": nohome}, "linux")[:2] == [real, other])
    noexec = T / "shim" / "noexec"
    noexec.mkdir(parents=True, exist_ok=True)
    (noexec / "claude").write_text("#!/bin/sh\n", encoding="utf-8")
    (noexec / "claude").chmod(0o644)
    ck("a file without the execute bit is not a candidate",
       cc.claude_candidates({"PATH": str(noexec), "HOME": nohome}, "linux") == [])
    ck("an empty PATH entry is skipped, not read as the current folder",
       cc.claude_candidates({"PATH": f":{d2}::", "HOME": nohome}, "linux") == [real])
    ck("a directory called claude is not a candidate",
       cc.claude_candidates({"PATH": str(T / "shim"), "HOME": nohome}, "linux") == [] and (T / "shim" / "one" / "claude").is_file())


@test
def claude_bin_known_install_folders():
    if WIN:
        skip("claude_bin on the POSIX branch", "PATH is split on ':' there")
        return
    home = T / "gui home"
    dirs = cc.known_install_dirs({"HOME": str(home)}, "linux")
    want = [str(home / ".local" / "bin"), str(home / ".claude" / "local"), "/opt/homebrew/bin", "/usr/local/bin",
            str(home / ".npm-global" / "bin")]
    ck("the POSIX install folders are ~/.local/bin, ~/.claude/local, /opt/homebrew/bin, /usr/local/bin, ~/.npm-global/bin",
       dirs == want, short(dirs))
    real = script(home / ".local" / "bin", "claude", "#!/bin/sh\necho x\n")
    ck("a GUI app with an empty PATH finds claude in ~/.local/bin", cc.claude_bin({"PATH": "", "HOME": str(home)}, "linux") == real)
    shimdir = T / "gui shim"
    script(shimdir, "claude", "#!/bin/sh\nexec cmux-wrapper\n")
    ck("a shim on PATH is skipped and the install folder's claude is used",
       cc.claude_bin({"PATH": str(shimdir), "HOME": str(home)}, "linux") == real)
    onpath = script(T / "gui path", "claude", "#!/bin/sh\necho x\n")
    ck("PATH comes before the install folders", cc.claude_bin({"PATH": str(Path(onpath).parent), "HOME": str(home)}, "linux") == onpath)
    npm = script(home / ".npm-global" / "bin", "claude", "#!/bin/sh\necho x\n")
    shutil.rmtree(home / ".local")
    ck("...and the folders are tried in order (~/.npm-global/bin is last)", cc.claude_candidates({"PATH": "", "HOME": str(home)}, "linux")[-1:] == [npm])


@test
def windows_branch_resolution():
    """Untested on real Windows: the choice of file with platform='win32' passed in."""
    prof = T / "winprof" / "Jane Do"
    local, roam = prof / "AppData" / "Local", prof / "AppData" / "Roaming"
    e = {"USERPROFILE": str(prof), "LOCALAPPDATA": str(local), "APPDATA": str(roam), "PATH": ""}
    dirs = cc.known_install_dirs(e, "win32")
    ck("Windows install folders: %USERPROFILE%\\.local\\bin, %LOCALAPPDATA%\\Programs\\claude, %APPDATA%\\npm",
       dirs == [str(prof / ".local" / "bin"), str(local / "Programs" / "claude"), str(roam / "npm")], short(dirs))
    ck("without LOCALAPPDATA and APPDATA they are taken from the profile folder",
       cc.known_install_dirs({"USERPROFILE": str(prof)}, "win32")[1:] ==
       [str(prof / "AppData" / "Local" / "Programs" / "claude"), str(prof / "AppData" / "Roaming" / "npm")])
    ck("no profile at all: no folders, no error", cc.known_install_dirs({}, "win32") == [])
    a, b = T / "win" / "a", T / "win" / "b"
    for d, names in ((a, ("claude.cmd", "claude.ps1", "claude")), (b, ("claude.exe", "claude.cmd"))):
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"x")
    cands = cc.claude_candidates({"PATH": f'"{a}";{b};', "USERPROFILE": str(prof)}, "win32")
    ck("claude.exe is tried before claude.cmd in a folder; claude.ps1 and a bare `claude` are never candidates",
       cands == [str(a / "claude.cmd"), str(b / "claude.exe"), str(b / "claude.cmd")], short(cands))
    ck("PATH is split on ';', quotes around a folder are dropped, an empty entry is skipped", len(cands) == 3)
    ck("claude_bin on Windows picks the first candidate", cc.claude_bin({"PATH": f"{b};{a}", "USERPROFILE": str(prof)}, "win32") == str(b / "claude.exe"))
    npmclaude = roam / "npm" / "claude.cmd"
    npmclaude.parent.mkdir(parents=True, exist_ok=True)
    npmclaude.write_bytes(b"@echo off\r\n")
    ck("the npm folder is searched when PATH has none (a GUI app)", cc.claude_bin(e, "win32") == str(npmclaude))
    ck("CLAUDE_BIN wins on Windows too", cc.claude_bin({**e, "CLAUDE_BIN": r"C:\x\claude.exe"}, "win32") == r"C:\x\claude.exe")
    ck("the POSIX name `claude` is not looked for on Windows",
       cc.claude_candidates({"PATH": str(T / "winonly"), "USERPROFILE": str(T / "winonly")}, "win32") == [])


@test
def windows_branch_command_line():
    """Untested on real Windows: the command built for a .cmd (never run here)."""
    exe = r"C:\Users\Jane Do\AppData\Local\Programs\claude\claude.exe"
    ck("claude.exe is run directly, as a list", cc.claude_command(exe, ["agents", "--json"], "win32") == [exe, "agents", "--json"])
    cmd = r"C:\Users\Jane Do\AppData\Roaming\npm\claude.cmd"
    got = cc.claude_command(cmd, ["agents", "--json"], "win32", {})
    ck("claude.cmd runs through cmd.exe /d /s /c as one command line",
       got == 'cmd.exe /d /s /c ""C:\\Users\\Jane Do\\AppData\\Roaming\\npm\\claude.cmd" agents --json"', short(got))
    ck("...with the shell taken from COMSPEC when set",
       cc.claude_command(cmd, ["--version"], "win32", {"COMSPEC": r"C:\Windows\System32\cmd.exe"}).startswith(r"C:\Windows\System32\cmd.exe /d /s /c "))
    ck("a path without a space is not quoted twice", cc.claude_command(r"C:\npm\claude.CMD", ["--version"], "win32", {}) == 'cmd.exe /d /s /c "C:\\npm\\claude.CMD --version"')
    ck("a .bat is run through cmd.exe too", isinstance(cc.claude_command(r"C:\npm\claude.bat", [], "win32", {}), str))
    ck("an argument with a space is quoted", '"two words"' in cc.claude_command(cmd, ["-n", "two words"], "win32", {}))
    for bad in ("a&b", "a|b", "a<b", "a>b", "a^b", "50%", 'a"b', "a\nb"):
        ck(f"an argument holding {bad!r} is refused, not quoted", attempt(cc.claude_command, cmd, ["x", bad], "win32", {}) == "CrewError")
    ck("a .cmd path holding & is refused", attempt(cc.claude_command, r"C:\a&b\claude.cmd", [], "win32", {}) == "CrewError")
    ck("the refusal says what to do", "set CLAUDE_BIN" in cc_refusal(cmd), short(cc_refusal(cmd)))
    ck("on POSIX a path ending .cmd is just run (a list)", cc.claude_command("/x/claude.cmd", ["agents"], "linux") == ["/x/claude.cmd", "agents"])
    tree = ast.parse((ROOT / "tares" / "crew" / "claude.py").read_text(encoding="utf-8"))
    shells = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "shell"]
    ck("no shell= argument anywhere in claude.py (no shell=True)", not shells, short(shells))


def cc_refusal(cmd):
    try:
        cc.claude_command(cmd, ["a&b"], "win32", {})
    except CrewError as e:
        return str(e)
    return ""


# ============================================================ run_claude and active_json


@test
def run_claude_and_active_json():
    if not needs_fake("run_claude and active_json"):
        return
    reset()
    agents(sess(), sess("0000000b", "s2", state_="stopped", status=None, pid=False))
    rows = cc.active_json()
    ck("active_json gives the fake's active sessions as a list of dicts (the stopped one is not listed)",
       [r["id"] for r in rows] == ["0000000a"], short(rows))
    calls = [json.loads(ln)["args"] for ln in (STATE / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    ck("it asked for `agents --json` and nothing else", calls == [["agents", "--json"]], short(calls))
    last = (STATE / "last.env").read_text(encoding="utf-8")
    ck("the child runs with NO_COLOR=1", "NO_COLOR=1\n" in last, short(last[-200:]))
    reset()
    ck("an empty list is []: a list that was read and has nobody in it", cc.active_json() == [])
    reset()
    fake_claude.write_agents(STATE, [sess(name="Zoë 名前 🚀", cwd="/work/dir ü")])
    ck("UTF-8 names and folders arrive intact", cc.find_live_job("Zoë 名前 🚀", "/work/dir ü") == "0000000a")


@test
def active_json_is_never_empty_when_unreadable():
    if not needs_fake("unreadable lists"):
        return
    reset()
    agents(sess())
    with env(FAKE_CLAUDE_AGENTS_FAIL="1"):
        ck("claude agents --json exits 1: SessionListUnreadableError, not []", attempt(cc.active_json) == "SessionListUnreadableError")
    with env(FAKE_CLAUDE_AGENTS_FAIL_AFTER="1"):
        (STATE / "agents-reads").unlink(missing_ok=True)
        first = attempt(cc.active_json)
        second = attempt(cc.active_json)
    ck("it reads while the fake works and raises from the call that fails", isinstance(first, list) and second == "SessionListUnreadableError", short((first, second)))
    for label, code in (("garbage", "print('this is not json')"), ("an object", "print('{}')"), ("a string", "print('\"hi\"')"),
                        ("nothing", "pass"), ("exit 3 with a valid list", "print('[]'); raise SystemExit(3)"),
                        ("a truncated list", "print('[{\"id\": \"0000000a\"')")):
        f = T / "bad" / "script.py"
        f.parent.mkdir(exist_ok=True)
        f.write_text(code + "\n", encoding="utf-8")
        launcher = make_launcher(T / "bad" / label.replace(" ", "_"), "claude", f)
        ck(f"output {label}: unreadable", attempt(cc.active_json, launcher) == "SessionListUnreadableError")
    ck("claude cannot start (no such file): unreadable, not empty", attempt(cc.active_json, str(T / "nothing here" / "claude")) == "SessionListUnreadableError")
    slow = T / "bad" / "slow.py"
    slow.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    launcher = make_launcher(T / "bad" / "slowone", "claude", slow)
    old = cc.AGENTS_TIMEOUT
    cc.AGENTS_TIMEOUT = 1
    t0 = time.time()
    try:
        r = attempt(cc.active_json, launcher)
    finally:
        cc.AGENTS_TIMEOUT = old
    ck("a claude that hangs is cut off by the timeout and counts as unreadable", r == "SessionListUnreadableError" and time.time() - t0 < 20, f"{r} {time.time() - t0:.1f}s")
    ck("the unreadable message is bash's UNREAD", cc.UNREAD == "could not read Claude Code's session list (claude agents --json)")
    reset()
    fake_claude.write_agents(STATE, [])
    with env(FAKE_CLAUDE_AGENTS_FAIL="1"):
        ck("quiet on an unreadable list is False, not an error", cc.quiet("none") is False)


# ============================================================ GONE and TURN


# (label, row fields, gone, turn). The same table as bash's GONE and TURN; the cases of tests/factory/crew_test.sh
# (a schedule armed, a prompt waiting, no process while waiting) are in it.
TABLE = [
    ("working, idle, pid: between turns", dict(state="working", status="idle", pid=1), False, "done"),
    ("working, busy, pid: in a turn", dict(state="working", status="busy", pid=1), False, "working"),
    ("working, waiting, pid: a permission prompt (the job file would say working)", dict(state="working", status="waiting", pid=1), False, "blocked"),
    ("blocked, waiting, pid", dict(state="blocked", status="waiting", pid=1), False, "blocked"),
    ("blocked with no pid is live (answering it starts one)", dict(state="blocked", status="waiting"), False, "blocked"),
    ("blocked, no pid, no status", dict(state="blocked"), False, "blocked"),
    ("a schedule armed, its process stopped: working, no pid, is gone", dict(state="working", status="idle"), True, "done"),
    ("working, no pid, no status: gone", dict(state="working"), True, "working"),
    ("stopped", dict(state="stopped"), True, "stopped"),
    ("failed", dict(state="failed", pid=1), True, "failed"),
    ("killed with a pid", dict(state="killed", status="busy", pid=1), True, "killed"),
    ("removed", dict(state="removed", pid=1, status="idle"), True, "removed"),
    ("working with a pid and no status: the state", dict(state="working", pid=1), False, "working"),
    ("an unknown status keeps the state", dict(state="working", status="thinking", pid=1), False, "working"),
    ("a null status keeps the state", dict(state="working", status=None, pid=1), False, "working"),
    ("an empty status keeps the state", dict(state="working", status="", pid=1), False, "working"),
    ("a null pid is still a pid key (has)", dict(state="working", status="busy", pid=None), False, "working"),
    ("no state, a pid: live, and jq -r prints null", dict(pid=1), False, "null"),
    ("no state, no pid: gone", dict(), True, "null"),
    ("state idle (not a state Claude Code lists): live with a pid", dict(state="idle", pid=1), False, "idle"),
]


def table_row(fields, i=0):
    r = {"id": "0000000a", "name": "s1", "cwd": "/w"}
    r.update(fields)
    return r


@test
def gone_and_turn_mapping():
    for label, fields, want_gone, want_turn in TABLE:
        row = table_row(fields)
        ck(f"gone: {label}", cc.gone(row) is want_gone, short(cc.gone(row)))
        if not want_gone:
            ck(f"turn: {label} -> {want_turn}", cc.jq_raw(cc.turn(row)) == want_turn, short(cc.turn(row)))
    for label, fields, want_gone, want_turn in TABLE:
        if want_gone and fields.get("state") in cc.DEAD_STATES:
            ck(f"turn of a dead state is the state: {label}", cc.turn(table_row(fields)) == want_turn)
    if not needs_fake("turn_state through the fake"):
        return
    for label, fields, want_gone, want_turn in TABLE:
        if fields.get("state") in (None, "stopped") and "pid" not in fields:
            continue  # the fake does not list these (no pid and not working/blocked)
        reset()
        row = table_row({k: v for k, v in fields.items()})
        agents(row)
        listed = [r["id"] for r in cc.active_json()]
        got = cc.turn_state("0000000a")
        want = "" if want_gone else want_turn
        if listed:
            ck(f"turn_state through claude: {label}", got == want, short(got))
    reset()
    agents(sess("0000000a"), sess("0000000b", "s2", status="busy"))
    ck("turn_state looks up by id: the other session's turn is its own", (cc.turn_state("0000000a"), cc.turn_state("0000000b")) == ("done", "working"))
    ck("an id nobody has gives ''", cc.turn_state("0000000f") == "" and cc.turn_state("") == "")
    with env(FAKE_CLAUDE_AGENTS_FAIL="1"):
        ck("turn_state on an unreadable list raises (bash prints nothing and exits non-zero)", attempt(cc.turn_state, "0000000a") == "SessionListUnreadableError")


# ============================================================ find_live_job and is_alive


@test
def find_live_job_picks_newest_live():
    if not needs_fake("find_live_job"):
        return
    reset()
    agents(sess("0000000a", "bob", "/w", startedAt=3), sess("0000000b", "bob", "/w", startedAt=9), sess("0000000c", "bob", "/w", startedAt=5),
           sess("0000000d", "bob", "/other", startedAt=99), sess("0000000e", "bob-build-x", "/w", startedAt=100),
           sess("0000000f", "bob", "/w", state_="stopped", status=None, pid=1, startedAt=200))
    ck("the newest live session by startedAt, whatever the list order", cc.find_live_job("bob", "/w") == "0000000b")
    ck("another folder is another session", cc.find_live_job("bob", "/other") == "0000000d")
    ck("an empty cwd means any folder (newest of all, the stopped one is not live)", cc.find_live_job("bob", "") == "0000000d")
    ck("the name is exact (bob is not bob-build-x)", cc.find_live_job("bob-build-x", "/w") == "0000000e" and cc.find_live_job("bo", "/w") == "")
    ck("no such name gives ''", cc.find_live_job("nobody", "/w") == "")
    agents(sess("0000000a", "bob", "/w", startedAt=1), sess("0000000b", "bob", "/w"), sess("0000000c", "bob", "/w"))
    ck("a session without startedAt sorts lowest (jq null), so the one that has it wins", cc.find_live_job("bob", "/w") == "0000000a")
    agents(sess("0000000b", "bob", "/w"), sess("0000000c", "bob", "/w"))
    ck("a tie takes the later row (jq's sort_by is stable and `last` is the end)", cc.find_live_job("bob", "/w") == "0000000c")
    agents(sess("0000000a", "bob", "/w", startedAt=1), sess("0000000b", "bob", "/w", state_="working", status="idle", pid=False, startedAt=50))
    ck("a schedule-armed session whose process is gone is not live, so the older live one is chosen", cc.find_live_job("bob", "/w") == "0000000a")
    agents(sess("0000000a", "bob", "/w", state_="blocked", status="waiting", pid=False, startedAt=7))
    ck("a blocked session with no pid is live", cc.find_live_job("bob", "/w") == "0000000a")
    reset()
    ck("an empty list gives ''", cc.find_live_job("bob", "/w") == "")
    agents(sess())
    with env(FAKE_CLAUDE_AGENTS_FAIL="1"):
        ck("an unreadable list raises; it is not taken for 'no session'", attempt(cc.find_live_job, "s1", "/w") == "SessionListUnreadableError")


@test
def is_alive_by_name_folder_or_id():
    if not needs_fake("is_alive"):
        return
    reset()
    station("crew-reviewer", id="0000000b", cwd=str(CREW))
    agents(sess("0000000b", "crew-reviewer", str(CREW)))
    ck("a listed station is alive (its recorded folder is the crew home)", cc.is_alive("crew-reviewer", paths=PATHS) is True)
    ck("the default folder is the crew home as typed", cc.is_alive("crew-reviewer", "", paths=PATHS))
    agents(sess("0000000c", "crew-reviewer", "/elsewhere"))
    ck("same name in another folder and a different id is another session", cc.is_alive("crew-reviewer", paths=PATHS) is False)
    ck("...but is alive when asked about that folder", cc.is_alive("crew-reviewer", "/elsewhere", paths=PATHS) is True)
    agents(sess("0000000b", "renamed", "/elsewhere"))
    ck("the recorded id finds it under another name and folder", cc.is_alive("crew-reviewer", paths=PATHS) is True)
    station("nobody-else", cwd="/w")
    ck("a station with no record and no session is not alive", cc.is_alive("ghost", paths=PATHS) is False)
    agents(sess("0000000b", "crew-reviewer", str(CREW), state_="working", status="idle", pid=False))
    ck("a schedule armed and the process stopped: not alive", cc.is_alive("crew-reviewer", paths=PATHS) is False)
    agents(sess("0000000b", "crew-reviewer", str(CREW), state_="blocked", status="waiting", pid=False))
    ck("waiting on a person with no process: alive", cc.is_alive("crew-reviewer", paths=PATHS) is True)
    agents(sess("0000000b", "crew-reviewer", str(CREW), state_="stopped", status=None, pid=7))
    ck("a stopped row that still has a pid is not alive", cc.is_alive("crew-reviewer", paths=PATHS) is False)
    state.stations_put("crew-orchestrator", {"cwd": "/orch"}, paths=PATHS)
    agents(sess("0000000d", "crew-orchestrator", "/orch"))
    ck("the folder recorded for the station is used when none is passed", cc.is_alive("crew-orchestrator", paths=PATHS) is True)
    ck("an id that does not match and a blank recorded id match nothing (a blank id never matches)",
       cc.is_alive("crew-orchestrator", "/not-it", paths=PATHS) is False)
    with env(FAKE_CLAUDE_AGENTS_FAIL="1"):
        try:
            cc.is_alive("crew-reviewer", paths=PATHS)
            got = None
        except SessionListUnreadableError as e:
            got = e
        ck("an unreadable list dies with bash's message",
           got is not None and str(got) == "could not read Claude Code's session list (claude agents --json); not guessing whether crew-reviewer runs", short(str(got)))
        ck("...as `tares factory: ...`", got is not None and got.line.startswith("tares factory: could not read"))


# ============================================================ transcript_of


@test
def transcript_of_finds_the_conversation():
    if not needs_fake("transcript_of"):
        return
    reset()
    proj = HOME / ".claude" / "projects"
    odd = [] if WIN else [("-w-d", "sess[x]*")]  # Windows cannot name a file with a *
    for p, s in (("-w-a", "sess-list"), ("-w-b", "sess-list"), ("-w-c", "sess-recorded"), *odd):
        (proj / p).mkdir(parents=True, exist_ok=True)
        (proj / p / f"{s}.jsonl").write_text("{}\n", encoding="utf-8")
    station("st", id="0000000a", sessionId="sess-recorded")
    agents(sess(sessionId="sess-list"))
    got = cc.transcript_of("st", paths=PATHS)
    ck("the session id on the station's row of the list is used first; the first project folder in sorted order",
       got == str(proj / "-w-a" / "sess-list.jsonl"), short(got))
    agents(sess())
    ck("a row without sessionId falls back to the recorded one", cc.transcript_of("st", paths=PATHS) == str(proj / "-w-c" / "sess-recorded.jsonl"))
    agents()
    ck("a station that is not listed uses the recorded session id", cc.transcript_of("st", paths=PATHS) == str(proj / "-w-c" / "sess-recorded.jsonl"))
    with env(FAKE_CLAUDE_AGENTS_FAIL="1"):
        ck("an unreadable list uses the recorded session id, no error", cc.transcript_of("st", paths=PATHS) == str(proj / "-w-c" / "sess-recorded.jsonl"))
    station("st2", id="0000000a", sessionId="nope")
    ck("a session with no transcript file gives ''", cc.transcript_of("st2", paths=PATHS) == "")
    ck("a station nobody recorded gives ''", cc.transcript_of("ghost", paths=PATHS) == "")
    if not WIN:
        station("st3", sessionId="sess[x]*")
        ck("glob characters in a session id are literal", cc.transcript_of("st3", paths=PATHS) == str(proj / "-w-d" / "sess[x]*.jsonl"))
    station("st4", sessionId="../-w-c/sess-recorded")
    ck("a session id with a path separator is refused (gives '')", cc.transcript_of("st4", paths=PATHS) == "")
    station("st5", sessionId="*")
    ck("a bare * does not match every transcript", cc.transcript_of("st5", paths=PATHS) == "")


# ============================================================ job_state, quiet, between_turns


def quiet_case(label, row, jobv, want, wait=None, now=None, between=None, **envv):
    """quiet() on one session and its job file."""
    reset()
    station("st", id="0000000a", cwd="/w")
    agents(row)
    if jobv is not None:
        job("0000000a", jobv)
    with env(**envv):
        got = cc.quiet("st", wait=wait, now=now, paths=PATHS)
        ck(f"quiet: {label} -> {want}", got is want, short(got))
        if between is not None:
            got2 = cc.between_turns("st", now=now, paths=PATHS)
            ck(f"between_turns: {label} -> {between}", got2 is between, short(got2))


@test
def job_state_reads_the_job_file():
    ck("a job with no file is {}", cc.job_state("0000000a") == {} and cc.job_state("") == {})
    reset()
    job("0000000a", {"sessionId": "s", "inFlight": {"tasks": 1}})
    ck("it parses ~/.claude/jobs/<id>/state.json under HOME", cc.job_state("0000000a") == {"sessionId": "s", "inFlight": {"tasks": 1}})
    job("0000000b", "\ufeff{\"a\": 1}")
    ck("a byte order mark is skipped", cc.job_state("0000000b") == {"a": 1})
    job("0000000c", "{\"half\": ")
    ck("a file that is not JSON raises JobStateUnreadableError, not {}", attempt(cc.job_state, "0000000c") == "JobStateUnreadableError")
    job("0000000d", "[1, 2]")
    ck("a file that is JSON but not an object raises too", attempt(cc.job_state, "0000000d") == "JobStateUnreadableError")
    ck("an id that is a path is not read", cc.job_state("../x") == {} and cc.job_state("a/b") == {} and cc.job_state("..") == {})
    src = (ROOT / "tares" / "crew" / "claude.py").read_text(encoding="utf-8")
    ck("job_state is marked as the state.json fallback in a comment and in its docstring",
       src.count("# STATE.JSON FALLBACK: fields claude agents --json does not give (updatedAt, inFlight)") >= 3)
    users = re.findall(r"^def (\w+)\(.*?(?=^def |\Z)", src, re.S | re.M)
    callers = sorted(n for n, body in ((m.group(1), m.group(0)) for m in re.finditer(r"^def (\w+)\(.*?(?=^def |\Z)", src, re.S | re.M))
                     if n != "job_state" and "job_state(" in re.sub(r'""".*?"""', "", body, flags=re.S) and not n.startswith("_"))
    ck("only quiet and station_updated read the job file", callers == ["quiet", "station_updated"] and users, short(callers))


@test
def quiet_matrix():
    if not needs_fake("quiet"):
        return
    idle = sess(status="idle")
    waiting = sess(state_="working", status="waiting")
    at = 1_800_000_000
    young, old = iso(60), iso(3600)
    quiet_case("done and nothing in flight, no job file", idle, None, True, between=True)
    quiet_case("done, an empty job file", idle, {}, True, between=True)
    quiet_case("busy in a turn", sess(status="busy"), {}, False, between=False)
    quiet_case("not listed", sess(name="other", id="0000000f"), {}, False, between=False)
    quiet_case("a stopped session", sess(state_="stopped", status=None, pid=1), {}, False)
    quiet_case("schedule armed, process gone", sess(pid=False), {}, False)
    quiet_case("a subagent running (local_agent)", idle, {"inFlight": {"tasks": 2, "queued": 0, "kinds": ["local_agent"]}}, False, between=False)
    quiet_case("a session cron only does not count", idle, {"inFlight": {"tasks": 1, "queued": 0, "kinds": ["session_cron"]}}, True, between=True)
    quiet_case("a session cron and a subagent", idle, {"inFlight": {"tasks": 2, "queued": 0, "kinds": ["local_agent", "session_cron"]}}, False)
    quiet_case("a session cron with its own wake-up armed", idle, {"inFlight": {"tasks": 1, "queued": 0, "kinds": ["session_cron"], "wake": {"at": 1, "keepalive": True}}}, False)
    quiet_case("a message queued", idle, {"inFlight": {"queued": 1}}, False)
    quiet_case("tasks running and no kinds named", idle, {"inFlight": {"tasks": 3}}, False)
    quiet_case("tasks 0, no kinds", idle, {"inFlight": {"tasks": 0, "queued": 0, "kinds": []}}, True)
    quiet_case("wake is null", idle, {"inFlight": {"wake": None}}, True)
    quiet_case("inFlight null", idle, {"inFlight": None}, True)
    quiet_case("inFlight is not an object", idle, {"inFlight": 3}, False)
    quiet_case("kinds is not a list", idle, {"inFlight": {"kinds": "local_agent"}}, False)
    quiet_case("queued as a float zero", idle, {"inFlight": {"queued": 0.0}}, True)
    quiet_case("queued is a string", idle, {"inFlight": {"queued": "0"}}, False)
    quiet_case("a job file that is broken is not quiet", idle, "{broken", False)
    quiet_case("waiting on a person, touched a minute ago", waiting, {"state": "working", "updatedAt": young}, False, between=True)
    quiet_case("waiting on a person for an hour", waiting, {"state": "working", "updatedAt": old}, True, between=True)
    quiet_case("waiting, no updatedAt (the wait never started)", waiting, {}, False, between=True)
    quiet_case("waiting, updatedAt is not a time", waiting, {"updatedAt": "yesterday"}, False, between=True)
    quiet_case("waiting, updatedAt is a number", waiting, {"updatedAt": 5}, False, between=False)
    quiet_case("waiting, no job file at all", waiting, None, False, between=True)
    quiet_case("blocked with no pid is still waiting on a person (an hour)", sess(state_="blocked", status="waiting", pid=False), {"updatedAt": old}, True)
    quiet_case("waiting an hour but a subagent running", waiting, {"updatedAt": old, "inFlight": {"kinds": ["local_agent"], "tasks": 1}}, False, between=False)
    quiet_case("FACTORY_QUIET_WAIT=0: a minute is enough", waiting, {"updatedAt": young}, True, FACTORY_QUIET_WAIT="0")
    quiet_case("FACTORY_QUIET_WAIT=7200: an hour is not enough (between_turns ignores it)", waiting, {"updatedAt": old}, False, between=True, FACTORY_QUIET_WAIT="7200")
    quiet_case("FACTORY_QUIET_WAIT=30 and 60 s ago", waiting, {"updatedAt": young}, True, FACTORY_QUIET_WAIT="30")
    quiet_case("FACTORY_QUIET_WAIT=abc is not a number: not quiet", waiting, {"updatedAt": old}, False, FACTORY_QUIET_WAIT="abc")
    quiet_case("an empty FACTORY_QUIET_WAIT is the default 1800", waiting, {"updatedAt": old}, True, FACTORY_QUIET_WAIT="")
    quiet_case("the wait given as an argument beats the variable", waiting, {"updatedAt": young}, True, wait=10, FACTORY_QUIET_WAIT="99999")
    t0 = cc.iso_epoch("2026-01-01T00:00:00Z")
    stamp = "2026-01-01T00:00:00.456Z"
    quiet_case("the default wait is 1800 s: 1799 s is too soon", waiting, {"updatedAt": stamp}, False, now=t0 + 1799)
    quiet_case("...and 1800 s is enough", waiting, {"updatedAt": stamp}, True, now=t0 + 1800)
    quiet_case("a done session needs no wait", idle, {"updatedAt": stamp}, True, now=t0)
    del at
    reset()
    station("st", id="0000000a")
    ck("a station with no recorded id is not quiet", cc.quiet("st", paths=PATHS) is False and cc.between_turns("st", paths=PATHS) is False)
    ck("a station nobody recorded is not quiet", cc.quiet("ghost", paths=PATHS) is False)


@test
def between_turns_is_quiet_with_no_wait():
    if not needs_fake("between_turns"):
        return
    reset()
    station("st", id="0000000a")
    agents(sess(state_="working", status="waiting"))
    job("0000000a", {"updatedAt": iso(5)})
    ck("blocked a moment ago: quiet waits, between_turns does not", cc.quiet("st", paths=PATHS) is False and cc.between_turns("st", paths=PATHS) is True)
    with env(FACTORY_QUIET_WAIT="99999"):
        ck("the variable does not change between_turns", cc.between_turns("st", paths=PATHS) is True)
    agents(sess(status="busy"))
    ck("working: not between turns", cc.between_turns("st", paths=PATHS) is False)
    agents(sess(status="idle"))
    job("0000000a", {"inFlight": {"kinds": ["monitor"], "tasks": 1}})
    ck("done with a background task in flight: not between turns", cc.between_turns("st", paths=PATHS) is False)
    job("0000000a", {"inFlight": {"kinds": ["session_cron"], "tasks": 1}})
    ck("done with only its schedule: between turns", cc.between_turns("st", paths=PATHS) is True)
    with env(FAKE_CLAUDE_AGENTS_FAIL="1"):
        ck("an unreadable list: not between turns", cc.between_turns("st", paths=PATHS) is False)


# ============================================================ station_updated


@test
def station_updated_reads_updatedAt():
    reset()
    station("st", id="0000000a")
    job("0000000a", {"updatedAt": "2026-05-01T09:30:00.123Z"})
    want = int(datetime.datetime(2026, 5, 1, 9, 30, 0, tzinfo=datetime.timezone.utc).timestamp())
    ck("the time of updatedAt in epoch seconds, fraction dropped", cc.station_updated("st", paths=PATHS) == want, short(cc.station_updated("st", paths=PATHS)))
    job("0000000a", {"updatedAt": "2026-05-01T09:30:00Z"})
    ck("a time without a fraction", cc.station_updated("st", paths=PATHS) == want)
    for label, v in (("no updatedAt", {}), ("a null one", {"updatedAt": None}), ("not a time", {"updatedAt": "soon"}),
                     ("an offset instead of Z", {"updatedAt": "2026-05-01T09:30:00+02:00"}), ("a number", {"updatedAt": 1}),
                     ("month 13", {"updatedAt": "2026-13-01T09:30:00Z"}), ("a broken file", "{no")):
        job("0000000a", v)
        ck(f"unknown ({label}): None", cc.station_updated("st", paths=PATHS) is None)
    ck("a station with no id or no job file: None", cc.station_updated("ghost", paths=PATHS) is None)
    ck("iso_epoch of a leap day", cc.iso_epoch("2024-02-29T23:59:59Z") == 1709251199)
    ck("iso_epoch rolls Feb 30 over to Mar 2, as jq does", cc.iso_epoch("2026-02-30T00:00:00Z") == cc.iso_epoch("2026-03-02T00:00:00Z"))
    ck("iso_epoch refuses day 32, hour 24, minute 60, second 61 and a year before 1970",
       all(cc.iso_epoch(x) is None for x in ("2026-01-32T00:00:00Z", "2026-01-01T24:00:00Z", "2026-01-01T00:60:00Z", "2026-01-01T00:00:61Z", "1969-12-31T23:59:59Z")))
    ck("iso_epoch takes a leap second as jq does", cc.iso_epoch("2026-01-01T00:00:60Z") == cc.iso_epoch("2026-01-01T00:01:00Z"))
    if not shutil.which("jq") or WIN:
        skip("station_updated against jq", "jq is missing")
        return
    bad = []
    for v in ("2026-05-01T09:30:00.123Z", "2026-05-01T09:30:00Z", "1999-12-31T23:59:59.9Z", "2038-01-19T03:14:08.000000Z", "soon",
              "2026-05-01 09:30:00", "2026-05-01T09:30:00+02:00", "2026-13-01T09:30:00Z", "2026-02-30T00:00:00Z", "2026-04-31T00:00:00Z", "2026-01-32T00:00:00Z", "2026-01-01T24:00:00Z", "2026-01-01T00:00:60Z", "2026-01-01T00:00:61Z", "", "1970-01-01T00:00:00Z"):
        out = subprocess.run(["jq", "-r", '((.updatedAt // "") | sub("\\\\.[0-9]+"; "") | fromdateiso8601?) // empty'], input=json.dumps({"updatedAt": v}),
                             capture_output=True, text=True).stdout.strip()
        job("0000000a", {"updatedAt": v})
        got = cc.station_updated("st", paths=PATHS)
        if out != ("" if got is None else str(got)):
            bad.append((v, out, got))
    ck("station_updated gives what the bash jq expression gives on the same updatedAt values", not bad, short(bad))


# ============================================================ attached and running_version (real processes)


PY_VERSIONS = "/versions/" in sys.executable.replace("\\", "/")
HOLDER = ("import subprocess, sys, json, time\n"
          "spec = json.loads(sys.stdin.readline())\n"
          "c = subprocess.Popen(spec['args'], executable=sys.executable, stdin=subprocess.DEVNULL, "
          "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
          "print(c.pid, flush=True)\n"
          "time.sleep(300)\n")
SLEEP = "import time; time.sleep(300)"
procs = []


def spawn(extra_args, parent_extra=None, with_child=False):
    """A process that sleeps and has these extra arguments on its command line. With a child, the parent is a
    holder with parent_extra and the child has extra_args; returns (parent pid, child pid)."""
    if not with_child:
        p = subprocess.Popen([sys.executable, "-c", SLEEP, *extra_args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        return p.pid
    p = subprocess.Popen([sys.executable, "-c", HOLDER, *(parent_extra or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    procs.append(p)
    p.stdin.write(json.dumps({"args": [sys.executable, "-c", SLEEP, *extra_args]}) + "\n")
    p.stdin.flush()
    child = int(p.stdout.readline())
    return p.pid, child


def stop_procs():
    for p in procs:
        try:
            for ch in psutil.Process(p.pid).children(recursive=True):
                ch.kill()
            p.kill()
            p.wait(timeout=10)
        except Exception:
            pass
    del procs[:]
    time.sleep(0.3)  # the children of a holder go with it, a moment later


def settle(pid):
    """Wait until psutil can read the command line of pid (a process just started may not show it yet)."""
    for _ in range(100):
        if cc._command_line(pid):
            return
        time.sleep(0.05)


@test
def attached_sees_a_terminal_session():
    reset()
    station("st", id="0000000a")
    station("noid", role="builder")
    ck("nobody has it open: not attached", cc.attached("st", paths=PATHS) is False)
    ck("a station with no recorded id is not attached", cc.attached("noid", paths=PATHS) is False and cc.attached("ghost", paths=PATHS) is False)
    try:
        pid = spawn(["attach", "0000000a"])
        settle(pid)
        ck("a process running `... attach <id>` means attached", cc.attached("st", paths=PATHS) is True)
        station("other", id="0000000b")
        ck("...but not for another station", cc.attached("other", paths=PATHS) is False)
        pid2 = spawn(["attach", "0000000b", "--read-only"])
        settle(pid2)
        ck("the id may be followed by more arguments", cc.attached("other", paths=PATHS) is True)
        pid3 = spawn(["attach", "0000000c0"])
        settle(pid3)
        station("c", id="0000000c")
        ck("an id that only starts the same ('0000000c0') is not it (pgrep -f 'attach <id>( |$)')", cc.attached("c", paths=PATHS) is False)
        pid4 = spawn(["reattach", "0000000d"])
        settle(pid4)
        station("d", id="0000000d")
        ck("`reattach <id>` still holds `attach <id>`, as with pgrep -f", cc.attached("d", paths=PATHS) is True)
        pid5 = spawn(["agents", "0000000e"])
        settle(pid5)
        station("e", id="0000000e")
        ck("a process that names the id without `attach` is not it", cc.attached("e", paths=PATHS) is False)
    finally:
        stop_procs()
    ck("once the process is gone it is not attached any more", cc.attached("st", paths=PATHS) is False and cc.attached("other", paths=PATHS) is False)
    station("dots", id="0000000.")
    try:
        pid = spawn(["attach", "0000000a"])
        settle(pid)
        ck("a recorded id with a '.' does not match any character", cc.attached("dots", paths=PATHS) is False)
    finally:
        stop_procs()


@test
def running_version_from_the_list_or_the_process():
    if not needs_fake("running_version"):
        return
    reset()
    agents(sess(version="2.1.300", pid=os.getpid()), sess("0000000b", "s2", pid=os.getpid()), sess("0000000c", "s3", pid=False, version=None))
    ck("the list's version is used when it has one", cc.running_version("0000000a") == "2.1.300")
    ck("...and a list passed in is used, not read again", cc.running_version("0000000a", [{"id": "0000000a", "version": "9.9.9"}]) == "9.9.9")
    ck("an empty id gives ''", cc.running_version("") == "")
    ck("an id not in the list gives ''", cc.running_version("0000000f") == "")
    ck("no version and no pid gives ''", cc.running_version("0000000c") == "")
    ck("a numeric version in the list is written as jq would", cc.running_version("0000000a", [{"id": "0000000a", "version": 2.1}]) == "2.1")
    ck("a pid that is not a process gives ''", cc.running_version("0000000a", [{"id": "0000000a", "pid": 2 ** 22 + 12345}]) == "")
    ck("an empty list passed in is still the list (no id in it)", cc.running_version("0000000a", []) == "")
    with env(FAKE_CLAUDE_AGENTS_FAIL="1"):
        ck("an unreadable list gives '' (bash: return 0 with no output)", cc.running_version("0000000a") == "")
    if PY_VERSIONS:
        skip("running_version from a process command line", "this Python's own path holds /versions/, which would be found first")
        return
    try:
        parent, child = spawn(["bg-spare"], ["claude", "bg-pty-host", "--bg-pty-host", "/tmp/cc/spare/x.pty.sock", "200", "50", "--",
                                              "/home/u/.local/share/claude/versions/2.1.311", "--bg-spare", "/tmp/cc/spare/x.claim.sock"], True)
        settle(parent)
        settle(child)
        ck("a bg-spare pid: the version is in its parent's command line (.../versions/<v>)",
           cc.running_version("0000000a", [{"id": "0000000a", "pid": child}]) == "2.1.311", short(cc._command_line(parent)))
        ck("...and the list's version still wins over the process", cc.running_version("0000000a", [{"id": "0000000a", "pid": child, "version": "2.1.400"}]) == "2.1.400")
        ck("the pid may be a string in the list", cc.running_version("0000000a", [{"id": "0000000a", "pid": str(child)}]) == "2.1.311")
        parent2, child2 = spawn(["/opt/claude/versions/2.1.312/claude", "--bg-spare"], ["holder", "x"], True)
        settle(parent2)
        settle(child2)
        ck("the parent is tried first; a parent without a version leaves the pid's own command line",
           cc.running_version("0000000a", [{"id": "0000000a", "pid": child2}]) == "2.1.312")
        parent3, child3 = spawn(["/a/versions/1.0.0/claude"], ["/b/versions/2.0.0/claude"], True)
        settle(parent3)
        settle(child3)
        ck("when both name a version the parent's is taken (bash tries the parent first)",
           cc.running_version("0000000a", [{"id": "0000000a", "pid": child3}]) == "2.0.0")
        parent4, child4 = spawn(["bg-spare"], ["holder"], True)
        settle(parent4)
        settle(child4)
        ck("neither names a version: ''", cc.running_version("0000000a", [{"id": "0000000a", "pid": child4}]) == "")
        pid5 = spawn(["C:\\Program Files\\claude\\versions\\2.1.313\\claude.exe"])
        settle(pid5)
        ck("a Windows path in the command line names the version too", cc.running_version("0000000a", [{"id": "0000000a", "pid": pid5}]) == "2.1.313")
        pid6 = spawn(["/x/versions/v2/claude"])
        settle(pid6)
        ck("/versions/ must be followed by a digit", cc.running_version("0000000a", [{"id": "0000000a", "pid": pid6}]) == "")
        pid7 = spawn(["/x/versions/2.1.314-rc.1/bin/claude"])
        settle(pid7)
        ck("a pre-release version is read up to the next slash", cc.running_version("0000000a", [{"id": "0000000a", "pid": pid7}]) == "2.1.314-rc.1")
    finally:
        stop_procs()


# ============================================================ older_than and claude_version


OLDER = [
    ("2.1.9", "2.1.10", True), ("2.1.10", "2.1.9", False), ("2.1.310", "2.1.310", False), ("2.1.309", "2.1.310", True),
    ("2.1.311", "2.1.310", False), ("2.0.99", "2.1.0", True), ("2.10.0", "2.9.0", False), ("1.9.9", "2.0.0", True),
    ("", "2.1.0", False), ("2.1.0", "", False), ("", "", False),
    ("2.1.0", "2.1.0-rc.1", True), ("2.1.0-rc.1", "2.1.0", False), ("2.1.0-rc.1", "2.1.0-rc.2", True),
    ("2.1.0-rc.10", "2.1.0-rc.9", False), ("2.1", "2.1.0", True), ("2.1.0", "2.1", False), ("2.1.0~rc1", "2.1.0", True),
    ("2.1.1", "2.1.0-rc.1", False), ("2.1.0a", "2.1.0b", True), ("2.1.01", "2.1.1", True), ("2.1.1", "2.1.01", False),
]


@test
def older_than_is_sort_v():
    for v, t, want in OLDER:
        ck(f"older_than({v!r}, {t!r}) is {want}", cc.older_than(v, t) is want)
    ck("unknown is never older, either way", not cc.older_than("", "9.9.9") and not cc.older_than("9.9.9", ""))
    sortv = subprocess.run(["sort", "-V"], input="1\n2\n", capture_output=True, text=True) if shutil.which("sort") and not WIN else None
    if sortv is None or sortv.returncode != 0 or sortv.stdout != "1\n2\n":
        skip("older_than against sort -V", "no sort -V here")
        return
    vs = sorted({x for a, b, _ in OLDER for x in (a, b) if x} | {"2.1.319", "2.1.0.1", "3", "10.0.0", "2.1.0+build", "2.0.0-alpha.1", "2.0.0-alpha.10", "2.0.0-alpha.2"})
    bad = []
    senv = {**os.environ, "LC_ALL": "C"}
    for a in vs:
        for b in vs:
            first = subprocess.run(["sort", "-V"], input=f"{a}\n{b}\n", capture_output=True, text=True, env=senv).stdout.split("\n")[0]
            if cc.older_than(a, b) != (a != b and first == a):
                bad.append((a, b))
    ck(f"older_than agrees with `sort -V | head -1` on all {len(vs) * len(vs)} pairs of {len(vs)} versions", not bad, short(bad[:6]))


@test
def claude_version_reads_the_first_line():
    if not needs_fake("claude_version"):
        return
    reset()
    ck("the fake's default version", cc.claude_version() == "0.0.0")
    for text, want in (("2.1.319", "2.1.319"), ("2.1.310-rc.1", "2.1.310-rc.1"), ("10.20.30.40", "10.20.30.40")):
        (STATE / "version").write_text(text + "\n", encoding="utf-8")
        ck(f"`claude --version` = '{text} (Claude Code)' gives {want}", cc.claude_version() == want, short(cc.claude_version()))
    (STATE / "version").write_text("banana\n", encoding="utf-8")
    ck("a version line that does not start with digits and dots gives ''", cc.claude_version() == "")
    (STATE / "version").write_text("7\n", encoding="utf-8")
    ck("one number alone is not a version (needs a dot)", cc.claude_version() == "")
    f = T / "bad" / "v.py"
    f.parent.mkdir(exist_ok=True)
    for label, code in (("claude fails", "raise SystemExit(1)"), ("claude prints nothing", "pass"), ("claude prints a banner first", "print('welcome'); print('2.1.1 (Claude Code)')")):
        f.write_text(code + "\n", encoding="utf-8")
        got = cc.claude_version(make_launcher(T / "bad" / "v", "claude", f))
        ck(f"{label}: ''", got == "", short(got))
    ck("claude that cannot start: ''", cc.claude_version(str(T / "none here" / "claude")) == "")
    f.write_text("print('2.5.6 (Claude Code)')\nraise SystemExit(0)\n", encoding="utf-8")
    got = cc.claude_version(make_launcher(T / "bad" / "v2", "claude", f))
    ck("a claude that is given as an argument is the one run", got == "2.5.6", short(got))


# ============================================================ bash as the oracle


def cut_bash():
    """The bash harness: the functions cut out of the real script by text."""
    src = FACTORY.read_text(encoding="utf-8")
    lines = src.split("\n")

    def func(name):
        start = next(i for i, ln in enumerate(lines) if ln.startswith(name + "() {"))
        ln = lines[start].rstrip()
        if re.match(r"^\w+\(\) \{ [^#].*\}( #.*)?$", ln):
            return ln
        i = start
        while lines[i].rstrip() != "}":
            i += 1
        return "\n".join(lines[start:i + 1])

    def assign(name):
        return re.search(r"^%s=(['\"]).*?\1" % name, src, re.S | re.M).group(0)

    body = "\n".join([
        "set -uo pipefail",
        f'CREW_HOME={CREW.as_posix()!r}', 'STATIONS="$CREW_HOME/stations.json"', f"CLAUDE={FAKE!r}",
        'die() { echo "tares factory: $*" >&2; exit 1; }',
        assign("UNREAD"), assign("GONE"), assign("TURN"),
        *[func(n) for n in ("stations_get", "active_json", "job_state", "turn_state", "find_live_job", "is_alive", "quiet", "between_turns")],
        '"$@"', ""])
    f = T / "oracle.sh"
    f.write_text(body, encoding="utf-8")
    return str(f)


def bash_call(oracle, args, **extra):
    e = {**os.environ, **extra}
    r = subprocess.run(["bash", oracle, *args], capture_output=True, text=True, encoding="utf-8", env=e, stdin=subprocess.DEVNULL, timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# (label, rows, job file for 0000000a, environment). The rows are what the fake lists.
SCEN = [
    ("idle live", [sess()], None, {}),
    ("busy", [sess(status="busy")], {}, {}),
    ("waiting for a person, just now", [sess(status="waiting")], {"updatedAt": "NOW-30"}, {}),
    ("waiting for an hour", [sess(status="waiting")], {"updatedAt": "NOW-3600"}, {}),
    ("waiting an hour, wait var 99999", [sess(status="waiting")], {"updatedAt": "NOW-3600"}, {"FACTORY_QUIET_WAIT": "99999"}),
    ("waiting, wait var 0", [sess(status="waiting")], {"updatedAt": "NOW-30"}, {"FACTORY_QUIET_WAIT": "0"}),
    ("waiting, no updatedAt", [sess(status="waiting")], {}, {}),
    ("blocked no pid", [sess(state_="blocked", status=None, pid=False)], {"updatedAt": "NOW-4000"}, {}),
    ("blocked no pid, no job", [sess(state_="blocked", status="waiting", pid=False)], None, {}),
    ("schedule armed, process gone", [sess(pid=False)], {}, {}),
    ("stopped with pid", [sess(state_="stopped", status=None, pid=9)], {}, {}),
    ("failed", [sess(state_="failed", status="idle", pid=9)], {}, {}),
    ("a subagent", [sess()], {"inFlight": {"tasks": 2, "queued": 0, "kinds": ["local_agent"]}}, {}),
    ("cron only", [sess()], {"inFlight": {"tasks": 1, "queued": 0, "kinds": ["session_cron"]}}, {}),
    ("cron and subagent", [sess()], {"inFlight": {"tasks": 2, "queued": 0, "kinds": ["local_agent", "session_cron"]}}, {}),
    ("cron with wake", [sess()], {"inFlight": {"tasks": 1, "kinds": ["session_cron"], "wake": {"at": 1}}}, {}),
    ("queued", [sess()], {"inFlight": {"queued": 2}}, {}),
    ("tasks and no kinds", [sess()], {"inFlight": {"tasks": 1}}, {}),
    ("empty kinds, no tasks", [sess()], {"inFlight": {"kinds": [], "tasks": 0}}, {}),
    ("inFlight null", [sess()], {"inFlight": None}, {}),
    ("job file broken", [sess()], "{oops", {}),
    ("wait var not a number", [sess(status="waiting")], {"updatedAt": "NOW-4000"}, {"FACTORY_QUIET_WAIT": "soon"}),
    ("no status, working with pid", [sess(status=None)], {}, {}),
    ("unknown status", [sess(status="thinking")], {}, {}),
    ("no state at all, with pid", [{"id": "0000000a", "name": "s1", "cwd": "/w", "pid": 5}], {}, {}),
    ("two with one name, newest wins", [sess(startedAt=5), sess("0000000b", startedAt=9), sess("0000000c", startedAt=7)], None, {}),
    ("newest is gone", [sess(startedAt=5), sess("0000000b", pid=False, startedAt=9)], None, {}),
    ("other folder", [sess(cwd="/elsewhere")], None, {}),
    ("another name, same id", [sess(name="renamed")], None, {}),
    ("empty list", [], None, {}),
    ("list unreadable", [sess()], None, {"FAKE_CLAUDE_AGENTS_FAIL": "1"}),
    ("unicode name", [sess(name="Zoë 名前")], None, {}),
]


def bash_state_text(code, out):
    return f"rc{code}:{out}"


@test
def bash_oracle_matrix():
    if WIN or not (shutil.which("bash") and shutil.which("jq")) or not needs_fake("the bash oracle"):
        skip("the bash oracle", "bash or jq is missing, or this is Windows")
        return
    oracle = cut_bash()
    bad = []
    n = 0
    for label, rows, jobv, ev in SCEN:
        reset()
        now = time.time()
        rows = json.loads(json.dumps(rows))
        agents(*rows)
        station("s1", id="0000000a", cwd="/w")
        station("unlisted", id="0000000f")
        if jobv is not None:
            if isinstance(jobv, dict) and isinstance(jobv.get("updatedAt"), str) and jobv["updatedAt"].startswith("NOW-"):
                jobv = {**jobv, "updatedAt": iso(int(jobv["updatedAt"][4:]))}
            job("0000000a", jobv)
        names = ["s1", "renamed", "Zoë 名前"]
        pyenv = {k: ev.get(k) for k in set(ev)}
        with env(**pyenv):
            def py(fn, *a, **k):
                try:
                    return fn(*a, **k)
                except SessionListUnreadableError:
                    return "ERR"

            def b_str(args):
                code, out, err = bash_call(oracle, args, **ev)
                return "ERR" if code != 0 and out == "" else out

            def b_bool(args):
                code, out, err = bash_call(oracle, args, **ev)
                return code == 0

            checks = [("turn_state", py(cc.turn_state, "0000000a"), b_str(["turn_state", "0000000a"]))]
            for nm in names:
                checks.append((f"find_live_job {nm} /w", py(cc.find_live_job, nm, "/w"), b_str(["find_live_job", nm, "/w"])))
                checks.append((f"find_live_job {nm} ''", py(cc.find_live_job, nm, ""), b_str(["find_live_job", nm, ""])))
            a_py = py(cc.is_alive, "s1", paths=PATHS)
            code, out, err = bash_call(oracle, ["is_alive", "s1"], **ev)
            a_b = "ERR" if code != 0 and "not guessing whether s1 runs" in err else code == 0
            checks.append(("is_alive s1", a_py, a_b))
            for stn in ("unlisted", "s1"):
                for fnn, pyfn in (("quiet", cc.quiet), ("between_turns", cc.between_turns)):
                    pyv = pyfn(stn if stn != "unlisted" else "unlisted", paths=PATHS)
                    checks.append((f"{fnn} {stn}", pyv, b_bool([fnn, stn])))
        for what, got, want in checks:
            n += 1
            if got != want:
                bad.append((label, what, got, want))
    ck(f"turn_state, find_live_job, is_alive, quiet and between_turns give bash's answers on {len(SCEN)} scenarios ({n} comparisons)", not bad, short(bad[:5]))


# ============================================================ main

if __name__ == "__main__":
    names = set(sys.argv[1:])
    unknown = names - {t.__name__ for t in TESTS}
    if unknown:
        print("unknown test: " + ", ".join(sorted(unknown)))
        sys.exit(2)
    for t in TESTS:
        if names and t.__name__ not in names:
            continue
        print(f"\n== {t.__name__} ==")
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            ck(f"{t.__name__} ran to the end", False, f"{type(e).__name__}: {e}")
        finally:
            stop_procs()
    shutil.rmtree(T, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
