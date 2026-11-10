"""The runtime slot: what runs a station."""
from __future__ import annotations

from .base import Integration
from .types import LaunchSpec


class Runtime(Integration):
    slot = "runtime"

    def launch_spec(self, role: str, station: str, ctx: dict) -> LaunchSpec:
        """How to start the station `station` playing `role`. `ctx` carries the project, repo
        and cell address. Returns a LaunchSpec. Raises ValueError for a role this runtime
        cannot run. A plain function, not async."""
        raise NotImplementedError
