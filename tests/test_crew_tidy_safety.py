"""tares factory tidy can never delete outside the crew's folders (TR-968).

The cases the bash suite (tests/factory/tidy_test.sh) cannot reach, and the Windows rules of the port:

  - a path is inside only when strictly below a root: a root itself, a sibling that shares the root's
    name as a prefix, `..` and a link out of the root are refused;
  - a link is removed, never what it points at (a symlink here; a junction on Windows, run there only);
  - a root that is a link, the filesystem root, the home folder and a folder that holds the crew home
    are not roots;
  - paths compare case-insensitively where the filesystem does (ntpath.normcase stands in on any OS);
  - a folder of read-only files (git writes its objects read-only, and Windows refuses to delete such a
    file) is removed, and the error handler retries a file that is busy for a moment;
  - the long-path prefix is added on Windows and only there;
  - each safety check is broken in turn and the case that guards it fails, then the code is restored.

Needs only git for none of this; the standard library and tares.crew. Run: python tests/test_crew_tidy_safety.py
"""
import contextlib
import io
import ntpath
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares.crew import tidy  # noqa: E402

P = F = 0
WIN = sys.platform == "win32"


def ck(label, cond, detail: object = ""):
    global P, F
    if cond:
        P += 1
        print("  ok   " + label)
    else:
        F += 1
        print("  FAIL " + label + (" :: " + str(detail) if detail else ""))


T = Path(os.path.realpath(tempfile.mkdtemp(prefix="tidy-safety-")))


def link_dir(target: Path, link: Path):
    """A link to a folder: a symlink, on Windows a junction (needs no privilege)."""
    if WIN:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(str(target), str(link))


def fresh(name):
    """A new crew home with its worktrees and tmp folders, and a folder outside that must survive."""
    base = T / name
    (base / "home" / "factory" / "worktrees").mkdir(parents=True)
    (base / "home" / "factory" / "tmp").mkdir(parents=True)
    (base / "outside" / "node_modules").mkdir(parents=True)
    (base / "outside" / "node_modules" / "keep").write_text("keep\n")
    (base / "outside" / "file").write_text("keep\n")
    env = {"HOME": str(base / "home"), "FACTORY_CREW_HOME": str(base / "home" / "factory"),
           "TARESD_URL": "http://127.0.0.1:9", "TARES_FACTORY_URL": "http://127.0.0.1:9"}
    return base, env


@contextlib.contextmanager
def no_tares():
    """No repo roots from a Tares: the roots are the crew's own folders and what the environment names."""
    real = tidy.Tidy.repo_folders
    tidy.Tidy.repo_folders = lambda self: []
    try:
        yield
    finally:
        tidy.Tidy.repo_folders = real


def run(env, *args):
    """cmd_tidy with these arguments: (exit code, stderr)."""
    err = io.StringIO()
    with no_tares(), contextlib.redirect_stderr(err):
        code = tidy.cmd_tidy(["tidy", *args], env)
    return code, err.getvalue()


def inside(env, path) -> bool:
    with no_tares():
        t = tidy.Tidy(env)
        return t.inside_roots(tidy.real(path))


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


# ===================================================================================================
print("\n== strictly inside a root ==")
base, env = fresh("roots")
wt = base / "home" / "factory" / "worktrees"
(wt / "a").mkdir()
(wt / "a" / "f").write_text("x\n")
ck("a folder below a root is inside", inside(env, wt / "a"))
ck("a file below a root is inside", inside(env, wt / "a" / "f"))
ck("a root itself is not inside", not inside(env, wt))
sib = base / "home" / "factory" / "worktrees-evil"
sib.mkdir()
ck("a sibling that shares the root's name as a prefix is not inside", not inside(env, sib))
ck("the crew home itself is not inside", not inside(env, base / "home" / "factory"))
ck("the outside folder is not inside", not inside(env, base / "outside"))
code, err = run(env, str(wt / "a" / ".." / ".." / "worktrees-evil"))
ck("an escape through .. is refused and nothing is removed", code == 1 and "outside the crew" in err and sib.exists(), err)
code, err = run(env, str(wt))
ck("tidy of a root itself is refused", code == 1 and wt.is_dir(), err)
code, err = run(env, str(base / "outside"))
ck("tidy of an outside folder is refused with the plain message", code == 1 and "outside the crew's folders" in err
   and (base / "outside" / "file").exists(), err)
code, err = run(env)
ck("tidy with no path prints the usage", code == 1 and err.strip().endswith(tidy.USAGE), err)

# ===================================================================================================
print("\n== a link out of a root ==")
base, env = fresh("links")
tmp = base / "home" / "factory" / "tmp"
link_dir(base / "outside", tmp / "door")
code, err = run(env, str(tmp / "door" / "node_modules"))
ck("a path that goes through a link out of the roots is refused", code == 1 and "outside the crew" in err, err)
ck("...and what is behind the link is untouched", (base / "outside" / "node_modules" / "keep").exists())
code, err = run(env, str(tmp / "door" / "node_modules" / "keep"))
ck("a file behind the link is refused too", code == 1 and (base / "outside" / "node_modules" / "keep").exists(), err)
code, err = run(env, str(tmp / "door"))
ck("the link itself is removed", code == 0 and not os.path.lexists(tmp / "door"), err)
ck("...and its target is intact", (base / "outside" / "file").exists() and (base / "outside" / "node_modules" / "keep").exists())
link_dir(base / "outside", tmp / "door2")
code, err = run(env, str(tmp / "door2") + os.sep)
ck("a link given with a trailing separator is removed, not followed",
   code == 0 and not os.path.lexists(tmp / "door2") and (base / "outside" / "file").exists(), err)

(tmp / "tree" / "inner").mkdir(parents=True)
link_dir(base / "outside", tmp / "tree" / "inner" / "node_modules")
code, err = run(env, str(tmp / "tree"))
ck("a folder holding a link is removed without following the link",
   code == 0 and not (tmp / "tree").exists() and (base / "outside" / "node_modules" / "keep").exists(), err)

# ===================================================================================================
print("\n== a root that is not a root ==")
base, env = fresh("badroots")
link_dir(base / "outside", base / "linkroot")
e2 = dict(env, FACTORY_WORKTREES=str(base / "linkroot"))
code, err = run(e2, str(base / "linkroot" / "file"))
ck("a root that is a link is skipped", code == 1 and (base / "outside" / "file").exists(), err)
e3 = dict(env, FACTORY_WORKTREES=env["HOME"])
(Path(env["HOME"]) / "x").mkdir()
code, err = run(e3, str(Path(env["HOME"]) / "x"))
ck("the home folder is never a root", code == 1 and (Path(env["HOME"]) / "x").is_dir(), err)
e4 = dict(env, FACTORY_WORKTREES=str(base))
code, err = run(e4, str(base / "outside" / "file"))
ck("a folder that holds the crew home is never a root", code == 1 and (base / "outside" / "file").exists(), err)
fs_root = os.path.abspath(os.sep)
e5 = dict(env, FACTORY_WORKTREES=fs_root)
code, err = run(e5, str(base / "outside" / "file"))
ck("the filesystem root is never a root", code == 1 and (base / "outside" / "file").exists(), err)
e6 = dict(env, FACTORY_WORKTREES=str(base / "not-there"))
code, err = run(e6, str(base / "outside" / "file"))
ck("a root that does not exist is skipped", code == 1, err)
code, err = run(env, str(base / "gone"))
ck("a path that is already gone is fine", code == 0 and "already gone" in err, err)

# ===================================================================================================
print("\n== the job's folder and the project's folder are roots ==")
base, env = fresh("jobroots")
(base / "job" / "tmp" / "v1").mkdir(parents=True)
(base / "job" / "tmp" / "v2").mkdir(parents=True)
(base / "repo" / ".factory" / "worktrees" / "T1" / "x").mkdir(parents=True)
code, err = run(dict(env, CLAUDE_JOB_DIR=str(base / "job")), str(base / "job" / "tmp" / "v1"))
ck("the job's scratch folder is inside", code == 0 and not (base / "job" / "tmp" / "v1").exists(), err)
code, err = run(env, str(base / "job" / "tmp" / "v2"))
ck("without CLAUDE_JOB_DIR another job's scratch is not", code == 1 and (base / "job" / "tmp" / "v2").exists(), err)
code, err = run(dict(env, FACTORY_REPO=str(base / "repo")), str(base / "repo" / ".factory" / "worktrees" / "T1"))
ck("FACTORY_REPO's .factory/worktrees is inside", code == 0 and not (base / "repo" / ".factory" / "worktrees" / "T1").exists(), err)

# ===================================================================================================
print("\n== case-insensitive on Windows ==")
ck("on Windows a path compares without regard to case", ntpath.normcase("C:\\Crew\\Worktrees\\A") == ntpath.normcase("c:\\crew\\worktrees\\a"))
with patched(os.path, "normcase", ntpath.normcase), patched(os.path, "normpath", ntpath.normpath), patched(os.path, "sep", "\\"), \
        patched(tidy.os, "sep", "\\"):
    ck("_below sees C:\\Crew\\worktrees\\A as below c:\\crew\\WORKTREES", tidy._below("C:\\Crew\\worktrees\\A", "c:\\crew\\WORKTREES"))
    ck("...but not the root itself", not tidy._below("C:\\CREW\\worktrees", "c:\\crew\\WORKTREES"))
    ck("...and not a sibling with the same prefix", not tidy._below("C:\\crew\\worktrees-evil\\a", "c:\\crew\\worktrees"))
    ck("_holds sees a folder that holds the crew home, in any case", tidy._holds("C:\\CREW", "c:\\crew\\factory"))
    ck("...and not a folder beside it", not tidy._holds("C:\\crew\\other", "c:\\crew\\factory"))
    ck("a trailing separator on a root changes nothing", tidy._below("C:\\crew\\worktrees\\a", "C:\\crew\\worktrees\\"))
ck("on this system the comparison is the system's own", tidy._key("/A/b") == os.path.normcase("/A/b"))

# ===================================================================================================
print("\n== junctions and links ==")
base, env = fresh("kinds")
(base / "d").mkdir()
(base / "f").write_text("x\n")
link_dir(base / "d", base / "l")
ck("a link to a folder is a link", tidy.is_link(base / "l"))
ck("a folder is not a link", not tidy.is_link(base / "d"))
ck("a file is not a link", not tidy.is_link(base / "f"))
ck("a missing path is not a link", not tidy.is_link(base / "nope"))
class Attrs:
    """What lstat shows for a Windows junction: a directory with the reparse-point attribute."""
    st_mode = stat.S_IFDIR | 0o777
    st_file_attributes = 0x10 | 0x400


with patched(tidy.os, "lstat", lambda p: Attrs()):
    ck("a directory with the reparse-point attribute counts as a link (a junction is no os.path.islink)", tidy.is_link("J:\\junction"))
if WIN:
    ck("a real junction counts as a link", tidy.is_link(base / "l") and not os.path.islink(base / "l"))
    ck("removing a real junction leaves its target", (tidy.remove_link(str(base / "l")), (base / "d").is_dir())[1] and not os.path.lexists(base / "l"))
else:
    tidy.remove_link(str(base / "l"))
    ck("remove_link removes a symlink and keeps its target", not os.path.lexists(base / "l") and (base / "d").is_dir())
tidy.remove_link(str(base / "f"))
ck("remove_link removes a file", not (base / "f").exists())
tidy.remove_link(str(base / "nope"))
ck("remove_link of something gone is not an error", True)

# ===================================================================================================
print("\n== read-only files ==")
base, env = fresh("readonly")
tmp = base / "home" / "factory" / "tmp"
objs = tmp / "scratch" / ".git" / "objects" / "ab"
objs.mkdir(parents=True)
for n in ("cdef01", "cdef02"):
    f = objs / n
    f.write_text("object\n")
    f.chmod(0o444)
if not WIN:
    objs.chmod(0o555)  # a read-only folder: the entries cannot be unlinked until it is writable
code, err = run(env, str(tmp / "scratch"))
ck("a folder of read-only git objects is removed", code == 0 and not (tmp / "scratch").exists(), err)
f2 = tmp / "ro.txt"
f2.write_text("x\n")
f2.chmod(0o444)
code, err = run(env, str(f2))
ck("a read-only file is removed", code == 0 and not f2.exists(), err)

busy = []


def flaky(path):
    busy.append(path)
    if len(busy) < 3:
        raise PermissionError(13, "in use")


with patched(tidy, "RETRY_PAUSE", 0.0):
    tidy._retrying(flaky, str(tmp))
ck("a file that is busy for a moment is retried until it goes", len(busy) == 3)
busy.clear()


def stuck(path):
    busy.append(path)
    raise PermissionError(13, "in use")


with patched(tidy, "RETRY_PAUSE", 0.0):
    try:
        tidy._retrying(stuck, str(tmp))
        raised = False
    except PermissionError:
        raised = True
ck("a file that stays busy raises after the retries", raised and len(busy) == tidy.RETRIES, len(busy))
ck("the error handler ignores a file that is already gone",
   tidy._on_error(os.unlink, str(tmp / "gone"), FileNotFoundError()) is None
   and tidy._on_error(os.unlink, str(tmp / "gone"), (FileNotFoundError, FileNotFoundError(), None)) is None)
g = tmp / "g.txt"
g.write_text("x\n")
g.chmod(0o444)
tidy._on_error(os.unlink, str(g), (PermissionError, PermissionError(13, "ro"), None))
ck("the error handler takes the onerror tuple form and clears the bit", not g.exists())
g.write_text("x\n")
g.chmod(0o444)
tidy._on_error(os.unlink, str(g), PermissionError(13, "ro"))
ck("...and the onexc exception form", not g.exists())

# ===================================================================================================
print("\n== the long-path prefix ==")
ck("elsewhere than Windows a path is unchanged", tidy.long_path("/a/b") == "/a/b" or WIN)
with patched(tidy, "_windows", lambda: True), patched(os.path, "abspath", ntpath.abspath):
    ck("on Windows a drive path gets \\\\?\\", tidy.long_path("C:\\a\\b") == "\\\\?\\C:\\a\\b", tidy.long_path("C:\\a\\b"))
    ck("on Windows a UNC path gets \\\\?\\UNC\\", tidy.long_path("\\\\srv\\share\\a") == "\\\\?\\UNC\\srv\\share\\a", tidy.long_path("\\\\srv\\share\\a"))
    ck("a path that has the prefix keeps it", tidy.long_path("\\\\?\\C:\\a") == "\\\\?\\C:\\a")

# ===================================================================================================
print("\n== a worktree with work in it ==")
if shutil.which("git"):
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": str(T / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*a, cwd=None):
        subprocess.run(["git", *a], cwd=str(cwd) if cwd else None, env=genv, check=True, capture_output=True)

    base, env = fresh("work")
    git("init", "-q", "--bare", str(base / "origin.git"))
    git("clone", "-q", str(base / "origin.git"), str(base / "repo"))
    (base / "repo" / "t.txt").write_text("a\n")
    git("add", ".", cwd=base / "repo")
    git("commit", "-q", "-m", "init", cwd=base / "repo")
    git("push", "-q", "origin", "HEAD", cwd=base / "repo")
    wt = base / "home" / "factory" / "worktrees" / "w"
    git("worktree", "add", "-q", "--detach", str(wt), "HEAD", cwd=base / "repo")
    old = {k: os.environ.get(k) for k in genv}
    os.environ.update(genv)
    try:
        ck("a clean worktree has no work", not tidy.worktree_has_work(str(wt)))
        (wt / "new.txt").write_text("n\n")
        ck("a new file is work", tidy.worktree_has_work(str(wt)))
        code, err = run(env, str(wt))
        ck("tidy refuses it without --force", code == 1 and "--force" in err and wt.is_dir(), err)
        (wt / "new.txt").unlink()
        git("commit", "-q", "--allow-empty", "-m", "wip", cwd=wt)
        ck("a commit no remote has is work", tidy.worktree_has_work(str(wt)))
        code, err = run(env, "--force", str(wt))
        ck("--force removes it through git", code == 0 and not wt.exists()
           and "w" not in [Path(x).name for x in subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=str(base / "repo"),
                                                                capture_output=True, text=True).stdout.split() if x.startswith(str(base))], err)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
else:
    print("  skip: git is not installed")

# ===================================================================================================
print("\n== break tests: each guard, switched off, makes its case fail ==")


def guarded_cases():
    """The cases the guards protect, as a list of booleans (True = the guard held)."""
    b, e = fresh("break%d" % len(os.listdir(T)))
    tmp = b / "home" / "factory" / "tmp"
    wtd = b / "home" / "factory" / "worktrees"
    sibling = b / "home" / "factory" / "worktrees-evil"
    sibling.mkdir()
    link_dir(b / "outside", tmp / "door")
    run(e, str(sibling))  # before the root itself: a root that was removed is no root any more
    run(e, str(wtd))
    run(e, str(tmp / "door" / "node_modules"))
    link_dir(b / "outside", b / "linkroot")
    run(dict(e, FACTORY_WORKTREES=str(b / "linkroot")), str(b / "linkroot" / "file"))
    return {
        "a root itself": wtd.is_dir(),
        "a sibling with the same prefix": sibling.is_dir(),
        "a path through a link": (b / "outside" / "node_modules" / "keep").exists(),
        "a root that is a link": (b / "outside" / "file").exists(),
    }


ck("with every guard in place all four cases hold", all(guarded_cases().values()), guarded_cases())

loose = lambda path, root: _key_prefix(path, root)  # noqa: E731


def _key_prefix(path, root):
    return tidy._key(path).startswith(tidy._key(root))  # no separator, and the root itself counts


with patched(tidy, "_below", loose):
    got = guarded_cases()
    ck("break: a _below that is not strict lets a root itself and a prefix sibling through",
       not got["a root itself"] and not got["a sibling with the same prefix"], got)

with patched(tidy, "real", lambda p: os.path.abspath(p)):
    got = guarded_cases()
    ck("break: a root check that does not resolve real paths lets a link out", not got["a path through a link"], got)

with patched(tidy, "is_link", lambda p: False):
    got = guarded_cases()
    ck("break: a root that is a link, taken as a root, deletes through it", not got["a root that is a link"], got)

ck("restored: all four cases hold again", all(guarded_cases().values()))

# A handler that does not clear read-only bits leaves the folder: the case that guards it fails.
base, env = fresh("breakro")
tmp = base / "home" / "factory" / "tmp"
d = tmp / "ro" / "sub"
d.mkdir(parents=True)
(d / "f").write_text("x\n")
(d / "f").chmod(0o444)
if not WIN:
    d.chmod(0o555)
with patched(tidy, "_writable", lambda p: None), patched(tidy, "RETRY_PAUSE", 0.0):
    code, err = run(env, str(tmp / "ro"))
if WIN:
    ck("break: a handler that does not clear read-only bits leaves the file (Windows)", code != 0 or (tmp / "ro").exists(), err)
elif os.geteuid() == 0:
    print("  skip: root deletes read-only folders, the break cannot show")
else:
    ck("break: a handler that does not clear read-only bits leaves the folder", code != 0 and (tmp / "ro").exists(), err)
code, err = run(env, str(tmp / "ro"))
ck("restored: the same folder is removed", code == 0 and not (tmp / "ro").exists(), err)

# ===================================================================================================
def clean():
    for dirpath, dirs, files in os.walk(T):
        for n in dirs + files:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o777)
                except OSError:
                    pass
    shutil.rmtree(T, ignore_errors=True)


clean()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
