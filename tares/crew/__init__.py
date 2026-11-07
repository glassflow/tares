"""The crew engine's shared state: the crew home's files, read and written the way the bash
`claude-plugin/bin/factory` does, so bash and Python can run one crew during the move.

Standard library only at import time (no duckdb, fastapi or tares.daemon): `import tares.crew`
stays fast and safe in a plain script. Bash is still the default engine: `tares factory` calls
dispatch.run, which runs a command in Python only once the command is ported, and wherever the two
differ, bash is right.

  paths     where each file lives (CrewPaths)
  state     crew.json, stations.json, events.jsonl, HALT, pace, wake and token files; names; time
  locks     the per-station lock, the same mkdir protocol as bash
  dispatch  which engine runs a `tares factory` command (PORTED, TARES_FACTORY_ENGINE)
  version   is the installed tares new enough for the plugin (check_plugin_compat, `tares hook compat`)
"""
from . import errors, locks, paths, state
from .errors import CrewError, StationLockedError
from .paths import CrewPaths

__all__ = ["CrewError", "CrewPaths", "StationLockedError", "errors", "locks", "paths", "state"]
