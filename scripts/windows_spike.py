#!/usr/bin/env python3
"""Windows spike for TR-946: can the tares-factory crew run on Windows with Claude Code?

THROWAWAY. Not shipped, not part of CI. It records facts so the crew can decide go / no-go for
porting the bash crew (claude-plugin/bin/factory) to Python. It needs only the standard library.
`psutil` and `keyring` are optional: the script reports them as missing instead of crashing, and
the answers that need them are marked "not answered".

HOW TO RUN (Windows 11, Claude Code installed and logged in, from a normal terminal)

    cd <the tares checkout>
    py scripts\\windows_spike.py            (or:  python scripts/windows_spike.py)

Better results with the two optional packages (the script works without them):

    py -m pip install psutil keyring

It takes about 3 to 6 minutes. The slowest parts are the one-line model call of the spike session
and `claude -p --resume ... --fork-session` (up to 2 minutes). Do not touch the terminal while it
runs. Run it from a folder you have already trusted in Claude Code (the spike session starts
there; pass --cwd DIR to pick another). Claude Code asks for trust once per folder and a
background session cannot answer that prompt.

WHAT IT CREATES
  - a fresh folder under your temp directory (printed at the end; --out DIR picks another). It holds
    the results, a tiny throwaway plugin ("plugin dir with space"), a settings file, probe files
    and the heartbeat file of step 7.
  - two or three Claude Code background sessions named spike-<random>..., each answering one short
    prompt. Two small conversation transcripts remain in ~/.claude/projects (one from the resumed
    session, one from --fork-session); they are harmless and can be deleted.
  - one detached Python process that writes a heartbeat file for 60 seconds (step 7).
  - one temporary entry in the Windows Credential Manager, service "tares-spike", user "spike".
    The value is random, never printed or logged, and deleted again within the same run.

WHAT IT REMOVES
  Every session it starts is stopped and removed (claude stop, claude rm) in a finally block, and
  the credential is deleted. Your own Claude Code config and sessions are only read, never changed
  (it reads ~/.claude/jobs/<id>/state.json of its own sessions, and the key names of settings
  files to see whether --bg is disabled). Nothing is written outside the results folder.

STEP 7 NEEDS YOU (about one minute)
  When the script ends it tells you to CLOSE THE TERMINAL it was started in, wait 20 seconds, open a
  new terminal and run the command it printed:

      py scripts\\windows_spike.py --check-detached <heartbeat file>

  That reports whether the detached child survived the terminal closing, stops it, and updates the
  results files (JSON and Markdown) with the answer.

WHERE THE RESULTS LAND, AND WHAT TO DO WITH THEM
  <results folder>\\windows_spike_results.md and windows_spike_results.json. Paste the whole
  windows_spike_results.md back (the end of it, "Items for the note", is the note "Windows spike
  results"). Nothing in it contains a token or secret; skim it anyway before sharing.

OTHER MODES
  --dry-run   Safe on Mac and Linux (it is switched on by itself when the OS is not Windows). Windows
              only steps are skipped, the keychain is replaced by a file in the results folder, and
              the detached child lives 6 seconds. Set CLAUDE_BIN to a fake `claude` to exercise the
              Claude steps (without it the Claude steps are skipped in a dry run).
  CLAUDE_BIN  The claude executable to use (default: claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

IS_WIN = os.name == "nt"
CAP = 2000
SPIKE_SERVICE, SPIKE_USER = "tares-spike", "spike"
PROMPT_BYTES = 8192
RESULTS_JSON, RESULTS_MD = "windows_spike_results.json", "windows_spike_results.md"
COLD_START_TARGET_MS = 400

# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------

_LAZY: dict = {}


def lazy(name):
    """Import an optional package once; None when it is missing."""
    if name not in _LAZY:
        try:
            _LAZY[name] = __import__(name)
        except Exception:  # missing or broken: report, never crash
            _LAZY[name] = None
    return _LAZY[name]


def write_lf(path: Path, text: str):
    """Write a file with LF line endings (a bash script with CRLF breaks)."""
    with open(str(path), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def force_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


SCRUB = re.compile(
    r"(sk-[A-Za-z0-9_\-]{10,}|Bearer\s+[A-Za-z0-9._~+/=\-]{8,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,})"
)
SECRET_KEY = re.compile(r"token|secret|key|password|passwd|authorization|credential|cookie", re.I)
LONG_RUN = re.compile(r"[A-Za-z0-9+_=\-]{40,}")
PLAIN_KEYS = {"cwd", "name", "id", "sessionId", "settings", "path", "version", "startedAt", "updatedAt"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def scrub(text) -> str:
    return SCRUB.sub("<redacted>", text if isinstance(text, str) else str(text))


def trim(text, cap: int = CAP) -> str:
    text = scrub(text or "")
    if len(text) > cap:
        return text[: cap - 24] + "...[trimmed %d chars]" % (len(text) - cap + 24)
    return text


def redact_value(key: str, value) -> str:
    """A short display of a state value; anything token-like is hidden."""
    key = (key or "").split(".")[-1]
    if value is None:
        return "null"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if SECRET_KEY.search(key) or SCRUB.search(text) or (key not in PLAIN_KEYS and LONG_RUN.search(text)):
        return "<redacted>"
    return text if len(text) <= 60 else text[:57] + "..."


def display_argv(argv):
    """The argv as recorded: long arguments (the 8 KB prompt) are summarised, token-like ones hidden."""
    if isinstance(argv, str):
        return [trim(argv, 300)]
    out = []
    for a in argv:
        a = str(a)
        n = len(a.encode("utf-8", "replace"))
        out.append("<text, %d bytes>" % n if n > 200 else scrub(a))
    return out


def get_path(obj, dotted: str):
    """(present, value) for a dotted path in nested dicts."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def parse_json_loose(text: str):
    """JSON from output that may carry a banner line before it."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for opener in ("[", "{"):
        i = text.find(opener)
        if i >= 0:
            try:
                return json.loads(text[i:])
            except Exception:
                continue
    raise ValueError("not JSON: " + text[:120].replace("\n", " "))


def poll(fn, timeout: float, interval: float = 1.0):
    """Call fn until it returns something truthy or the time is up; returns the last value."""
    end = time.monotonic() + timeout
    val = fn()
    while not val and time.monotonic() < end:
        time.sleep(interval)
        val = fn()
    return val


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    ps = lazy("psutil")
    if ps:
        try:
            return ps.pid_exists(pid)
        except Exception:
            pass
    if IS_WIN:
        try:
            import ctypes

            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_tree(pid: int):
    """Kill a process and everything below it. Never raises."""
    ps = lazy("psutil")
    try:
        if ps:
            try:
                parent = ps.Process(pid)
            except Exception:
                return
            procs = parent.children(recursive=True) + [parent]
            for p in procs:
                try:
                    p.kill()
                except Exception:
                    pass
            return
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


class Proc:
    """A child process with output in temp files (a grandchild holding a pipe can never hang us)."""

    def __init__(self, argv, cwd=None, env=None, stdin_pipe=False):
        self.argv = argv
        self.out, self.err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
        self.t0 = time.monotonic()
        self.timed_out = False
        self.error = None
        self.p = None
        kw = dict(stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL, stdout=self.out,
                  stderr=self.err, cwd=cwd, env=env)
        if not IS_WIN:
            kw["start_new_session"] = True
        try:
            self.p = subprocess.Popen(argv, **kw)
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)

    def wait(self, timeout: float):
        if not self.p:
            return None
        try:
            self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            kill_tree(self.p.pid)
            try:
                self.p.wait(timeout=10)
            except Exception:
                pass
        return self.p.returncode

    def record(self) -> dict:
        def read(f):
            try:
                f.seek(0)
                return f.read().decode("utf-8", "replace")
            except Exception:
                return ""

        rec = {
            "argv": display_argv(self.argv),
            "exit_code": self.p.returncode if self.p else None,
            "elapsed_ms": int((time.monotonic() - self.t0) * 1000),
            "stdout": trim(read(self.out)),
            "stderr": trim(read(self.err)),
            "timed_out": self.timed_out,
        }
        if self.error:
            rec["error"] = self.error
        for f in (self.out, self.err):
            try:
                f.close()
            except Exception:
                pass
        return rec


def run_cmd(argv, timeout: float = 60, cwd=None, env=None) -> dict:
    pr = Proc(argv, cwd=cwd, env=env)
    pr.wait(timeout)
    rec = pr.record()
    rec["_raw_out"] = None
    return rec


def list_processes():
    """(rows, source). Rows: pid, ppid, name, exe, cmdline, create_time."""
    ps = lazy("psutil")
    rows = []
    if ps:
        for p in ps.process_iter(["pid", "ppid", "name", "exe", "cmdline", "create_time"]):
            i = p.info
            ct = i.get("create_time")
            rows.append({"pid": i.get("pid"), "ppid": i.get("ppid"), "name": i.get("name") or "",
                         "exe": i.get("exe") or "", "cmdline": " ".join(i.get("cmdline") or []),
                         "create_time": datetime.fromtimestamp(ct, timezone.utc).isoformat(timespec="seconds") if ct else ""})
        return rows, "psutil"
    if IS_WIN:
        ps_cmd = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,"
                  "CommandLine | ConvertTo-Json -Compress")
        r = run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd], timeout=60)
        try:
            data = parse_json_loose(r["stdout"] if len(r["stdout"]) < CAP else "")
        except Exception:
            data = None
        if data is None:  # output was trimmed: ask again, straight to a file-less capture
            try:
                out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                                     capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
                data = parse_json_loose(out)
            except Exception:
                return [], "powershell failed"
        if isinstance(data, dict):
            data = [data]
        for d in data:
            rows.append({"pid": d.get("ProcessId"), "ppid": d.get("ParentProcessId"), "name": d.get("Name") or "",
                         "exe": d.get("ExecutablePath") or "", "cmdline": d.get("CommandLine") or "", "create_time": ""})
        return rows, "powershell"
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
    except Exception:
        return [], "ps failed"
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if m:
            cmd = m.group(3)
            rows.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "name": cmd.split(" ")[0].split("/")[-1],
                         "exe": "", "cmdline": cmd, "create_time": ""})
    return rows, "ps"


# ---------------------------------------------------------------------------------------------
# step bookkeeping
# ---------------------------------------------------------------------------------------------


class Step:
    def __init__(self, name: str, item: int, title: str):
        self.name, self.item, self.title = name, item, title
        self.status = None
        self.finding = ""
        self.commands: list = []
        self.primary = None
        self.details: dict = {}
        self.t0 = time.monotonic()

    def ok(self, finding):
        self.status, self.finding = "ok", finding

    def fail(self, finding):
        self.status, self.finding = "fail", finding

    def skip(self, finding):
        self.status, self.finding = "skipped", finding

    def add(self, rec: dict, primary: bool = False):
        rec.pop("_raw_out", None)
        self.commands.append(rec)
        if primary or self.primary is None:
            self.primary = rec
        return rec

    def run(self, argv, timeout: float = 60, primary: bool = False, cwd=None, env=None, keep: bool = True):
        rec = run_cmd(argv, timeout=timeout, cwd=cwd, env=env)
        if keep:
            self.add(rec, primary)
        return rec

    def to_dict(self) -> dict:
        p = self.primary or {}
        return {
            "name": self.name, "item": self.item, "title": self.title,
            "status": self.status or "fail",
            "finding": self.finding or "the step did not record a result",
            "argv": p.get("argv"), "exit_code": p.get("exit_code"),
            "elapsed_ms": p.get("elapsed_ms", int((time.monotonic() - self.t0) * 1000)),
            "stdout": p.get("stdout", ""), "stderr": p.get("stderr", ""),
            "commands": self.commands, "details": self.details,
            "wall_ms": int((time.monotonic() - self.t0) * 1000),
        }


def one_line(text, n: int = 160) -> str:
    text = re.sub(r"\s+", " ", scrub(text or "")).strip()
    return text if len(text) <= n else text[: n - 3] + "..."


# ---------------------------------------------------------------------------------------------
# the fields bash reads from a job's state.json and from `claude agents --json`
# ---------------------------------------------------------------------------------------------

# (field, where claude-plugin/bin/factory uses it)
STATE_FIELDS = [
    ("sessionId", "the conversation id a station resumes (job_state, ~lines 826, 902, 2397, 2443)"),
    ("respawnFlags", "model and effort recovered when a session is adopted (~631); temp-settings check (~3067)"),
    ("updatedAt", "how long a blocked session has waited, quiet() (~864); last activity (~1626)"),
    ("inFlight.queued", "quiet(): queued turns (~860)"),
    ("inFlight.wake", "quiet(): a wake armed (~860)"),
    ("inFlight.kinds", "quiet(): kinds of work in flight (~861-862)"),
    ("inFlight.tasks", "quiet(): background tasks (~862)"),
    ("tempo", "crew status display (~2271)"),
    ("state", "GONE / turn_state(): stopped, failed, killed, removed, blocked (~542-558)"),
    ("status", "turn(): idle, busy, waiting (~542)"),
]
AGENT_FIELDS = [
    ("id", "find_live_job, is_alive (~560-571)"),
    ("name", "find_live_job, is_alive: exact session name"),
    ("cwd", "find_live_job, is_alive: exact string compare with the station cwd"),
    ("state", "GONE: stopped/failed/killed/removed/blocked"),
    ("status", "turn(): idle/busy/waiting"),
    ("pid", "GONE: a session without a pid is gone unless blocked; running_version (~1832)"),
    ("startedAt", "find_live_job: newest by startedAt"),
    ("version", "running_version (~1832): the Claude Code version a session runs"),
    ("sessionId", "listed by adopt (~643)"),
]


# ---------------------------------------------------------------------------------------------
# the spike
# ---------------------------------------------------------------------------------------------


class Spike:
    def __init__(self, args):
        self.args = args
        self.dry = bool(args.dry_run) or not IS_WIN
        self.out = Path(args.out).resolve() if args.out else Path(tempfile.mkdtemp(prefix="windows_spike_"))
        self.out.mkdir(parents=True, exist_ok=True)
        self.name = "spike-" + secrets.token_hex(3)
        self.steps: list = []
        self.sessions: list = []  # {"id", "name", "removed"}
        self.cleaned = False
        self.cwd = str(Path(args.cwd).resolve()) if args.cwd else os.getcwd()
        self.plugin = self.out / "plugin dir with space"
        self.settings = self.out / "spike.settings.json"
        self.hookfile = self.out / "hook_probe.txt"
        self.heartbeat = self.out / "heartbeat.json"
        self.jobs = Path.home() / ".claude" / "jobs"
        self.sid = None
        self.sid_source = None
        self.job_id = None
        self.agent_row = None
        self.state_json = None
        self.launch_ok = None
        self.env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0")
        want = os.environ.get("CLAUDE_BIN", "")
        if self.dry and not want:
            self.claude, self.claude_why = None, "dry run without CLAUDE_BIN: the Claude steps are skipped (set CLAUDE_BIN to a fake claude)"
        else:
            self.claude = shutil.which(want or "claude") or (want if want and Path(want).exists() else None)
            self.claude_why = "" if self.claude else "%s was not found on PATH" % (want or "claude")

    # --- step runner -------------------------------------------------------------------------

    def step(self, name: str, item: int, title: str, fn):
        st = Step(name, item, title)
        print("  running %-18s ..." % name, flush=True)
        try:
            fn(st)
        except Exception as e:
            tb = traceback.format_exc().strip().splitlines()[-3:]
            st.fail("the spike script raised %s in this step: %s (%s)" % (type(e).__name__, e, " | ".join(t.strip() for t in tb)))
        d = st.to_dict()
        self.steps.append(d)
        print("  %-26s %-7s %s" % (name, d["status"], one_line(d["finding"], 70)), flush=True)
        return d

    def need_claude(self, st: Step) -> bool:
        if self.claude:
            return True
        if self.dry and "without CLAUDE_BIN" in self.claude_why:
            st.skip(self.claude_why)
        else:
            st.fail("claude cannot be run: " + self.claude_why)
        return False

    def cl(self, st: Step, args, timeout=60, primary=False, cwd=None, keep=True):
        return st.run([self.claude] + list(args), timeout=timeout, primary=primary, cwd=cwd or self.cwd,
                      env=self.env, keep=keep)

    def agents(self, st: Step, all_=False, keep=False):
        """(list or None, record)."""
        rec = self.cl(st, ["agents", "--json"] + (["--all"] if all_ else []), timeout=30, keep=keep)
        if rec["exit_code"] != 0:
            return None, rec
        try:
            data = parse_json_loose(self._full_out(rec))
        except Exception:
            return None, rec
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v, rec
            return None, rec
        return (data if isinstance(data, list) else None), rec

    def _full_out(self, rec):
        # records are capped at 2000 chars; agents output can be longer, so ask again uncapped
        out = rec["stdout"]
        if "[trimmed" not in out:
            return out
        r = subprocess.run([self.claude, "agents", "--json"] + (["--all"] if "--all" in rec["argv"] else []),
                           capture_output=True, timeout=30, env=self.env, cwd=self.cwd)
        return r.stdout.decode("utf-8", "replace")

    def find_row(self, rows, id_=None, name=None):
        best = None
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if (id_ and r.get("id") == id_) or (name and r.get("name") == name):
                best = r
        return best

    # --- setup -------------------------------------------------------------------------------

    def setup_files(self, st: Step):
        (self.plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (self.plugin / "hooks").mkdir(exist_ok=True)
        (self.plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "tares-spike", "version": "0.0.1",
                        "description": "Throwaway plugin of the Windows spike (TR-946). Safe to delete."}, indent=2), encoding="utf-8")
        probe_file = self.hookfile.as_posix()
        write_lf(self.plugin / "hooks" / "probe.sh",
                 '#!/usr/bin/env bash\necho "probe-sh bash=$BASH_VERSION root=${CLAUDE_PLUGIN_ROOT:-unset}" >> "%s"\n' % probe_file)
        write_lf(self.plugin / "hooks" / "probe.py",
            "import json, os, sys\n"
            "with open(%r, 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps({'hook': 'probe-py ' + (sys.argv[1] if len(sys.argv) > 1 else ''),\n"
            "                        'exe': sys.executable, 'version': sys.version.split()[0],\n"
            "                        'argv0': sys.argv[0], 'root_env': os.environ.get('CLAUDE_PLUGIN_ROOT', 'unset')}) + '\\n')\n"
            % probe_file)
        inline = 'echo "probe-inline bash=[$BASH_VERSION] cmd=[%COMSPEC%] ps=[$PSVersionTable]" >> "' + probe_file + '"'
        hooks = {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": inline, "timeout": 5},
            {"type": "command", "command": 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/probe.sh"', "timeout": 5},
            {"type": "command", "command": "python", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/probe.py", "python"], "timeout": 10},
            {"type": "command", "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/probe.py", "python3"], "timeout": 10},
            {"type": "command", "command": "py", "args": ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/probe.py", "py"], "timeout": 10},
        ]}]}}
        (self.plugin / "hooks" / "hooks.json").write_text(json.dumps(hooks, indent=2), encoding="utf-8")
        # a harmless settings file; every path inside it uses forward slashes
        self.settings.write_text(json.dumps({"env": {"TARES_SPIKE": "1", "TARES_SPIKE_OUT": self.out.as_posix()}}, indent=2),
                                 encoding="utf-8")
        st.details = {"plugin_dir": str(self.plugin), "settings": self.settings.as_posix(), "hook_probe_file": probe_file}
        st.ok("wrote the throwaway plugin (manifest plus 5 SessionStart probe hooks) and a settings file in %s" % self.out)

    # --- 1. launch ---------------------------------------------------------------------------

    def bg_args(self, name: str, resume: str = None):
        text = ("Spike filler text, harmless. " * 400)[:PROMPT_BYTES]
        assert len(text.encode("utf-8")) == PROMPT_BYTES
        args = ["--bg", "-n", name, "--settings", self.settings.as_posix(), "--append-system-prompt", text,
                "--plugin-dir", str(self.plugin)]
        if resume:
            args += ["--resume", resume]
        args.append("Spike test. Reply with the single word ok and nothing else.")
        return args

    def launch_once(self, st: Step, name: str, resume: str = None):
        """Start a --bg session; returns (id or None, record)."""
        rec = self.cl(st, self.bg_args(name, resume), timeout=120, primary=True)
        text = ANSI.sub("", rec["stdout"] + "\n" + rec["stderr"])
        m = re.search(r"backgrounded[^0-9a-f]+([0-9a-f]{8})", text)
        sid = m.group(1) if m else None
        if not sid and "not trusted" not in text:
            time.sleep(2)
            rows, _ = self.agents(st)
            row = self.find_row(rows, name=name)
            sid = row.get("id") if row else None
        if sid:
            self.sessions.append({"id": sid, "name": name, "removed": False})
        return sid, rec

    def step_launch(self, st: Step):
        if not self.need_claude(st):
            self.launch_ok = False
            return
        ext = Path(self.claude).suffix.lower()
        st.details["claude_path"] = self.claude
        st.details["prompt_bytes"] = PROMPT_BYTES
        sid, rec = self.launch_once(st, self.name)
        self.job_id = sid
        text = ANSI.sub("", rec["stdout"] + " " + rec["stderr"])
        if sid:
            self.launch_ok = True
            st.ok("claude --bg started session %s (%s); the %d-byte --append-system-prompt passed (exit %s, %d ms)%s" % (
                sid, self.name, PROMPT_BYTES, rec["exit_code"], rec["elapsed_ms"],
                ("; note: claude resolves to a %s shim, which cmd.exe limits to 8191 characters" % ext) if ext in (".cmd", ".bat") else ""))
            return
        self.launch_ok = False
        why = "Claude Code has not trusted %s yet: run `claude` there once, accept the trust prompt, then run the spike again" % self.cwd \
            if "not trusted" in text else one_line(text, 300)
        extra = ""
        if ext in (".cmd", ".bat"):
            extra = " claude resolves to a %s shim: cmd.exe cuts command lines at 8191 characters, so the 8192-byte prompt may be the cause." % ext
        st.fail("claude --bg did not start a session (exit %s, timed out: %s). %s%s" % (rec["exit_code"], rec["timed_out"], why, extra))

    # --- 2. agents --json and state.json -----------------------------------------------------

    def step_agents(self, st: Step):
        if not self.need_claude(st):
            return
        found = {}

        def look():
            rows, rec = self.agents(st, keep=False)
            found["rec"] = rec
            if rows is None:
                return None
            found["rows"] = rows
            row = self.find_row(rows, id_=self.job_id, name=self.name)
            return row

        row = poll(look, 20 if self.launch_ok or self.job_id else 3, 1.5)
        rec = found.get("rec")
        if rec:
            st.add(rec, primary=True)
        if "rows" not in found:
            st.fail("claude agents --json did not return a JSON list (exit %s): %s" % (
                rec["exit_code"] if rec else None, one_line((rec or {}).get("stderr") or (rec or {}).get("stdout"), 200)))
            return
        if not row:
            st.fail("claude agents --json worked (%d sessions) but does not list the spike session %s / %s" % (
                len(found["rows"]), self.job_id, self.name))
            return
        self.agent_row = row
        if not self.job_id:
            self.job_id = row.get("id")
            if self.job_id and not any(s["id"] == self.job_id for s in self.sessions):
                self.sessions.append({"id": self.job_id, "name": self.name, "removed": False})
        if row.get("sessionId"):
            self.sid, self.sid_source = row["sessionId"], "agents --json"
        wanted = ["id", "name", "cwd", "state", "status", "pid", "startedAt"]
        fields = {k: (redact_value(k, row[k]) if k in row else "(absent)") for k in wanted}
        st.details["fields"] = fields
        st.details["all_keys"] = sorted(row.keys())
        a, b = self.cwd, str(row.get("cwd", ""))
        st.details["cwd_exact_match"] = a == b
        st.details["cwd_we_started_in"], st.details["cwd_claude_reports"] = a, b
        missing = [k for k in ("id", "name", "cwd", "state") if k not in row]
        cwd_note = "cwd matches exactly" if a == b else "cwd differs from the folder we started in (we: %r, Claude: %r), and bash compares it as an exact string" % (a, b)
        if missing:
            st.fail("agents --json lists the session but these fields are missing: %s. Present: %s" % (", ".join(missing), ", ".join(sorted(row.keys()))))
        else:
            st.ok("agents --json lists the session with id=%s name=%s state=%s status=%s pid=%s startedAt=%s; %s" % (
                fields["id"], fields["name"], fields["state"], fields["status"], fields["pid"], fields["startedAt"], cwd_note))

    def step_state(self, st: Step):
        if not self.job_id:
            st.skip("no session id to look up")
            return
        path = self.jobs / self.job_id / "state.json"
        st.details["path"] = str(path)
        data = None
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                st.details["parse_error"] = repr(e)
        else:
            try:
                st.details["jobs_dir_listing"] = sorted(p.name for p in self.jobs.iterdir())[:30]
            except Exception as e:
                st.details["jobs_dir_listing"] = "unreadable: %r" % e
        rows = []
        for field, use in STATE_FIELDS:
            in_state, sv = get_path(data, field) if isinstance(data, dict) else (False, None)
            in_agents, av = get_path(self.agent_row, field) if isinstance(self.agent_row, dict) else (False, None)
            rows.append({"field": field, "bash_use": use,
                         "state_json": ("present: " + redact_value(field, sv)) if in_state else "absent",
                         "agents_json": ("present: " + redact_value(field, av)) if in_agents else "absent"})
        for field, use in AGENT_FIELDS:
            if any(r["field"] == field for r in rows):
                continue
            in_state, sv = get_path(data, field) if isinstance(data, dict) else (False, None)
            in_agents, av = get_path(self.agent_row, field) if isinstance(self.agent_row, dict) else (False, None)
            rows.append({"field": field, "bash_use": use + " (read from agents --json)",
                         "state_json": ("present: " + redact_value(field, sv)) if in_state else "absent",
                         "agents_json": ("present: " + redact_value(field, av)) if in_agents else "absent"})
        st.details["field_table"] = rows
        if isinstance(data, dict):
            st.details["state_json_keys"] = sorted(data.keys())
            if data.get("sessionId"):
                self.sid, self.sid_source = data["sessionId"], "state.json"
            self.state_json = data
        st.add({"argv": ["read", str(path)], "exit_code": 0 if data is not None else None, "elapsed_ms": 0,
                "stdout": trim(", ".join(sorted(data.keys())) if isinstance(data, dict) else ""), "stderr": "", "timed_out": False},
               primary=True)
        if data is None:
            st.fail("%s %s" % (path, "does not exist" if not path.is_file() else "could not be parsed"))
            return
        absent = [r["field"] for r in rows[:len(STATE_FIELDS)] if r["state_json"] == "absent" and r["agents_json"] == "absent"]
        st.ok("state.json exists with keys: %s. Bash fields found in neither file: %s" % (
            ", ".join(sorted(data.keys())), ", ".join(absent) or "none"))

    # --- 4. processes ------------------------------------------------------------------------

    def step_processes(self, st: Step):
        ps = lazy("psutil")
        if not ps:
            st.skip("psutil is not installed (py -m pip install psutil); the process tree is not answered")
            return
        pid = (self.agent_row or {}).get("pid")
        if not pid:
            st.skip("agents --json gave no pid for the session, so there is nothing to walk from")
            return
        rows, src = list_processes()
        by_pid = {r["pid"]: r for r in rows}
        st.details["source"] = src
        if pid not in by_pid:
            st.fail("the pid %s from agents --json is not a running process" % pid)
            return
        chain, cur = [], pid
        for _ in range(8):
            r = by_pid.get(cur)
            if not r:
                break
            chain.append(r)
            cur = r["ppid"]
        children = []

        def walk(p, depth=0):
            for r in rows:
                if r["ppid"] == p and r["pid"] != p and depth < 6:
                    children.append(r)
                    walk(r["pid"], depth + 1)

        walk(pid)
        seen, table = set(), []
        for kind, group in (("parent chain (child first)", chain), ("below the agents pid", children)):
            for r in group:
                if r["pid"] in seen:
                    continue
                seen.add(r["pid"])
                table.append({"kind": kind, "pid": r["pid"], "ppid": r["ppid"], "name": r["name"], "exe": r["exe"],
                              "cmdline": trim(r["cmdline"], 240), "create_time": r["create_time"]})
        spares = [{"pid": r["pid"], "cmdline": trim(r["cmdline"], 240)} for r in rows
                  if re.search(r"bg-spare|bg-pty-host|--bg-", r["cmdline"])]
        attach_re = re.compile(r"attach %s( |$)" % re.escape(self.job_id or "-"))
        st.details["tree"] = table
        st.details["spare_like_processes"] = spares[:20]
        st.details["agents_pid"] = pid
        st.details["name_with_id_of_attach_pattern"] = attach_re.pattern
        st.add({"argv": ["psutil", "walk", str(pid)], "exit_code": 0, "elapsed_ms": 0, "stdout": trim("\n".join(
            "%s ppid=%s %s" % (t["pid"], t["ppid"], t["name"]) for t in table)), "stderr": "", "timed_out": False}, primary=True)
        spare_text = ("%d process(es) on the machine look like bash's bg-spare / pty-host pattern" % len(spares)) if spares else \
            "no process matches bash's bg-spare / pty-host pattern (prune_spares has nothing to find on this OS)"
        st.ok("pid %s runs as %r under %s; %d process(es) in its tree; %s" % (
            pid, chain[0]["name"], " > ".join(c["name"] for c in chain[1:4]) or "(no parent)", len(table), spare_text))

    # --- hooks run by Claude Code ------------------------------------------------------------

    def step_hooks(self, st: Step):
        if not self.launch_ok:
            st.skip("no spike session was started, so Claude Code never ran the probe hooks")
            return

        def lines():
            try:
                t = self.hookfile.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                return None
            return t if len(t) >= 5 else None

        got = poll(lines, 20, 2)
        try:
            text = self.hookfile.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        got = [l for l in text.splitlines() if l.strip()]
        st.add({"argv": ["read", self.hookfile.as_posix()], "exit_code": 0 if got else None, "elapsed_ms": 0,
                "stdout": trim(text), "stderr": "", "timed_out": False}, primary=True)
        st.details["lines"] = [trim(l, 400) for l in got]
        if not got:
            st.fail("no probe hook wrote anything within 20 s: either SessionStart hooks do not run for --bg sessions, or the throwaway plugin was not loaded. The hook shell is not answered by this path")
            return
        facts = []
        for l in got:
            if l.startswith("probe-inline"):
                m_bash = re.search(r"bash=\[([^\]]*)\]", l)
                m_cmd = re.search(r"cmd=\[([^\]]*)\]", l)
                m_ps = re.search(r"ps=\[([^\]]*)\]", l)
                if m_bash and m_bash.group(1) and "$BASH" not in m_bash.group(1):
                    shell = "bash (%s)" % m_bash.group(1)
                elif m_cmd and m_cmd.group(1) and "%COMSPEC%" not in m_cmd.group(1):
                    shell = "cmd.exe (%s)" % m_cmd.group(1)
                elif m_ps and "Hashtable" in m_ps.group(1):
                    shell = "PowerShell"
                else:
                    shell = "unclear"
                st.details["inline_hook_shell"] = shell
                facts.append("an inline `command` string ran in: " + shell)
            elif l.startswith("probe-sh"):
                facts.append("`bash \"${CLAUDE_PLUGIN_ROOT}/hooks/probe.sh\"` ran (root has a space: %s)" % ("plugin dir with space" in l))
            else:
                try:
                    d = json.loads(l)
                    facts.append("%s ran on Python %s (%s)" % (d.get("hook"), d.get("version"), d.get("exe")))
                except Exception:
                    facts.append(one_line(l, 120))
        missing = [n for n in ("probe-inline", "probe-sh", "probe-py python ", "probe-py python3", "probe-py py") if not any(n in l for l in got)]
        st.ok("%d of 5 probe hooks wrote a line. %s. Did not run or write: %s" % (len(got), "; ".join(facts), ", ".join(m.strip() for m in missing) or "none"))

    # --- bg enabled --------------------------------------------------------------------------

    def step_bg_enabled(self, st: Step):
        if not self.need_claude(st):
            return
        rec = self.cl(st, ["--help"], timeout=30, primary=True)
        full = ""
        try:
            full = subprocess.run([self.claude, "--help"], capture_output=True, timeout=30, env=self.env).stdout.decode("utf-8", "replace")
        except Exception:
            full = rec["stdout"]
        in_help = "--bg" in full
        hits = []
        homes = [Path.home() / ".claude" / "settings.json"]
        for var in ("ProgramData", "ProgramFiles"):
            if os.environ.get(var):
                homes.append(Path(os.environ[var]) / "ClaudeCode" / "managed-settings.json")
        homes += [Path("/etc/claude-code/managed-settings.json"), Path("/Library/Application Support/ClaudeCode/managed-settings.json")]

        def keys(d, prefix=""):
            if isinstance(d, dict):
                for k, v in d.items():
                    p = prefix + "." + k if prefix else k
                    if re.search(r"bg|background|daemon|disable", k, re.I):
                        hits.append("%s=%s" % (p, v if isinstance(v, (bool, int)) else "<set>"))
                    keys(v, p)

        scanned = []
        for f in homes:
            try:
                if f.is_file():
                    scanned.append(str(f))
                    keys(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                scanned.append(str(f) + " (unreadable)")
        envs = sorted(k for k in os.environ if re.match(r"CLAUDE", k, re.I) and re.search(r"BG|BACKGROUND|DISABLE", k, re.I))
        st.details.update({"help_mentions_bg": in_help, "settings_files_read": scanned, "settings_keys_that_may_disable": hits,
                           "env_names_that_may_disable": envs})
        tail = "help mentions --bg: %s; settings keys that could disable it: %s; env names that could: %s" % (
            in_help, ", ".join(hits) or "none", ", ".join(envs) or "none")
        if self.launch_ok:
            st.ok("--bg is enabled: a session started. " + tail)
        elif self.launch_ok is False and self.claude:
            st.fail("--bg did not start a session, so it is disabled or broken here. " + tail)
        else:
            st.skip("not answered. " + tail)

    # --- 3. attach, logs, stop, rm, fork ------------------------------------------------------

    def step_attach(self, st: Step):
        if not self.need_claude(st):
            return
        if not self.job_id:
            st.skip("no session id to attach to")
            return
        pr = Proc([self.claude, "attach", self.job_id], cwd=self.cwd, env=self.env, stdin_pipe=True)
        if pr.error or not pr.p:
            st.add(pr.record(), primary=True)
            st.fail("claude attach could not be started: " + str(pr.error))
            return
        deadline = time.monotonic() + 5
        while pr.p.poll() is None and time.monotonic() < deadline:
            time.sleep(0.25)
        still = pr.p.poll() is None
        st.details["running_after_5s_with_idle_stdin"] = still
        outcome, seen_by = "", []
        if still:
            rows, src = list_processes()
            rx = re.compile(r"attach %s( |$)" % re.escape(self.job_id))
            seen_by = [r["pid"] for r in rows if rx.search(r["cmdline"])]
            st.details["attached_check"] = {"pattern": rx.pattern, "process_source": src, "matching_pids": seen_by,
                                            "our_attach_pid": pr.p.pid, "empty_cmdlines": sum(1 for r in rows if not r["cmdline"])}
            try:  # the left arrow leaves a session running (bash: "<- to leave it running")
                pr.p.stdin.write(b"\x1b[D")
                pr.p.stdin.flush()
            except Exception:
                pass
            try:
                pr.p.stdin.close()
            except Exception:
                pass
            t_left = time.monotonic()
            while pr.p.poll() is None and time.monotonic() < t_left + 6:
                time.sleep(0.25)
            left = pr.p.poll() is not None
            st.details["exited_after_left_arrow_and_stdin_close"] = left
            outcome = "stayed up with stdin idle; matching bash's pgrep pattern: %s; after left arrow and EOF it %s" % (
                "yes (pids %s)" % seen_by if seen_by else "NO process cmdline matched", "exited" if left else "did not exit (killed)")
        pr.wait(2)
        rec = pr.record()
        st.add(rec, primary=True)
        code = rec["exit_code"]
        text = rec["stdout"] + " " + rec["stderr"]
        if still:
            if seen_by:
                st.ok("attach worked non-interactively: " + outcome)
            else:
                st.fail("attach ran but no process command line matches the `attach <id>` pattern bash's attached() uses; " + outcome)
        elif code == 0:
            st.ok("attach exited 0 on its own after %d ms (stdin closed counts as detach)" % rec["elapsed_ms"])
        elif re.search(r"tty|terminal|interactive|isatty|raw mode|stdin", text, re.I):
            st.skip("not answered: attach needs a real terminal (exit %s: %s). Check by hand: start any `claude --bg -n x hi`, run `claude attach <id>` in a normal terminal, press the left arrow, confirm the session keeps running" % (code, one_line(text, 160)))
        else:
            st.fail("attach exited %s after %d ms: %s" % (code, rec["elapsed_ms"], one_line(text, 200)))

    def step_logs(self, st: Step):
        if not self.need_claude(st):
            return
        if not self.job_id:
            st.skip("no session id")
            return
        rec = self.cl(st, ["logs", self.job_id], timeout=20, primary=True)
        if rec["exit_code"] == 0 and not rec["timed_out"]:
            st.ok("claude logs printed %d chars and exited 0" % len(rec["stdout"]))
        elif rec["timed_out"] and (rec["stdout"] or rec["stderr"]):
            st.ok("claude logs printed output and kept running (it follows the log); stopped after 20 s")
        else:
            st.fail("claude logs exit %s, timed out %s: %s" % (rec["exit_code"], rec["timed_out"], one_line(rec["stderr"] or rec["stdout"], 200)))

    def step_stop(self, st: Step):
        if not self.need_claude(st):
            return
        if not self.job_id:
            st.skip("no session id")
            return
        rec = self.cl(st, ["stop", self.job_id], timeout=40, primary=True)
        rows, _ = self.agents(st)
        row = self.find_row(rows, id_=self.job_id)
        after = ("no longer listed" if rows is not None and not row else "state=%s pid=%s" % (
            (row or {}).get("state"), (row or {}).get("pid", "(none)"))) if rows is not None else "agents --json unreadable"
        st.details["after_stop"] = after
        if rec["exit_code"] == 0:
            st.ok("claude stop exited 0 in %d ms; afterwards the session is %s" % (rec["elapsed_ms"], after))
        else:
            st.fail("claude stop exit %s: %s" % (rec["exit_code"], one_line(rec["stderr"] or rec["stdout"], 200)))

    def step_fork(self, st: Step):
        if not self.need_claude(st):
            return
        if not self.sid:
            st.skip("no conversation id (not in state.json or agents --json) to resume")
            return
        rec = self.cl(st, ["-p", "--resume", self.sid, "--fork-session", "Reply with the single word ok and nothing else."],
                      timeout=120, primary=True)
        if rec["exit_code"] == 0 and rec["stdout"].strip():
            st.ok("claude -p --resume <sid> --fork-session answered in %d ms: %r" % (rec["elapsed_ms"], one_line(rec["stdout"], 60)))
        else:
            st.fail("fork-session exit %s, timed out %s: %s" % (rec["exit_code"], rec["timed_out"], one_line(rec["stderr"] or rec["stdout"], 200)))

    def step_resume(self, st: Step):
        if not self.need_claude(st):
            return
        if not self.sid:
            st.skip("no conversation id known (state.json and agents --json both lacked sessionId), so --resume cannot be tried")
            return
        name = self.name + "-r"
        sid2, rec = self.launch_once(st, name, resume=self.sid)
        if not sid2:
            st.fail("claude --bg --resume %s did not start a session (exit %s): %s" % (
                "<sid from %s>" % self.sid_source, rec["exit_code"], one_line(rec["stdout"] + " " + rec["stderr"], 240)))
            return

        def listed():
            rows, _ = self.agents(st)
            return self.find_row(rows, id_=sid2)

        row = poll(listed, 15, 1.5)
        same = bool(row and row.get("sessionId") in (None, self.sid)) if row else None
        st.ok("--resume <sid> started session %s; agents --json lists it: %s; same conversation id: %s" % (
            sid2, bool(row), "unknown" if row and not row.get("sessionId") else same))

    def step_rm(self, st: Step):
        if not self.need_claude(st):
            return
        if not self.job_id:
            st.skip("no session id")
            return
        self.cl(st, ["stop", self.job_id], timeout=40, keep=False)
        rec = self.cl(st, ["rm", self.job_id], timeout=40, primary=True)
        rows, _ = self.agents(st, all_=True)
        gone = rows is not None and not self.find_row(rows, id_=self.job_id)
        dir_gone = not (self.jobs / self.job_id).exists()
        if rec["exit_code"] == 0:
            for s in self.sessions:
                if s["id"] == self.job_id:
                    s["removed"] = True
            st.ok("claude rm exited 0; gone from `agents --json --all`: %s; job folder gone: %s" % (gone, dir_gone))
        else:
            st.fail("claude rm exit %s: %s" % (rec["exit_code"], one_line(rec["stderr"] or rec["stdout"], 200)))

    def step_version(self, st: Step):
        if not self.need_claude(st):
            return
        rec = self.cl(st, ["--version"], timeout=30, primary=True)
        if rec["exit_code"] == 0:
            st.ok("claude --version: %s (resolved to %s)" % (one_line(rec["stdout"], 80), self.claude))
        else:
            st.fail("claude --version exit %s: %s" % (rec["exit_code"], one_line(rec["stderr"], 200)))

    # --- cleanup -----------------------------------------------------------------------------

    def cleanup_needed(self):
        return bool(self.claude) and any(not s["removed"] for s in self.sessions)

    def step_cleanup(self, st: Step):
        if not self.claude:
            st.skip("no claude to clean up with")
            return
        for s in self.sessions:
            if s["removed"]:
                continue
            self.cl(st, ["stop", s["id"]], timeout=40)
            rec = self.cl(st, ["rm", s["id"]], timeout=40)
            s["removed"] = rec["exit_code"] == 0
        left = []
        rows, _ = self.agents(st, all_=True)
        if rows:
            for r in rows:  # also any session with our name prefix whose id we never parsed
                if isinstance(r, dict) and str(r.get("name", "")).startswith(self.name):
                    self.cl(st, ["stop", r["id"]], timeout=40)
                    rec = self.cl(st, ["rm", r["id"]], timeout=40)
                    if rec["exit_code"] != 0:
                        left.append(r["id"])
            rows, _ = self.agents(st, all_=True)
            left = [r.get("id") for r in (rows or []) if isinstance(r, dict) and str(r.get("name", "")).startswith(self.name)]
        if left:
            st.fail("could not remove: %s. Run `claude stop <id>` and `claude rm <id>` for them by hand" % ", ".join(map(str, left)))
        else:
            st.ok("every spike session was stopped and removed (%d started)" % len(self.sessions))

    def cleanup(self):
        if self.cleaned and not self.cleanup_needed():
            return
        name = "cleanup" if not self.cleaned else "cleanup_late"
        self.cleaned = True
        self.step(name, 3, "Cleanup: stop and rm every session the spike started", self.step_cleanup)

    # --- 5. shell, python --------------------------------------------------------------------

    def step_shell_tools(self, st: Step):
        facts = {}
        if IS_WIN:
            st.run(["where", "bash"], 20, primary=True)
            st.run(["where", "git"], 20)
        facts["bash_on_path"] = shutil.which("bash")
        facts["git_on_path"] = shutil.which("git")
        facts["COMSPEC"] = os.environ.get("COMSPEC")
        gitexe = facts["git_on_path"]
        if gitexe:
            st.run([gitexe, "--version"], 20, primary=not IS_WIN)
        for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe",
                     r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if IS_WIN and Path(cand).exists():
                facts.setdefault("git_for_windows_bash", []).append(cand)
        facts["WSL_bash_shim"] = bool(facts["bash_on_path"] and "system32" in (facts["bash_on_path"] or "").lower())
        st.details = facts
        gfw = IS_WIN and (bool(facts.get("git_for_windows_bash")) or bool(gitexe and "\\git\\" in gitexe.lower()))
        st.ok("bash on PATH: %s; git on PATH: %s; Git for Windows present: %s; COMSPEC=%s%s" % (
            facts["bash_on_path"] or "no", gitexe or "no", gfw if IS_WIN else "n/a (not Windows)", facts["COMSPEC"] or "(unset)",
            "; the bash on PATH is in System32, i.e. the WSL launcher, not Git Bash" if facts["WSL_bash_shim"] else ""))

    def step_space(self, st: Step):
        d = self.plugin
        (d / "hello.txt").write_text("hello from a path with a space\n", encoding="utf-8")
        results = []
        bash = shutil.which("bash")
        # ${CLAUDE_PLUGIN_ROOT} expands to d, quoted or not
        root_win, root_posix = str(d), d.as_posix()
        if IS_WIN:
            for label, cmd in (("cmd /c quoted", 'type "%s"' % (root_win + "\\hello.txt")),
                               ("cmd /c unquoted", "type %s" % (root_win + "\\hello.txt"))):
                rec = st.run("cmd /c " + cmd, 20, primary=not results)
                results.append((label, rec))
        else:
            results.append(("cmd /c (Windows only)", None))
        if bash:
            for label, cmd in (("bash -c quoted", 'cat "%s"' % (root_posix + "/hello.txt")),
                               ("bash -c unquoted", "cat %s" % (root_posix + "/hello.txt"))):
                rec = st.run([bash, "-c", cmd], 20, primary=not st.primary)
                results.append((label, rec))
        else:
            results.append(("bash -c", None))
        rows = []
        for label, rec in results:
            if rec is None:
                rows.append({"form": label, "result": "skipped (%s)" % ("not Windows" if "cmd" in label else "no bash on PATH")})
            else:
                good = rec["exit_code"] == 0 and "hello" in rec["stdout"]
                rows.append({"form": label, "result": "works" if good else "FAILS (exit %s: %s)" % (rec["exit_code"], one_line(rec["stderr"] or rec["stdout"], 80))})
        st.details["forms"] = rows
        ran = [r for r in rows if not r["result"].startswith("skipped")]
        if not ran:
            st.skip("neither cmd nor bash could run the path-with-space check")
        else:
            bad = [r["form"] for r in ran if r["result"].startswith("FAILS")]
            st.ok("; ".join("%s: %s" % (r["form"], r["result"]) for r in rows) + ("" if not bad else ". Unquoted ${CLAUDE_PLUGIN_ROOT} breaks in: " + ", ".join(bad)))

    def step_python_names(self, st: Step):
        table = []
        for n in ("python", "py", "python3"):
            path = shutil.which(n)
            row = {"name": n, "path": path, "version": None, "exit": None, "store_stub": None}
            if path:
                rec = st.run([path, "--version" if n != "py" else "-V"], 20, primary=not table, keep=bool(not table))
                row["exit"], row["version"] = rec["exit_code"], one_line(rec["stdout"] or rec["stderr"], 60)
                row["store_stub"] = "windowsapps" in path.lower() and rec["exit_code"] not in (0,)
            table.append(row)
        st.details["names"] = table
        working = [r["name"] for r in table if r["path"] and r["exit"] == 0]
        text = "; ".join("%s: %s" % (r["name"], ("%s (%s)%s" % (r["version"], r["path"], " STORE STUB" if r["store_stub"] else "")) if r["path"] else "not on PATH") for r in table)
        if working:
            st.ok("working: %s. %s" % (", ".join(working), text))
        else:
            st.fail("no python, py or python3 runs. " + text)

    def step_cold_start(self, st: Step):
        rows = []
        for n in ("python", "py", "python3"):
            path = shutil.which(n)
            if not path:
                rows.append({"name": n, "runs_ms": [], "note": "not on PATH"})
                continue
            times, codes = [], []
            for i in range(5):
                rec = run_cmd([path, "-c", "pass"], timeout=30)
                times.append(rec["elapsed_ms"])
                codes.append(rec["exit_code"])
                if n == "python" and i == 0:
                    st.add(rec, primary=True)
            row = {"name": n, "runs_ms": times, "exit_codes": sorted(set(map(str, codes)))}
            if all(c == 0 for c in codes):
                row.update(median_ms=int(statistics.median(times)), min_ms=min(times), max_ms=max(times))
            else:
                row["note"] = "exit codes %s: not a working interpreter" % row["exit_codes"]
            rows.append(row)
        st.details = {"target_ms": COLD_START_TARGET_MS, "interpreters": rows}
        good = [r for r in rows if "median_ms" in r]
        text = "; ".join("%s median %s ms (min %s, max %s)" % (r["name"], r["median_ms"], r["min_ms"], r["max_ms"]) if "median_ms" in r
                         else "%s: %s" % (r["name"], r.get("note")) for r in rows)
        if not good:
            st.fail("no interpreter ran: " + text)
        elif min(r["median_ms"] for r in good) <= COLD_START_TARGET_MS:
            over = [r["name"] for r in good if r["median_ms"] > COLD_START_TARGET_MS]
            st.ok("within the %d ms target (hook limit 5 s): %s%s" % (COLD_START_TARGET_MS, text, ("; OVER target: " + ", ".join(over)) if over else ""))
        else:
            st.fail("every interpreter is over the %d ms target: %s" % (COLD_START_TARGET_MS, text))

    # --- 6. tares ----------------------------------------------------------------------------

    def step_tares(self, st: Step):
        found = shutil.which("tares")
        parts = ["shutil.which(tares): %s" % (found or "not found")]
        if IS_WIN:
            r = st.run(["where", "tares"], 20, primary=True)
            parts.append("`where tares` (exit %s): %s" % (r["exit_code"], one_line(r["stdout"], 100) or "nothing"))
            r = st.run(["cmd", "/c", "where tares"], 20)
            parts.append("from cmd (exit %s): %s" % (r["exit_code"], one_line(r["stdout"], 100) or "nothing"))
        bash = shutil.which("bash")
        if bash:
            r = st.run([bash, "-lc", "command -v tares"], 30, primary=not IS_WIN)
            parts.append("from Git Bash `bash -lc \"command -v tares\"` (exit %s): %s" % (r["exit_code"], one_line(r["stdout"], 100) or "nothing"))
        else:
            parts.append("no bash on PATH to ask")
        if found:
            r = st.run([found, "--version"], 30)
            parts.append("tares --version exit %s: %s" % (r["exit_code"], one_line(r["stdout"] or r["stderr"], 60)))
        st.details = {"which": found}
        (st.ok if found else st.fail)("; ".join(parts))

    # --- 7. detached child -------------------------------------------------------------------

    def step_detached(self, st: Step):
        secs = 6 if self.dry else 60
        shell_pid = nearest_shell_pid()
        argv = [sys.executable, str(Path(__file__).resolve()), "--heartbeat-child", str(self.heartbeat), str(secs), str(shell_pid)]
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        if IS_WIN:
            kw["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000)  # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        t0 = time.monotonic()
        p = subprocess.Popen(argv, **kw)
        st.add({"argv": display_argv(argv), "exit_code": None, "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "stdout": "", "stderr": "", "timed_out": False}, primary=True)
        st.details = {"child_pid": p.pid, "heartbeat_file": str(self.heartbeat), "seconds": secs, "shell_pid": shell_pid,
                      "flags": "DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW" if IS_WIN else "start_new_session"}
        poll(lambda: self.heartbeat.exists(), 10, 0.5)
        if self.dry:
            time.sleep(3)
            status, finding, det = check_detached(self.heartbeat, require_shell_gone=False, proc=p)
            st.details.update(det)
            st.status, st.finding = status, "dry run: " + finding
            return
        st.skip("NOT ANSWERED YET: the detached child is running (pid %s, %d s heartbeat). Close this terminal, wait 20 s, open a new one and run: "
                "py scripts\\windows_spike.py --check-detached \"%s\"" % (p.pid, secs, self.heartbeat))

    # --- 8. keyring --------------------------------------------------------------------------

    def kr_child(self, st: Step, mode: str, primary=False):
        argv = [sys.executable, str(Path(__file__).resolve()), mode, "--out", str(self.out)] + (["--dry-run"] if self.dry else [])
        rec = st.run(argv, 60, primary=primary)
        info = {}
        for line in (rec["stdout"] or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        return rec, info

    def step_keyring(self, st: Step):
        if not self.dry and not lazy("keyring"):
            st.skip("keyring is not installed (py -m pip install keyring); the keychain is not answered")
            return
        wrote = False
        try:
            r1, w = self.kr_child(st, "--keyring-write", primary=True)
            wrote = r1["exit_code"] == 0
            if not wrote:
                st.fail("process 1 could not write: %s" % one_line(r1["stderr"] or r1["stdout"], 240))
                return
            r2, rd = self.kr_child(st, "--keyring-read")
            if r2["exit_code"] != 0:
                st.fail("process 2 could not read: %s" % one_line(r2["stderr"] or r2["stdout"], 240))
                return
            same = bool(w.get("sha256_prefix")) and w.get("sha256_prefix") == rd.get("sha256_prefix")
            st.details = {"backend": w.get("backend"), "write_prefix": w.get("sha256_prefix"), "read_prefix": rd.get("sha256_prefix")}
            if same:
                st.ok("process 2 read the value process 1 wrote (sha256 prefix %s matched); backend: %s" % (w["sha256_prefix"], w.get("backend")))
            else:
                st.fail("process 2 read %s but process 1 wrote %s; backend: %s" % (rd.get("sha256_prefix"), w.get("sha256_prefix"), w.get("backend")))
        finally:
            if wrote:
                r3, dl = self.kr_child(st, "--keyring-delete")
                st.details["deleted"] = dl.get("deleted")
                if r3["exit_code"] != 0 or dl.get("deleted") != "yes":
                    st.finding += " WARNING: delete failed, remove the credential %s / %s by hand." % (SPIKE_SERVICE, SPIKE_USER)

    # --- the run -----------------------------------------------------------------------------

    def step_env(self, st: Step):
        st.details = {
            "platform": platform.platform(), "python": sys.version.split()[0], "executable": sys.executable,
            "is_windows": IS_WIN, "dry_run": self.dry, "psutil": bool(lazy("psutil")), "keyring": bool(lazy("keyring")),
            "claude": self.claude, "claude_note": self.claude_why, "cwd": self.cwd, "home": str(Path.home()),
            "jobs_dir_exists": self.jobs.is_dir(), "session_name": self.name, "out": str(self.out),
            "stdout_encoding": getattr(sys.stdout, "encoding", None),
        }
        st.ok("%s, Python %s, psutil %s, keyring %s, claude %s%s" % (
            platform.platform(), sys.version.split()[0], "installed" if lazy("psutil") else "MISSING", "installed" if lazy("keyring") else "MISSING",
            self.claude or "NOT FOUND", " (dry run)" if self.dry else ""))

    def run(self):
        T = self.step
        T("env", 0, "Environment", self.step_env)
        T("setup", 1, "Throwaway plugin and settings files", self.setup_files)
        try:
            T("claude_version", 3, "claude --version", self.step_version)
            T("launch", 1, "claude --bg with settings, plugin dir and an 8 KB system prompt", self.step_launch)
            T("agents_json", 2, "claude agents --json lists the session", self.step_agents)
            T("state_json", 2, "~/.claude/jobs/<id>/state.json and the fields bash reads", self.step_state)
            T("processes", 4, "Processes of the background session (psutil)", self.step_processes)
            T("hook_via_claude", 5, "Which shell Claude Code runs hook commands in (probe hooks of the spike plugin)", self.step_hooks)
            T("bg_enabled", 3, "Is --bg enabled", self.step_bg_enabled)
            T("attach", 3, "claude attach <id> (stdin closed)", self.step_attach)
            T("logs", 3, "claude logs <id>", self.step_logs)
            T("stop", 3, "claude stop <id>", self.step_stop)
            T("fork_session", 3, "claude -p --resume <sid> --fork-session", self.step_fork)
            T("resume_bg", 1, "claude --bg ... --resume <sid>", self.step_resume)
            T("rm", 3, "claude rm <id>", self.step_rm)
            self.cleanup()
            T("shell_tools", 5, "bash, git, Git for Windows, COMSPEC", self.step_shell_tools)
            T("space_expansion", 5, "${CLAUDE_PLUGIN_ROOT} with a space, cmd /c and bash -c", self.step_space)
            T("python_names", 5, "python, py, python3 on PATH", self.step_python_names)
            T("python_cold_start", 5, "Python cold start (5 runs each)", self.step_cold_start)
            T("tares_exe", 6, "tares on PATH from where, Git Bash and cmd", self.step_tares)
            T("keyring", 8, "Keychain across two processes", self.step_keyring)
            T("detached_child", 7, "Detached child survives closing the terminal", self.step_detached)
        except BaseException as e:  # Ctrl+C or a bug outside a step: still write what we have
            self.steps.append({"name": "interrupted", "item": 0, "title": "Run interrupted", "status": "fail",
                               "finding": "the run stopped early: %s %s" % (type(e).__name__, e), "argv": None, "exit_code": None,
                               "elapsed_ms": 0, "stdout": "", "stderr": "", "commands": [], "details": {}})
        finally:
            try:
                if self.cleanup_needed() or not self.cleaned:
                    self.cleanup()
            except BaseException as e:
                print("cleanup failed: %r; stop and rm sessions named %s by hand" % (e, self.name))
        return self.results()

    def results(self) -> dict:
        return {"meta": {"generated": now_iso(), "tool": "scripts/windows_spike.py (TR-946)", "session_name": self.name,
                         "dry_run": self.dry, "windows": IS_WIN, "platform": platform.platform(), "python": sys.version.split()[0],
                         "out": str(self.out), "heartbeat_file": str(self.heartbeat)},
                "steps": self.steps}


# ---------------------------------------------------------------------------------------------
# hidden modes: heartbeat child, check-detached, keyring
# ---------------------------------------------------------------------------------------------


def nearest_shell_pid() -> int:
    """The terminal's shell: the closest ancestor that is not a Python or py launcher."""
    me = os.getppid()
    ps = lazy("psutil")
    if not ps:
        return me
    try:
        p = ps.Process(os.getpid()).parent()
        for _ in range(6):
            if p is None:
                break
            if not re.match(r"(py|pyw|python[\d.]*|pythonw|uv)(\.exe)?$", (p.name() or "").lower()):
                return p.pid
            p = p.parent()
    except Exception:
        pass
    return me


def write_atomic(path: Path, text: str):
    tmp = path.with_suffix(".tmp")
    for _ in range(5):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(str(tmp), str(path))
            return
        except OSError:
            time.sleep(0.05)


def heartbeat_child(path: Path, seconds: float, shell_pid: int):
    stop = Path(str(path) + ".stop")
    start = time.time()
    data = {"pid": os.getpid(), "started": start, "until": start + seconds, "count": 0, "last": start, "done": False,
            "shell_pid": shell_pid, "shell_gone_at": None, "stopped_by_marker": False}
    while True:
        now = time.time()
        data["count"] += 1
        data["last"] = now
        if data["shell_gone_at"] is None and not pid_alive(shell_pid):
            data["shell_gone_at"] = now
        done = now >= data["until"] or stop.exists()
        if done:
            data["done"] = True
            data["stopped_by_marker"] = stop.exists()
        write_atomic(path, json.dumps(data))
        if done:
            return
        time.sleep(1)


def read_heartbeat(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def check_detached(path: Path, require_shell_gone: bool = True, stop_child: bool = True, proc=None):
    """(status, finding, details). Stops the child at the end unless the check proved nothing yet."""
    d1 = read_heartbeat(path)
    if not d1:
        return "fail", "no readable heartbeat file at %s: the child never started or it was deleted" % path, {}
    if not d1.get("done"):
        time.sleep(3)
    d2 = read_heartbeat(path) or d1
    advancing = d2["count"] > d1["count"]
    finished = bool(d2.get("done")) and not d2.get("stopped_by_marker")
    age = time.time() - d2["last"]
    gone_at = d2.get("shell_gone_at")
    det = {"beats_first_read": d1["count"], "beats_second_read": d2["count"], "last_beat_age_s": round(age, 1),
           "child_pid": d2["pid"], "shell_pid": d2.get("shell_pid"), "shell_gone_at": gone_at,
           "survived_s_after_shell_gone": round(d2["last"] - gone_at, 1) if gone_at else None,
           "finished_its_full_run": finished}
    survived = advancing or finished
    if survived and (gone_at or not require_shell_gone):
        if gone_at:
            msg = "the child kept writing heartbeats %.0f s after the terminal's shell (pid %s) ended: it survived closing the terminal" % (d2["last"] - gone_at, d2.get("shell_pid"))
        else:
            msg = "the heartbeat is advancing (%d beats); the terminal-close part was not exercised in a dry run" % d2["count"]
        status = "ok"
    elif survived:
        msg = ("the heartbeat is advancing but the shell (pid %s) that started the script is still running, so this proves nothing about closing the terminal. "
               "Close that terminal and run the check again (the child is left running)." % d2.get("shell_pid"))
        status, stop_child = "skipped", False
    else:
        msg = ("the heartbeat stopped %.0f s ago at beat %d, before its planned end: the child died%s" % (
            age, d2["count"], " when the terminal closed" if gone_at else " (the shell is %s)" % ("still open" if pid_alive(d2.get("shell_pid")) else "gone")))
        status = "fail"
    if stop_child:
        try:
            Path(str(path) + ".stop").write_text("stop\n", encoding="utf-8")
        except Exception:
            pass
        def alive():
            return proc.poll() is None if proc is not None else pid_alive(d2["pid"])

        end = time.monotonic() + 5
        while time.monotonic() < end and alive():
            time.sleep(0.5)
        if alive():  # it ignored the marker: kill it only when its command line is ours
            rows, _ = list_processes()
            for r in rows:
                if r["pid"] == d2["pid"] and "--heartbeat-child" in r["cmdline"]:
                    kill_tree(d2["pid"])
        det["child_stopped"] = not alive()
        msg += "; the child was stopped" if det["child_stopped"] else "; WARNING the child (pid %s) is still running, end it in Task Manager" % d2["pid"]
    return status, msg, det


def check_detached_mode(file: str) -> int:
    path = Path(file)
    status, msg, det = check_detached(path, require_shell_gone=True)
    print("Detached child: %s" % status.upper())
    print(msg)
    rj = path.parent / RESULTS_JSON
    if rj.is_file():
        try:
            results = json.loads(rj.read_text(encoding="utf-8"))
            for s in results["steps"]:
                if s["name"] == "detached_child":
                    s["status"], s["finding"] = status, msg
                    s["details"].update(det)
                    s["details"]["checked_at"] = now_iso()
            write_outputs(results, path.parent)
            print("Updated %s and %s" % (path.parent / RESULTS_JSON, path.parent / RESULTS_MD))
            print("Paste %s back." % (path.parent / RESULTS_MD))
        except Exception as e:
            print("could not update the results files: %r (the answer above is still valid)" % e)
    return 0


class FileKeyring:
    """Dry-run stand-in: a JSON file in the results folder (proves the two-process logic only)."""
    name = "dry-run file backend (not a real keychain)"

    def __init__(self, out: Path):
        self.f = out / "dry_run_keyring.json"

    def _load(self):
        try:
            return json.loads(self.f.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def set_password(self, s, u, v):
        d = self._load()
        d["%s/%s" % (s, u)] = v
        self.f.write_text(json.dumps(d), encoding="utf-8")

    def get_password(self, s, u):
        return self._load().get("%s/%s" % (s, u))

    def delete_password(self, s, u):
        d = self._load()
        d.pop("%s/%s" % (s, u), None)
        self.f.write_text(json.dumps(d), encoding="utf-8")


def keyring_mode(mode: str, out: str, dry: bool) -> int:
    if dry:
        kr, backend = FileKeyring(Path(out)), FileKeyring.name
    else:
        kr = lazy("keyring")
        if not kr:
            print("keyring is not installed", file=sys.stderr)
            return 3
        try:
            b = kr.get_keyring()
            backend = "%s.%s" % (type(b).__module__, type(b).__name__)
        except Exception as e:
            backend = "unknown (%r)" % e
    try:
        print("backend=%s" % backend)
        if mode == "write":
            secret = secrets.token_urlsafe(24)
            kr.set_password(SPIKE_SERVICE, SPIKE_USER, secret)
            same = kr.get_password(SPIKE_SERVICE, SPIKE_USER) == secret
            print("sha256_prefix=%s" % hashlib.sha256(secret.encode()).hexdigest()[:12])
            print("read_back_in_same_process=%s" % ("yes" if same else "no"))
        elif mode == "read":
            v = kr.get_password(SPIKE_SERVICE, SPIKE_USER)
            print("sha256_prefix=%s" % (hashlib.sha256(v.encode()).hexdigest()[:12] if v else "none"))
        else:
            kr.delete_password(SPIKE_SERVICE, SPIKE_USER)
            print("deleted=%s" % ("yes" if kr.get_password(SPIKE_SERVICE, SPIKE_USER) is None else "no"))
        return 0
    except Exception as e:
        print("keyring %s failed: %s: %s" % (mode, type(e).__name__, e), file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------------------------
# verdict and report
# ---------------------------------------------------------------------------------------------

HARD = [
    ("launch", "claude --bg starts a session"),
    ("agents_json", "claude agents --json lists the session with id, name, cwd and state"),
    ("attach", "claude attach works"),
    ("detached_child", "a detached child survives closing the terminal"),
    ("keyring", "keyring works across two processes"),
]

ITEMS = [
    (1, "1. Launch with --bg, an 8 KB system prompt, and --resume"),
    (2, "2. agents --json and the state.json fields"),
    (3, "3. attach, logs, stop, rm, fork-session, version, and is --bg enabled"),
    (4, "4. Processes (the attached check and spare pruning)"),
    (5, "5. Hook shell, ${CLAUDE_PLUGIN_ROOT} with a space, Python cold start"),
    (6, "6. tares.exe on PATH"),
    (7, "7. Detached child"),
    (8, "8. Keychain"),
]

PLAN_B = """\
Detached `claude -p` sessions driven by the watch. Used only if `claude --bg` or `claude agents --json` cannot be relied on.

What we lose:
- Claude Code's own session list (`agents --json`) and its state: the watch could no longer ask "is it working, blocked or done".
- `claude attach`, `claude logs`, `claude stop`, `claude rm` for these sessions. A person could only follow a station by tailing a log the watch keeps, or by resuming its conversation in a terminal after stopping the background process.

What the watch must build:
- Its own process table (station, pid, start time, cwd, conversation id, log file), checked every round with psutil or tasklist, and rebuilt after a restart.
- One detached `claude -p --resume <sid>` process per turn (stream-json output into a log file). Done, working and blocked are inferred from that stream and the exit code, instead of read from Claude Code.
- Its own quiet check (what `inFlight` and `updatedAt` give today), its own stop (kill the process tree), and its own wake-ups for stations that sleep between turns.

Rough cost, an estimate: 4 to 6 working days. Process table and logs 1 day; the turn loop with resume 1 to 2 days; quiet, blocked and wake detection from the stream 1 to 2 days; an attach replacement 0.5 day; fakes and tests 1 day. It also adds a cold Claude Code start for every turn.

This spike marks the result no-go until Ashish decides whether to accept Plan B.
"""


def step_by_name(results, name):
    for s in results["steps"]:
        if s["name"] == name:
            return s
    return None


def verdict(results):
    rows = []
    for name, label in HARD:
        s = step_by_name(results, name)
        if s is None or s["status"] == "skipped":
            ans = "not answered"
        else:
            ans = "yes" if s["status"] == "ok" else "NO"
        rows.append((name, label, ans, s["finding"] if s else "the step did not run"))
    go = all(r[2] == "yes" for r in rows)
    plan_b = any(r[0] in ("launch", "agents_json") and r[2] == "NO" for r in rows)
    return rows, go, plan_b


def cell(text) -> str:
    return re.sub(r"\s+", " ", str(text if text is not None else "")).replace("|", "\\|").strip()


def fence(text: str) -> str:
    return "```\n%s\n```" % str(text).replace("```", "'''")


def render_markdown(results: dict) -> str:
    m = results["meta"]
    rows, go, plan_b = verdict(results)
    L = ["# Windows spike results (TR-946)", "",
         "Generated %s by `%s`. %s, Python %s%s. Results folder: `%s`." % (
             m["generated"], m["tool"], m["platform"], m["python"], " (DRY RUN, not a Windows result)" if m["dry_run"] else "", m["out"]),
         "", "## Summary", "", "| Step | Status | ms | Finding |", "|---|---|---|---|"]
    for s in results["steps"]:
        L.append("| %s | %s | %s | %s |" % (s["name"], s["status"], s["elapsed_ms"], cell(one_line(s["finding"], 140))))
    L += ["", "## Steps in detail", ""]
    for s in results["steps"]:
        L += ["### %s: %s" % (s["name"], s["status"].upper()), "", "%s" % s["title"], "", "Finding: %s" % s["finding"], ""]
        for c in s.get("commands") or []:
            L.append("- `%s` exit %s, %s ms%s" % (cell(" ".join(json.dumps(a) if " " in a else a for a in c["argv"])) if c.get("argv") else "(none)",
                                                   c.get("exit_code"), c.get("elapsed_ms"), ", TIMED OUT" if c.get("timed_out") else ""))
            if c.get("stdout"):
                L += ["", "  stdout:", "", fence(c["stdout"]), ""]
            if c.get("stderr"):
                L += ["", "  stderr:", "", fence(c["stderr"]), ""]
        L.append("")
    if plan_b:
        L += ["## Plan B", "", PLAN_B]
    L += ["## Items for the note", "",
          "Paste this section as the note \"Windows spike results\".", ""]
    L += ["Result: go" if go else "Result: no-go until decided", ""]
    if not go:
        bad = [r for r in rows if r[2] != "yes"]
        L.append("Hard requirements not met: " + "; ".join("%s (%s)" % (r[1], "not answered" if r[2] == "not answered" else "failed") for r in bad) + ".")
        L.append("")
    L += ["| Hard requirement | Answer |", "|---|---|"] + ["| %s | %s |" % (r[1], r[2]) for r in rows] + [""]
    for n, heading in ITEMS:
        L += ["### " + heading, ""]
        steps = [s for s in results["steps"] if s["item"] == n and s["name"] not in ("setup",)]
        if not steps:
            L.append("- not answered")
        for s in steps:
            label = {"ok": "ok", "fail": "FAILED", "skipped": "not answered"}.get(s["status"], s["status"])
            L.append("- %s [%s]: %s" % (s["title"], label, one_line(s["finding"], 700)))
            ft = s.get("details", {}).get("field_table")
            if ft:
                L += ["", "| Field | Bash uses it for | state.json | agents --json |", "|---|---|---|---|"]
                L += ["| %s | %s | %s | %s |" % (cell(r["field"]), cell(r["bash_use"]), cell(r["state_json"]), cell(r["agents_json"])) for r in ft]
                L.append("")
            tree = s.get("details", {}).get("tree")
            if tree:
                L += ["", "| Kind | pid | ppid | name | exe | cmdline | started |", "|---|---|---|---|---|---|---|"]
                L += ["| %s | %s | %s | %s | %s | %s | %s |" % (cell(t["kind"]), t["pid"], t["ppid"], cell(t["name"]), cell(t["exe"]),
                                                              cell(t["cmdline"]), cell(t["create_time"])) for t in tree]
                L.append("")
            if s["name"] == "python_cold_start":
                L += ["", "| Interpreter | runs (ms) | median | min | max |", "|---|---|---|---|---|"]
                for r in s["details"].get("interpreters", []):
                    L.append("| %s | %s | %s | %s | %s |" % (r["name"], ", ".join(map(str, r["runs_ms"])) or r.get("note", ""),
                                                             r.get("median_ms", "-"), r.get("min_ms", "-"), r.get("max_ms", "-")))
                L.append("")
            if s["name"] == "python_names":
                L += ["", "| Name | Path | Version | Exit |", "|---|---|---|---|"]
                for r in s["details"].get("names", []):
                    L.append("| %s | %s | %s | %s |" % (r["name"], cell(r["path"] or "not on PATH"), cell(r["version"] or ""), r["exit"]))
                L.append("")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def write_outputs(results: dict, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / RESULTS_JSON).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / RESULTS_MD).write_text(render_markdown(results), encoding="utf-8")


def print_summary(results: dict):
    print()
    print("%-20s %-8s %7s  %s" % ("STEP", "STATUS", "MS", "FINDING"))
    print("-" * 100)
    for s in results["steps"]:
        print("%-20s %-8s %7s  %s" % (s["name"], s["status"], s["elapsed_ms"], one_line(s["finding"], 62)))
    rows, go, plan_b = verdict(results)
    print()
    print("Result: go" if go else "Result: no-go until decided")
    for r in rows:
        print("  %-62s %s" % (r[1], r[2]))
    if plan_b:
        print("  Plan B section written to the Markdown (--bg or agents --json failed).")


# ---------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="Windows spike for TR-946 (throwaway). See the top of this file.")
    ap.add_argument("--out", help="results folder (default: a fresh temp folder)")
    ap.add_argument("--cwd", help="folder the spike sessions start in (default: the current folder; Claude Code must trust it)")
    ap.add_argument("--dry-run", action="store_true", help="safe mode for Mac/Linux (automatic when the OS is not Windows)")
    ap.add_argument("--check-detached", metavar="HEARTBEAT_FILE", help="step 7 part two: run after closing the terminal")
    ap.add_argument("--heartbeat-child", nargs=3, metavar=("FILE", "SECONDS", "SHELL_PID"), help=argparse.SUPPRESS)
    ap.add_argument("--keyring-write", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--keyring-read", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--keyring-delete", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.heartbeat_child:
        f, secs, shell = args.heartbeat_child
        heartbeat_child(Path(f), float(secs), int(shell))
        return 0
    for flag, mode in ((args.keyring_write, "write"), (args.keyring_read, "read"), (args.keyring_delete, "delete")):
        if flag:
            return keyring_mode(mode, args.out or ".", bool(args.dry_run))
    if args.check_detached:
        return check_detached_mode(args.check_detached)

    spike = Spike(args)
    print("Windows spike (TR-946). Results folder: %s" % spike.out)
    if spike.dry:
        print("Dry run: %s" % ("requested" if args.dry_run else "this is not Windows, so Windows-only steps are skipped"))
    results = spike.run()
    write_outputs(results, spike.out)
    print_summary(results)
    print()
    print("Results written to: %s" % spike.out)
    print("  %s" % (spike.out / RESULTS_MD))
    print("  %s" % (spike.out / RESULTS_JSON))
    det = step_by_name(results, "detached_child")
    if det and det["status"] == "skipped" and not spike.dry:
        print()
        print("STEP 7 NEEDS YOU: the detached child is writing a heartbeat for 60 s. Now:")
        print("  1. CLOSE THE TERMINAL this script was started in.")
        print("  2. Wait 20 seconds.")
        print("  3. Open a new terminal and run:")
        print('       py scripts\\windows_spike.py --check-detached "%s"' % spike.heartbeat)
        print("  (If more than 60 s pass, that is fine: the check handles a finished run.)")
        print("Then paste %s back." % (spike.out / RESULTS_MD))
    else:
        print("Paste %s back." % (spike.out / RESULTS_MD))
    return 0


if __name__ == "__main__":
    sys.exit(main())
