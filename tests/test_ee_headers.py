"""Every file of the enterprise code says so (TR-913): each text file under a directory named `ee`
has the line `Tares enterprise licence, see ee/LICENSE` in its first 3 lines, behind whatever
comment mark the file uses (`# ...` in Python and bash, `// ...` in TypeScript).

Not checked: ee/LICENSE and ee/README.md (they are the licence and its readme), files that are not
text, and folders of build output or dependencies (node_modules, __pycache__, dist, hidden folders,
virtual environments).

Run: python tests/test_ee_headers.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = "Tares enterprise licence, see ee/LICENSE"
SKIP_DIRS = {"node_modules", "__pycache__", "dist", "build"}
SKIP_FILES = {"LICENSE", "README.md"}   # only the ones at the top of an ee folder

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def ee_files(root):
    """(path) of every file under a directory named `ee` below `root`."""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         and not os.path.isfile(os.path.join(dirpath, d, "pyvenv.cfg")))
        rel = Path(dirpath).relative_to(root).parts
        if "ee" not in rel:
            continue
        for f in sorted(files):
            if rel[-1] == "ee" and f in SKIP_FILES:
                continue
            yield Path(dirpath) / f


def text_of(path):
    """The file's text, or None when it is not text."""
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def missing_header(root):
    out = []
    for p in ee_files(root):
        t = text_of(p)
        if t is None:
            continue
        if not any(HEADER in ln for ln in t.splitlines()[:3]):
            out.append(str(p.relative_to(root)))
    return out


# self-checks on a scratch tree
tmp = Path(tempfile.mkdtemp(prefix="tares-ee-headers-"))
(tmp / "a" / "ee" / "pkg").mkdir(parents=True)
(tmp / "a" / "ee" / "LICENSE").write_text("licence\n")
(tmp / "a" / "ee" / "README.md").write_text("readme\n")
(tmp / "a" / "ee" / "pkg" / "ok.py").write_text(f"# {HEADER}\nx = 1\n")
(tmp / "a" / "ee" / "pkg" / "ok.ts").write_text(f"\n// {HEADER}\nexport {{}}\n")
(tmp / "a" / "ee" / "pkg" / "logo.png").write_bytes(b"\x89PNG\0\0")
(tmp / "a" / "ee" / "pkg" / "node_modules").mkdir()
(tmp / "a" / "ee" / "pkg" / "node_modules" / "dep.js").write_text("x\n")
(tmp / "a" / "other.py").write_text("x = 1\n")
ck("a tree with headers, a licence, a readme, a binary and node_modules passes", missing_header(tmp) == [])
(tmp / "a" / "ee" / "pkg" / "bad.py").write_text("x = 1\n")
ck("a file with no header is caught", missing_header(tmp) == ["a/ee/pkg/bad.py"], missing_header(tmp))
(tmp / "a" / "ee" / "pkg" / "bad.py").write_text(f"a\nb\nc\n# {HEADER}\n")
ck("a header on line 4 is caught", missing_header(tmp) == ["a/ee/pkg/bad.py"], missing_header(tmp))
(tmp / "a" / "ee" / "pkg" / "bad.py").write_text(f"a\nb\n# {HEADER}\n")
ck("a header on line 3 passes", missing_header(tmp) == [], missing_header(tmp))

# the real tree
found = list(ee_files(ROOT))
bad = missing_header(ROOT)
ck(f"every text file under an ee folder has the header ({len(found)} files)", not bad, ", ".join(bad))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
