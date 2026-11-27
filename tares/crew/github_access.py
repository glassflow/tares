"""A station's access to GitHub with a token the cell gives (`GET /api/factory/github/token`), for a machine
with no `gh auth login` and no git setup of its own.

  `tares factory git-credential get`  git's credential helper for github.com
  `tares factory gh -- <args>`        the real gh; one with no login gets the cell's token
  launch_env, write_station_files     what a station's launch carries so that both are used

A token is never written to a file, a log line, an argument list or the launch plan. It goes to git on stdout
(the credential protocol) and into the environment of the gh process, and nowhere else.

Standard library only, plus what tares.crew.client needs (httpx). Imported on demand.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import signal
import subprocess
import sys
from urllib.parse import urlsplit

from . import state
from .client import TaresClient, TaresHTTPError
from .paths import ME, CrewPaths

CAPABILITY = "github_token"            # the cell capability of the token route
HOST = "github.com"
HELPER_KEY = f"credential.https://{HOST}.helper"
SHIM_MARKER = "tares-factory gh shim"  # a line of both shim files: how a gh on PATH is known to be one
IDENTITY_VARS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")

ASK_AT_LAUNCH = 10     # seconds a start waits for the token answer
ASK_FOR_A_COMMAND = 30  # seconds git or gh waits for it; the cell may have to get a token from GitHub first
LOGIN_CHECK = 10       # seconds `gh auth token` may take
GIT_TIMEOUT = 10       # seconds `git remote get-url` may take

_NAME = re.compile(r"[A-Za-z0-9_.-]+")                       # an owner or a repository name, as the cell takes it
_SCP = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")  # git@host:owner/name
_URL_STARTS = ("https://", "http://", "ssh://", "git://", "git@")
_TEXT_FLAGS = ("--body", "-b", "--body-file", "--title", "-t", "--subject", "--notes", "--description",
               "--homepage", "--field", "--raw-field")   # their value is text, so an address in it names no target
_GIT_PLACE = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


# ---------- repositories ----------


def _slug(path: str) -> str:
    """`owner/name` from a repository's path (outer slashes and a final .git dropped), "" for anything else."""
    parts = path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(_NAME.fullmatch(p) and p not in (".", "..") for p in parts):
        return ""
    return "/".join(parts)


def _parse_ref(value: str) -> tuple:
    """(host, owner/name) of a repository as gh and git write one: owner/name, host/owner/name, a URL, or
    git@host:owner/name. The host is "" when none is named, the name "" when there is none."""
    value = value.strip()
    if "://" in value:
        try:
            url = urlsplit(value)
            host = url.hostname or ""
        except ValueError:
            return "unreadable", ""
        return host, _slug("/".join(url.path.strip("/").split("/")[:2]))
    scp = _SCP.match(value)
    if scp:
        return scp.group(1), _slug(scp.group(2))
    parts = value.strip("/").split("/")
    if len(parts) == 3:
        return parts[0], _slug("/".join(parts[1:]))
    return "", _slug(value)


def _origin(folder: str, env) -> str:
    """owner/name of the origin remote of `folder` ("" means the working folder) when it is on github.com,
    else ""."""
    command = ["git", *(["-C", folder] if folder else []), "remote", "get-url", "origin"]
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              env=dict(env), timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if done.returncode != 0:
        return ""
    host, name = _parse_ref(done.stdout)
    return name if host.lower() == HOST else ""


# ---------- the token ----------


def _ask(env, repo: str, seconds: float) -> tuple:
    """(status, body) of one token request for `repo`."""
    with TaresClient(CrewPaths.from_env(), env, say=lambda _message: None) as client:
        return client.github_token(repo, seconds)


def _usable(token) -> bool:
    return isinstance(token, str) and token != "" and not any(c in token for c in "\r\n\0")


# ---------- git's credential helper ----------


def _asked(lines) -> dict:
    """The `key=value` lines git sends, up to a blank line or the end."""
    out = {}
    for line in lines:
        line = line.rstrip("\r\n")
        if not line:
            break
        key, equals, value = line.partition("=")
        if equals:
            out[key] = value
    return out


def _credential(asked: dict, env) -> str:
    """The answer to git's question, "" when there is none to give."""
    if asked.get("protocol") != "https" or (asked.get("host") or "").lower() != HOST:
        return ""
    repo = _slug(asked.get("path") or "")
    if not repo:
        return ""
    status, body = _ask(env, repo, ASK_FOR_A_COMMAND)
    token = body.get("token")
    if status != 200 or not _usable(token):
        return ""
    return f"username=x-access-token\npassword={token}\n\n"


def cmd_git_credential(argv: list, env: dict) -> int:
    """`tares factory git-credential <get|store|erase>`; argv starts with the word git-credential. `get`
    answers for an https repository on github.com, or exits 1 and says nothing at all. There is nothing to
    store or erase: every question gets a token of its own, and any other word is ignored, as git asks."""
    if argv[1:2] != ["get"]:
        return 0
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            answer = _credential(_asked(sys.stdin), env)
        if not answer:
            return 1
        sys.stdout.flush()
        sys.stdout.buffer.write(answer.encode("utf-8"))   # bytes: the protocol's lines end with LF everywhere
        sys.stdout.buffer.flush()
    except BaseException:  # noqa: BLE001 - whatever went wrong, git reads an exit code and no text
        return 1
    return 0


# ---------- gh ----------


def _flag_values(words: list, long: str, short: str = "") -> list:
    """Every value given to a flag: `--long v`, `--long=v`, `-s v`, `-sv` and `-s=v`."""
    out = []
    for i, word in enumerate(words):
        if word == long or (short and word == short):
            out += words[i + 1:i + 2]
        elif word.startswith(long + "="):
            out.append(word[len(long) + 1:])
        elif short and word.startswith(short):
            out.append(word[len(short):].removeprefix("="))
    return out


def _command(words: list) -> list:
    """The command words of a gh call: what is neither a flag nor the value of --repo or --hostname."""
    out, value = [], False
    for word in words:
        if value:
            value = False
        elif word in ("-R", "--repo", "--hostname"):
            value = True
        elif not word.startswith("-"):
            out.append(word)
    return out


def _shows_token(word: str) -> bool:
    if word.startswith("--"):
        return word.split("=", 1)[0] == "--show-token"
    return word.startswith("-") and "t" in word[1:]


def _flags_end(args: list) -> int:
    return args.index("--") if "--" in args else len(args)


def excluded(args: list) -> bool:
    """A gh command that never gets the cell's token: `gh auth ...`, except `gh auth status` that does not
    ask for the token to be shown."""
    words = args[:_flags_end(args)]
    command = _command(words)
    if command[:1] != ["auth"]:
        return False
    return command[1:2] != ["status"] or any(_shows_token(w) for w in words)


def target_repo(args: list, env) -> str:
    """owner/name of the github.com repository a gh call is about, "" when there is none or when the call or
    the environment names another host. Looked for in this order: -R/--repo, GH_REPO, a `gh api` endpoint
    repos/<owner>/<name>, a repository's web address among the arguments, the origin remote of the working
    folder."""
    words = args[:_flags_end(args)]
    command = _command(words)
    flagged = _flag_values(words, "--repo", "-R")
    from_env = env.get("GH_REPO") or ""
    urls = [w for i, w in enumerate(words)   # one word with no space in it: a longer text only starts with a link
            if w.lower().startswith(_URL_STARTS) and not any(c.isspace() for c in w)
            and not (i and words[i - 1] in _TEXT_FLAGS)]
    hosts = [env.get("GH_HOST") or "", *_flag_values(words, "--hostname")]
    if command[:1] == ["auth"]:
        hosts += _flag_values(words, "--hostname", "-h")
    hosts += [_parse_ref(v)[0] for v in [*flagged, from_env, *urls] if v]
    hosts += [w.split("/", 1)[0] for w in words if "/" in w and w.split("/", 1)[0].lower().endswith(".ghe.com")]
    if any(h and h.lower() != HOST for h in hosts):
        return ""
    if flagged:
        return _parse_ref(flagged[-1])[1]
    if from_env:
        return _parse_ref(from_env)[1]
    if command[:1] == ["api"]:
        for word in words:
            if word.startswith(("repos/", "/repos/")):
                parts = word.lstrip("/").split("?", 1)[0].split("/")
                name = _slug("/".join(parts[1:3]))   # "" for {owner}/{repo}, which gh fills itself
                if name:
                    return name
    for url in urls:
        if url.lower().startswith(("https://", "http://")):
            name = _parse_ref(url)[1]
            if name:
                return name
    return _origin("", env)


def _same_folder(a: str, b: str) -> bool:
    def key(path: str) -> str:
        return os.path.normcase(os.path.realpath(path))
    return bool(a) and key(a) == key(b)


def _is_shim(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return SHIM_MARKER.encode() in f.read(4096)
    except OSError:
        return False


def real_gh(env, bin_dir: str) -> str:
    """The first gh on PATH that is neither in the crew's bin folder nor one of the shim files, "" when there
    is none. On Windows a name is tried with each ending of PATHEXT; the working folder is not searched unless
    PATH names it."""
    names = ["gh"]
    if os.name == "nt":
        names = ["gh" + ext.lower() for ext in (env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if ext]
    for folder in (env.get("PATH") or os.defpath).split(os.pathsep):
        if not folder or _same_folder(folder, bin_dir):
            continue
        for name in names:
            found = os.path.join(folder, name)
            if os.path.isfile(found) and os.access(found, os.X_OK) and not _is_shim(found):
                return found
    return ""


def has_login(gh: str, env) -> bool | None:
    """Whether gh has a login for github.com (GH_TOKEN and GITHUB_TOKEN count), None when that cannot be told.
    `gh auth token` prints the token, so all it reads and writes goes nowhere: only its exit code is used."""
    try:
        done = subprocess.run([gh, "auth", "token", "--hostname", HOST], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(env),
                              timeout=LOGIN_CHECK)
    except (OSError, subprocess.SubprocessError):
        return None
    return None if done.returncode < 0 else done.returncode == 0


def _no_token(repo: str, why: str) -> None:
    print(f"{ME} gh: Tares gave no token for {repo} ({why}); gh runs without it", file=sys.stderr)


def _cell_token(env, repo: str) -> str:
    """The cell's token for `repo`; "" after one line that says why there is none."""
    try:
        status, body = _ask(env, repo, ASK_FOR_A_COMMAND)
    except TaresHTTPError as e:
        _no_token(repo, str(e))
        return ""
    except Exception as e:  # noqa: BLE001 - named by its type: its text may quote a request
        _no_token(repo, type(e).__name__)
        return ""
    token = body.get("token")
    if status == 200 and _usable(token):
        return token
    detail = body.get("detail")
    if status != 200 and isinstance(detail, str) and detail.strip():
        _no_token(repo, " ".join(detail.split())[:300])
    else:
        _no_token(repo, f"it answered {status} with no token")
    return ""


def _run_gh(command: list, env: dict) -> int:
    """Run gh with this process's stdin, stdout and stderr, and give its exit code. Where the platform can, gh
    takes this process's place, so signals and the exit code reach the caller as they are."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        if os.name != "nt":
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # Python ignores it, and gh would inherit that
            os.execve(command[0], command, env)
        return subprocess.call(command, env=env)
    except OSError as e:
        print(f"{ME} gh: could not run {command[0]}: {e.strerror or e}", file=sys.stderr)
        return 126
    except KeyboardInterrupt:
        return 130


def cmd_gh(argv: list, env: dict) -> int:
    """`tares factory gh -- <args>`; argv starts with the word gh. Runs the real gh. A gh with a login of its
    own keeps it. One without gets the cell's token for the repository the call is about, as GH_TOKEN in the
    environment of the gh process only: the token is written to no file and passed as no argument. Without a
    token, for whatever reason, gh runs as it was called.

    gh gets the environment this process was started with, not `env`, which also holds the Tares address and
    key that `tares factory` sets for its own commands."""
    args = argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    own = dict(os.environ)
    gh = real_gh(own, str(CrewPaths.from_env().home / "bin"))
    if not gh:
        print(f"{ME} gh: no gh found on PATH besides the crew's own; install the GitHub CLI", file=sys.stderr)
        return 127
    repo = "" if excluded(args) else target_repo(args, own)
    if repo and has_login(gh, own) is False:
        token = _cell_token(env, repo)
        if token:
            own["GH_TOKEN"] = token
    return _run_gh([gh, *args], own)


# ---------- a station's launch ----------


def _station_repo(folders: list, env) -> str:
    """The first github.com repository among the origin remotes of the folders, "" when there is none."""
    clean = {k: v for k, v in env.items() if k not in _GIT_PLACE}
    for folder in folders:
        name = _origin(folder, clean)
        if name:
            return name
    return ""


def launch_env(client: TaresClient, station: str, tares_bin: str, folders, claude: bool) -> dict:
    """What a station's launch adds to its environment so that git and gh use the cell's token; {} when it adds
    nothing: no `tares` command was found, the cell does not list the capability, no repository folder has a
    github.com origin, or the cell holds no credential. `folders` gives the folders to look at, when called: the
    station's repository, or the crew's for a station without one. The same inputs give the same result, also
    when the launching process is itself a station. `tares_bin` is the path with forward slashes."""
    if not tares_bin or CAPABILITY not in client.capabilities():
        return {}
    repo = _station_repo(folders(), client.env)
    if not repo:
        return {}
    try:
        status, body = client.github_token(repo, ASK_AT_LAUNCH)
    except Exception:  # noqa: BLE001 - a start goes on: git and gh ask again when they need a token
        status, body = 0, {}
    if status == 404 and body.get("code") == "no_credential":
        return {}
    name, email = (body.get("name"), body.get("email")) if status == 200 else (None, None)
    del body   # the token in it goes no further
    out = {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": HELPER_KEY, "GIT_CONFIG_VALUE_0": "",   # drops the helpers set up before, for this host
        "GIT_CONFIG_KEY_1": HELPER_KEY,
        "GIT_CONFIG_VALUE_1": f"!{shlex.quote(tares_bin)} factory git-credential",
        "GIT_CONFIG_KEY_2": f"credential.https://{HOST}.useHttpPath", "GIT_CONFIG_VALUE_2": "true",
    }
    if all(isinstance(v, str) and v.strip() and "\0" not in v for v in (name, email)):
        out.update(zip(IDENTITY_VARS, (str(name), str(email)) * 2))
    out.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    bin_dir = str(client.paths.home / "bin")
    rest = [p for p in (client.env.get("PATH") or os.defpath).split(os.pathsep)
            if os.path.normcase(os.path.normpath(p)) != os.path.normcase(os.path.normpath(bin_dir))]
    out["PATH"] = os.pathsep.join([bin_dir, *rest])
    if claude:
        file = str(client.paths.station_file(station, "env.sh"))
        out["CLAUDE_ENV_FILE"] = file.replace("\\", "/") if sys.platform == "win32" else file
    return out


def shell_path(env: dict) -> str:
    """The PATH a station's shell commands have to see when its launch env holds what launch_env adds, else ""."""
    return (env.get("PATH") or "") if env.get("GIT_CONFIG_KEY_0") == HELPER_KEY else ""


def _sh_path(path: str, platform: str | None = None) -> str:
    """A path as sh takes it in PATH: on Windows C:/x is /c/x, since a colon separates PATH's entries."""
    if (platform or sys.platform) != "win32":
        return path
    path = path.replace("\\", "/")
    drive = re.match(r"^([A-Za-z]):(/.*)?$", path)
    return f"/{drive.group(1).lower()}{drive.group(2) or '/'}" if drive else path


def _env_file_text(paths: CrewPaths, before: str) -> str:
    """A Claude station's env file, which Claude Code runs before each Bash command: the env file the launching
    environment had, unless that is a station's of this crew, then the crew's bin folder first on PATH."""
    lines = ["# written by tares factory when this station starts"]
    if before and not (before.endswith(".env.sh")
                       and _same_folder(os.path.dirname(before), str(paths.stations_dir))):
        quoted = shlex.quote(_sh_path(before))
        lines.append(f"if [ -f {quoted} ]; then . {quoted}; fi")
    lines.append(f'export PATH={shlex.quote(_sh_path(str(paths.home / "bin")))}:"$PATH"')
    return "\n".join(lines) + "\n"


def write_station_files(client, station: str, env: dict) -> None:
    """The files a station with this launch env uses: the gh shim in <crew home>/bin and, for a Claude station,
    its env file. Nothing is written, and nothing of the client is read, for a launch env without the GitHub
    access."""
    if not shell_path(env):
        return
    paths = client.paths
    folder = paths.home / "bin"
    folder.mkdir(parents=True, exist_ok=True)
    state.atomic_write_text(folder / "gh", f'#!/bin/sh\n# {SHIM_MARKER}\n'
                                           'exec "$FACTORY_TARES" factory gh -- "$@"\n', mode=0o755)
    state.atomic_write_text(folder / "gh.cmd", f'@echo off\r\nrem {SHIM_MARKER}\r\n'
                                               '"%FACTORY_TARES%" factory gh -- %*\r\n', mode=0o755)
    if env.get("CLAUDE_ENV_FILE"):
        paths.stations_dir.mkdir(parents=True, exist_ok=True)
        state.atomic_write_text(paths.station_file(station, "env.sh"),
                                _env_file_text(paths, (client.env.get("CLAUDE_ENV_FILE") or "")), mode=0o600)
