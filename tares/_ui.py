"""Where the built console (the SPA in ui/dist) lives, and whether this install has one.

An install from a release carries it as tares/console. An install from a git checkout has none unless
`npm run build` ran in ui/ (TR-1170), and everything but the console works without it.
"""
import os
from pathlib import Path

NO_CONSOLE_HINT = ("this install has no console (it was built from source without ui/dist). The API and the crew work. "
                   "To get the console: install a release (`pip install tares`), or run `npm ci && npm run build` in ui/, "
                   "or point TARES_UI_DIST at a built folder")


def ui_dist() -> Path:
    """Where the built console SPA lives. Prefer the copy shipped inside the wheel
    (tares/console, via the build hook); fall back to the source tree's ui/dist for
    `npm run dev`-style local work. TARES_UI_DIST overrides both."""
    if env := os.getenv("TARES_UI_DIST"):
        return Path(env)
    from importlib import resources
    packaged = resources.files("tares") / "console"   # present in an installed wheel
    if packaged.is_dir():
        return Path(str(packaged))
    return Path(__file__).resolve().parent.parent / "ui" / "dist"   # running from the source tree


def console_built(dist: Path | None = None) -> bool:
    """Whether there is a console to serve: the folder holds its index.html."""
    return ((dist or ui_dist()) / "index.html").is_file()
