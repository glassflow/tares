"""Errors the crew code raises. Bash dies with `tares factory: <message>`; `.line` is that text."""
from .paths import ME


class CrewError(Exception):
    """A refusal bash would make with `die`. str(error) is the message without the `tares factory: ` prefix.
    `code` is the stable name the error shape carries with --json (schemas/error.json)."""

    code = "error"

    @property
    def message(self) -> str:
        return str(self)

    @property
    def line(self) -> str:
        return f"{ME}: {self}"


class StationLockedError(CrewError):
    """Another live factory command holds the station's lock."""

    code = "station_locked"

    def __init__(self, station: str, holder: str):
        self.station = station
        self.holder = holder
        super().__init__(f"another factory command (pid {holder}) is starting or stopping {station}; try again in a minute")


class ClaudeNotFoundError(CrewError):
    """No usable `claude` binary: none on PATH or in the known install folders, and CLAUDE_BIN is not set."""

    code = "claude_not_found"

    def __init__(self):
        super().__init__("claude not found on PATH (set CLAUDE_BIN to its path)")


class SessionListUnreadableError(CrewError):
    """`claude agents --json` failed or printed something that is not a list. A list that cannot be read is
    never taken for an empty one: that would make every station look stopped."""

    code = "sessions_unreadable"


class JobStateUnreadableError(CrewError):
    """A session's state.json exists but is not a JSON object (a half-written file, say)."""

    code = "job_state_unreadable"
