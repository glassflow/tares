"""`tares capabilities [--json]` (TR-1415): what this engine can do, and what the Tares it is signed in to can do.

An app that drives the crew reads this one document before it relies on a command or a cell feature, instead of
trying a call and reading its failure. The document (schemas/capabilities.json):

    {"schema_version": 1, "engine": "1.50.0", "commands": ["factory.status", ...],
     "cell": {"url": "https://...", "version": "1.50.0", "capabilities": ["recall", ...]}}

`commands` is COMMANDS below, a fixed list of the dotted names of the machine commands (those with `--json` and a
schema). A new machine command adds its name here in the same change; tests/test_crew_capabilities.py fails when a
registered command has no name here and no reason to be left out. `cell` is what the cell's /health says. Not
signed in, a cell that does not answer, or an answer that is not a Tares: the command still exits 0, with nulls and
an empty list, so a caller never has to handle a failure to ask. Only the address is taken from the login file; the
key is not used or sent: /health is open.

Standard library plus httpx (already a dependency of tares login), imported when the command runs.
"""
from __future__ import annotations

import os
import sys

from . import contract
from .errors import CrewError

# Dotted names: `factory.` and the words after `tares factory`, or the top-level word (login, logout, ...).
COMMANDS = (
    "factory.status",
    "factory.roster",
    "factory.desk",
    "factory.events",
    "factory.doctor",
    "factory.projects",
    "factory.setup",
    "factory.setup.apply",
    "factory.settings",
    "factory.tidy",
    "factory.restart",
    "factory.revive",
    "factory.retire",
    "factory.crew.status",
    "factory.crew.desk",
    "factory.crew.setup",
    "factory.crew.up",
    "factory.crew.down",
    "factory.crew.halt",
    "factory.crew.resume",
    "factory.crew.freeze",
    "factory.crew.unfreeze",
    "factory.crew.update",
    "factory.crew.restart",
    "factory.crew.watch",
    "login",
    "logout",
    "capabilities",
)

HEALTH_TIMEOUT = 3  # seconds: this command is meant to be quick when the cell is slow or gone


def engine_version() -> str:
    """The version `tares --version` shows."""
    from ..cli import _pkg_version
    return _pkg_version()


def cell_url() -> str | None:
    """The address of the Tares a command would talk to, or None when none is named: the address in
    TARES_FACTORY_URL or TARESD_URL, else the one `tares login` saved. Nothing at the local default counts as
    not signed in. Only the address is read: the key (and the keychain it may be in) is not used."""
    url = (os.getenv("TARES_FACTORY_URL") or os.getenv("TARESD_URL") or "").strip()
    if not url:
        from ..login import _read_file
        data = _read_file()
        url = str((data or {}).get("cell_url") or "").strip()
    return url.rstrip("/") or None


def cell_health(url: str) -> tuple[str | None, list[str]]:
    """(version, capabilities) from the cell's /health; (None, []) for anything else: no answer, an error status,
    a body that is not JSON or not an object."""
    import httpx

    from ..login import _http
    try:
        status, body = _http("GET", f"{url}/health", timeout=HEALTH_TIMEOUT)
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
        return None, []
    if not 200 <= status < 300:
        return None, []
    version = body.get("version")
    caps = body.get("capabilities")
    return (version if isinstance(version, str) and version else None,
            [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else [])


def collect() -> dict:
    url = cell_url()
    version, caps = cell_health(url) if url else (None, [])
    return {"engine": engine_version(), "commands": list(COMMANDS),
            "cell": {"url": url, "version": version, "capabilities": caps}}


def cmd_capabilities(json_out: bool = False) -> int:
    try:
        doc = collect()
    except Exception as e:  # a bug: with --json still one shape for the caller
        if not json_out:
            raise
        contract.refuse(CrewError(f"{type(e).__name__}: {e}"), ["--json"])
        return contract.EXIT_FAILED
    if json_out:
        contract.emit(doc, "capabilities")
        return 0
    cell = doc["cell"]
    print(f"tares {doc['engine']}")
    print(f"machine commands ({len(doc['commands'])}): " + ", ".join(doc["commands"]))
    if not cell["url"]:
        print("Tares: not signed in (run tares login)")
    elif cell["version"] is None and not cell["capabilities"]:
        print(f"Tares: {cell['url']} did not answer")
    else:
        print(f"Tares: {cell['url']}, version {cell['version'] or 'unknown'}")
        print("  capabilities: " + (", ".join(cell["capabilities"]) or "none"))
    return 0
