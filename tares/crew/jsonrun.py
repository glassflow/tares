"""Run a text command for a program (TR-1283): `--json` gives one result document instead of lines for a person.

The lifecycle commands (up, down, halt, resume, freeze, unfreeze, update, restart, and a station's restart,
revive and retire) print what they do as lines on stderr and leave stdout empty. With `--json` this module runs the
same command with everything it prints collected, and prints one document on stdout when it is done:

    {"schema_version": 1, "ok": true, "command": "crew halt", "notes": ["crew halted: ..."]}

or, for a refusal, the error document (schemas/error.json), which carries the notes collected so far as `notes`
beside `error` when the command printed any before it failed. `notes` are the lines the text mode prints on
stderr, for a person, not for parsing. Without `--json` the command runs exactly as before.

Standard library plus contract.py.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from typing import Callable

from . import contract
from .errors import CrewError
from .paths import ME

FLAG = "--json"


@contextlib.contextmanager
def _collect(buf: io.StringIO):
    """Python's stdout and stderr go to buf, and file descriptor 1 goes to stderr, so a child process that
    inherits it cannot write into the document. Everything is put back on exit, before the caller prints."""
    saved = None
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    try:
        saved = os.dup(1)
        os.dup2(2, 1)
    except OSError:   # no descriptor 1 (closed): nothing to guard
        saved = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield
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
    try:
        with _collect(buf):
            code = body(args) or 0
    except CrewError as e:
        error = e
    if error is None and code:
        error = CrewError(f"{command} failed (exit {code})")
    if error is not None:
        contract.refuse(error, [FLAG], notes=_notes(buf.getvalue()))
        return contract.EXIT_USAGE if error.code == "usage" else contract.EXIT_FAILED
    contract.emit({"ok": True, "command": command, "notes": _notes(buf.getvalue())}, "result")
    return 0
