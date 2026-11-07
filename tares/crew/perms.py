"""Make a file that holds a key readable by its owner only.

Mac and Linux: mode 0600. Windows has no mode bits, so the file gets an access list that names the
current user and nobody else (icacls, which ships with Windows). The key is never on icacls's command
line; only the file's path and the user's name are.
"""
import getpass
import os
import subprocess


def _windows() -> bool:
    return os.name == "nt"


def windows_user() -> str:
    """DOMAIN\\name of the current user as icacls wants it, else just the name."""
    name = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN") or ""
    return f"{domain}\\{name}" if domain else name


def icacls_command(path, user: str | None = None) -> list[str]:
    """The icacls call that drops every inherited entry and grants the user full control alone."""
    return ["icacls", os.fspath(path), "/inheritance:r", "/grant:r", f"{user or windows_user()}:F"]


def owner_only(path, *, run=None) -> bool:
    """Restrict `path` to the current user. True when it worked; False when it could not be done (the
    caller decides whether that is fatal)."""
    if not _windows():
        try:
            os.chmod(path, 0o600)
        except OSError:
            return False
        return True
    run = run or subprocess.run
    try:
        r = run(icacls_command(path), stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
