"""The runtime slot: what runs a station."""
from __future__ import annotations

import re

from .base import Integration
from .types import LaunchSpec

# The placeholders a launch spec leaves for the machine that starts the station, and what each stands for.
# The cell cannot know them: they are paths and lists of this machine's folders. The `tares factory` command
# line fills them with `fill_placeholders` just before it starts the session.
PLACEHOLDERS: dict[str, str] = {
    "CREW_HOME": "the crew's own folder on the machine that starts the station",
    "PLUGIN_DIR": "the Tares plugin folder the station runs on (a frozen crew runs from its frozen copy)",
    "PLUGIN_DIR_SHELL": "the same folder written as one word for the shell that runs the status line",
    "WT_REPOS": "the sentence naming the repositories whose worktree folders the station may use",
    "REPOS": "the sentence naming the repositories checked out on that machine",
    "TOKEN_FILE": "the file that holds the crew's key (the key itself is never in a spec)",
    "SETTINGS_FILE": "the settings file the station is started with (--settings)",
}


def fill_placeholders(value, values: dict):
    """A copy of `value` (a string, or lists and dicts holding them, as a spec's command, env and
    settings do) with each `{NAME}` of `values` replaced in every string. Dict keys and any other
    `{...}` stay as they are; `value` itself is not changed."""
    if isinstance(value, str):
        if not values:
            return value
        pattern = "|".join(re.escape(name) for name in sorted(values, key=len, reverse=True))
        # one pass: a value that holds another `{NAME}` is left as it is, whatever the order of `values`
        return re.sub(r"\{(" + pattern + r")\}", lambda m: str(values[m.group(1)]), value)
    if isinstance(value, list):
        return [fill_placeholders(v, values) for v in value]
    if isinstance(value, tuple):
        return tuple(fill_placeholders(v, values) for v in value)
    if isinstance(value, dict):
        return {k: fill_placeholders(v, values) for k, v in value.items()}
    return value


class Runtime(Integration):
    slot = "runtime"

    def launch_spec(self, role: str, station: str, ctx: dict) -> LaunchSpec:
        """How to start the station `station` playing `role`. `ctx` carries the project, repo
        and cell address. Returns a LaunchSpec. Raises ValueError for a role this runtime
        cannot run. A plain function, not async."""
        raise NotImplementedError
