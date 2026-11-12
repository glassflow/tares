"""The crew engine's shared state: the crew home's files, read and written the way the old bash crew
(removed in TR-975) did, so a crew started before the upgrade keeps working.

Standard library only at import time (no duckdb, fastapi or tares.daemon): `import tares.crew`
stays fast and safe in a plain script. `tares factory` calls dispatch.run, which runs the command.

  paths     where each file lives (CrewPaths)
  state     crew.json, stations.json, events.jsonl, HALT, pace, wake and token files; names; time
  locks     the per-station lock, the same mkdir protocol as the old bash crew
  perms     a file that holds a key readable by its owner only (0600, or an access list on Windows)
  client    the engine's client for Tares (httpx); import it as `tares.crew.client`, it is not loaded here
  dispatch  which function runs a `tares factory` command (PORTED), and the help text
  up        `crew up` and the `crew setup` questions-and-answers contract; import it as `tares.crew.up`
  version   is the installed tares new enough for the plugin (check_plugin_compat, `tares hook compat`)
"""
from . import errors, locks, paths, perms, state
from .errors import CrewError, StationLockedError
from .paths import CrewPaths

__all__ = ["CrewError", "CrewPaths", "StationLockedError", "errors", "locks", "paths", "perms", "state"]
