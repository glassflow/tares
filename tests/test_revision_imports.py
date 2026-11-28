"""A revision of the session store imports no live Tares code (TR-1700).

    python tests/test_revision_imports.py

A database must get from a revision what it got when the revision was written. A revision that
calls live code gets whatever that code does today. So env.py and the revisions (everything under
tares/session_store/migrations) may import the standard library, SQLAlchemy and Alembic, and
nothing of Tares, except what ALLOWED names.

The imports are read with ast, so one inside a function or under an `if` counts too.
"""
import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MIGRATIONS = os.path.join(ROOT, "tares", "session_store", "migrations")
LIVE = ("tares", "tares_ee")

# (file under migrations/, the module it imports): why it may
ALLOWED: dict = {}

FAILS = []


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def _live(module):
    return module.split(".")[0] in LIVE


def live_imports(source, path):
    """[(line, what is imported)] of `source`, the file at `path` under migrations/: every import
    of live Tares code, and every one that cannot be read from the source."""
    depth = path.count("/")                       # how many packages the file is below migrations/
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            # `import tares.x.y` binds `tares`, and with it every module of it
            found += [(node.lineno, a.name) for a in node.names if _live(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:                        # relative: fine while it stays under migrations/
                if node.level - 1 > depth:
                    found.append((node.lineno, "." * node.level + module))
            elif _live(module):
                found.append((node.lineno, module))
        elif isinstance(node, ast.Call):          # __import__(...), importlib.import_module(...)
            f = node.func
            called = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if called in ("__import__", "import_module"):
                found.append((node.lineno, called + "()"))
    return sorted(found)


def the_check_itself():
    print("== the check sees each way to import live code ==")
    rev = "versions/0099_x.py"
    for label, source, path, want in (
            ("from tares...", "from tares.session_store.takeover import takeover", rev,
             ["tares.session_store.takeover"]),
            ("from tares import", "from tares import store", rev, ["tares"]),
            ("import tares...", "import tares.store", rev, ["tares.store"]),
            ("import tares as", "import tares.store as s", rev, ["tares.store"]),
            ("a module under migrations by its full name",
             "from tares.session_store.migrations import helpers", rev,
             ["tares.session_store.migrations"]),
            ("the enterprise package", "from tares_ee import team", rev, ["tares_ee"]),
            ("inside a function", "def upgrade():\n    from tares.session_store import tables\n", rev,
             ["tares.session_store"]),
            ("inside a branch", "if x:\n    try:\n        import tares\n    except ImportError:\n        pass\n",
             rev, ["tares"]),
            ("relative, out of migrations from a revision", "from ...tables import settings", rev,
             ["...tables"]),
            ("relative, out of migrations from env.py", "def f():\n    from .. import tables\n", "env.py",
             [".."]),
            ("by name at run time", "import importlib\nimportlib.import_module('tares.store')", rev,
             ["import_module()"]),
            ("__import__", "__import__('tares.store')", rev, ["__import__()"])):
        got = [what for _, what in live_imports(source, path)]
        ck(f"{label} is found", got == want, got)
    for label, source, path in (
            ("a revision that imports logging, sqlalchemy and alembic",
             "import logging\n\nimport sqlalchemy as sa\nfrom alembic import op\n"
             "from sqlalchemy.dialects import postgresql\n", rev),
            ("relative inside migrations", "from . import helpers\n", rev),
            ("a package named like tares", "import taresque", rev)):
        got = live_imports(source, path)
        ck(f"{label} passes", not got, got)


def the_tree():
    print("== no file under migrations/ imports live code ==")
    files, used, bad = [], set(), []
    for folder, _, names in os.walk(MIGRATIONS):
        for name in names:
            if name.endswith(".py"):
                files.append(os.path.relpath(os.path.join(folder, name), MIGRATIONS).replace(os.sep, "/"))
    for path in sorted(files):
        with open(os.path.join(MIGRATIONS, path), encoding="utf-8") as f:
            source = f.read()
        for line, what in live_imports(source, path):
            if (path, what) in ALLOWED:
                used.add((path, what))
            else:
                bad.append(f"migrations/{path}:{line} imports {what}")
    ck("it read env.py and the revisions", "env.py" in files
       and "versions/0001_baseline.py" in files, files)
    ck("none imports live code (copy what it needs into the revision)", not bad, bad)
    ck("every entry of ALLOWED has a reason and is still needed",
       all(ALLOWED.values()) and used == set(ALLOWED), set(ALLOWED) - used)


if __name__ == "__main__":
    the_check_itself()
    the_tree()
    print("FAIL " + ", ".join(FAILS) if FAILS else "OK")
    sys.exit(1 if FAILS else 0)
