"""Make a file that holds a key readable by its owner only.

Mac and Linux: mode 0600. Windows has no mode bits, so the file gets an access list that names the
current user and nobody else (icacls, which ships with Windows). The user is named by security identifier
(whoami /user), which resolves on a domain, Azure AD and Microsoft-account login alike; where whoami
cannot say, by DOMAIN\\name. The key is never on icacls's command line; only the file's path and the
user are.
"""
import csv
import getpass
import io
import os
import re
import subprocess


def _windows() -> bool:
    return os.name == "nt"


def windows_user() -> str:
    """DOMAIN\\name of the current user as icacls wants it, else just the name."""
    name = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN") or ""
    return f"{domain}\\{name}" if domain else name


_SID = re.compile(r"S-1-\d+(-\d+)+")


def windows_sid(*, run=None) -> str | None:
    """The current user's security identifier from `whoami /user /fo csv /nh` (second column), else None."""
    run = run or subprocess.run
    try:
        r = run(["whoami", "/user", "/fo", "csv", "/nh"], stdin=subprocess.DEVNULL, capture_output=True,
                timeout=30)
        if r.returncode != 0:
            return None
        out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
        row = next(csv.reader(io.StringIO(out.strip().splitlines()[0])))
    except (OSError, subprocess.SubprocessError, StopIteration, IndexError, csv.Error, AttributeError):
        return None
    return row[1].strip() if len(row) > 1 and _SID.fullmatch(row[1].strip()) else None


def windows_grantee(*, run=None) -> str:
    """Who icacls grants: `*S-1-5-...` (the SID), else DOMAIN\\name when the SID cannot be read."""
    sid = windows_sid(run=run)
    return f"*{sid}" if sid else windows_user()


def icacls_command(path, user: str | None = None) -> list[str]:
    """The icacls call that drops every inherited entry and grants the user full control alone. `user` is
    a name or `*SID`; unset, the DOMAIN\\name from the environment."""
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
        r = run(icacls_command(path, windows_grantee(run=run)), stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
