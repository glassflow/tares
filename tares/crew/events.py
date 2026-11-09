"""`tares factory events --json [--follow] [--since <cursor>]`: the crew's event log as a stream (TR-1067).

events.jsonl is appended to by every crew command (state.event). This command reads it for a program: one
JSON line per event, `{"schema_version": 1, "cursor": "<file id>:<offset>", "event": {...}}`.

  --follow        keep reading as the file grows, until the reader goes away; without it, print what the
                  file holds from the cursor and stop
  --since CURSOR  start after the line CURSOR came with; nothing older is sent again

The cursor is opaque to a caller: keep the last one, hand it back after a reconnect. It is the file's id
(os.stat().st_ino) and the byte offset after the line. Reading survives the file being truncated (a size
below the offset: start again at 0), rotated (another id: the new file from 0) or missing for a while.
A cursor from another file, or one past the end of this file, starts at 0 as well: a line is then sent
twice rather than lost. Only complete lines are sent, so a writer in the middle of a line is waited for.
One case is not seen: a file truncated and written past the old offset between two polls.

Standard library only; untested on real Windows (the file id there is the file index os.stat reports).
"""
from __future__ import annotations

import errno
import json
import os
import sys
import time
from typing import Callable, Iterator, Optional

from . import contract
from .errors import CrewError
from .paths import ME, CrewPaths

POLL = 0.25
USAGE = f"usage: {ME} events --json [--follow] [--since <cursor>]"


class UsageError(CrewError):
    code = "usage"


class BadCursorError(CrewError):
    code = "bad_cursor"


def parse_cursor(text: str):
    """(file id, offset) from a cursor, or BadCursorError."""
    a, sep, b = str(text).partition(":")
    if not sep or not a.isdigit() or not b.isdigit():
        raise BadCursorError(f'"{text}" is not a cursor from {ME} events')
    return int(a), int(b)


def make_cursor(ino: int, offset: int) -> str:
    return f"{ino}:{offset}"


def _open(path):
    try:
        return open(path, "rb")
    except OSError:
        return None


def follow(path, since: Optional[str] = None, *, tail: bool = True, poll: float = POLL,
           stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep
           ) -> Iterator[tuple]:
    """Yield (cursor, event) for each complete line of the file at path, from `since` on.

    tail=False stops at the end of what the file holds; tail=True waits for more until stop() is true.
    Lines that are not a JSON object are skipped (their offset still passes)."""
    wanted = parse_cursor(since) if since else None
    ino = None  # the file id we are reading
    offset = 0
    while True:
        f = _open(path)
        if f is not None:
            with f:
                st = os.fstat(f.fileno())
                if ino is None:  # first sight of a file
                    ino = st.st_ino
                    offset = wanted[1] if wanted and wanted[0] == ino and wanted[1] <= st.st_size else 0
                elif st.st_ino != ino:  # rotated: the new file from its start
                    ino, offset = st.st_ino, 0
                elif st.st_size < offset:  # truncated
                    offset = 0
                if st.st_size > offset:
                    f.seek(offset)
                    data = f.read(st.st_size - offset)
                    end = data.rfind(b"\n") + 1  # complete lines only
                    pos = 0
                    while pos < end:
                        nl = data.index(b"\n", pos)
                        raw = data[pos:nl]
                        pos = nl + 1
                        offset += len(raw) + 1
                        try:
                            obj = json.loads(raw.decode("utf-8", errors="replace"))
                        except ValueError:
                            continue
                        if isinstance(obj, dict):
                            yield make_cursor(ino, offset), obj
        if not tail or stop():
            return
        sleep(poll)


def _parse(argv: list) -> tuple:
    """(follow, since) from the arguments after the word `events`."""
    follow_on, since, i = False, None, 0
    args = argv[1:]
    while i < len(args):
        a = args[i]
        if a == "--json":
            pass
        elif a == "--follow":
            follow_on = True
        elif a == "--since":
            i += 1
            if i >= len(args) or not args[i]:
                raise UsageError(f"--since needs a cursor; {USAGE}")
            since = args[i]
        elif a.startswith("--since="):
            since = a.split("=", 1)[1]
            if not since:
                raise UsageError(f"--since needs a cursor; {USAGE}")
        else:
            raise UsageError(f"unknown argument {a}; {USAGE}")
        i += 1
    return follow_on, since


def cmd_events(argv: list, env: dict) -> int:
    """`events --json [--follow] [--since <cursor>]`. argv starts with the word events."""
    if not contract.wants_json(argv):
        print(f"{ME}: events prints JSON only; {USAGE}", file=sys.stderr)
        return contract.EXIT_USAGE
    try:
        follow_on, since = _parse(argv)
        path = CrewPaths.from_env().events
        for cursor, event in follow(path, since, tail=follow_on):
            contract.emit({"cursor": cursor, "event": event}, "events")
    except CrewError as e:
        contract.refuse(e, argv)
        return contract.EXIT_USAGE if isinstance(e, (UsageError, BadCursorError)) else contract.EXIT_FAILED
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        _quiet_stdout()
        return 0
    except OSError as e:
        if e.errno == errno.EPIPE:
            _quiet_stdout()
            return 0
        raise
    return 0


def _quiet_stdout() -> None:
    """The reader went away: point stdout at nowhere so the flush at exit does not raise again."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):
        pass
