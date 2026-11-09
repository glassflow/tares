"""Errors the crew code raises. Bash dies with `tares factory: <message>`; `.line` is that text."""
from .paths import ME


class CrewError(Exception):
    """A refusal bash would make with `die`. str(error) is the message without the `tares factory: ` prefix."""

    @property
    def message(self) -> str:
        return str(self)

    @property
    def line(self) -> str:
        return f"{ME}: {self}"


class StationLockedError(CrewError):
    """Another live factory command holds the station's lock."""

    def __init__(self, station: str, holder: str):
        self.station = station
        self.holder = holder
        super().__init__(f"another factory command (pid {holder}) is starting or stopping {station}; try again in a minute")


class ClaudeNotFoundError(CrewError):
    """No usable `claude` binary: none on PATH or in the known install folders, and CLAUDE_BIN is not set."""

    def __init__(self):
        super().__init__("claude not found on PATH (set CLAUDE_BIN to its path)")


class SessionListUnreadableError(CrewError):
    """`claude agents --json` failed or printed something that is not a list. A list that cannot be read is
    never taken for an empty one: that would make every station look stopped."""


class JobStateUnreadableError(CrewError):
    """A session's state.json exists but is not a JSON object (a half-written file, say)."""
