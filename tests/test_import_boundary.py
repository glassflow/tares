"""Core never imports the enterprise code (TR-913).

Python: nothing under tares/ imports tares_ee (import, from-import, or importlib.import_module with
a literal name). The ee code is loaded through the `tares.ee` entry point only.
Console: nothing under ui/src outside ui/src/ee imports into ui/src/ee statically (import, export
from, import "x"). A dynamic import('./ee/...') or import.meta.glob("./ee/...") is allowed only in
ui/src/eeSlots.tsx, the one file that wires ee screens in.

The scanners are functions of source text, so the self-checks below prove each catches a bad
import before the real tree is scanned.

Run: python tests/test_import_boundary.py
"""
import ast
import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EE_UI = "ui/src/ee"
SLOTS = "ui/src/eeSlots.tsx"

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


# ── Python ──
def _is_ee(name):
    return name == "tares_ee" or name.startswith("tares_ee.")


def py_violations(source, label="<source>"):
    """One message per import of tares_ee in the Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"{label}: does not parse ({e.msg})"]
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [f"{label}:{n.lineno}: import {a.name}" for a in n.names if _is_ee(a.name)]
        elif isinstance(n, ast.ImportFrom):
            if n.level == 0 and n.module and _is_ee(n.module):
                out.append(f"{label}:{n.lineno}: from {n.module} import ...")
        elif isinstance(n, ast.Call) and n.args:
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            a0 = n.args[0]
            if name in ("import_module", "__import__") and isinstance(a0, ast.Constant) \
                    and isinstance(a0.value, str) and _is_ee(a0.value):
                out.append(f"{label}:{n.lineno}: {name}({a0.value!r})")
    return out


# ── the console ──
# a string (kept) or a comment (dropped); strings come first so "/**/" in a glob is not a comment
_COMMENT = re.compile(r"""("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)|/\*.*?\*/|//[^\n]*""", re.S)
_STATIC = [
    re.compile(r"""(?ms)^[ \t]*(?:import|export)\b[^;'"`]*?\bfrom\s*['"]([^'"]+)['"]"""),
    re.compile(r"""(?m)^[ \t]*import\s*['"]([^'"]+)['"]"""),
]
_DYNAMIC = re.compile(r"""\b(?:import|require)\s*\(\s*['"`]([^'"`]+)['"`]"""
                      r"""|import\.meta\.glob(?:Eager)?\s*\(\s*\[?\s*['"`]([^'"`]+)['"`]""")


def _strip(source):
    return _COMMENT.sub(lambda m: m.group(1) or " ", source)


def _into_ee(spec, relpath):
    """True when the import specifier, written in the file at repo-relative `relpath`, points into
    ui/src/ee (a relative path, or the @/ alias for ui/src)."""
    if spec.startswith("@/"):
        target = posixpath.normpath("ui/src/" + spec[2:])
    elif spec.startswith("."):
        target = posixpath.normpath(posixpath.join(posixpath.dirname(relpath), spec))
    else:
        return False
    return target == EE_UI or target.startswith(EE_UI + "/")


def ts_violations(source, relpath):
    """One message per import into ui/src/ee that the file at `relpath` may not make."""
    if relpath == EE_UI or relpath.startswith(EE_UI + "/"):
        return []
    text = _strip(source)
    out = []
    for rx in _STATIC:
        out += [f"{relpath}: static import of {m.group(1)}" for m in rx.finditer(text)
                if _into_ee(m.group(1), relpath)]
    if relpath != SLOTS:
        for m in _DYNAMIC.finditer(text):
            spec = m.group(1) or m.group(2)
            if _into_ee(spec, relpath):
                out.append(f"{relpath}: dynamic import of {spec} (only {SLOTS} may)")
    return out


# ── self-checks: each scanner must catch a bad import, and pass a good one ──
print("-- the scanners catch what they should")
ck("python: `from tares_ee import x` is caught", len(py_violations("from tares_ee import x")) == 1)
ck("python: `import tares_ee.team` is caught", len(py_violations("import tares_ee.team as t")) == 1)
ck("python: import_module with a literal ee name is caught",
   len(py_violations("import importlib\nimportlib.import_module('tares_ee.team')")) == 1)
ck("python: a docstring or comment that names tares_ee passes",
   py_violations('"""loads tares_ee"""\n# import tares_ee\nx = "tares_ee"') == [])
ck("python: the entry point loader passes",
   py_violations("import importlib.metadata\nimportlib.metadata.entry_points(group='tares.ee')") == [])
ck("python: tares_eex is another name", py_violations("import tares_eex") == [])
ck("console: a static import of ./ee/x is caught",
   len(ts_violations("import X from '../ee/x'", "ui/src/pages/A.tsx")) == 1)
ck("console: a multi-line import from ../ee/x is caught",
   len(ts_violations("import {\n  a,\n  b,\n} from '../ee/x';", "ui/src/pages/A.tsx")) == 1)
ck("console: export * from the @/ee alias is caught",
   len(ts_violations("export * from '@/ee/x'", "ui/src/lib/a.ts")) == 1)
ck("console: a bare import of ee is caught",
   len(ts_violations("import './ee'", "ui/src/main.tsx")) == 1)
ck("console: a dynamic import outside eeSlots is caught",
   len(ts_violations("const m = await import('./ee/x')", "ui/src/App.tsx")) == 1)
ck("console: import.meta.glob outside eeSlots is caught",
   len(ts_violations("const m = import.meta.glob('./ee/**/*.tsx')", "ui/src/App.tsx")) == 1)
ck("console: a dynamic import and a glob in eeSlots.tsx pass",
   ts_violations("import('./ee/x'); import.meta.glob('./ee/**/*.tsx')", SLOTS) == [])
ck("console: a static import in eeSlots.tsx is still caught",
   len(ts_violations("import X from './ee/x'", SLOTS)) == 1)
ck("console: files inside ui/src/ee may import each other and core",
   ts_violations("import A from './a'; import B from '../lib/b'", "ui/src/ee/x.tsx") == [])
ck("console: a comment or string that names ./ee/ passes",
   ts_violations("// import X from './ee/x'\n/* import Y from './ee/y' */\nconst s = \"./ee/z\"", "ui/src/A.tsx") == [])
ck("console: ./editions/x and ./eefoo are not ./ee",
   ts_violations("import A from './editions/x'; import B from './eefoo/y'", "ui/src/A.tsx") == [])
ck("console: a package import passes", ts_violations("import React from 'react'", "ui/src/A.tsx") == [])

# ── the tree ──
print("-- the tree")
py_files = sorted((ROOT / "tares").rglob("*.py"))
ck("python: the scan sees the tares package", len(py_files) > 50, len(py_files))
bad = [m for f in py_files for m in py_violations(f.read_text(encoding="utf-8"), str(f.relative_to(ROOT)))]
ck("python: nothing under tares/ imports tares_ee", not bad, "; ".join(bad))

ui = ROOT / "ui" / "src"
ts_files = sorted(p for p in ui.rglob("*") if p.suffix in (".ts", ".tsx") and "node_modules" not in p.parts)
ck("console: the scan sees the console source", len(ts_files) > 20, len(ts_files))
bad = [m for f in ts_files for m in ts_violations(f.read_text(encoding="utf-8"), f.relative_to(ROOT).as_posix())]
ck("console: nothing outside ui/src/ee imports ui/src/ee as it may not", not bad, "; ".join(bad))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
