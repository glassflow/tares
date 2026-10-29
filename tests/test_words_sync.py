"""The words list: the copy the wheel carries is the console's file, and the Python matcher and command behave."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tares import words  # noqa: E402

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def cli(body, *args):
    p = subprocess.run([sys.executable, "-m", "tares.words", *args], input=json.dumps(body), text=True,
                       capture_output=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
    return p.returncode, p.stdout.strip()


src = ROOT / "ui" / "src" / "editions" / "words.json"
copy = ROOT / "tares" / "editions_words.json"
ck("tares/editions_words.json is byte-identical to ui/src/editions/words.json", src.read_bytes() == copy.read_bytes())
ck("load() reads the list", len(words.load()["forbidden"]) >= 12 and words.load()["punctuation"] == ["\u2014"])
ck("the list holds the ten internal names", {"source", "view", "trigger", "run", "dispatch", "wiring", "catalog",
   "entity", "ingest", "station"} <= {e["word"] for e in words.load()["forbidden"]})

ck("offenders: '3 runs today'", words.offenders("3 runs today") == ["runs"], str(words.offenders("3 runs today")))
ck("offenders: 'View PR' is fine", words.offenders("View PR") == [])
ck("offenders: 'open source' is fine", words.offenders("open source") == [])
ck("offenders: 'Run the tests' is fine", words.offenders("Run the tests") == [])
ck("offenders: 'Run its 5 checks'", words.offenders("Run its 5 checks") == ["run"])
ck("offenders: 'Trigger happy'", words.offenders("Trigger happy") == ["trigger"])
ck("offenders: whole words only", words.offenders("Preview the datasource") == [])
ck("offenders: an allowed phrase does not hide a plural", words.offenders("View the sources") == ["sources"])
ck("offenders: an em dash", words.offenders("a\u2014b") == ["em dash"])
ck("offenders: each word once, in order", words.offenders("3 runs, 2 sources, 4 runs") == ["runs", "sources"])

body = {"a": [{"b": "2 firings"}, "fine"], "name": "my-source", "odd key": "an entity", "n": 3}
ck("offenders_in: paths, keys not checked",
   words.offenders_in(body) == ["$.a[0].b: firings", "$.name: source", '$["odd key"]: entity'], str(words.offenders_in(body)))
ck("offenders_in: skip_keys", words.offenders_in(body, ("name", "odd key")) == ["$.a[0].b: firings"])
ck("offenders_in: a key is never checked", words.offenders_in({"runs": "fine"}) == [])

code, out = cli({"a": "3 runs", "name": "my-source"}, "--skip", "name")
ck("command: prints the path and the word, exits 1", (code, out) == (1, "$.a: runs"), f"{code} {out!r}")
code, out = cli({"a": "View PR"})
ck("command: clean JSON prints 0 problems, exits 0", (code, out) == (0, "0 problems"), f"{code} {out!r}")
code, out = cli({"a": "x\u2014y"})
ck("command: an em dash exits 1", code == 1 and out == "$.a: em dash", f"{code} {out!r}")
code, out = cli({"a": "3 runs", "name": "my-source"}, "--skip=name,goal")
ck("command: --skip=a,b form", (code, out) == (1, "$.a: runs"), f"{code} {out!r}")

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
