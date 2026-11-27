"""Starting a station on Codex (TR-1592): one `codex app-server` of its own, detached, on a private socket.

`launch.launch` comes here when the cell's launch spec is the Codex runtime's
(`tares.integrations.builtin.runtime_codex`). What a start does, in order, and what stops it:

  1. finds `codex` (CODEX_BIN, else PATH) and reads its version. A version older than TESTED_VERSION is
     refused: the app-server is marked experimental by Codex itself and was measured on that version
     (TR-1576). A newer one starts, with a line saying it was not tested.
  2. makes the station's own Codex folder, <crew home>/stations/<station>.codex (CODEX_HOME). Of the person's
     setup it holds one thing, a link to their login (`auth.json`): no plugin, MCP server, notify program,
     skill or trusted-project record of theirs is read, and the station's conversations are kept there, not
     among the person's. A `config.toml` a last run left there is removed, so the folder never grows a setup
     of its own. Codex writes a renewed login through the link, into the person's file (measured: `codex
     login` kept the link and wrote its target), so the person and their stations stay signed in together.
  3. clears the way for the socket, <crew home>/stations/<station>.sock (see socket_path for a name too long
     for one): a file nothing listens on (a killed app-server leaves one) is removed; one that answers means
     the station is running, and the start stops. On macOS Codex 0.171.0 keeps the socket itself in
     /tmp/codex-daemon-<uid>/ (a folder of the owner's alone) and makes the path a link to it; everything here
     follows the link.
  4. starts `codex <spec command>` in the station's folder, in a session of its own, with no terminal, its
     output appended to <station>.codex.log, and the station's environment (the FACTORY_* and TARES_*
     variables reach its hooks and commands from here).
  5. waits for the socket and checks what came up (`verify`): the socket is the owner's and theirs alone, the
     app-server answers `initialize` and runs on the station's folder, it has loaded no MCP server the launch
     did not name and has no notify program. A station that fails a check is stopped, not left running.
  6. records the station (stations.json, runtime "codex", the process's id and start time) and hands it its
     first prompt (`codex_station.hand`): a turn on the conversation recorded for it, or on a new one. A
     station that will not take the prompt is stopped, and its record is left as it was.

What happens to a station after its start (is it alive, where is it in its turn, the next prompt, the stop)
is tares.crew.codex_station (TR-1593).

Unix only: the app-server listens on a Unix socket. psutil and the standard library.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil

from . import state
from .codex_wire import Wire, WireError
from .errors import CrewError, GuardOffError, NeverComparedError
from .paths import CrewPaths, home_dir

RUNTIME = "codex"
TESTED_VERSION = (0, 171, 0)
READY_TIMEOUT = 30   # seconds the app-server may take to open its socket; it takes about one
STOP_GRACE = 10      # seconds between asking an app-server to end and ending it
VERSION_TIMEOUT = 30
# The longest path a Unix socket takes, in bytes: the address holds 104 on macOS and 108 on Linux, with
# the zero that ends it.
SOCKET_MAX = 103 if sys.platform == "darwin" else 107

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?")


def _dotted(version: tuple) -> str:
    return ".".join(str(n) for n in version)


# ---------- the codex program ----------


def codex_bin(env=None) -> str:
    """The `codex` program: CODEX_BIN as typed, else the one on PATH."""
    env = os.environ if env is None else env
    if env.get("CODEX_BIN"):
        return env["CODEX_BIN"]
    found = shutil.which("codex", path=env.get("PATH"))
    if not found:
        raise CrewError("codex not found on PATH (set CODEX_BIN to its path)")
    return found


def codex_version(codex: str) -> str:
    """What `codex --version` prints, one line."""
    try:
        done = subprocess.run([codex, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=VERSION_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise CrewError(f"cannot run {codex} --version: {e}") from None
    return (done.stdout or done.stderr or "").strip()


def check_version(text: str, say=None) -> str:
    """The version in `text` (what `codex --version` printed), as `1.2.3`. Dies for a version older than
    TESTED_VERSION, a pre-release of it included, and for a text with no version in it. A newer version
    passes, with a line through `say`."""
    tested = _dotted(TESTED_VERSION)
    m = _VERSION.search(text or "")
    if not m:
        raise CrewError(f"cannot tell the version of Codex from `codex --version` ({(text or '')[:80]!r}); a station "
                        f"starts on Codex {tested} or newer")
    found = tuple(int(n) for n in m.group(1, 2, 3))
    shown = m.group(0)
    if found < TESTED_VERSION or (found == TESTED_VERSION and m.group(4)):
        raise CrewError(f"Codex {shown} is older than {tested}, the oldest version a station starts on: Codex marks "
                        "its app-server experimental, and stations were tested on that version. Update Codex, then "
                        "run this again.")
    if found > TESTED_VERSION and say:
        say(f"Codex {shown} is newer than {tested}, the version stations were tested on")
    return shown


# ---------- the station's own files ----------


def socket_path(paths: CrewPaths, station: str) -> Path:
    """The station's socket: <crew home>/stations/<station>.sock, or, when that is longer than a socket's
    path may be (a builder's name can run to 120 characters), a short name made from the station's:
    <crew home>/stations/<16 hex digits>.sock. The same station always gets the same path."""
    plain = os.path.abspath(paths.station_file(station, "sock"))
    if len(os.fsencode(plain)) <= SOCKET_MAX:
        return Path(plain)
    short = hashlib.sha256(station.encode("utf-8")).hexdigest()[:16]
    return Path(os.path.abspath(paths.stations_dir / f"{short}.sock"))


def station_home(paths: CrewPaths, station: str) -> Path:
    return Path(os.path.abspath(paths.station_file(station, "codex")))


def machine_values(paths: CrewPaths, station: str) -> dict:
    """What fills the Codex runtime's own `{PLACEHOLDER}`s on this machine (see runtime.PLACEHOLDERS)."""
    return {"STATION_SOCKET": str(socket_path(paths, station)), "CODEX_HOME": str(station_home(paths, station))}


def check_socket_path(station: str, sock: Path) -> None:
    n = len(os.fsencode(str(sock)))
    if n > SOCKET_MAX:
        raise CrewError(f"the socket of {station} would be {sock}, {n} bytes long, and a socket's path holds "
                        f"{SOCKET_MAX} at most. Use a crew home with a shorter path.")


def person_login(env=None) -> Path:
    """The file that holds the person's Codex login: `auth.json` of their CODEX_HOME, else of ~/.codex, with
    links followed (a station that starts another has its own folder as CODEX_HOME, and its login is a link)."""
    env = os.environ if env is None else env
    base = env.get("CODEX_HOME") or f"{home_dir(env)}/.codex"
    return Path(os.path.realpath(os.path.join(base, "auth.json")))


def _git_folder(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", *args], capture_output=True, text=True, timeout=5,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def not_trusted(cwd: str) -> dict:
    """The `projects` table that marks the folder a station works in as not trusted: the folder as given and
    as it resolves, its repository's top, and the main checkout when the folder is a worktree (Codex keeps a
    worktree's trust under the main checkout). One mark is enough on 0.171.0; all are given.

    Why (TR-1594, measured on 0.171.0): a conversation started in a folder nobody marked makes Codex record
    the folder as trusted by itself. It then reads the repository's own `.codex/config.toml`: its MCP servers
    are started for the conversation, and its hooks run next to the guard (the conversation's trust for hooks
    covers them too), where one could change a command after the guard read it. With the mark, in the
    station's own `config.toml` or in the conversation's start, none of that is read and Codex leaves the
    record alone. A repository's skills and AGENTS.md are still read; they are text."""
    if not cwd:
        return {}
    folders = {cwd, os.path.realpath(cwd), _git_folder(cwd, "--show-toplevel")}
    common = _git_folder(cwd, "--path-format=absolute", "--git-common-dir")
    if os.path.basename(common) == ".git":
        folders.add(os.path.dirname(common))
    return {f: {"trust_level": "untrusted"} for f in sorted(folders) if f}


def _toml_text(value: str) -> str:
    """A TOML basic string."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(c if c >= " " and c != "\x7f" else f"\\u{ord(c):04x}" for c in out) + '"'


def home_config(cwd: str, shell_path: str = "") -> str:
    """The station's own `config.toml`: the marks of `not_trusted` and, with `shell_path`, the PATH its shell
    commands get, since a login shell orders PATH its own way. Nothing else."""
    text = "".join(f"[projects.{_toml_text(folder)}]\ntrust_level = {_toml_text(mark['trust_level'])}\n\n"
                   for folder, mark in not_trusted(cwd).items())
    if shell_path:
        text += f"[shell_environment_policy.set]\nPATH = {_toml_text(shell_path)}\n"
    return text


def prepare_home(paths: CrewPaths, station: str, env=None, cwd: str = "", shell_path: str = "") -> Path:
    """Make the station's own Codex folder, link the person's login into it and write its `config.toml` anew:
    the folder the station works in (`cwd`) marked not trusted and, when given, the shell commands' PATH,
    nothing else. Dies when the person has no login file, or when the folder holds a login of its own where the
    link belongs."""
    login = person_login(env)
    if not login.is_file():
        raise CrewError(f"no Codex login at {login}: a station runs on the login of the person who starts it. Run "
                        "`codex login`, then run this again. A login kept in the system keyring, or an API key in "
                        "the environment, is not used by a station yet.")
    home = station_home(paths, station)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    config = home / "config.toml"
    config.unlink(missing_ok=True)   # whatever a last run left: the folder never grows a setup of its own
    text = home_config(cwd, shell_path)
    if text:
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    link = home / "auth.json"
    if link.is_symlink():
        if os.path.realpath(link) != str(login):
            link.unlink()
            os.symlink(login, link)
    elif link.exists():
        raise CrewError(f"{link} is a file of its own where the link to the person's Codex login belongs. It may hold "
                        f"a newer login than {login}: compare the two, remove it, then run this again.")
    else:
        os.symlink(login, link)
    return home


def clear_socket(station: str, sock: Path) -> None:
    """Make room for the station's socket. What Codex leaves at the path is a socket or, on macOS with 0.171.0,
    a link to one it keeps in a folder of its own under /tmp. One nothing listens on is removed (an app-server
    that was killed leaves its file behind; only the path in the crew home is removed, Codex replaces its own
    socket). Dies when something answers on it: the station is running."""
    try:
        mode = os.lstat(sock).st_mode
    except FileNotFoundError:
        return
    target = str(sock)
    if stat.S_ISLNK(mode):
        target = os.path.realpath(sock)
        try:
            mode = os.stat(target).st_mode
        except OSError:   # the link outlived its socket
            _unlink(sock)
            return
    if not stat.S_ISSOCK(mode):
        raise CrewError(f"{sock} is in the way of the socket of {station} and is not a socket; move it away, then "
                        "run this again")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(target)
    except (ConnectionRefusedError, FileNotFoundError):
        _unlink(sock)
        return
    except OSError as e:
        raise CrewError(f"cannot tell whether {station} is running: {sock} gives {e}") from None
    finally:
        s.close()
    raise CrewError(f"{station} is running: its app-server answers on {sock}")


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def remove_socket(sock) -> None:
    """Remove the socket of an app-server that has ended: the path in the crew home and, where that is a link,
    the socket Codex kept elsewhere. A killed app-server removes neither, and a station that is never started
    again would leave its socket in /tmp for good."""
    target = os.path.realpath(sock) if os.path.islink(sock) else ""
    _unlink(sock)
    try:
        if target and stat.S_ISSOCK(os.lstat(target).st_mode):
            os.unlink(target)
    except OSError:
        pass


# ---------- the process ----------


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def _born(pid: int):
    """When the process started (seconds since 1970), which with its id says which process it is; None when
    it cannot be read."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def stop(proc, sock: Path | None = None) -> None:
    """End an app-server this start began: ask, wait STOP_GRACE seconds, then end it. Its socket file goes
    with it."""
    try:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(STOP_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(STOP_GRACE)
    except (OSError, subprocess.SubprocessError):
        pass
    if sock is not None:
        remove_socket(sock)


def _tail(path: Path, lines: int = 8) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _mcp_names(wire: Wire) -> list:
    names, cursor = [], None
    for _ in range(50):
        page = wire.request("mcpServerStatus/list", {"cursor": cursor} if cursor else {})
        names += [str(row.get("name")) for row in page.get("data") or [] if isinstance(row, dict)]
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return names


def _connect(sock: Path, deadline: float) -> Wire:
    """A connection to the app-server on `sock`, tried again until `deadline`: the path can be there a moment
    before the app-server listens on it."""
    while True:
        try:
            return Wire(str(sock))
        except WireError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def verify(station: str, sock: Path, home: Path, allowed_mcp, deadline: float | None = None) -> None:
    """Check the app-server that came up on `sock` is the station the launch asked for. Dies with what is
    wrong: no answer to `initialize` (by `deadline`, a time.monotonic() value; now when none is given), a
    socket others can reach, a Codex folder that is not the station's own, an MCP server the launch did not
    name, a notify program."""
    try:
        with _connect(sock, time.monotonic() if deadline is None else deadline) as wire:
            found = os.stat(sock)   # through the link, when the path is one
            mode = stat.S_IMODE(found.st_mode)
            if found.st_uid != os.getuid() or mode & 0o077:
                raise CrewError(f"the socket of {station} is open to others (mode {mode:o}); it must be the owner's alone")
            hello = wire.initialize()
            runs_on = str(hello.get("codexHome") or "")
            if not runs_on or os.path.realpath(runs_on) != os.path.realpath(home):
                raise CrewError(f"{station} came up on the Codex folder {runs_on or '(not named)'}, not on its own "
                                f"({home}): it would load the person's own Codex setup")
            extra = sorted(n for n in _mcp_names(wire) if n not in set(allowed_mcp))
            if extra:
                raise CrewError(f"{station} loaded an MCP server its launch does not name ({', '.join(extra)}): a "
                                "station loads nothing of the person's account or setup")
            config = wire.request("config/read", {}).get("config")
            if isinstance(config, dict) and config.get("notify"):
                raise CrewError(f"{station} has a notify program configured: Codex would start it at the end of "
                                "each turn and hand it the prompt")
    except (WireError, OSError) as e:
        raise CrewError(f"the app-server of {station} did not answer as one: {e}") from None


_CLAUDE_MODEL = re.compile(r"^(claude-|opus|sonnet|haiku)", re.IGNORECASE)


def claude_model(model: str) -> bool:
    """Is `model` the name of a Claude model? The crew's settings file (crew.json) starts from a template
    that names them (opus, sonnet), and Codex takes none: measured on 0.171.0, a turn asked for `opus` fails
    with "The 'opus' model is not supported", while the station reads as started and at rest."""
    return bool(_CLAUDE_MODEL.match(str(model or "").strip()))


def without_claude_model(settings: dict) -> dict:
    """`settings` (a filled spec's) with a Claude model taken out of the conversation's start, and the effort
    that was chosen for that model with it: the station then runs on the model and effort Codex gives the
    account by default. Settings that name any other model, or none, come back as they are."""
    thread = settings.get("thread")
    if not isinstance(thread, dict) or not claude_model(thread.get("model")):
        return settings
    return {**{k: v for k, v in settings.items() if k != "effort"},
            "thread": {k: v for k, v in thread.items() if k != "model"}}


def plan(spec: dict, values: dict, tares_bin: str = "") -> dict:
    """What a Codex station starts with, from the cell's spec and not started: {runtime, args, env, settings}
    with every placeholder filled from `values`, and FACTORY_TARES when `tares_bin` is given. A Claude model's
    name is not handed to Codex (see without_claude_model). `otel`, `otel_file` and `mcp` are there and empty,
    the shape `launch.launch_plan` gives for Claude Code."""
    from ..integrations.runtime import fill_placeholders
    env = fill_placeholders(spec["env"], values)
    if tares_bin:
        env["FACTORY_TARES"] = tares_bin
    return {"runtime": RUNTIME, "args": fill_placeholders(spec["command"], values), "env": env,
            "settings": without_claude_model(fill_placeholders(spec["settings"], values)),
            "otel": {}, "otel_file": {}, "mcp": {}}


def fingerprint(plan: dict, kit: str = "") -> str:
    """One number for what a station is started with (its arguments, environment, settings and kit): a
    restart compares it with the one recorded at the start to tell whether anything changed. PATH and the
    commit identity are left out: PATH follows whichever process composes the plan, and the identity is
    missing from a plan made while the token endpoint cannot be asked."""
    from .client import cksum
    from .github_access import IDENTITY_VARS
    env = plan.get("env")
    if isinstance(env, dict):
        env = {k: v for k, v in env.items() if k != "PATH" and k not in IDENTITY_VARS}
    text = state.dumps_compact([plan.get("args"), env, plan.get("settings"), kit])
    return str(cksum(text.encode("utf-8")))


def _put_back(paths: CrewPaths, station: str, before) -> None:
    """The station's row as it was before a start that did not succeed (no row when it had none)."""
    data = state.stations_load(paths)
    if before is None:
        data.pop(station, None)
    else:
        data[station] = before
    state.write_json(paths.stations, data)


def start(client, station: str, plan: dict, cwd: str, prompt: str, row: dict, *, drop_env=(), codex: str | None = None,
          sleep=time.sleep) -> None:
    """Start the station's app-server, record it and hand it its first prompt (see the module's note for the
    steps). `plan` is `launch.launch_plan`'s answer for the Codex runtime. `row` is what the caller records of
    a station whatever its runtime (role, parent, project, repo, cwd, model, effort, settings, kit, plugin,
    grants, sessionId: the conversation to carry on, "" for a new one). `prompt` may be "" for a station
    that is to get no turn yet. `drop_env` names variables of this process the station must not inherit.
    Dies (CrewError) for anything that stops a start; an app-server that came up wrong, or would not take
    its prompt, is stopped first and the station's record is left as it was."""
    if sys.platform == "win32":
        raise CrewError("a Codex station listens on a Unix socket, which a start does not set up on Windows yet")
    from . import codex_station   # here: it imports this module
    from .github_access import shell_path
    paths = client.paths
    codex = codex or codex_bin(client.env)
    version = check_version(codex_version(codex), lambda text: print(f"  note: {text}", file=sys.stderr))
    sock = socket_path(paths, station)
    check_socket_path(station, sock)
    paths.stations_dir.mkdir(parents=True, exist_ok=True)
    clear_socket(station, sock)
    home = prepare_home(paths, station, client.env, cwd or paths.raw,   # the folder its conversations start in
                        shell_path(plan["env"]))

    env = {k: v for k, v in os.environ.items() if k not in drop_env}
    env.update({k: str(v) for k, v in plan["env"].items()})
    log = paths.station_file(station, "codex.log")
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        proc = subprocess.Popen([codex, *plan["args"]], stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, cwd=cwd,
                                env=env, start_new_session=True)
    except OSError as e:
        raise CrewError(f"could not start {station}: {e}") from None
    finally:
        os.close(fd)

    deadline = time.monotonic() + READY_TIMEOUT
    while not os.path.lexists(sock):
        code = proc.poll()
        if code is not None:
            print(_tail(log), file=sys.stderr)
            raise CrewError(f"could not start {station}: codex app-server ended at once (exit {code}); see {log}")
        if time.monotonic() > deadline:
            stop(proc, sock)
            raise CrewError(f"could not start {station}: its app-server did not open {sock} in {READY_TIMEOUT} s; "
                            f"see {log}")
        sleep(0.1)
    try:
        verify(station, sock, home, (plan["settings"].get("mcp_servers") or {}).keys(), deadline)
    except CrewError:
        stop(proc, sock)
        raise

    before = state.stations_load(paths).get(station)
    state.stations_put(station, {
        **row, "runtime": RUNTIME, "id": "", "status": "active", "startedAt": state.utc_stamp(),
        "codex": {"pid": proc.pid, "born": _born(proc.pid), "version": version, "socket": str(sock),
                  "home": str(home), "log": str(log), "launched": fingerprint(plan, str(row.get("kit") or ""))}}, paths)
    if prompt:
        try:
            got = codex_station.hand(paths, station, prompt, fresh=not row.get("sessionId"))
        except GuardOffError as e:   # already stopped; the row as it was, marked, so the watch leaves it down
            stop(proc, sock)
            _put_back(paths, station, before)
            codex_station.mark_off(paths, station, e.why, e.turn)
            raise
        except NeverComparedError as e:   # the row as it was, with the conversation marked: only a person gives it up
            stop(proc, sock)
            _put_back(paths, station, before)
            codex_station.mark_unverified(paths, station, e.thread, f"its conversation cannot be loaded ({e.why})",
                                          event=False)   # `hand` recorded the event; the row was put back since
            raise
        except CrewError:
            stop(proc, sock)
            _put_back(paths, station, before)
            raise
        if got["replaced"]:
            print(f"  note: the last conversation of {station} could not be carried on; it starts a new one",
                  file=sys.stderr)
    state.event("started", station, f"codex {version}, pid {proc.pid}", paths=paths)
    print(f"  \u2713 started {station} on Codex {version} ({row.get('model') or 'its default model'}, "
          f"{row.get('effort') or 'its default effort'})", file=sys.stderr)
