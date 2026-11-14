"""Where the crew's files live. The same names bash uses, under the crew home."""
import os
from dataclasses import dataclass
from pathlib import Path

ME = "tares factory"

CREW_NAME_RULE = ("1 to 31 characters: a-z, 0-9 and hyphens, starting with a letter or digit, "
                  "and not containing the word build between hyphens")


def home_dir(env=None) -> str:
    """The person's home folder: $HOME first as bash has it, then USERPROFILE, then expanduser (which covers
    Windows, where HOME is often unset)."""
    env = os.environ if env is None else env
    return env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")


def default_home(env=None) -> str:
    """~/factory."""
    return f"{home_dir(env)}/factory"


@dataclass(frozen=True)
class CrewPaths:
    """The crew home and every file in it. `raw` is the home as the person wrote it (FACTORY_CREW_HOME or
    the default), kept as typed so a message names it exactly as bash would; `home` is the same place as a Path."""
    raw: str

    @classmethod
    def from_env(cls) -> "CrewPaths":
        """FACTORY_CREW_HOME, else ~/factory (an empty value counts as unset, as `${VAR:-default}` does)."""
        return cls(os.environ.get("FACTORY_CREW_HOME") or default_home())

    @classmethod
    def at(cls, home) -> "CrewPaths":
        return cls(os.fspath(home))

    def shown(self, name: str) -> str:
        """A file of the crew home as bash writes it in a message: "$CREW_HOME/<name>"."""
        return f"{self.raw}/{name}"

    @property
    def home(self) -> Path:
        return Path(self.raw)

    @property
    def crew_cfg(self) -> Path:
        return self.home / "crew.json"

    @property
    def stations(self) -> Path:
        return self.home / "stations.json"

    @property
    def halt_file(self) -> Path:
        return self.home / "HALT"

    @property
    def frozen_kit(self) -> Path:
        return self.home / "frozen-kit"

    @property
    def newer_file(self) -> Path:
        return self.home / "kit-newer"

    @property
    def freezing(self) -> Path:
        return self.home / "freezing"

    @property
    def cc_newer_file(self) -> Path:
        return self.home / "claude-newer"

    @property
    def checkout_file(self) -> Path:
        return self.home / "kit-checkout"

    @property
    def events(self) -> Path:
        return self.home / "events.jsonl"

    @property
    def token_file(self) -> Path:
        return self.home / ".tares-token"

    @property
    def pace_file(self) -> Path:
        """pace.json, the crew-wide pace (the stopped stations)."""
        return self.home / "pace.json"

    @property
    def stations_dir(self) -> Path:
        return self.home / "stations"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    @property
    def worktrees_dir(self) -> Path:
        return self.home / "worktrees"

    @property
    def launch_dir(self) -> Path:
        """launch/, the launch spec the cell gave each station, kept for a start while Tares is down (TR-870)."""
        return self.home / "launch"

    def launch_file(self, station: str) -> Path:
        return self.launch_dir / f"{station}.json"

    def station_file(self, station: str, suffix: str) -> Path:
        return self.stations_dir / f"{station}.{suffix}"

    def pace_station_file(self, station: str) -> Path:
        return self.station_file(station, "pace")

    def wake_file(self, station: str) -> Path:
        return self.station_file(station, "wake")

    def done_file(self, station: str) -> Path:
        return self.station_file(station, "done")

    def lock_dir(self, station: str) -> Path:
        return self.station_file(station, "lock")
