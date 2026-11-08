"""scripts/check_port_drift.py: a PR that changes a ported bash function must change its Python module too.

Each case builds a tiny throwaway git repo (a bash file, a helper script, a ledger), makes a branch and
runs the script against it. No network, no real keys, nothing outside a temp folder.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check_port_drift.py"
FACTORY = "claude-plugin/bin/factory"
GUARD = "claude-plugin/hooks/guard.sh"

P = F = 0


def ck(l, c, d=""):
    global P, F
    P += 1 if c else 0
    F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


BASH = """#!/usr/bin/env bash
slugify() {
  echo "${1}" | tr A-Z a-z
}
tricky() {
  local x=1 y="${x:-2}"
  cat <<EOF
  value ${x}
  }
EOF
  case "$1" in
    a) echo "${y}" ;;
    *) echo "other" ;;
  esac
}
neighbour() {
  echo "next ${1}"
}
plain() {
  echo plain
}
"""

GUARD_SH = """#!/usr/bin/env bash
guard_check() {
  echo "guard ${1}"
}
"""


def ledger(extra=None):
    def row(name, ported, python, **kw):
        r = {"name": name, "kind": "function", "ticket": "TR-1", "dropped": False, "python": python, "ported": ported, "note": ""}
        r.update(kw)
        return r
    data = {
        "version": 1,
        "source": FACTORY,
        "functions": [
            row("slugify", True, "tares.crew.state:slugify"),
            row("tricky", True, "tares.crew.tricky:run"),
            row("neighbour", True, "tares.crew.neighbour"),
            row("plain", False, "tares.crew.plain:run"),  # a Python draft exists, but the row is not ported yet
        ],
        "helpers": [row("guard_check", True, "tares.crew.guard:check", file=GUARD)],
    }
    if extra:
        data.update(extra)
    return data


class Repo:
    def __init__(self, files=None, ledger_data=None):
        self.dir = Path(tempfile.mkdtemp(prefix="port_drift_"))
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.ledger = self.dir.parent / (self.dir.name + "_ledger.json")
        self.ledger.write_text(json.dumps(ledger_data or ledger()), encoding="utf-8")
        self.commit(files or {FACTORY: BASH, GUARD: GUARD_SH, "tares/crew/state.py": "def slugify(): pass\n"}, "base")

    def git(self, *args):
        r = subprocess.run(["git", "-C", str(self.dir)] + list(args), capture_output=True, text=True, env=self.env)
        assert r.returncode == 0, (args, r.stderr)
        return r.stdout.strip()

    def commit(self, files, msg="change"):
        for rel, text in files.items():
            p = self.dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def branch(self, name="pr"):
        self.git("checkout", "-q", "-b", name)

    def checkout(self, name):
        self.git("checkout", "-q", name)

    def run(self, base="main", head="pr", *extra):
        r = subprocess.run([sys.executable, str(CHECK), base, head, "--repo", str(self.dir), "--ledger", str(self.ledger), *extra],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def done(self):
        shutil.rmtree(str(self.dir), ignore_errors=True)
        self.ledger.unlink()


def scenario(files_pr):
    """A repo with the PR's files committed on branch pr; returns the repo."""
    r = Repo()
    r.branch()
    r.commit(files_pr)
    return r


# changed + ported + no python change -> 1, and the module is named
r = scenario({FACTORY: BASH.replace('tr A-Z a-z', 'tr A-Z a-z | sed s/x/y/')})
code, out = r.run()
ck("a changed ported function with no Python change exits 1", code == 1, f"exit {code}: {out}")
ck("the message names the function and the module file", "slugify" in out and "tares/crew/state.py" in out, out)
ck("the message names the bash file", FACTORY in out, out)
ck("the message has no em dash", chr(0x2014) not in out)
ck("only that function is named", "tricky" not in out and "neighbour" not in out and "plain" not in out, out)
r.done()

# changed + ported + the python module changed too -> 0
r = scenario({FACTORY: BASH.replace('tr A-Z a-z', 'tr A-Z a-z | sed s/x/y/'), "tares/crew/state.py": "def slugify(): return 1\n"})
code, out = r.run()
ck("a changed ported function with its Python module changed exits 0", code == 0, f"exit {code}: {out}")
r.done()

# changed ported function, but only some other Python file changed -> 1
r = scenario({FACTORY: BASH.replace('tr A-Z a-z', 'tr A-Z a-z | sed s/x/y/'), "tares/crew/other.py": "x = 1\n"})
code, out = r.run()
ck("changing a different Python file does not count", code == 1 and "tares/crew/state.py" in out, f"exit {code}: {out}")
r.done()

# changed + not ported -> 0
r = scenario({FACTORY: BASH.replace('echo plain', 'echo changed')})
code, out = r.run()
ck("a changed function that is not ported exits 0", code == 0, f"exit {code}: {out}")
r.done()

# unchanged -> 0 (only a comment line outside every function changes)
r = scenario({FACTORY: BASH.replace('#!/usr/bin/env bash', '#!/usr/bin/env bash\n# a note')})
code, out = r.run()
ck("a change outside every function exits 0", code == 0, f"exit {code}: {out}")
r.done()
r = Repo()
r.branch()
code, out = r.run()
ck("no change at all exits 0 and says so", code == 0 and "ok" in out, f"exit {code}: {out}")
r.done()

# a function with ${x}, a heredoc and a case: a change inside it is seen
r = scenario({FACTORY: BASH.replace('a) echo "${y}" ;;', 'a) echo "${y}!" ;;')})
code, out = r.run()
ck("a change inside a function with ${x}, a heredoc and a case exits 1", code == 1 and "tricky" in out and "tares/crew/tricky.py" in out, f"exit {code}: {out}")
ck("only that function is named (the span ends at its own closing brace)", "neighbour" not in out and "slugify" not in out, out)
r.done()

# a change in the heredoc body itself, after the heredoc's own `  }` line
r = scenario({FACTORY: BASH.replace('  value ${x}', '  value ${x} more')})
code, out = r.run()
ck("a change in the heredoc of that function exits 1 and names it", code == 1 and "tricky" in out, f"exit {code}: {out}")
r.done()

# a change in the NEXT function is attributed to that one, not the tricky one
r = scenario({FACTORY: BASH.replace('echo "next ${1}"', 'echo "next ${1} changed"')})
code, out = r.run()
ck("a change in the next function exits 1", code == 1, f"exit {code}: {out}")
ck("it is attributed to the next function, not the tricky one", "neighbour" in out and "tares/crew/neighbour.py" in out and "tricky" not in out, out)
r.done()

# a symbol-less module value maps to the file
r = scenario({FACTORY: BASH.replace('echo "next ${1}"', 'echo "next"')})
code, out = r.run()
ck("a python value without a symbol maps to the module file", "tares/crew/neighbour.py" in out, out)
r.done()

# a removed ported function -> 1
r = scenario({FACTORY: BASH.replace('slugify() {\n  echo "${1}" | tr A-Z a-z\n}\n', '')})
code, out = r.run()
ck("a removed ported function exits 1", code == 1 and "slugify" in out and "tares/crew/state.py" in out, f"exit {code}: {out}")
r.done()

# a renamed ported function -> 1 (found by its old name)
r = scenario({FACTORY: BASH.replace('slugify() {', 'slugify_v2() {')})
code, out = r.run()
ck("a renamed ported function exits 1 and names the old name", code == 1 and "slugify()" in out, f"exit {code}: {out}")
r.done()

# a helper script function is checked too
r = scenario({GUARD: GUARD_SH.replace('echo "guard ${1}"', 'echo "guard ${1} changed"')})
code, out = r.run()
ck("a changed ported function in a helper script exits 1", code == 1 and "guard_check" in out and "tares/crew/guard.py" in out, f"exit {code}: {out}")
ck("the message names the helper script", GUARD in out, out)
r.done()
r = scenario({GUARD: GUARD_SH.replace('echo "guard ${1}"', 'echo "guard ${1} changed"'), "tares/crew/guard.py": "def check(): pass\n"})
code, out = r.run()
ck("the helper function with its Python module changed exits 0", code == 0, f"exit {code}: {out}")
r.done()

# a package module maps to its __init__.py when that is what exists
led = ledger()
led["functions"][2]["python"] = "tares.crew.pkg:run"
r = Repo({FACTORY: BASH, GUARD: GUARD_SH, "tares/crew/pkg/__init__.py": "def run(): pass\n"}, led)
r.branch()
r.commit({FACTORY: BASH.replace('echo "next ${1}"', 'echo "next"')})
code, out = r.run()
ck("a package module is named by its __init__.py", code == 1 and "tares/crew/pkg/__init__.py" in out, f"exit {code}: {out}")
r.done()

# F1: definitions that are not `name() {` alone on the line must be seen too
STYLES = """#!/usr/bin/env bash
oneliner() { jq -rn --arg p "$1" '$p | @uri'; }
commented() { # a note after the brace; the body follows
  echo "commented ${1}"
}
continued() { crew_projects | jq -c --arg p "$1" \\
  '.[] | select(.name == $p)'; }
trailing() { pace_json | jq -e '.tried | length'; }          # a note after the closing brace
afterwards() {
  echo "afterwards ${1}"
}
"""


def styles_ledger(names):
    led = ledger()
    led["functions"] = [{"name": n, "kind": "function", "ticket": "TR-1", "dropped": False, "python": "tares.crew.%s:run" % n,
                         "ported": True, "note": ""} for n in names]
    led["helpers"] = []
    return led


STYLE_NAMES = ["oneliner", "commented", "continued", "trailing", "afterwards"]
for name, old, new_ in (("oneliner", "@uri", "@uri2"), ("commented", 'echo "commented ${1}"', 'echo "commented ${1} x"'),
                        ("continued", "select(.name == $p)", "select(.id == $p)"), ("trailing", "length", "length + 1"),
                        ("afterwards", 'echo "afterwards ${1}"', 'echo "afterwards"')):
    r = Repo({FACTORY: STYLES, "tares/crew/%s.py" % name: "x = 1\n"}, styles_ledger(STYLE_NAMES))
    r.branch()
    r.commit({FACTORY: STYLES.replace(old, new_)})
    code, out = r.run()
    ck("a change in the %s() style function exits 1 and names it" % name, code == 1 and "%s()" % name in out, f"exit {code}: {out}")
    others = [n for n in STYLE_NAMES if n != name and ("%s()" % n) in out]
    ck("only %s() is named (each style ends at its own end)" % name, not others, out)
    r.done()
r = Repo({FACTORY: STYLES}, styles_ledger(STYLE_NAMES))
r.branch()
r.commit({FACTORY: STYLES.replace("# a note after the closing brace", "# another note")})
code, out = r.run()
ck("a change only in the comment after a one-line function counts as a change", code == 1 and "trailing()" in out, f"exit {code}: {out}")
r.done()

# F5 and F6: a multi-line function closed by `} # note`, and one opened by `name() { # note; }` (the comment
# on the open line even ends like a one-line function, but the body follows on the next lines)
COMMENTS = """#!/usr/bin/env bash
closed() {
  echo "closed ${1}"
  echo "closed again"
} # a note after the closing brace
held() { # a note that looks like the end of a one-line function; }
  echo "held ${1}"
  echo "held again"
}
tail_fn() {
  echo "tail ${1}"
}
"""
COMMENT_NAMES = ["closed", "held", "tail_fn"]
for name, old, new_ in (("closed", 'echo "closed again"', 'echo "closed twice"'),
                        ("held", 'echo "held again"', 'echo "held twice"'),
                        ("tail_fn", 'echo "tail ${1}"', 'echo "tail"')):
    r = Repo({FACTORY: COMMENTS}, styles_ledger(COMMENT_NAMES))
    r.branch()
    r.commit({FACTORY: COMMENTS.replace(old, new_)})
    code, out = r.run()
    ck("a change in the body of %s() (comment style) exits 1 and names it" % name, code == 1 and "%s()" % name in out, f"exit {code}: {out}")
    others = [n for n in COMMENT_NAMES if n != name and ("%s()" % n) in out]
    ck("only %s() is named (the comment styles end at their own end)" % name, not others, out)
    r.done()
# a ported row whose function the parser cannot find must not pass quietly
led = styles_ledger(STYLE_NAMES)
led["functions"].append({"name": "not_there", "kind": "function", "ticket": "TR-1", "dropped": False, "python": "tares.crew.x:y",
                         "ported": True, "note": ""})
r = Repo({FACTORY: STYLES}, led)
r.branch()
r.commit({FACTORY: STYLES.replace("@uri", "@uri2")})
code, out = r.run()
ck("a ported row whose function is in neither revision exits 2 and names it", code == 2 and "not_there" in out, f"exit {code}: {out}")
r.done()
led = ledger()
led["helpers"][0]["file"] = "claude-plugin/bin/unknown-script"
r = Repo(None, led)
r.branch()
code, out = r.run()
ck("a ported helper row with an unwatched file exits 2 and names it", code == 2 and "guard_check" in out and "unknown-script" in out and "not a watched script" in out, f"exit {code}: {out}")
r.done()
# but a ported function the PR removes (it is there at the merge base) is a drift, not an error
r = scenario({FACTORY: BASH.replace('neighbour() {\n  echo "next ${1}"\n}\n', '')})
code, out = r.run()
ck("a ported function removed by the PR is a drift (exit 1), not an error", code == 1 and "neighbour" in out, f"exit {code}: {out}")
r.done()

# F3: a sibling file inside a package counts as touching the package
led = ledger()
led["functions"][2]["python"] = "tares.crew.pkg:run"
r = Repo({FACTORY: BASH, GUARD: GUARD_SH, "tares/crew/pkg/__init__.py": "def run(): pass\n"}, led)
r.branch()
r.commit({FACTORY: BASH.replace('echo "next ${1}"', 'echo "next"'), "tares/crew/pkg/helpers.py": "x = 1\n"})
code, out = r.run()
ck("a changed file inside the package folder counts as the package's Python change", code == 0, f"exit {code}: {out}")
r.done()
r = Repo({FACTORY: BASH, GUARD: GUARD_SH, "tares/crew/pkg/__init__.py": "def run(): pass\n"}, led)
r.branch()
r.commit({FACTORY: BASH.replace('echo "next ${1}"', 'echo "next"'), "tares/crew/pkgother/x.py": "x = 1\n"})
code, out = r.run()
ck("a file in a folder that only shares the package's name prefix does not count", code == 1, f"exit {code}: {out}")
r.done()

# F3: a dropped row is not checked, even when it says ported
led = ledger()
led["functions"][0]["dropped"] = "not needed in Python"
r = Repo(None, led)
r.branch()
r.commit({FACTORY: BASH.replace('tr A-Z a-z', 'tr A-Z a-z | sed s/x/y/')})
code, out = r.run()
ck("a dropped row is not checked", code == 0, f"exit {code}: {out}")
r.done()

# F3: a function defined twice is compared as a whole (a change in the first definition counts)
DUP = BASH + 'slugify() {\n  echo "second ${1}"\n}\n'
r = Repo({FACTORY: DUP, GUARD: GUARD_SH, "tares/crew/state.py": "def slugify(): pass\n"})
r.branch()
r.commit({FACTORY: DUP.replace('tr A-Z a-z', 'tr A-Z a-z | sed s/x/y/')})
code, out = r.run()
ck("a change in the first of two definitions of one name exits 1", code == 1 and "slugify" in out, f"exit {code}: {out}")
r.done()

# the base moved ahead after the branch point: that is not the PR's change
r = Repo()
r.branch()
r.commit({"README.md": "the PR touches something else\n"})
r.checkout("main")
r.commit({FACTORY: BASH.replace('tr A-Z a-z', 'tr A-Z a-z | sed s/x/y/')}, "main moves on")
code, out = r.run()
ck("a bash change that landed on main after the branch point is not counted", code == 0, f"exit {code}: {out}")
r.done()

# and the other direction: the PR's own change still counts when main has also moved
r = Repo()
r.branch()
r.commit({FACTORY: BASH.replace('echo "next ${1}"', 'echo "next"')})
r.checkout("main")
r.commit({"README.md": "main moves on\n"}, "main moves on")
code, out = r.run()
ck("the PR's own change still counts when main has moved", code == 1 and "neighbour" in out, f"exit {code}: {out}")
r.done()

# the label skips the check before any work (even with a bad revision)
r = scenario({FACTORY: BASH.replace('tr A-Z a-z', 'tr A-Z a-z | sed s/x/y/')})
code, out = r.run("main", "pr", "--labels", "bug, port-drift-ok ,docs")
ck("the port-drift-ok label exits 0 with the skip message", code == 0 and "port-drift-ok label set: port drift check skipped" in out, f"exit {code}: {out}")
code, out = r.run("main", "pr", "--labels", "bug,docs")
ck("other labels do not skip the check", code == 1, f"exit {code}: {out}")
code, out = r.run("nope", "nope", "--labels", "port-drift-ok")
ck("the label skips before any revision is read", code == 0, f"exit {code}: {out}")
r.done()

# bad input -> 2
r = scenario({FACTORY: BASH})
code, out = r.run("no-such-rev", "pr")
ck("an unknown base revision exits 2", code == 2 and "no-such-rev" in out, f"exit {code}: {out}")
code, out = r.run("main", "no-such-rev")
ck("an unknown head revision exits 2", code == 2, f"exit {code}: {out}")
r.ledger.write_text("{not json", encoding="utf-8")
code, out = r.run()
ck("an unreadable ledger exits 2", code == 2, f"exit {code}: {out}")
r.ledger.unlink()
code, out = r.run()
ck("a missing ledger exits 2", code == 2, f"exit {code}: {out}")
r.ledger.write_text("{}", encoding="utf-8")
code, out = r.run()
ck("a ledger that is not version 1 exits 2", code == 2, f"exit {code}: {out}")
r.done()
res = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True)
ck("a missing command line argument exits 2", res.returncode == 2, f"exit {res.returncode}")

# the script stays light: no heavy imports
src = CHECK.read_text(encoding="utf-8")
ck("the script imports no duckdb, fastapi or tares", not any(w in src for w in ("import duckdb", "import fastapi", "import tares", "from tares")))
ck("the script has no em dash", chr(0x2014) not in src)

# the real tree: the parser sees every top-level definition, and every ported ledger row (F1: it used to miss 14)
sys.path.insert(0, str(ROOT / "scripts"))
import check_port_drift as cpd  # noqa: E402
import re  # noqa: E402

for rel in cpd.SCRIPTS:
    text = (ROOT / rel).read_text(encoding="utf-8")
    defs = {m.group(1) for line in text.splitlines() for m in [cpd.DEF_RE.match(line)] if m}
    seen = cpd.functions(text)
    ck("every top-level definition of %s is parsed" % rel, defs == set(seen), "missing: %s" % sorted(defs - set(seen))[:5])
led_real = json.loads((ROOT / "scripts" / "crew_ledger.json").read_text(encoding="utf-8"))
for rel, name, spec in cpd.ported_rows(led_real):
    if name not in cpd.functions((ROOT / rel).read_text(encoding="utf-8")):
        ck("ported row %s() is found in %s" % (name, rel), False)
ck("every ported ledger row is found in its script", True)
ck("the check watches at least the 32 rows ported by M1", len(cpd.ported_rows(led_real)) >= 32)

# the real tree: this branch has no bash change yet against itself
res = subprocess.run([sys.executable, str(CHECK), "HEAD", "HEAD"], capture_output=True, text=True, cwd=str(ROOT))
ck("the real repo against itself exits 0", res.returncode == 0, res.stdout + res.stderr)

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
