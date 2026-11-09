"""`tares factory tidy [--force] <path>...`: remove a worktree, a scratch folder, a file or a link, but only
inside the crew's own folders. The port of cmd_tidy, tidy_roots, inside_tidy_roots and worktree_has_work.

Stations clean up through this rather than rm: Claude Code's delete check stops a session on some rm
commands until a person approves. It removes a worktree through git, removes a link itself and never what
it points at, and refuses anything outside the roots.

The root check works on real paths, so a symlink, a Windows junction or an 8.3 short name cannot lead out
of a root. On Windows paths compare without regard to case. A path is inside only when it is strictly
below a root: a root itself is refused, and so is a root that is a link, the filesystem root, the home
folder and any folder that holds the crew home.

Standard library only, except for the Tares client the repo roots come from.
"""
from __future__ import annotations

import ntpath
import os
import shutil
import stat
import subprocess
import sys
import time

from .client import TaresClient
from .errors import CrewError
from .paths import ME, CrewPaths, default_home, home_dir

USAGE = "usage: factory tidy [--force] <worktree or scratch folder>..."
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: a symlink or a junction on Windows
RETRIES = 4            # a file another process just closed is released a moment later on Windows
RETRY_PAUSE = 0.05


def _windows() -> bool:
    """A function so a test can patch it."""
    return sys.platform == "win32"


def _say(msg: str) -> None:
    print(f"{ME}: {msg}", file=sys.stderr)


def is_link(path) -> bool:
    """A symlink, or on Windows any reparse point (a junction counts: os.path.islink is False for one)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT)


def real(path) -> str:
    """The physical path: links, junctions and 8.3 names resolved (`cd -P`)."""
    return os.path.realpath(path)


def _key(path: str) -> str:
    """How two paths are compared: case-folded on Windows, as the filesystem sees them."""
    return os.path.normcase(os.path.normpath(path))


def _below(path: str, root: str) -> bool:
    """path is strictly inside root (both real paths)."""
    p, r = _key(path), _key(root)
    r = r if r.endswith(os.sep) else r + os.sep
    return p.startswith(r) and p != r


def _holds(root: str, folder: str) -> bool:
    """root is folder itself or holds it."""
    r = _key(root)
    r = r if r.endswith(os.sep) else r + os.sep
    f = _key(folder)
    f = f if f.endswith(os.sep) else f + os.sep
    return f.startswith(r)


def long_path(path: str) -> str:
    """On Windows the \\\\?\\ form, so a path past 260 characters can be removed. Elsewhere unchanged."""
    if not _windows():
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


class Tidy:
    """One run of the command: the environment, the crew home and the roots, found once."""

    def __init__(self, env: dict):
        self.env = env
        base = home_dir(env)
        self.home = base
        crew = env.get("FACTORY_CREW_HOME") or default_home(env)  # the same place paths.default_home names
        self.crew_home = crew
        self.paths = CrewPaths(crew)
        self._roots: list | None = None

    def repo_folders(self) -> list:
        """The repo folders of every project handed to the crew; none when Tares does not answer."""
        try:
            with TaresClient(self.paths, self.env, say=lambda _m: None) as client:
                return client.all_repos() if client.tares_on() else []
        except Exception:  # the roots from Tares are a bonus: no Tares, fewer roots
            return []

    def roots(self) -> list:
        if self._roots is None:
            e = self.env
            roots = [f"{self.crew_home}/worktrees", f"{self.crew_home}/tmp"]
            if e.get("FACTORY_WORKTREES"):
                roots.append(e["FACTORY_WORKTREES"])
            if e.get("FACTORY_REPO"):
                roots.append(f"{e['FACTORY_REPO']}/.factory/worktrees")
            if e.get("CLAUDE_JOB_DIR"):
                roots.append(f"{e['CLAUDE_JOB_DIR']}/tmp")
            roots += [f"{r}/.factory/worktrees" for r in self.repo_folders()]
            self._roots = roots
        return self._roots

    def inside_roots(self, resolved: str) -> bool:
        """True when the resolved path is strictly inside one of the roots."""
        home = real(self.home) if os.path.isdir(self.home) else self.home
        top = real(self.crew_home) if os.path.isdir(self.crew_home) else self.crew_home
        for r in self.roots():
            if not r or is_link(r):  # a root that is a link could point anywhere
                continue
            if not os.path.isdir(r):
                continue
            rr = real(r)
            if os.path.dirname(rr) == rr or _key(rr) == _key(home):  # the filesystem root, the home folder
                continue
            if _holds(rr, top):  # never a folder that holds the crew home
                continue
            if _below(resolved, rr):
                return True
        return False


def _git(args: list, **kw):
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", **kw)
    except OSError:
        return None


def worktree_has_work(path: str) -> bool:
    """Uncommitted changes or new files, or commits no remote has."""
    st = _git(["--no-optional-locks", "-C", path, "status", "--porcelain"])
    if st is not None and st.stdout.strip():
        return True
    lg = _git(["-C", path, "log", "--oneline", "-1", "HEAD", "--not", "--remotes"])
    return lg is not None and bool(lg.stdout.strip())


def _writable(path: str) -> None:
    """Clear the read-only bit of path (and, so an entry can be unlinked, make its folder writable)."""
    for p, extra in ((path, stat.S_IWRITE), (os.path.dirname(path), stat.S_IWRITE | stat.S_IRUSR | stat.S_IXUSR)):
        if is_link(p):  # chmod follows a link: it would change what the link points at
            continue
        try:
            os.chmod(p, os.stat(p).st_mode | extra)
        except OSError:
            pass


def _retrying(func, path):
    """func(path) again after clearing read-only bits, a few times with a short pause."""
    last = None
    for i in range(RETRIES):
        _writable(path)
        try:
            func(path)
            return
        except FileNotFoundError:
            return
        except OSError as e:
            last = e
            time.sleep(RETRY_PAUSE * (i + 1))
    if last is not None:
        raise last


def _on_error(func, path, exc):
    """shutil.rmtree's error handler: a read-only file (git objects are) gets its bit cleared and a retry."""
    if isinstance(exc, tuple):  # onerror passes (type, value, tb)
        exc = exc[1]
    if isinstance(exc, FileNotFoundError):
        return
    _retrying(func, path)


def remove_link(path: str) -> None:
    """Remove a symlink, junction or file itself, never what it points at."""
    def go(p):
        try:
            os.unlink(p)
        except (IsADirectoryError, PermissionError):
            if is_link(p) and os.path.isdir(p):  # a Windows junction or directory symlink
                os.rmdir(p)
            else:
                raise
    _retrying(go, path)


def remove_tree(path: str) -> None:
    """Remove a folder and all in it. Links inside are removed, not followed; read-only entries are
    made writable and retried; on Windows the long-path form is used."""
    target = long_path(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=lambda f, p, e: _on_error(f, p, e))
    else:
        shutil.rmtree(target, onerror=_on_error)


def cmd_tidy(argv: list, env: dict) -> int:
    """`tidy [--force] path...`. argv starts with the word tidy."""
    try:
        return _tidy(argv[1:], env)
    except CrewError as e:
        print(e.line, file=sys.stderr)
        return 1


def _strip(p: str) -> str:
    """Trailing slashes go, so a link given as `link/` is still the link. A drive or share root keeps its
    slash: `C:\\` stripped to `C:` would mean the current folder on that drive, not the drive's root."""
    if len(p) <= 1:
        return p
    if _windows():
        drive, tail = ntpath.splitdrive(p)
        if drive and not tail.strip("/\\"):
            return p
        return p.rstrip("/\\") or p[0]
    return p.rstrip("/") or p[0]


def _tidy(args: list, env: dict) -> int:
    force = "--force" in args
    paths = [a for a in args if a != "--force"]
    if not paths:
        raise CrewError(USAGE)
    t = Tidy(env)
    for p in paths:
        p = _strip(p)
        if is_link(p) or os.path.isfile(p):
            folder = os.path.dirname(p) or "."
            if not os.path.isdir(folder):
                raise CrewError(f"{p}: its folder doesn't exist")
            resolved = os.path.join(real(folder), os.path.basename(p))
        elif os.path.isdir(p):
            resolved = real(p)
        else:
            _say(f"{p} is already gone")
            continue
        if not os.path.lexists(resolved):
            _say(f"{p} is already gone")
            continue
        if not t.inside_roots(resolved):
            raise CrewError(
                f"{p} is outside the crew's folders. factory tidy removes only inside {t.crew_home}/worktrees, "
                f"{t.crew_home}/tmp, the projects' .factory/worktrees and your job's scratch folder.")
        common = None
        if not is_link(resolved) and os.path.isfile(os.path.join(resolved, ".git")):
            g = _git(["-C", resolved, "rev-parse", "--path-format=absolute", "--git-common-dir"])
            if g is not None and g.returncode == 0:
                common = g.stdout.strip()
        try:
            _remove(p, resolved, common, force)
        except OSError as e:  # a file in use, a permission: say which path and why, not a traceback
            raise CrewError(f"could not remove {p}: {e.strerror or e}") from e
        _say(f"removed {p}")
    return 0


def _remove(p: str, resolved: str, common, force: bool) -> None:
    """Remove one resolved path that is inside the roots: a link or file, a worktree, or a folder."""
    if is_link(resolved) or os.path.isfile(resolved):
        remove_link(resolved)
    elif common is not None:
        if not force and worktree_has_work(resolved):
            raise CrewError(
                f"{p} has uncommitted changes or commits no remote has. If it's your own worktree and the "
                f"work is done (merged, or a review you've finished), run factory tidy --force {p}")
        r = _git([f"--git-dir={common}", "worktree", "remove", "--force", resolved])
        if r is None or r.returncode != 0:
            why = (r.stderr.strip().splitlines() or [""])[-1] if r is not None else "git did not run"
            raise CrewError(f"git could not remove the worktree {p}{': ' + why if why else ''}; "
                            "if it's locked, git worktree list shows why")
        if os.path.lexists(resolved):
            # git said yes and left the folder (on Windows a junction or a handle can do that): remove
            # what is left ourselves, never through a link, then drop the registration git still holds
            remove_tree(resolved)
            _git([f"--git-dir={common}", "worktree", "prune"])
            if os.path.lexists(resolved):
                raise CrewError(f"git removed the worktree {p} from its list but the folder is still there")
    else:
        remove_tree(resolved)
