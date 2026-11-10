"""The monitoring slot: where station traces and metrics go."""
from __future__ import annotations

from .base import Integration


class Monitoring(Integration):
    slot = "monitoring"

    def exporter_config(self) -> dict:
        """The exporter settings for the cell's own traces: endpoint, headers and provider.
        Returns a dict; it is empty when monitoring is off. A plain function, not async."""
        raise NotImplementedError

    def station_env(self, labels: dict) -> dict:
        """The environment variables a station needs so its sessions report to this
        monitoring, with `labels` (station, project, role) attached. Returns a dict of
        variable names to values. A plain function, not async."""
        raise NotImplementedError
