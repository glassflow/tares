"""The crew home's files, written the way bash writes them.

bash builds every file with jq and the shell: pretty JSON with 2-space indent, no ASCII escaping, a
trailing newline; compact lines for events, pace and wake. This module produces the same bytes. Text
is always UTF-8 with LF line endings, on every platform, and a file is replaced whole (temp file in
the same folder, then os.replace) so a reader never sees half of one.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import perms
from .errors import CrewError
from .paths import CREW_NAME_RULE, CrewPaths

def trust_keys(path: str) -> list:
    """The keys Claude Code may file a folder's trust under in ~/.claude.json: the path as given and,
    on Windows, with forward slashes, the way Claude Code writes them there (C:/Users/me/factory)."""
    keys = [path]
    if os.name == "nt":
        for p in (path, os.path.normpath(path)):
            k = p.replace("\\", "/")
            if k not in keys:
                keys.append(k)
    return keys


# ---------- JSON text the way jq prints it ----------


def _jq_text(s: str) -> str:
    # jq writes DEL as \u007f; Python leaves it raw. Outside a string DEL can't occur, so this is exact.
    return s.replace("\x7f", "\\u007f")


def dumps_pretty(value) -> str:
    """`jq .`: 2-space indent, keys in the order given, non-ASCII as is. No trailing newline (jq adds one)."""
    return _jq_text(json.dumps(value, indent=2, ensure_ascii=False))


def dumps_compact(value) -> str:
    """`jq -c .`: no spaces. No trailing newline."""
    return _jq_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def jq_raw(value) -> str:
    """What `jq -r` prints for a value: a string as is, anything else as JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return dumps_pretty(value)
    return dumps_compact(value)


# ---------- files ----------

REPLACE_TRIES = 8
_UMASK = None


def _default_mode() -> int:
    """The mode a file made with `>` gets: 0666 less the umask."""
    global _UMASK
    if _UMASK is None:
        _UMASK = os.umask(0o22)
        os.umask(_UMASK)
    return 0o666 & ~_UMASK


def read_text(path) -> str:
    """UTF-8, and no newline translation: a CRLF in a file stays one."""
    with open(path, encoding="utf-8", newline="\n") as f:
        return f.read()


def loads_json(text: str):
    """json.loads, but a leading byte order mark (Notepad adds one) is skipped, as jq skips it."""
    return json.loads(text[1:] if text.startswith("\ufeff") else text)


def atomic_write_text(path, text: str, *, mode=None, private: bool = False) -> None:
    """Write `text` (UTF-8, LF as given) to a temp file beside `path`, then os.replace it over `path`.
    Windows refuses the replace while a reader has the file open, so a PermissionError is retried a few
    times with short sleeps. The file keeps the mode it had, else `mode`, else what `>` would give.
    `private` is for a file that holds a key: the temp file is made readable by its owner only (mode 0600,
    or on Windows an access list naming the current user) before the key is written to it, and a file
    that cannot be restricted is refused, not written."""
    path = Path(path)
    data = text.encode("utf-8")
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = None if os.name == "nt" else _default_mode()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            if private:
                mode = 0o600
                if not perms.owner_only(tmp):
                    raise CrewError(f"could not make {path} readable by you only")
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, value, *, compact: bool = False, mode=None) -> None:
    """The file as jq writes it: pretty (or `-c`) JSON and a newline, replaced whole."""
    atomic_write_text(path, (dumps_compact(value) if compact else dumps_pretty(value)) + "\n", mode=mode)


_MISSING = object()


def read_json(path, default=_MISSING):
    """The parsed file. A missing or unreadable file, or one that is not JSON, gives `default` when one is
    given, else a CrewError that says which file."""
    try:
        return loads_json(read_text(path))
    except (OSError, ValueError) as e:
        if default is not _MISSING:
            return default
        raise CrewError(f"{path} is not valid JSON") from e


def utc_stamp(ts=None) -> str:
    """`now | todate`: 2026-05-01T09:30:00Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def now_s() -> int:
    """The time the idle wake works by, epoch seconds. FACTORY_NOW stands in for it in tests."""
    v = os.environ.get("FACTORY_NOW")
    if v:
        try:
            return int(v)
        except ValueError:
            raise CrewError(f"FACTORY_NOW is not whole seconds: {v}") from None
    return int(time.time())


# ---------- names ----------

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


def slugify(text: str) -> str:
    """bash's slugify: lower-case ASCII letters and digits, every other run of characters one hyphen, no
    hyphen at either end. bash's version prints a newline after it; `$(slugify x)` drops that, so this
    returns the text without it."""
    lowered = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def crew_name_ok(name: str) -> bool:
    """Can `name` name a crew: the rule in CREW_NAME_RULE."""
    return _NAME_RE.fullmatch(name) is not None and "-build-" not in f"-{name}-"


def jq_get(value, keys, default=""):
    """`jq -r '.a.b // empty'` on a parsed value: the text of what is at `keys`, "" when it is missing, null,
    false, or the path runs through something that is not an object or list."""
    for k in keys:
        if isinstance(value, dict) and isinstance(k, str) and k in value:
            value = value[k]
        elif isinstance(value, list) and isinstance(k, int) and -len(value) <= k < len(value):
            value = value[k]
        else:
            return default
    if value is None or value is False:
        return default
    return jq_raw(value)


def ccfg(*keys, paths=None) -> str:
    """bash's ccfg '.a.b // empty': a value of crew.json as text, "" when it is not there or the file can't be read."""
    paths = paths or CrewPaths.from_env()
    return jq_get(read_json(paths.crew_cfg, None), keys)


def crew_name(paths=None) -> str:
    """The crew's name: FACTORY_CREW_NAME, else crew.json's crew_name, else "crew". A name that can't be a
    station name is refused with a message that names it, where it came from and the rule."""
    paths = paths or CrewPaths.from_env()
    n = os.environ.get("FACTORY_CREW_NAME") or ""
    src = "FACTORY_CREW_NAME"
    if not n:
        n = ccfg("crew_name", paths=paths)
        src = f"crew_name in {paths.shown('crew.json')}"
    if not n:
        n, src = "crew", "default"
    if not crew_name_ok(n):
        raise CrewError(f'the crew name "{n}" (from {src}) is not valid: it is {CREW_NAME_RULE}')
    return n


def crew_orchestrator(paths=None) -> str:
    return f"{crew_name(paths)}-orchestrator"


def builder_name(project_slug: str, slug: str, paths=None) -> str:
    c = crew_name(paths)
    return f"{project_slug}-build-{slug}" if c == "crew" else f"{c}-{project_slug}-build-{slug}"


# ---------- events ----------


def event(name: str, station: str = "", detail: str = "", *, paths=None, at=None) -> None:
    """Append one line to events.jsonl: {at, station, event, detail}, compact. Only when the crew home
    exists, and never fails. One write of one line, so two writers never mix a line."""
    try:
        paths = paths or CrewPaths.from_env()
        if not paths.home.is_dir():
            return
        line = dumps_compact({"at": at or utc_stamp(), "station": station, "event": name, "detail": detail}) + "\n"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(paths.events, flags, 0o666)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


# ---------- crew.json ----------


def merge_objects(base, over):
    """jq's `base * over`: objects merge key by key, deeply; anything else, `over` wins. Keys keep the order
    of `base`, then those only `over` has."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = merge_objects(out[k], v) if k in out else v
        return out
    return over


def kit_templates() -> Path:
    """The kit's templates folder: inside the install (tares/claude-plugin), else the checkout's."""
    tares = Path(__file__).resolve().parent.parent
    for c in (tares / "claude-plugin" / "templates", tares.parent / "claude-plugin" / "templates"):
        if (c / "crew.json").is_file():
            return c
    raise CrewError("the kit's templates/crew.json is missing")


def merge_crew_defaults(paths=None, templates=None) -> None:
    """A crew.json written by hand, or by an older kit, gets the template's defaults under it. The file is
    rewritten only when that changes it."""
    paths = paths or CrewPaths.from_env()
    cfg = paths.crew_cfg
    if not cfg.is_file():
        return
    tpl = Path(templates) if templates else kit_templates()
    try:
        merged = merge_objects(loads_json(read_text(tpl / "crew.json")), loads_json(read_text(cfg)))
    except ValueError:
        raise CrewError(f"{paths.shown('crew.json')} is not valid JSON") from None
    if not isinstance(merged, dict):
        raise CrewError(f"{paths.shown('crew.json')} is not valid JSON")
    text = dumps_pretty(merged) + "\n"
    try:
        same = cfg.read_bytes() == text.encode("utf-8")
    except OSError:
        same = False
    if not same:
        atomic_write_text(cfg, text)


def _command_output(cmd, timeout):
    """A command's output as `$(...)` gives it (trailing newlines gone); None when it fails or is missing."""
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace").rstrip("\n")


def ensure_crew_home(paths=None, templates=None, *, human_name=None, human_github=None, today=None) -> None:
    """Make the crew home: its folders, the defaults merged into crew.json, and a crew.json from the template
    when there is none, with the person's name (git config --global user.name, else "the user"), their
    GitHub login (gh, when installed) and who authorized the crew and when (today, UTC)."""
    paths = paths or CrewPaths.from_env()
    for d in (paths.stations_dir, paths.reports_dir, paths.worktrees_dir):
        d.mkdir(parents=True, exist_ok=True)
    merge_crew_defaults(paths, templates)
    if paths.crew_cfg.is_file():
        return
    tpl = Path(templates) if templates else kit_templates()
    if human_github is None:
        human_github = ""
        if shutil.which("gh"):
            human_github = _command_output(["gh", "api", "user", "-q", ".login"], 60) or ""
    if human_name is None:
        human_name = _command_output(["git", "config", "--global", "user.name"], 30)
        if human_name is None:
            human_name = "the user"
    cfg = json.loads(read_text(tpl / "crew.json"))
    cfg["human"]["name"] = human_name
    cfg["human"]["github"] = human_github
    cfg["authorized"] = {"by": human_name, "at": today or time.strftime("%Y-%m-%d", time.gmtime())}
    atomic_write_text(paths.crew_cfg, dumps_pretty(cfg) + "\n")


def require_crew(paths=None) -> None:
    paths = paths or CrewPaths.from_env()
    if not paths.crew_cfg.is_file():
        raise CrewError(f"no crew yet in {paths.raw}; run: tares factory crew up")


# ---------- stations.json ----------


def stations_get(station: str, *keys, paths=None) -> str:
    """bash's `stations_get <station> '.a.b'`: the field as text, "" when the file, the station or the field
    is not there."""
    paths = paths or CrewPaths.from_env()
    if not paths.stations.is_file():
        return ""
    data = read_json(paths.stations, None)
    return jq_get(data, (station, *keys))


def stations_load(paths=None) -> dict:
    """All of stations.json ({} when there is none)."""
    paths = paths or CrewPaths.from_env()
    if not paths.stations.is_file():
        return {}
    data = read_json(paths.stations)
    if not isinstance(data, dict):
        raise CrewError(f"{paths.shown('stations.json')} is not an object")
    return data


def stations_put(station: str, values: dict, paths=None) -> None:
    """bash's stations_put: merge `values` into stations.json[station] (jq's `+`: existing keys keep their
    place, new ones follow). A missing stations.json starts as {}."""
    paths = paths or CrewPaths.from_env()
    if not paths.stations.is_file():
        atomic_write_text(paths.stations, "{}\n")
    data = stations_load(paths)
    old = data.get(station)
    if old is None or old is False:
        old = {}
    if not isinstance(old, dict) or not isinstance(values, dict):
        raise CrewError(f"{station} in {paths.shown('stations.json')} is not an object")
    data[station] = {**old, **values}
    write_json(paths.stations, data)


# ---------- halt, pace, wake, token ----------


def halted(paths=None) -> bool:
    return (paths or CrewPaths.from_env()).halt_file.is_file()


def halt_write(reason: str = "halted by the human", *, paths=None, at=None) -> None:
    """`crew halt`: HALT holds {at, reason}, compact."""
    paths = paths or CrewPaths.from_env()
    write_json(paths.halt_file, {"at": at or utc_stamp(), "reason": reason or "halted by the human"}, compact=True)


def halt_reason(paths=None) -> str:
    """`jq -r .reason HALT`, "" when it can't be read."""
    paths = paths or CrewPaths.from_env()
    return jq_get(read_json(paths.halt_file, None), ("reason",))


def resume(paths=None) -> None:
    """`crew resume`: remove HALT."""
    try:
        (paths or CrewPaths.from_env()).halt_file.unlink()
    except FileNotFoundError:
        pass


PACE_DEFAULT = {"stretched": False, "held": False}
WAKE_DEFAULT = {"seen": [], "n": 0, "last_wake": None, "clock": None, "baseline": None, "released": False}


def pace_station_read(station: str, paths=None) -> dict:
    """stations/<station>.pace: {stretched, held}, the defaults under whatever the file has."""
    paths = paths or CrewPaths.from_env()
    data = read_json(paths.pace_station_file(station), None)
    return {**PACE_DEFAULT, **data} if isinstance(data, dict) else dict(PACE_DEFAULT)


def pace_station_put(station: str, stretched: bool, held: bool, paths=None) -> None:
    """What the station has been told; nothing told, no file."""
    paths = paths or CrewPaths.from_env()
    f = paths.pace_station_file(station)
    if not stretched and not held:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    write_json(f, {"stretched": stretched, "held": held}, compact=True)


def wake_read(station: str, paths=None) -> dict:
    """stations/<station>.wake: the wake state, the defaults under whatever the file has."""
    paths = paths or CrewPaths.from_env()
    data = read_json(paths.wake_file(station), None)
    return {**WAKE_DEFAULT, **data} if isinstance(data, dict) else dict(WAKE_DEFAULT, seen=[])


def wake_write(station: str, state: dict, paths=None) -> None:
    """The wake state, written whole and compact."""
    paths = paths or CrewPaths.from_env()
    f = paths.wake_file(station)
    f.parent.mkdir(parents=True, exist_ok=True)
    write_json(f, state, compact=True)


def wake_update(station: str, fields: dict, paths=None) -> None:
    """Set these fields of the wake state and write it whole (bash's wake_update with a filter that sets them)."""
    state = wake_read(station, paths)
    state["seen"] = list(state["seen"]) if isinstance(state["seen"], list) else state["seen"]
    state.update(fields)
    wake_write(station, state, paths)


def crew_key(paths=None, env=None) -> str:
    """The crew's key: TARES_FACTORY_TOKEN_FILE, else the crew home's .tares-token, with all whitespace
    removed; "" when it can't be read."""
    paths = paths or CrewPaths.from_env()
    env = os.environ if env is None else env
    f = env.get("TARES_FACTORY_TOKEN_FILE") or paths.token_file
    try:
        return "".join(read_text(f).split())
    except OSError:
        return ""


def write_token(token: str, paths=None) -> None:
    """The key as keep_token writes it: the token and a newline, readable by the person only."""
    paths = paths or CrewPaths.from_env()
    atomic_write_text(paths.token_file, f"{token}\n", mode=0o600, private=True)
