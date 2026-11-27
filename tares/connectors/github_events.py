"""Moved to tares.github_events (Tares 2.0, TR-1807); this name stays until the connectors go (TR-1811)."""
import sys as _sys

from .. import github_events as _moved

_sys.modules[__name__] = _moved
