"""MCP headers helper: prints the JSON headers Claude Code adds to its calls to the Tares MCP endpoint.
The Python twin of bin/mcp-headers, with the same answers; `tares hook mcp-headers` runs it in process.

The bearer comes from the first of these that gives one:
  1. The crew's token file, named by TARES_FACTORY_CRED_FILE (the factory keeps the token in a file,
     never in a station's settings, and Claude Code hides variables named *TOKEN* from this helper).
  2. The key `tares login` saved, only when Claude Code is calling that same workspace's MCP endpoint
     (CLAUDE_CODE_MCP_SERVER_URL is the saved cell_url plus /mcp), so the key never goes to another
     address. The key is read from login.json, or from the OS keychain when login.json says it is there.
  3. Nothing: `{}`.

It never fails and never prints anything else: a missing or broken file, a locked keychain or a
workspace that does not match all give `{}`. A token is never put in argv, and never in a message.
Standard library only, and 3.9-safe: it runs on a person's machine, not in the repo's venv. The keychain
is read through tares.login, imported only when login.json needs it.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys


def env(name, default=""):
    """${NAME:-default}: the default when the variable is unset or empty."""
    return os.environ.get(name) or default


def _bearer(token: str) -> str:
    return json.dumps({"Authorization": "Bearer " + token}, separators=(",", ":"))


def crew_token() -> str:
    """The token in the crew's token file with every space and newline removed, or ""."""
    path = env("TARES_FACTORY_CRED_FILE")
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            return b"".join(f.read().split()).decode("utf-8")
    except (OSError, ValueError):
        return ""


def login_path() -> str:
    return os.path.join(env("TARES_HOME") or os.path.join(os.path.expanduser("~"), ".tares"), "login.json")


def keychain_key() -> str:
    """The saved key when it is in the OS keychain: tares.login does the reading (it knows the opt-in
    and the account), with what it would say on stderr dropped. "" when it cannot be had."""
    try:
        from tares import login
        with contextlib.redirect_stderr(io.StringIO()):
            data = login.saved()
        return str((data or {}).get("key") or "")
    except Exception:
        return ""


def saved_key(url: str) -> str:
    """The key `tares login` saved when `url` is that workspace's MCP endpoint, else ""."""
    if not url:
        return ""
    try:
        with open(login_path(), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
        if not isinstance(data, dict):
            return ""
        cell = str(data.get("cell_url") or "").rstrip("/")
        key = str(data.get("key") or "")
        in_keychain = data.get("key_store") == "keychain"
    except (OSError, ValueError):
        return ""
    if not cell or url.rstrip("/") != cell + "/mcp":
        return ""
    if not key and in_keychain:
        key = keychain_key()
    return key


def headers_json() -> str:
    token = crew_token()
    if not token:
        token = saved_key(os.environ.get("CLAUDE_CODE_MCP_SERVER_URL", ""))
    return _bearer(token) if token else "{}"


def main() -> int:
    out = headers_json()
    # bytes, so Windows does not turn the newline into CRLF: the helper prints what the bash one printed
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:  # a stdout that is not a real file (a test's StringIO)
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    else:
        stream.write(out.encode("utf-8") + b"\n")
        stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
