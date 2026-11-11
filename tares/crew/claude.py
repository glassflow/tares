"""The process layer: finding Claude Code, reading its sessions, and telling when a station is quiet.

Ports these functions of `claude-plugin/bin/factory`, with the same answers: claude_bin, active_json,
job_state, turn_state, parse_bg_id, find_live_job, is_alive, transcript_of, quiet, between_turns,
attached, running_version, and (the working doc of TR-950 lists them) station_updated, claude_version,
older_than. Where this module and the bash differ, the bash is right; every difference is listed under
"Differences from bash" below.

How it reads things
  * Sessions are read only through `claude agents --json` (active_json). A list that cannot be read is
    never taken for an empty one: active_json raises SessionListUnreadableError, and is_alive turns that
    into the die message bash prints. turn_state and find_live_job let it through where bash prints
    nothing and exits non-zero; quiet and between_turns answer False, as bash does.
  * STATE.JSON FALLBACK: `claude agents --json` has no updatedAt and no inFlight (queued, wake, kinds,
    tasks), so job_state reads ~/.claude/jobs/<id>/state.json for them. Only quiet and station_updated
    use it.
  * Processes (a pid's command line and parent, who has a session open) are read with psutil. Nothing
    here sends a signal to ask whether a process lives.
  * Every `claude` call is UTF-8 text with undecodable bytes replaced, NO_COLOR=1, no stdin, a timeout,
    and on Windows no console window. No secret goes in an argument list, and nothing is logged.

Finding `claude`
  claude_bin: CLAUDE_BIN wins as typed. Else the first `claude` on PATH that is not a terminal shim (a
  file that starts with `#!` and mentions cmux), then the same in the known install folders (a GUI app
  has no shell PATH). Windows looks for claude.exe, then claude.cmd (CreateProcess does not apply
  PATHEXT, so each name is tried by hand); a .cmd runs through `cmd.exe /d /s /c "..."` as one command
  line, and a path or argument that cmd would act on (% ^ & | < > a quote, a line break) is refused
  instead of quoted. No shell=True anywhere.

Untested on real Windows
  The Windows branches take the platform as a parameter so tests/test_crew_claude.py runs them on any
  machine (tests named windows_branch_*). They check the choice of file, the install folders and the
  command line built for a .cmd. They do not start claude.exe, run cmd.exe, or pass CREATE_NO_WINDOW to a
  real CreateProcess; the windows-crew CI job runs the script on Windows, but nothing there runs a real
  Claude Code, and psutil's Windows behaviour for command lines is taken on trust.

Differences from bash
  * claude_bin also looks in the known install folders after PATH, and on Windows for claude.exe and
    claude.cmd. It skips empty PATH entries (bash's `which` reads one as the current folder).
  * transcript_of takes the session id from the job's state.json first, as bash does (TR-1155: after a
    /clear it names the new session, and the list may still show the old one), then from the station's
    row of `claude agents --json`, then from the station's recorded sessionId.
  * active_json and job_state raise instead of returning non-zero; station_updated gives None, not "",
    for an unknown time; the other values are the same text bash prints.
  * attached and running_version read process command lines with psutil, not pgrep and ps: a process
    this user may not inspect is skipped, as it would be invisible to those too.
  * station_updated (iso_epoch) takes updatedAt as jq's fromdateiso8601 does for a UTC time, with a day 1..31
    rolled over by the month (Feb 30 is Mar 2) and a leap second (second 61 too off macOS, as glibc does); a time before 1970 is unknown. A libc that
    accepts a day 0, as macOS's does, is not copied.
  * running_version also accepts a Windows path (`\\versions\\<v>`) in a command line.

Standard library and psutil only (no duckdb, fastapi, tares.daemon or tares.store). Not imported by
tares.crew, so `import tares.crew` stays free of psutil; import it as `tares.crew.claude`.
"""
from __future__ import annotations

import calendar
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

from .errors import (ClaudeNotFoundError, CrewError, JobStateUnreadableError,
                     SessionListUnreadableError)
from .paths import CrewPaths
from .state import jq_raw, loads_json, read_text, stations_get

UNREAD = "could not read Claude Code's session list (claude agents --json)"

AGENTS_TIMEOUT = 60      # seconds `claude agents --json` may take
VERSION_TIMEOUT = 30     # seconds `claude --version` may take
DEFAULT_QUIET_WAIT = 1800  # seconds a session waiting on a person must have waited before it counts as quiet

# The states `claude agents --json` gives a session that is over (bash's GONE).
DEAD_STATES = ("stopped", "failed", "killed", "removed")
# The list's `status` for a live session, as the turn it is in (bash's TURN).
TURN_OF_STATUS = {"idle": "done", "busy": "working", "waiting": "blocked"}

_CMD_UNSAFE = re.compile(r'[%^&|<>"\r\n\x00]')
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BG_ID = re.compile(r"backgrounded[^0-9a-f\n]+[0-9a-f]{8}")
_VERSION_WORD = re.compile(r"^[0-9]+(\.[0-9]+)+[^ ]*")
_VERSIONS_IN_COMMAND = re.compile(r"[/\\]versions[/\\]([0-9][^ /\\]*)")
_ISO_SECONDS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")


def _platform(platform) -> str:
    return sys.platform if platform is None else platform


def _is_windows(platform=None) -> bool:
    return _platform(platform) == "win32"


def _home(env=None) -> str:
    """$HOME first, as bash has it, else the user's profile folder."""
    env = os.environ if env is None else env
    return env.get("HOME") or os.path.expanduser("~")


# ---------- finding claude ----------


def known_install_dirs(env=None, platform=None) -> list:
    """Where Claude Code is installed that a GUI app's PATH may not list, most likely first."""
    env = os.environ if env is None else env
    if _is_windows(platform):
        profile = env.get("USERPROFILE") or env.get("HOME") or ""
        local = env.get("LOCALAPPDATA") or (os.path.join(profile, "AppData", "Local") if profile else "")
        roaming = env.get("APPDATA") or (os.path.join(profile, "AppData", "Roaming") if profile else "")
        dirs = [os.path.join(profile, ".local", "bin") if profile else "",
                os.path.join(local, "Programs", "claude") if local else "",
                os.path.join(roaming, "npm") if roaming else ""]
    else:
        home = _home(env)
        dirs = [os.path.join(home, ".local", "bin"), os.path.join(home, ".claude", "local"),
                "/opt/homebrew/bin", "/usr/local/bin", os.path.join(home, ".npm-global", "bin")]
    return [d for d in dirs if d]


def _executable(path: str, platform=None) -> bool:
    if not os.path.isfile(path):
        return False
    return True if _is_windows(platform) else os.access(path, os.X_OK)


def claude_candidates(env=None, platform=None) -> list:
    """Every `claude` that could run, in the order tried: PATH, then the known install folders; a path once.
    (bash: `which -a claude | awk '!seen[$0]++'`.) Windows tries claude.exe, then claude.cmd, in each folder."""
    env = os.environ if env is None else env
    win = _is_windows(platform)
    names = ("claude.exe", "claude.cmd") if win else ("claude",)
    dirs = [d.strip('"') for d in (env.get("PATH") or "").split(";" if win else ":")]
    dirs = [d for d in dirs if d] + known_install_dirs(env, platform)
    seen, out = set(), []
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if p not in seen:
                seen.add(p)
                if _executable(p, platform):
                    out.append(p)
    return out


def is_terminal_shim(path: str) -> bool:
    """A script (it starts with `#!`) that mentions cmux. Such a shim puts temp --settings files and socket
    tokens into the launch flags Claude Code saves for respawn, which later breaks the respawn and leaks the token."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"#!":
                return False
            return b"cmux" in f.read()
    except OSError:
        return False


def claude_bin(env=None, platform=None) -> str:
    """The real claude binary: CLAUDE_BIN as typed, else the first candidate that is not a terminal shim.
    Raises ClaudeNotFoundError ("claude not found on PATH (set CLAUDE_BIN to its path)")."""
    env = os.environ if env is None else env
    if env.get("CLAUDE_BIN"):
        return env["CLAUDE_BIN"]
    for c in claude_candidates(env, platform):
        if not is_terminal_shim(c):
            return c
    raise ClaudeNotFoundError()


# Windows refuses a command line over 32767 characters (CreateProcess), and cmd.exe one over 8191 (a .cmd or
# .bat shim). A start with a long task in its prompt could pass 8191; it is refused by name, not cut off.
WIN_LINE_MAX = 32767
CMD_LINE_MAX = 8191


def _check_length(claude: str, args) -> None:
    line = subprocess.list2cmdline([claude, *args])
    limit = CMD_LINE_MAX if claude.lower().endswith((".cmd", ".bat")) else WIN_LINE_MAX
    if len(line) > limit:
        raise CrewError(f"the command line to start Claude Code is {len(line)} characters long and Windows allows "
                        f"{limit}; shorten the task in the prompt, or put it in a file the station can read")


def claude_command(claude: str, args, platform=None, env=None):
    """What to hand subprocess.run to run `claude <args>`. A list, except for a Windows .cmd or .bat, which
    cmd.exe must run: one command line string, `cmd.exe /d /s /c "<quoted path and arguments>"`. A path or
    argument cmd would act on (% ^ & | < > a quote, a line break) is refused, not quoted."""
    args = [str(a) for a in args]
    if _is_windows(platform):
        _check_length(claude, args)
    if not (_is_windows(platform) and claude.lower().endswith((".cmd", ".bat"))):
        return [claude, *args]
    env = os.environ if env is None else env
    for part in (claude, *args):
        if _CMD_UNSAFE.search(part):
            raise CrewError(f"cannot run {claude} through cmd.exe with {part!r} in the command line "
                            "(it holds a character cmd acts on: % ^ & | < > a quote or a line break); "
                            "set CLAUDE_BIN to a claude.exe or to a path without those characters")
    comspec = env.get("COMSPEC") or "cmd.exe"
    return f'{subprocess.list2cmdline([comspec])} /d /s /c "{subprocess.list2cmdline([claude, *args])}"'


def _creationflags() -> int:
    """No console window for a child on Windows; 0 elsewhere (the flag is an error there)."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _child_env(extra=None, drop=()) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["NO_COLOR"] = "1"
    env.update(extra or {})
    return env


def _kill_tree(pid: int) -> None:
    """Kill a process and everything it started. A Windows .cmd runs python (or node) as a child of cmd.exe,
    and killing cmd.exe alone leaves that child holding our pipe open until it ends."""
    try:
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
    except (psutil.Error, OSError):
        return
    for p in procs:
        try:
            p.kill()
        except (psutil.Error, OSError):
            pass


def run_claude(args, claude=None, timeout=AGENTS_TIMEOUT, platform=None, *, cwd=None, merge_stderr=False,
               env_extra=None, drop_env=()) -> subprocess.CompletedProcess:
    """Run `claude <args>`: UTF-8 text, undecodable bytes replaced, NO_COLOR=1, no stdin, stderr dropped, a
    timeout that ends the whole process tree. OSError (claude cannot start) and subprocess.TimeoutExpired
    reach the caller.

    For starting a session (`claude --bg`, see launch.py): cwd is the folder it runs in; merge_stderr
    gives stdout and stderr as one text, in the order they were written (bash's `2>&1`), because the line
    that names the new session or says the folder is not trusted may be on either; env_extra adds
    variables (FORCE_COLOR=0); drop_env removes some the person's shell has (the crew's key, which no
    process the engine starts may inherit)."""
    cmd = claude_command(claude or claude_bin(), args, platform)
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
                          text=True, encoding="utf-8", errors="replace", env=_child_env(env_extra, drop_env),
                          cwd=cwd, creationflags=_creationflags()) as proc:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise subprocess.TimeoutExpired(cmd, timeout) from None
        except BaseException:
            _kill_tree(proc.pid)
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, None)


# ---------- the session list ----------


def active_json(claude=None) -> list:
    """The sessions Claude Code counts as active (`claude agents --json` without --all), as a list of dicts.
    A list that cannot be read is never taken for an empty one: a failing command, a timeout, output that
    is not JSON or not a list all raise SessionListUnreadableError."""
    try:
        r = run_claude(["agents", "--json"], claude, AGENTS_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise SessionListUnreadableError(UNREAD) from e
    if r.returncode != 0:
        raise SessionListUnreadableError(UNREAD)
    try:
        rows = loads_json(r.stdout)
    except ValueError as e:
        raise SessionListUnreadableError(UNREAD) from e
    if not isinstance(rows, list):
        raise SessionListUnreadableError(UNREAD)
    return [row for row in rows if isinstance(row, dict)]


def _val(v, default):
    """jq's `v // default`."""
    return default if v is None or v is False else v


def gone(row: dict) -> bool:
    """bash's GONE. A session is live while Claude Code lists it as active and a process holds it. One whose
    turn ended and whose process is gone drops out of the list, but one with a schedule armed stays in it
    as "working" after its process is stopped, so the process is checked per session. Only a session
    waiting on a person may have none and still be live: answering it starts one."""
    state = row.get("state")
    return _val(state, "") in DEAD_STATES or ("pid" not in row and state != "blocked")


def turn(row: dict):
    """bash's TURN: where a live session is in its turn, done between turns, working in one, blocked on a
    person. The list's `status` says it; its `state` reads "working" for as long as a schedule is armed,
    and the job file keeps "working" through a permission prompt."""
    state = row.get("state")
    if _val(state, "") in DEAD_STATES:
        return state
    status = _val(row.get("status"), "")
    mapped = TURN_OF_STATUS.get(status) if isinstance(status, str) else None
    return state if mapped is None else mapped


def turn_state(job_id: str, claude=None) -> str:
    """done, working or blocked for a live session; "" when it is not live. Raises SessionListUnreadableError
    where bash prints nothing and exits non-zero. (A row with no usable state or status gives "null", as jq -r.)"""
    for row in active_json(claude):
        if row.get("id") == job_id and not gone(row):
            return jq_raw(turn(row))
    return ""


def parse_bg_id(text: str) -> str:
    """The session id in `claude --bg` output ("backgrounded · <id> · <name>"), "" when there is none. From
    inside a Claude session the id comes wrapped in colour codes, which hide it from a plain match, so they
    are stripped first."""
    for line in text.splitlines():
        m = _BG_ID.search(_ANSI.sub("", line))
        if m:
            return m.group(0)[-8:]
    return ""


def cmd_parse_bg_id(argv: list, env: dict) -> int:
    """The hidden `_parse-bg-id`: read `claude --bg` output on stdin and print the session id (bash's
    `parse_bg_id`, which tests and scripts call). Like bash it checks no crew name, merges the crew.json
    defaults and prints nothing when there is no id."""
    from .paths import default_home
    from .state import merge_crew_defaults
    merge_crew_defaults(CrewPaths(env.get("FACTORY_CREW_HOME") or default_home(env)))
    found = parse_bg_id(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    if found:
        print(found)
    return 0


def _jq_order(v):
    """A sort key in jq's order: null, false, true, numbers, strings, arrays, objects."""
    if v is None:
        return (0, 0)
    if v is False:
        return (1, 0)
    if v is True:
        return (2, 0)
    if isinstance(v, (int, float)):
        return (3, v)
    if isinstance(v, str):
        return (4, v)
    return (5 if isinstance(v, list) else 6, 0)


def find_live_job(name: str, cwd: str, claude=None) -> str:
    """The id of the newest live session with exactly this name in cwd ("" for any folder); "" when there is
    none. Raises SessionListUnreadableError where bash prints nothing and exits non-zero."""
    rows = [r for r in active_json(claude)
            if r.get("name") == name and (cwd == "" or r.get("cwd") == cwd) and not gone(r)]
    rows.sort(key=lambda r: _jq_order(r.get("startedAt")))
    return jq_raw(_val(rows[-1].get("id"), "")) if rows else ""


def is_alive(station: str, cwd: str = "", claude=None, paths=None) -> bool:
    """True if the station's session is running or waiting. The station is its name and folder (cwd, else the
    one recorded for it, else the crew home) or its recorded id. Dies (SessionListUnreadableError) when
    the session list cannot be read: "<UNREAD>; not guessing whether <station> runs"."""
    paths = paths or CrewPaths.from_env()
    cwd = cwd or stations_get(station, "cwd", paths=paths)
    cwd = cwd or paths.raw
    sid = stations_get(station, "id", paths=paths)
    try:
        rows = active_json(claude)
    except SessionListUnreadableError as e:
        raise SessionListUnreadableError(f"{UNREAD}; not guessing whether {station} runs") from e
    return any(not gone(r) and ((r.get("name") == station and r.get("cwd") == cwd)
                                or (sid != "" and r.get("id") == sid)) for r in rows)


# ---------- a session's job file ----------


def job_state(job_id: str) -> dict:
    """~/.claude/jobs/<id>/state.json as a dict; {} when the job has no file.

    # STATE.JSON FALLBACK: fields claude agents --json does not give (updatedAt, inFlight)
    quiet(), station_updated() and transcript_of() call this. Raises JobStateUnreadableError for a file that is there
    but is not a JSON object, so a half-written file is never read as "nothing in flight"."""
    if not job_id or job_id in (".", "..") or "/" in job_id or "\\" in job_id:
        return {}
    f = Path(_home()) / ".claude" / "jobs" / job_id / "state.json"
    if not f.is_file():
        return {}
    try:
        data = loads_json(read_text(f))
    except (OSError, ValueError) as e:
        raise JobStateUnreadableError(f"{f} is not valid JSON") from e
    if not isinstance(data, dict):
        raise JobStateUnreadableError(f"{f} is not a JSON object")
    return data


def transcript_of(station: str, claude=None, paths=None, agents=None) -> str:
    """The station's conversation transcript, ~/.claude/projects/*/<session>.jsonl; "" when there is none.
    The session id is the one in the job's state.json, else the one on the station's row of
    `claude agents --json`, else the one recorded for the station. `agents` is that list when the caller already has it (status asks once, not per station)."""
    paths = paths or CrewPaths.from_env()
    sid = stations_get(station, "id", paths=paths)
    s = ""
    if sid:
        try:   # the job's own state.json comes first, as in bash: after a /clear it names the new session
            s = jq_raw(_val(job_state(sid).get("sessionId"), ""))
        except JobStateUnreadableError:
            pass
    if sid and not s:
        try:
            for row in (agents if agents is not None else active_json(claude)):
                if row.get("id") == sid:
                    s = jq_raw(_val(row.get("sessionId"), ""))
                    break
        except SessionListUnreadableError:
            pass
    s = s or stations_get(station, "sessionId", paths=paths)
    if not s or "/" in s or "\\" in s:
        return ""
    pattern = os.path.join(glob.escape(os.path.join(_home(), ".claude", "projects")), "*",
                           glob.escape(s) + ".jsonl")
    found = sorted(glob.glob(pattern))
    return found[0] if found else ""


# ---------- quiet ----------


def _number_zero(v) -> bool:
    """jq's `(v // 0) == 0`; a value that is not a number is not zero."""
    v = _val(v, 0)
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0


def iso_epoch(text: str):
    """jq's `sub("\\.[0-9]+"; "") | fromdateiso8601`: epoch seconds of 2026-05-01T09:30:00.123Z; None when
    the text is not a UTC time in that shape."""
    m = _ISO_SECONDS.match(re.sub(r"\.[0-9]+", "", text, count=1))
    if not m:
        return None
    year, month, day, hour, minute, second = map(int, m.groups())
    # strptime takes a day 1..31 whatever the month and a leap second (60; glibc also takes 61, macOS does not);
    # timegm then rolls Feb 30 over to Mar 2. Anything else it refuses, and so does jq's `?`. Times before 1970
    # give a negative number jq refuses.
    if not (1970 <= year and 1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60
            and second < (61 if sys.platform == "darwin" else 62)):
        return None
    return calendar.timegm((year, month, 1, hour, minute, second)) + (day - 1) * 86400


def _quiet_wait(wait):
    """The wait in seconds: wait, else FACTORY_QUIET_WAIT (1800); None when the variable is not a JSON number."""
    if wait is None:
        raw = os.environ.get("FACTORY_QUIET_WAIT") or str(DEFAULT_QUIET_WAIT)
        try:
            wait = json.loads(raw)
        except ValueError:
            return None
    return wait if isinstance(wait, (int, float)) and not isinstance(wait, bool) else None


def quiet(station: str, wait=None, claude=None, paths=None, now=None) -> bool:
    """True when the station is quiet between its turns with nothing running in the background or queued. Its
    own schedule (a session cron) comes back on resume, so it does not count. One waiting on a person (a
    question or a permission prompt) counts only after `wait` seconds (FACTORY_QUIET_WAIT, else 1800), so a
    restart never cuts off someone answering it. A session list or job file that cannot be read is not quiet."""
    paths = paths or CrewPaths.from_env()
    sid = stations_get(station, "id", paths=paths)
    try:
        st = turn_state(sid, claude)
        if st not in ("done", "blocked"):
            return False
        # STATE.JSON FALLBACK: fields claude agents --json does not give (updatedAt, inFlight)
        job = job_state(sid)
    except (SessionListUnreadableError, JobStateUnreadableError):
        return False
    limit = _quiet_wait(wait)
    inflight = job.get("inFlight")
    if inflight is None:
        inflight = {}
    if limit is None or not isinstance(inflight, dict):
        return False
    kinds = _val(inflight.get("kinds"), [])
    if not isinstance(kinds, list):
        return False
    if not (_number_zero(inflight.get("queued")) and inflight.get("wake") is None
            and not [k for k in kinds if k != "session_cron"]
            and (len(kinds) > 0 or _number_zero(inflight.get("tasks")))):
        return False
    if st == "done":
        return True
    updated = _val(job.get("updatedAt"), "")
    if not isinstance(updated, str):
        return False
    now = time.time() if now is None else now
    at = iso_epoch(updated)
    return (now - (now if at is None else at)) >= limit


def between_turns(station: str, claude=None, paths=None, now=None) -> bool:
    """True when the station is done or blocked with nothing in flight: quiet with no waiting."""
    return quiet(station, 0, claude, paths, now)


def station_updated(station: str, paths=None):
    """When the station's session last did anything, epoch seconds; None when unknown.
    # STATE.JSON FALLBACK: fields claude agents --json does not give (updatedAt, inFlight)"""
    paths = paths or CrewPaths.from_env()
    try:
        job = job_state(stations_get(station, "id", paths=paths))
    except JobStateUnreadableError:
        return None
    updated = _val(job.get("updatedAt"), "")
    return iso_epoch(updated) if isinstance(updated, str) else None


# ---------- processes ----------


def _fake_ps_file(pid, field: str):
    """Test hook TARES_FAKE_PS_DIR: the folder a suite's fake `ps` answers from (tests/factory/fake-ps), where
    <dir>/ps/<pid>.<field> holds what `ps -o <field>= -p <pid>` prints. None when the hook is off or the file
    is not there, so the real process is read, as the fake `ps` hands such a pid to the real one."""
    d = os.environ.get("TARES_FAKE_PS_DIR")
    if not d:
        return None
    try:
        return (Path(d) / "ps" / f"{int(pid)}.{field}").read_text(encoding="utf-8").strip()
    except (OSError, ValueError, TypeError):
        return None


def fake_ps_step(version: str) -> None:
    """Test hook, with TARES_FAKE_PS_DIR set: run the folder's `sort-hook` file once (a shell command) when a spare
    of version 0.9.9 is looked at. The bash engine orders versions with `sort -V`, and the suite puts a fake `sort`
    on PATH that runs that command at this step, so something happens between reading the listing and acting on
    it; Python orders them itself, so it takes the step here."""
    d = os.environ.get("TARES_FAKE_PS_DIR")
    if not d or "0.9.9" not in version:
        return
    hook = Path(d) / "sort-hook"
    try:
        cmd = hook.read_text(encoding="utf-8")
        hook.unlink()
    except OSError:
        return
    subprocess.run(["bash", "-c", cmd], check=False)


def process_listing() -> list:
    """[(pid, command line)] of the processes this user can see; the command line joined by spaces, as
    `ps -axo pid=,command=` shows it. With TARES_FAKE_PS_DIR set it is the folder's ps-list file (empty when
    there is none), so a test never reads the machine's real processes."""
    d = os.environ.get("TARES_FAKE_PS_DIR")
    out = []
    if d:
        try:
            text = (Path(d) / "ps-list").read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if pid.isdigit():
                out.append((int(pid), cmd.strip()))
        return out
    for pid in psutil.pids():
        try:   # one process at a time: psutil on macOS can raise SystemError for one that is just ending
            out.append((pid, " ".join(psutil.Process(pid).cmdline())))
        except (psutil.Error, OSError, SystemError):
            continue
    return out


def _command_line(pid) -> str:
    """A process's command line as `ps -o command=` shows it; "" when it is gone or may not be read."""
    fake = _fake_ps_file(pid, "command")
    if fake is not None:
        return fake
    try:
        return " ".join(psutil.Process(int(pid)).cmdline())
    except (psutil.Error, ValueError, TypeError, OSError):
        return ""


def attached(station: str, paths=None) -> bool:
    """True while someone has the station's session open in a terminal (`claude attach <id>`): a process
    whose command line holds `attach <id>`, followed by a space or its end. (bash: pgrep -f.)"""
    sid = stations_get(station, "id", paths=paths or CrewPaths.from_env())
    if not sid:
        return False
    pattern = re.compile("attach " + re.escape(sid) + "( |$)")
    me = os.getpid()
    # One pid at a time: process_iter reads each command line itself and can raise a PermissionError that ends
    # the whole loop (a process it may not read), where this skips just that process. psutil on macOS can also
    # leak a SystemError ("returned a result with an exception set") for such a process.
    for pid in psutil.pids():
        if pid == me:
            continue
        try:
            if pattern.search(" ".join(psutil.Process(pid).cmdline() or [])):
                return True
        except (psutil.Error, OSError, SystemError):
            continue
    return False


def running_version(session_id: str, agents=None, claude=None) -> str:
    """The Claude Code version a session's process runs; "" when it cannot be told. The session list may
    give it (`version`); if not, a station's pid is a `bg-spare` process under a pty host whose command
    line names the binary it runs (.../versions/<v>), so the pid's parent is read, then the pid. `agents`
    is a list already read (active_json); without one it is read here, and an unreadable list gives ""."""
    if not session_id:
        return ""
    if agents is None:
        try:
            agents = active_json(claude)
        except SessionListUnreadableError:
            return ""
    row = next((r for r in agents if isinstance(r, dict) and r.get("id") == session_id), None)
    if row is None:
        return ""
    version = jq_raw(_val(row.get("version"), ""))
    if version:
        return version
    pid = _val(row.get("pid"), "")
    if pid == "":
        return ""
    fake = _fake_ps_file(pid, "ppid")
    if fake is not None and fake.isdigit():
        ppid = int(fake)
    else:
        try:
            ppid = psutil.Process(int(pid)).ppid()
        except (psutil.Error, ValueError, TypeError, OSError):
            ppid = None
    for p in (ppid, pid):
        if p is None:
            continue
        m = _VERSIONS_IN_COMMAND.search(_command_line(p))
        if m:
            return m.group(1)
    return ""


# ---------- versions ----------


def claude_version(claude=None) -> str:
    """The version `claude` on PATH is now (2.1.310); "" when it cannot be read."""
    try:
        r = run_claude(["--version"], claude, VERSION_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = r.stdout.splitlines()
    m = _VERSION_WORD.match(lines[0]) if lines else None
    return m.group(0) if m else ""


def _version_order(c: str) -> int:
    """How one character sorts in a non-digit stretch of a version (GNU sort -V): the end first, then ~, then
    letters, then everything else."""
    if c == "":
        return 0
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    return ord(c) if c.isalpha() else ord(c) + 256


def _version_compare(a: str, b: str) -> int:
    """GNU `sort -V` order of two strings: runs of digits compare as numbers, the rest character by character."""
    i = j = 0
    while i < len(a) or j < len(b):
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _version_order(a[i] if i < len(a) else "")
            bc = _version_order(b[j] if j < len(b) else "")
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        first = 0
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            first = first or ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first:
            return first
    return 0


def older_than(version: str, target: str) -> bool:
    """True when `version` is a lower Claude Code version than `target` (it sorts first under `sort -V`, ties
    by bytes as sort does); an unknown (empty) version is never older."""
    if not version or not target or version == target:
        return False
    c = _version_compare(version, target)
    return c < 0 or (c == 0 and version.encode("utf-8") < target.encode("utf-8"))
