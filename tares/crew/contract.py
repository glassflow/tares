"""The machine interface of the crew commands (TR-1067): one way to print JSON, one shape for a failure.

A command run with `--json` prints one JSON document (or, for `events`, one document per line) on stdout and
nothing else there. Every document carries `schema_version`, the number in schemas/schema_version.json.
A failure prints `{"schema_version": 1, "error": {"code": "...", "message": "..."}}` on stdout and exits
with the code the caller gives. The schemas and the exit codes are written down in schemas/README.md.

Standard library only. The schemas are for callers and tests to validate against; nothing here reads them
except schema_version.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .errors import CrewError

SCHEMAS = Path(__file__).resolve().parent / "schemas"

# Exit codes (the README repeats these).
EXIT_OK = 0
EXIT_FAILED = 1  # the command ran and failed; with --json the error shape is on stdout
EXIT_USAGE = 2  # a usage error, or the dispatcher refusing the engine; text on stderr


def wants_json(argv) -> bool:
    """True when `--json` is one of the arguments."""
    return "--json" in argv


def schema_version() -> int:
    return int(json.loads((SCHEMAS / "schema_version.json").read_text(encoding="utf-8"))["schema_version"])


def line(obj: dict) -> str:
    """One document as it is printed: compact, UTF-8 text, no newline."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _write(text: str) -> None:
    """Write text to stdout as UTF-8 with LF line ends on every platform, then flush."""
    out = sys.stdout
    raw = getattr(out, "buffer", None)
    if raw is not None:
        out.flush()
        raw.write(text.encode("utf-8"))
        raw.flush()
    else:  # a stream a test or a wrapper put in place
        out.write(text)
        out.flush()


def document(obj: dict) -> dict:
    """obj with `schema_version` first."""
    return {"schema_version": schema_version(), **{k: v for k, v in obj.items() if k != "schema_version"}}


def emit(obj: dict, schema: str = "") -> None:
    """Print obj as one JSON document and a newline. `schema` is the name of the schema file the document
    follows (status, roster, ...); it is not printed, it is for the reader of the code and for the test that
    validates every emitted document against schemas/<schema>.json."""
    del schema
    _write(line(document(obj)) + "\n")


def error_document(code: str, message: str) -> dict:
    return document({"error": {"code": code, "message": message}})


def fail(code: str, message: str, exit_code: int = EXIT_FAILED):
    """Print the error shape and leave with exit_code. Does not return."""
    _write(line(error_document(code, message)) + "\n")
    raise SystemExit(exit_code)


def refuse(e: CrewError, argv) -> bool:
    """Print a CrewError as the error shape when the caller asked for JSON. True when it did, False when
    argv has no `--json` and the caller prints the text line itself."""
    if not wants_json(argv):
        return False
    _write(line(error_document(getattr(e, "code", "error"), e.message)) + "\n")
    return True
