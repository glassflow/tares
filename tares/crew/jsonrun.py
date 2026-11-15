"""Run a text command for a program (TR-1283): `--json` gives one result document instead of lines for a person.

The lifecycle commands (up, down, halt, resume, freeze, unfreeze, update, restart, and a station's restart,
revive and retire) print what they do as lines on stderr and leave stdout empty. With `--json` this module runs the
same command with everything it prints collected (the lines on stderr also go live to the real stderr, one line
each, so a caller can show progress; stdout holds only the document), and prints one document on stdout when done:

    {"schema_version": 1, "ok": true, "command": "crew halt", "notes": ["crew halted: ..."]}

or, for a refusal, the error document (schemas/error.json), which carries the notes collected so far as `notes`
beside `error` when the command printed any before it failed. `notes` are the lines the text mode prints on
stderr, for a person, not for parsing. Without `--json` the command runs exactly as before.

Standard library plus contract.py.
"""
from __future__ import annotations

import contextlib
import contextvars
import io
import os
import sys
from typing import Callable

from . import contract
from .errors import CrewError
from .paths import ME

FLAG = "--json"

# optional keys a command adds to its result document (add_result); one dict per --json run, held for that run only
_extra: contextvars.ContextVar = contextvars.ContextVar("tares_crew_json_extra", default=None)


@contextlib.contextmanager
def scope():
    """Collect the keys add_result gets inside the block; yields the dict. run() opens one for every --json run."""
    d: dict = {}
    token = _extra.set(d)
    try:
        yield d
    finally:
        _extra.reset(token)


def add_result(key: str, value) -> None:
    """A command names an optional key for the result document it prints with --json (`crew update` lists the
    stations it left to the watch). The document carries it on success and on a failure. Without --json it is not
    used."""
    d = _extra.get()
    if d is not None:
        d[key] = value


class _Tee(io.TextIOBase):
    """Python's stderr under --json (TR-1344): every complete line goes into buf, for the document's notes, and,
    as it happens, to the real stderr as "tares factory: <msg>", flushed, so a program that runs the command can
    show what it is doing. They are the same text as the notes."""

    def __init__(self, buf: io.StringIO, real):
        self.buf = buf
        self.real = real
        self.pending = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self.buf.write(text)
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self._live(line)
        return len(text)

    def _live(self, raw: str) -> None:
        line = raw.strip()
        if not line:
            return
        if not line.startswith(f"{ME}: "):
            line = f"{ME}: {line}"
        try:
            self.real.write(line + "\n")
            self.real.flush()
        except (OSError, ValueError, AttributeError):   # a closed, broken or missing (pythonw) stderr must not fail the command
            pass

    def flush(self) -> None:
        if self.pending:
            line, self.pending = self.pending, ""
            self._live(line)


@contextlib.contextmanager
def _collect(buf: io.StringIO):
    """Python's stdout goes to buf, stderr to buf and live to the real stderr (_Tee), and file descriptor 1 goes
    to stderr, so a child process that inherits it cannot write into the document. Everything is put back on exit,
    before the caller prints."""
    saved = None
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    try:
        dup = os.dup(1)
    except OSError:   # no descriptor 1 (closed): nothing to guard
        dup = None
    if dup is not None:
        try:
            os.dup2(2, 1)
            saved = dup
        except OSError:   # no descriptor 2 (pythonw): nothing to point 1 at; do not leak the copy
            os.close(dup)
    try:
        tee = _Tee(buf, sys.stderr)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(tee):
            try:
                yield
            finally:
                tee.flush()
    finally:
        if saved is not None:
            try:
                os.dup2(saved, 1)
            finally:
                os.close(saved)


def _notes(text: str) -> list:
    prefix = f"{ME}: "
    notes = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
        if line:
            notes.append(line)
    return notes


def run(argv: list, command: str, body: Callable[[list], int], *, text_fail: Callable[[CrewError], None]) -> int:
    """body(argv) -> exit code. Without `--json`: as the command always ran, a CrewError goes to text_fail and
    gives 1. With it: every `--json` is taken out of argv, the output is collected, and one document is printed."""
    if FLAG not in argv:
        try:
            return body(argv) or 0
        except CrewError as e:
            text_fail(e)
            return 1
    args = [a for a in argv if a != FLAG]
    buf = io.StringIO()
    error = None
    code = 0
    with scope() as extra:
        try:
            with _collect(buf):
                code = body(args) or 0
        except CrewError as e:
            error = e
    if error is None and code:
        error = CrewError(f"{command} failed (exit {code})")
    if error is not None:
        contract.refuse(error, [FLAG], notes=_notes(buf.getvalue()), extra=extra)
        return contract.EXIT_USAGE if error.code == "usage" else contract.EXIT_FAILED
    contract.emit({"ok": True, "command": command, "notes": _notes(buf.getvalue()), **extra}, "result")
    return 0
