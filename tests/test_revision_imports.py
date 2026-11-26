"""A revision of the session store imports no live Tares code (TR-1700).

    python tests/test_revision_imports.py

An old database must get from a revision what it got when the revision was written. A revision
that calls live code gets whatever that code does today. So env.py, the revisions and the frozen
helpers (everything under tares/session_store/migrations) may import the standard library,
SQLAlchemy, Alembic and migrations/frozen, and nothing else of Tares, except what ALLOWED names.

The imports are read with ast, so one inside a function or under an `if` counts too. A frozen
helper is imported as `from tares.session_store.migrations.frozen... import name`.
"""
import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MIGRATIONS = os.path.join(ROOT, "tares", "session_store", "migrations")
FROZEN = "tares.session_store.migrations.frozen"
LIVE = ("tares", "tares_ee")

# (file under migrations/, the module it imports): why it may
ALLOWED = {
    ("versions/0008_core_settings.py", "tares.session_store.takeover"):
        "0008 is older than the rule: TR-1700 froze revisions 0010 onward and left earlier ones alone",
}

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
            # `import tares.x.y` binds `tares`, and with it every module of it: a frozen one too
            found += [(node.lineno, a.name) for a in node.names if _live(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:                        # relative: fine while it stays under migrations/
                if node.level - 1 > depth:
                    found.append((node.lineno, "." * node.level + module))
            elif _live(module) and module != FROZEN and not module.startswith(FROZEN + "."):
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
            ("import of a frozen module by its full name",
             "import tares.session_store.migrations.frozen.takeover", rev,
             ["tares.session_store.migrations.frozen.takeover"]),
            ("a name that only starts like frozen",
             "from tares.session_store.migrations.frozenish import x", rev,
             ["tares.session_store.migrations.frozenish"]),
            ("the enterprise package", "from tares_ee import team", rev, ["tares_ee"]),
            ("inside a function", "def upgrade():\n    from tares.session_store import tables\n", rev,
             ["tares.session_store"]),
            ("inside a branch", "if x:\n    try:\n        import tares\n    except ImportError:\n        pass\n",
             rev, ["tares"]),
            ("relative, out of migrations from a revision", "from ...tables import settings", rev,
             ["...tables"]),
            ("relative, out of migrations from a frozen module", "from ... import pr_columns",
             "frozen/x.py", ["..."]),
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
            ("a frozen helper", f"from {FROZEN}.takeover import takeover\nfrom {FROZEN} import takeover", rev),
            ("relative inside migrations", "from .takeover import takeover\nfrom ..frozen import takeover\n",
             "frozen/x.py"),
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
    ck("it read env.py, the revisions and the frozen helpers", "env.py" in files
       and "versions/0015_ticket_pr_columns.py" in files and "frozen/pr_columns.py" in files, files)
    ck("none imports live code (copy what it needs into the revision or into migrations/frozen/)",
       not bad, bad)
    ck("every entry of ALLOWED has a reason and is still needed",
       all(ALLOWED.values()) and used == set(ALLOWED), set(ALLOWED) - used)


if __name__ == "__main__":
    the_check_itself()
    the_tree()
    print("FAIL " + ", ".join(FAILS) if FAILS else "OK")
    sys.exit(1 if FAILS else 0)
