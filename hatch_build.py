"""Wheel build hook: ship the built console as tares/console when there is one (TR-1170).

The console (ui/dist) is a build artifact, gitignored, so a fresh clone has none. A fixed force-include of
it made `uv tool install git+...` fail with "Forced include not found". Here it is added only when
ui/dist/index.html exists, and the build says so in one line when it is not. A release sets
TARES_REQUIRE_CONSOLE=1 so it can never go out without it.
"""
import os

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ConsoleBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        dist = os.path.join(self.root, "ui", "dist")
        if os.path.isfile(os.path.join(dist, "index.html")):
            build_data["force_include"][dist] = "tares/console"
            return
        if os.environ.get("TARES_REQUIRE_CONSOLE", "").strip().lower() not in ("", "0", "false", "no", "off"):
            raise RuntimeError("TARES_REQUIRE_CONSOLE is set but ui/dist/index.html is missing: "
                               "build the console first (cd ui && npm ci && npm run build)")
        self.app.display_warning("tares: building without the console (ui/dist/index.html not found); "
                                 "`tares up` will serve the API only")
