"""Is the installed `tares` new enough for the Claude Code plugin that is running it?

The plugin names the oldest tares it works with in `min_tares.txt` at the plugin root (a plugin.json
key `min_tares` is read too, but `claude plugin validate` warns about an unknown manifest field, so the
file is the one to use). `check_plugin_compat` returns one line to show when the installed tares is
older, and an empty string in every other case. It never raises: a missing or broken file, or a
version it cannot read, counts as nothing to say.

Standard library only (no duckdb, fastapi or tares.daemon), like the rest of tares.crew.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

__all__ = ["check_plugin_compat", "installed_version", "parse_version", "plugin_min_version"]

_RELEASE = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$")
_PRE = re.compile(r"^[-._]?(?:a|alpha|b|beta|c|rc|pre|preview)[-._]?(\d*)", re.I)


def parse_version(text: str) -> tuple | None:
    """A sortable key for `1.2.3`, `1.2.3-rc.1`, `1.2.3rc1`, or None when it is not a version.

    The key is (release numbers without trailing zeros, 0 for a pre-release or 1 for a release, the
    pre-release number), so an rc of X sorts below X and 1.2 equals 1.2.0. A local suffix (`+abc`) and
    a post or dev tag are ignored."""
    m = _RELEASE.match((text or "").strip())
    if not m:
        return None
    nums = [int(n) for n in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    rest = m.group(2).split("+", 1)[0]
    pre = _PRE.match(rest)
    if pre:
        return (tuple(nums), 0, int(pre.group(1) or 0))
    if rest.strip(".-_"):
        # `.post1`, `.dev3` and the like: not a pre-release of the next version, keep the release
        if not re.match(r"^[-._]?(?:post|dev|rev|r)[-._]?\d*", rest, re.I):
            return None
    return (tuple(nums), 1, 0)


def _editable_version(dist, pyproject) -> str:
    """The version in a checkout's pyproject.toml when `dist` is an editable install of it, else "".
    An editable install's metadata keeps the version it was installed at, so after the checkout moves
    to a newer release the metadata is older than the code that runs."""
    try:
        info = json.loads(dist.read_text("direct_url.json") or "{}")
        if not (isinstance(info, dict) and (info.get("dir_info") or {}).get("editable")):
            return ""
        import tomllib
        with open(pyproject, "rb") as f:
            value = tomllib.load(f).get("project", {}).get("version")
        return value.strip() if isinstance(value, str) else ""
    except Exception:
        return ""


def installed_version() -> str:
    """The installed tares version: for an editable install the checkout's pyproject.toml, else the
    package metadata, else `tares.__version__`."""
    try:
        from importlib.metadata import PackageNotFoundError, distribution
        try:
            dist = distribution("tares")
        except PackageNotFoundError:
            dist = None
        if dist is not None:
            source = _editable_version(dist, Path(__file__).resolve().parents[2] / "pyproject.toml")
            return source or dist.version
    except Exception:
        pass
    try:
        from .. import __version__
        return str(__version__)
    except Exception:
        return ""


def plugin_min_version(plugin_root) -> str:
    """The `min_tares` of the plugin at plugin_root, or "" when it names none."""
    root = Path(plugin_root)
    try:
        text = (root / "min_tares.txt").read_text(encoding="utf-8").strip()
        if text:
            return text.splitlines()[0].strip()
    except (OSError, ValueError):
        pass
    try:
        data = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        value = data.get("min_tares") if isinstance(data, dict) else None
        return value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""


WINDOWS_UPGRADE_NOTE = ("On Windows an upgrade while a tares command is running can leave the install broken; "
                        "if that happens, stop every tares process and run the same command again.")


def on_windows() -> bool:
    return os.name == "nt"


def upgrade_advice(text: str, sentence: bool = False) -> str:
    """The advice to upgrade or install tares. On Windows it first says to stop the crew, because an upgrade
    while any tares command runs can leave the install broken (TR-1281); elsewhere `text` comes back as given.
    `sentence` is for a full sentence, which starts with a capital."""
    if not on_windows():
        return text
    lead = "stop the crew first (tares factory crew down), then "
    body = text.rstrip(".")
    if sentence:
        lead, body = lead[0].upper() + lead[1:], body[:1].lower() + body[1:]
    return f"{lead}{body}. {WINDOWS_UPGRADE_NOTE}"


def check_plugin_compat(plugin_root) -> str:
    """One line when the installed tares is older than the plugin's `min_tares`, else ""."""
    try:
        needed = plugin_min_version(plugin_root)
        have = installed_version()
        need_key, have_key = parse_version(needed), parse_version(have)
        if need_key is None or have_key is None or have_key >= need_key:
            return ""
        return (f"This Tares plugin needs tares {needed} or newer, but tares {have} is installed. "
                + upgrade_advice("Upgrade it with `uv tool upgrade tares` or `pip install -U tares`.", sentence=True))
    except Exception:
        return ""
