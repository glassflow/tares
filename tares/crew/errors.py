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
