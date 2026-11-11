"""tares.crew reads and writes the crew's files (tares/crew/state.py, locks.py, paths.py).

Run on a throwaway crew home (a path with a space in it): the names of the files; slugify and crew name rules and
error text; FACTORY_NOW; ensure_crew_home and merge_crew_defaults; stations.json, events.jsonl, HALT, pace, wake and
the crew's key as the bytes they are written in; the per-station lock (a holder refuses a second taker, a stale lock
is taken over, the "no pid yet" grace); and that importing tares.crew pulls in nothing but the standard library.

The expectations are literals and rules written down here, not another implementation's output.

Run: .venv/bin/python tests/test_crew_state.py
"""
import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares.crew import CrewError, CrewPaths, StationLockedError, locks, state  # noqa: E402
from tares.crew.paths import CREW_NAME_RULE  # noqa: E402

TEMPLATES = ROOT / "claude-plugin" / "templates"

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def short(x, n=300):
    s = repr(x)
    return s if len(s) <= n else s[:n] + "..."


# ---------- the sandbox: everything under one temp folder whose name has a space ----------

T = Path(tempfile.mkdtemp(prefix="tares crew state "))
HOME = T / "home dir"
BIN = T / "bin"
HOME.mkdir()
BIN.mkdir()
GITCFG = T / "gitconfig"
GITCFG.write_text("[user]\n\tname = Zoë Ørsted 名前\n", encoding="utf-8")
EMPTYGIT = T / "gitconfig-empty"
EMPTYGIT.write_text("", encoding="utf-8")
# a gh that answers like `gh api user -q .login`, or fails (FAKE_GH=fail)
(BIN / "gh").write_text('#!/bin/sh\n[ "${FAKE_GH:-ok}" = ok ] && echo zoe-gh || exit 1\n')
(BIN / "gh").chmod(0o755)

for k in list(os.environ):
    if k.startswith("FACTORY_") or k in ("TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_URL"):
        del os.environ[k]
os.environ.update(HOME=str(HOME), GIT_CONFIG_GLOBAL=str(GITCFG), GIT_CONFIG_NOSYSTEM="1",
                  PATH=str(BIN) + os.pathsep + os.environ.get("PATH", ""), TMPDIR=str(T))

_n = 0


def newhome(label="home"):
    """A fresh crew home path (not created) with a space in its name."""
    global _n
    _n += 1
    return T / f"crew {label} {_n}"


@contextlib.contextmanager
def env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def read_bytes(p):
    return Path(p).read_bytes()


def merge(base, over):
    """jq's `*`: objects merge key by key, anything else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = merge(base[k], v) if k in base else v
        return out
    return over


# ====================================================================================
print("\n== names of the files ==")
h = newhome("names")
cp = CrewPaths.at(h)
for got, name in ((cp.crew_cfg, "crew.json"), (cp.stations, "stations.json"), (cp.halt_file, "HALT"), (cp.frozen_kit, "frozen-kit"),
                  (cp.newer_file, "kit-newer"), (cp.freezing, "freezing"), (cp.cc_newer_file, "claude-newer"),
                  (cp.checkout_file, "kit-checkout"), (cp.events, "events.jsonl"), (cp.token_file, ".tares-token"),
                  (cp.pace_file, "pace.json")):
    ck(f"path {name} is in the crew home", got == h / name, f"{got}")
for got, rel in ((cp.pace_station_file("x"), "stations/x.pace"), (cp.wake_file("x"), "stations/x.wake"),
                 (cp.done_file("x"), "stations/x.done"), (cp.lock_dir("x"), "stations/x.lock")):
    ck(f"per-station path {rel}", got == h / rel, f"{got}")
ck("the crew home itself is the one given, with its space", str(cp.home) == str(h) and " " in str(h))
ck("CREW_NAME_RULE is the sentence the refusals quote",
   CREW_NAME_RULE == "1 to 31 characters: a-z, 0-9 and hyphens, starting with a letter or digit, and not containing the word build between hyphens")
ck("WAKE_DEFAULT is the empty wake state", state.WAKE_DEFAULT == {"seen": [], "n": 0, "last_wake": None, "clock": None, "baseline": None, "released": False})
ck("PACE_DEFAULT is not stretched and not held", state.PACE_DEFAULT == {"stretched": False, "held": False})
with env(HOME="/hm", FACTORY_CREW_HOME=None):
    ck("default crew home is $HOME/factory", CrewPaths.from_env().raw == "/hm/factory")
with env(FACTORY_CREW_HOME=""):
    ck("an empty FACTORY_CREW_HOME counts as unset", CrewPaths.from_env().raw == f"{HOME}/factory")

# ====================================================================================
print("\n== slugify ==")
SLUGS = [("", ""), ("Hello World", "hello-world"), ("  lead", "lead"), ("trail  ", "trail"), ("---a---", "a"), ("a__b", "a-b"),
         ("UPPER lower 123", "upper-lower-123"), ("Ünïcödé", "n-c-d"), ("日本語", ""), ("x/y\\z", "x-y-z"), ("a\nb", "a-b"),
         ("!!!", ""), ("Fix: the (big) bug #42!", "fix-the-big-bug-42"), ("a.b.c", "a-b-c"), ("ABC-def_GHI.jkl", "abc-def-ghi-jkl"),
         ("0", "0"), ("x" * 200, "x" * 200)]
bad = [(i, o, state.slugify(i)) for i, o in SLUGS if state.slugify(i) != o]
ck(f"slugify: ASCII lower-cased, every other run of characters one hyphen, none at the ends ({len(SLUGS)} inputs)", not bad, short(bad))
ck("slugify has no trailing newline", state.slugify("A b") == "a-b")

# ====================================================================================
print("\n== crew name ==")
NAMES = ["crew", "a", "0", "my-crew", "Bad_Name", "-x", "UPPER", "a b", "x-build-y", "build", "a-build", "build-a",
         "a-b-build-c", "a--b", "-", "a" * 31, "a" * 32, "buildx", "xbuild", "ünï", "ab\n"]
FLAGS = "111100000000010101100"
mine = "".join("1" if state.crew_name_ok(n) else "0" for n in NAMES)
ck("crew_name_ok: a-z, 0-9 and hyphens, 1 to 31, first a letter or digit, no hyphen-delimited build", mine == FLAGS, f"want {FLAGS} got {mine}")


def name_of(cfg_text=None, env_name=None):
    h = newhome("name")
    h.mkdir()
    if cfg_text is not None:
        (h / "crew.json").write_text(cfg_text, encoding="utf-8")
    with env(FACTORY_CREW_NAME=env_name):
        try:
            return ("ok", state.crew_name(CrewPaths.at(h)))
        except CrewError as ex:
            return ("err", ex.line)


for n in NAMES:
    got = name_of(env_name=n)
    ck(f"crew name from env {n!r}", got == ("ok", n) if state.crew_name_ok(n) else got[0] == "err" and CREW_NAME_RULE in got[1], short(got))
ck("crew name: no env, no file is the default, crew", name_of() == ("ok", "crew"))
ck("crew name: an empty env falls to the file", name_of('{"crew_name": "fromfile"}', "") == ("ok", "fromfile"))
ck("crew name: env wins over the file", name_of('{"crew_name": "fromfile"}', "fromenv") == ("ok", "fromenv"))
ck("crew name: the file", name_of('{"crew_name": "fromfile"}') == ("ok", "fromfile"))
r = name_of('{"crew_name": "Bad Name"}')
ck("crew name: a bad file name is refused, naming the name, crew.json and the rule", r[0] == "err" and 'the crew name "Bad Name" (from crew_name in ' in r[1]
   and "crew.json) is not valid: it is " + CREW_NAME_RULE in r[1], short(r))
r = name_of(env_name="Bad_Name")
ck("crew name: a bad env name is refused with the line", r == ("err", f'tares factory: the crew name "Bad_Name" (from FACTORY_CREW_NAME) is not valid: it is {CREW_NAME_RULE}'), short(r))
for label, text, want in (("a number", '{"crew_name": 5}', ("ok", "5")), ("true", '{"crew_name": true}', ("ok", "true")),
                          ("null", '{"crew_name": null}', ("ok", "crew")), ("empty", '{"crew_name": ""}', ("ok", "crew")),
                          ("a file that is not JSON", "{nope", ("ok", "crew")), ("a nested crew_name", '{"x": {"crew_name": "no"}}', ("ok", "crew"))):
    ck(f"crew name: {label} in the file gives {want[1]!r}", name_of(text) == want, short(name_of(text)))
ck("crew name: a non-ASCII name in the file is refused", name_of('{"crew_name": "ünï"}')[0] == "err")
with env(FACTORY_CREW_NAME="acme"):
    ck("crew_orchestrator is <crew>-orchestrator", state.crew_orchestrator(CrewPaths.at(newhome("x"))) == "acme-orchestrator")

# ====================================================================================
print("\n== FACTORY_NOW ==")
with env(FACTORY_NOW="1700000000"):
    ck("now_s honours FACTORY_NOW", state.now_s() == 1700000000)
with env(FACTORY_NOW=None):
    ck("without it, the clock", abs(state.now_s() - time.time()) < 3)
with env(FACTORY_NOW=""):
    ck("an empty FACTORY_NOW is the clock", abs(state.now_s() - time.time()) < 3)
with env(FACTORY_NOW="soon"):
    try:
        state.now_s()
        ck("a FACTORY_NOW that is not a number is refused", False)
    except CrewError as ex:
        ck("a FACTORY_NOW that is not a number is refused", "FACTORY_NOW" in str(ex))
ck("utc_stamp is `now | todate`", state.utc_stamp(1700000000) == "2023-11-14T22:13:20Z")

# ====================================================================================
print("\n== ensure_crew_home, merge_crew_defaults ==")
template = json.loads((TEMPLATES / "crew.json").read_text(encoding="utf-8"))
for label, e, gh, who in (("git name, gh login", {}, "zoe-gh", "Zoë Ørsted 名前"),
                          ("gh fails", {"FAKE_GH": "fail"}, "", "Zoë Ørsted 名前"),
                          ("no git name", {"GIT_CONFIG_GLOBAL": str(EMPTYGIT)}, "zoe-gh", "the user")):
    hp = newhome("ensure-py")
    with env(**e):
        state.ensure_crew_home(CrewPaths.at(hp))
    cfg = json.loads(read_bytes(hp / "crew.json"))
    ck(f"ensure_crew_home ({label}): the folders", all((hp / d).is_dir() for d in ("stations", "reports", "worktrees")))
    ck(f"ensure_crew_home ({label}): the person is {who!r}, the login {gh!r}",
       cfg["human"] == {"name": who, "github": gh} and cfg["authorized"]["by"] == who, short(cfg["human"]))
    ck(f"ensure_crew_home ({label}): the rest is the template's", merge(template, {"human": cfg["human"], "authorized": cfg["authorized"]}) == cfg)
hp = newhome("ens-p")
state.ensure_crew_home(CrewPaths.at(hp))
raw = read_bytes(hp / "crew.json")
ck("crew.json: non-ASCII is written as is, not escaped", "Zoë Ørsted 名前".encode() in raw and b"\\u" not in raw)
ck("crew.json: LF only, ends with one newline", b"\r" not in raw and raw.endswith(b"}\n") and not raw.endswith(b"\n\n"))
ck("ccfg reads it back", state.ccfg("human", "name", paths=CrewPaths.at(hp)) == "Zoë Ørsted 名前"
   and state.ccfg("human", "github", paths=CrewPaths.at(hp)) == "zoe-gh"
   and state.ccfg("stations", "orchestrator", "model", paths=CrewPaths.at(hp)) == template["stations"]["orchestrator"]["model"])
hh = newhome("req")
try:
    state.require_crew(CrewPaths.at(hh))
    msg = None
except CrewError as ex:
    msg = ex.line
ck("require_crew: no crew yet, names the folder and the way out", msg == f"tares factory: no crew yet in {hh}; run: tares factory crew up", short(msg))
state.require_crew(CrewPaths.at(hp))

# merge_crew_defaults: hand-written files get the template's defaults under them
HAND = [
    ("keys in another order, an extra key, a nested override",
     '{"zzz": [1, {"a": null}], "human": {"github": "g", "name": "n é"}, "stations": {"builder": {"model": "haiku"}, "extra": 1}, "crew_name": "acme"}'),
    ("null replaces a default object", '{"review": null, "tares": {"url": "http://x y"}}'),
    ("a scalar replaces an object", '{"limits": 5, "release": {"environments": []}}'),
    ("empty object", "{}"),
    ("compact one-line file", '{"human":{"name":"A"}}'),
    ("CRLF line endings", '{\r\n  "human": {"name": "A"}\r\n}\r\n'),
    ("DEL and escapes", '{"human": {"name": "a\\u007fb\\n\\"q\\" \\ud83d\\ude00 \\u00e9"}}'),
]
for label, text in HAND:
    hp = newhome("merge-p")
    hp.mkdir()
    (hp / "crew.json").write_bytes(text.encode("utf-8"))
    state.merge_crew_defaults(CrewPaths.at(hp))
    got = json.loads(read_bytes(hp / "crew.json"))
    ck(f"merge defaults ({label}): the template under the person's values, a null or scalar replacing", got == merge(template, json.loads(text)), short(got))
    ck(f"merge defaults ({label}): the file is pretty, LF, one newline", b"\r" not in read_bytes(hp / "crew.json") and read_bytes(hp / "crew.json").endswith(b"}\n")
       and read_bytes(hp / "crew.json").startswith(b"{\n  "))
hp = newhome("merge-order")
hp.mkdir()
(hp / "crew.json").write_text('{"zzz": [1], "human": {"name": "n"}}', encoding="utf-8")
state.merge_crew_defaults(CrewPaths.at(hp))
keys = list(json.loads(read_bytes(hp / "crew.json")))
ck("merge defaults: the template's keys in the template's order, the person's extra keys after", keys == list(template) + ["zzz"], short(keys))
# a file already merged is left alone: same inode and time
hp = newhome("merge-same")
state.ensure_crew_home(CrewPaths.at(hp))
before = os.stat(hp / "crew.json")
time.sleep(0.05)
state.merge_crew_defaults(CrewPaths.at(hp))
after = os.stat(hp / "crew.json")
ck("merge defaults: an unchanged crew.json is not rewritten", (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns))
# a missing crew.json is skipped; invalid JSON is refused
hm = newhome("merge-none")
hm.mkdir()
state.merge_crew_defaults(CrewPaths.at(hm))
ck("merge defaults: no crew.json, nothing happens", not (hm / "crew.json").exists())
for label, text in (("not JSON", "{nope"), ("empty file", ""), ("an array", "[]"), ("null", "null"), ("a string", '"x"')):
    hp = newhome("bad-p")
    hp.mkdir()
    (hp / "crew.json").write_text(text)
    try:
        state.merge_crew_defaults(CrewPaths.at(hp))
        msg = None
    except CrewError as ex:
        msg = ex.line.replace(str(hp), "HOME")
    ck(f"merge defaults ({label}): refused, naming crew.json", msg == "tares factory: HOME/crew.json is not valid JSON", f"{msg!r}")
    ck(f"merge defaults ({label}): the file is left as it was", (hp / "crew.json").read_text() == text)

# ====================================================================================
print("\n== stations.json ==")
PUTS = [("crew-orchestrator", {"role": "orchestrator", "id": "x1", "cwd": "/p q/r"}),
        ("crew-orchestrator", {"id": "x2", "new": 1}),
        ("b", {"name": "é\u007f\"\\\n\t ü 日本", "empty": "", "e": {}, "l": []}),
        ("crew-orchestrator", {"nested": {"k": [1, 2, {"z": None}], "s": "x"}, "t": True, "f": False, "n": None, "num": 3, "neg": -7}),
        ("crew-orchestrator", {"role": "reviewer"}),
        ("c d", {"k": "space in the station name"})]
GETS = [("crew-orchestrator", "", None), ("crew-orchestrator", ".role", "reviewer"), ("crew-orchestrator", ".id", "x2"),
        ("crew-orchestrator", ".nested.k", '[\n  1,\n  2,\n  {\n    "z": null\n  }\n]'), ("crew-orchestrator", ".nested.s", "x"), ("crew-orchestrator", ".t", "true"),
        ("crew-orchestrator", ".f", ""), ("crew-orchestrator", ".n", ""), ("crew-orchestrator", ".num", "3"),
        ("crew-orchestrator", ".role.deep", ""), ("crew-orchestrator", ".missing", ""),
        ("b", ".name", "é\u007f\"\\\n\t ü 日本"), ("b", ".empty", ""), ("b", ".e", "{}"), ("b", ".l", "[]"), ("zz", ".x", ""), ("zz", "", ""),
        ("c d", ".k", "space in the station name")]


def keys_of(path):
    return tuple(k for k in path.split(".") if k)


hp = newhome("st-p")
hp.mkdir()
ck("stations_get on a crew home with no stations.json is empty", state.stations_get("x", "id", paths=CrewPaths.at(hp)) == "")
for s_, v in PUTS:
    state.stations_put(s_, v, CrewPaths.at(hp))
raw = read_bytes(hp / "stations.json")
ck("stations.json: non-ASCII raw, DEL escaped, LF, one trailing newline",
   "日本".encode() in raw and b"\\u007f" in raw and b"\r" not in raw and raw.endswith(b"}\n"))
doc = json.loads(raw)
ck("stations_put merges field by field into the station's row and keeps the other rows",
   doc["crew-orchestrator"] == {"role": "reviewer", "id": "x2", "cwd": "/p q/r", "new": 1,
                                "nested": {"k": [1, 2, {"z": None}], "s": "x"}, "t": True, "f": False, "n": None, "num": 3, "neg": -7}
   and doc["b"]["name"] == "é\u007f\"\\\n\t ü 日本" and list(doc) == ["crew-orchestrator", "b", "c d"], short(doc))
ck("stations.json is pretty with a 2-space indent", raw.startswith(b'{\n  "crew-orchestrator": {\n    "role"'), short(raw[:60]))
bad = []
for s_, pth, want in GETS:
    if want is None:
        continue
    got = state.stations_get(s_, *keys_of(pth), paths=CrewPaths.at(hp))
    if got != want:
        bad.append((s_, pth, want, got))
ck(f"stations_get gives each field as text: strings raw, objects and lists pretty, '' for null, false or missing ({len(GETS) - 1} fields)", not bad, short(bad))
# stations_put on something that is not an object: refused, the file left alone
hn = newhome("st-bad")
hn.mkdir()
(hn / "stations.json").write_text('{"a": "text"}\n')
try:
    state.stations_put("a", {"k": 1}, CrewPaths.at(hn))
    py_refused = False
except CrewError:
    py_refused = True
ck("stations_put onto a non-object is refused", py_refused)
ck("...and leaves the file alone", (hn / "stations.json").read_text() == '{"a": "text"}\n')
(hn / "stations.json").write_text("{nope")
ck("stations_get on a broken stations.json is empty", state.stations_get("a", "k", paths=CrewPaths.at(hn)) == "")

# ====================================================================================
print("\n== events.jsonl ==")
EVENTS = [("start", "crew-orchestrator", "detail"), ("resume", "", ""), ("halt", "", "by the \"human\""),
          ("odd", "s\\t", "line\nbreak and \ttab"), ("uni", "stäti\u00f6n", "日本語 \U0001F600 é"), ("del", "x", "a\u007fb"),
          ("ctl", "x", "\u0001\u001f"), ("slash", "a/b", "</script> & <b>")]
AT = re.compile(r'^\{"at":"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",(.*)\}$')
hp = newhome("ev-p")
hp.mkdir()
for e_, s_, d_ in EVENTS:
    state.event(e_, s_, d_, paths=CrewPaths.at(hp))
lp = read_bytes(hp / "events.jsonl").decode("utf-8").split("\n")
ck("events: one line per event and a final newline", len(lp) == len(EVENTS) + 1 and lp[-1] == "", f"{len(lp)}")
ck("events: each line is compact JSON starting with the UTC time and the Z", all(AT.match(x) for x in lp[:-1]), short(lp[:2]))
rows = [json.loads(x) for x in lp[:-1]]
ck("events: the keys are at, station, event, detail, in that order", all(list(r) == ["at", "station", "event", "detail"] for r in rows), short(rows[:2]))
ck("events: the values come back whole (quotes, tabs, newlines, DEL, control characters, emoji)",
   [(r["event"], r["station"], r["detail"]) for r in rows] == [(e_, s_, d_) for e_, s_, d_ in EVENTS], short(rows))
ck("events: non-ASCII is written as is", "日本語".encode() in read_bytes(hp / "events.jsonl") and b"\xf0\x9f\x98\x80" in read_bytes(hp / "events.jsonl"))
# a crew home that does not exist: nothing is created, nothing fails
gone = newhome("ev-gone")
state.event("x", paths=CrewPaths.at(gone))
ck("events: no crew home, no file and no folder made", not gone.exists())
# a crew home that cannot be written to never fails the caller
ro = newhome("ev-ro")
ro.mkdir()
(ro / "events.jsonl").mkdir()   # a folder where the file should be
try:
    state.event("x", paths=CrewPaths.at(ro))
    ck("events: an unwritable events.jsonl does not raise", True)
except Exception as ex:
    ck("events: an unwritable events.jsonl does not raise", False, repr(ex))
# many writers at once never mix a line
hc = newhome("ev-conc")
hc.mkdir()
ppaths = CrewPaths.at(hc)
big = "x" * 3000


def writer(n):
    for i in range(60):
        state.event("w", f"t{n}", f"{i} {big}", paths=ppaths)


ths = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
for t in ths:
    t.start()
for t in ths:
    t.join()
lines = read_bytes(hc / "events.jsonl").decode().split("\n")[:-1]
ok = True
for ln in lines:
    try:
        json.loads(ln)
    except ValueError:
        ok = False
ck("events: 6 threads x 60 lines, every line whole", ok and len(lines) == 6 * 60, f"{len(lines)} lines, parse ok={ok}")

# ====================================================================================
print("\n== HALT, pace, wake, token ==")
hp = newhome("halt-p")
hp.mkdir()
(hp / "crew.json").write_text("{}\n")
HALT = re.compile(r'^\{"at":"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",(.*)\}\n$')
for reason in ("why é \"q\"", None):
    if reason is not None:
        state.halt_write(reason, paths=CrewPaths.at(hp))
    else:
        state.halt_write(paths=CrewPaths.at(hp))
    text = read_bytes(hp / "HALT").decode()
    m = HALT.match(text)
    want = reason or "halted by the human"
    ck(f"HALT ({reason!r}): one compact line, the time first, then the reason", bool(m) and json.loads(text)["reason"] == want and list(json.loads(text)) == ["at", "reason"], short(text))
    ck(f"HALT ({reason!r}): halted and halt_reason report it", state.halted(CrewPaths.at(hp)) and state.halt_reason(CrewPaths.at(hp)) == want)
state.resume(CrewPaths.at(hp))
ck("resume removes HALT, and a second resume does not fail", not (hp / "HALT").exists() and state.resume(CrewPaths.at(hp)) is None and not state.halted(CrewPaths.at(hp)))

# pace
hp = newhome("pace-p")
(hp / "stations").mkdir(parents=True)
for s_, st, hd in (("crew-builder", True, False), ("crew-builder", False, True), ("crew-reviewer", True, True)):
    state.pace_station_put(s_, st, hd, CrewPaths.at(hp))
    ck(f"pace put {s_} stretched={st} held={hd}: compact JSON, stretched then held",
       read_bytes(hp / "stations" / f"{s_}.pace").decode().strip() == '{"stretched":%s,"held":%s}' % (str(st).lower(), str(hd).lower()),
       short(read_bytes(hp / "stations" / f"{s_}.pace")))
state.pace_station_put("crew-builder", False, False, CrewPaths.at(hp))
ck("pace put false false removes the file", not (hp / "stations/crew-builder.pace").exists())
state.pace_station_put("crew-builder", False, False, CrewPaths.at(hp))   # and a second time is fine
for label, text, want in (("missing", None, {"stretched": False, "held": False}), ("partial", '{"held":true}', {"stretched": False, "held": True}),
                          ("extra key", '{"stretched":true,"held":false,"x":[1]}', {"stretched": True, "held": False, "x": [1]}),
                          ("not JSON", "{nope", {"stretched": False, "held": False}), ("an array", "[1]", {"stretched": False, "held": False}),
                          ("empty", "", {"stretched": False, "held": False})):
    f = hp / "stations" / "s.pace"
    f.unlink(missing_ok=True)
    if text is not None:
        f.write_text(text)
    py = state.pace_station_read("s", CrewPaths.at(hp))
    ck(f"pace read ({label}): the defaults under what the file holds", py == want, f"python {py!r}")
ck("pace read: keys in the order stretched, held, then the file's own",
   state.dumps_compact(state.pace_station_read("s", CrewPaths.at(hp))) == '{"stretched":false,"held":false}')

# wake
hp = newhome("wake-p")
(hp / "stations").mkdir(parents=True)
w = state.wake_read("crew-reviewer", CrewPaths.at(hp))
ck("wake read: a missing file is the default", w == state.WAKE_DEFAULT)
w.update(n=3, seen=["a", "é 日本"], clock=1700000000, released=True)
state.wake_write("crew-reviewer", w, CrewPaths.at(hp))
raw = read_bytes(hp / "stations/crew-reviewer.wake")
ck("wake write: compact JSON in the default's key order, non-ASCII raw, one newline",
   raw.decode() == '{"seen":["a","é 日本"],"n":3,"last_wake":null,"clock":1700000000,"baseline":null,"released":true}\n', short(raw))
ck("wake read: reads it back", state.wake_read("crew-reviewer", CrewPaths.at(hp)) == w)
for label, text, want in (("missing", None, state.WAKE_DEFAULT), ("partial", '{"n":9}', {**state.WAKE_DEFAULT, "n": 9}),
                          ("extra key", '{"zz":1,"n":2}', {**state.WAKE_DEFAULT, "n": 2, "zz": 1}),
                          ("not JSON", "{nope", state.WAKE_DEFAULT), ("an array", "[]", state.WAKE_DEFAULT)):
    f = hp / "stations" / "w.wake"
    f.unlink(missing_ok=True)
    if text is not None:
        f.write_text(text)
    py = state.wake_read("w", CrewPaths.at(hp))
    ck(f"wake read ({label}): the defaults under what the file holds", py == want, f"python {py!r}")

# the crew's key
hb = newhome("tok")
hb.mkdir()
tok = hb / ".tares-token"
tok.write_text("  abc\tdef \n\nghi\n")
ck("crew_key: whitespace removed", state.crew_key(CrewPaths.at(hb)) == "abcdefghi")
ck("crew_key: TARES_FACTORY_TOKEN_FILE wins", state.crew_key(CrewPaths.at(hb), {"TARES_FACTORY_TOKEN_FILE": str(T / "none")}) == "")
state.write_token("s3cret-é", CrewPaths.at(hb))
ck("write_token: the token and a newline", read_bytes(tok) == "s3cret-é\n".encode())
if os.name != "nt":
    ck("write_token: readable by the person only (0600)", (os.stat(tok).st_mode & 0o777) == 0o600, oct(os.stat(tok).st_mode))
ck("crew_key reads the token write_token wrote", state.crew_key(CrewPaths.at(hb)) == "s3cret-é")

# ====================================================================================
print("\n== writing files ==")
d = T / "w dir"
d.mkdir()
f = d / "file.json"
state.write_json(f, {"b": 1, "a": ["é", {"x": None}], "e": {}, "l": []})
ck("write_json: 2-space pretty text, keys in the order given, non-ASCII raw, LF, one newline",
   read_bytes(f) == '{\n  "b": 1,\n  "a": [\n    "é",\n    {\n      "x": null\n    }\n  ],\n  "e": {},\n  "l": []\n}\n'.encode())
ck("no temp file is left beside it", [p.name for p in d.iterdir()] == ["file.json"], str([p.name for p in d.iterdir()]))
state.write_json(f, {"a": 1}, compact=True)
ck("write_json compact: no spaces", read_bytes(f) == b'{"a":1}\n')
calls = []
real_replace = os.replace
os.replace = lambda a, b: (calls.append((a, b)), real_replace(a, b))[1]
try:
    state.atomic_write_text(f, "x\n")
finally:
    os.replace = real_replace
ck("atomic_write_text replaces the file with os.replace from a temp file in the same folder",
   len(calls) == 1 and Path(calls[0][1]) == f and Path(calls[0][0]).parent == d and Path(calls[0][0]) != f, str(calls))
n = {"n": 0}


def flaky(a, b):
    n["n"] += 1
    if n["n"] < 3:
        raise PermissionError("in use")
    return real_replace(a, b)


os.replace = flaky
try:
    state.atomic_write_text(f, "retried\n")
except PermissionError:
    pass
finally:
    os.replace = real_replace
ck("a PermissionError from os.replace (Windows) is retried until it works", read_bytes(f) == b"retried\n" and n["n"] == 3, str(n))


def always(a, b):
    raise PermissionError("in use")


os.replace = always
t0 = time.time()
try:
    state.atomic_write_text(f, "never\n")
    raised = False
except PermissionError:
    raised = True
finally:
    os.replace = real_replace
ck("...and given up on after a few tries, the old file intact and no temp file left",
   raised and read_bytes(f) == b"retried\n" and [p.name for p in d.iterdir()] == ["file.json"] and time.time() - t0 < 5)
state.atomic_write_text(f, "a\r\nb\n")
ck("text is written as given: a CRLF stays, no translation either way", read_bytes(f) == b"a\r\nb\n" and state.read_text(f) == "a\r\nb\n")
# a reader never sees half a file
big = state.dumps_pretty({"k": ["x" * 50] * 4000})
stop = threading.Event()
torn = []


def reader():
    while not stop.is_set():
        try:
            json.loads(state.read_text(f))
        except (FileNotFoundError, PermissionError):  # Windows refuses an open while the file is being replaced
            pass
        except ValueError as ex:
            torn.append(ex)
        if os.name == "nt":
            time.sleep(0.005)  # a reader that never pauses can hold the file open past the engine's few retries


state.atomic_write_text(f, big + "\n")
rt = threading.Thread(target=reader, daemon=True)
rt.start()
try:
    for i in range(60):
        state.atomic_write_text(f, (big if i % 2 else '{"small": 1}') + "\n")
finally:
    stop.set()
    rt.join(10)
ck("a reader polling while the file is rewritten never sees a partial file", not torn, short(torn[:1]))
ck("read_json: a file that is not JSON gives the default, or a CrewError naming it",
   state.read_json(T / "nope.json", 7) == 7)
try:
    (d / "bad.json").write_text("{x")
    state.read_json(d / "bad.json")
    ck("read_json raises on bad JSON without a default", False)
except CrewError as ex:
    ck("read_json raises on bad JSON without a default", "bad.json" in str(ex))

# ====================================================================================
print("\n== locks ==")


def lock_dir(h, s="crew-reviewer"):
    return Path(h) / "stations" / f"{s}.lock"


def start_holder(h, s="crew-reviewer"):
    """A live process that holds the station's lock. Returns the Popen."""
    e = {**os.environ, "FACTORY_CREW_HOME": str(h)}
    code = (f"import sys, time; sys.path.insert(0, {str(ROOT)!r}); from tares.crew import locks, CrewPaths; "
            f"locks.station_lock({s!r}, CrewPaths.at({str(h)!r})); import os; print('held', os.getpid(), flush=True); time.sleep(120)")
    p = subprocess.Popen([sys.executable, "-c", code], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    line = p.stdout.readline().decode().strip()
    assert line.split()[:1] == ["held"], f"holder did not take the lock: {line!r} {p.stderr.read(500)!r}"
    # On Windows a venv's python.exe starts the real interpreter as a child, so Popen.pid is the launcher's;
    # the pid in <lock>/pid is the interpreter's own, which the holder prints.
    p.holder_pid = int(line.split()[1]) if len(line.split()) > 1 else p.pid
    return p


def kill(p):
    if os.name == "nt":  # the launcher and the interpreter under it both go
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    p.kill()
    p.wait()
    p.stdout.close()
    p.stderr.close()


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def py_lock(h, s="crew-reviewer"):
    """station_lock in this process: ("ok", seconds) or ("locked", error message, seconds)."""
    t0 = time.time()
    try:
        locks.station_lock(s, CrewPaths.at(h))
        return ("ok", time.time() - t0)
    except StationLockedError as ex:
        return ("locked", str(ex), time.time() - t0, ex)


MSG = "another factory command (pid {pid}) is starting or stopping {s}; try again in a minute"
for kind in ("python",):
    h = newhome(f"lock-{kind}-holder")
    holder = start_holder(h)
    pidfile = (lock_dir(h) / "pid").read_text()
    ck("a holder writes its own pid and a newline into <lock>/pid", pidfile == f"{holder.holder_pid}\n", repr(pidfile))
    r = py_lock(h)
    ck("a held lock refuses a second taker with the typed error and the text",
       r[0] == "locked" and r[1] == MSG.format(pid=holder.holder_pid, s="crew-reviewer") and r[3].holder == str(holder.holder_pid) and isinstance(r[3], CrewError), short(r))
    ck("...at once, without waiting", r[2] < 1.5, f"{r[2]:.2f}s")
    ck("...and the line carries the tares factory: prefix", r[3].line == "tares factory: " + MSG.format(pid=holder.holder_pid, s="crew-reviewer"))
    ck("the refused command left the holder's lock alone", (lock_dir(h) / "pid").read_text() == pidfile)
    kill(holder)
    # the holder died without releasing: a stale lock; the next taker takes it over
    r = py_lock(h)
    ck("a stale lock left by a killed holder is taken over", r[0] == "ok" and (lock_dir(h) / "pid").read_text() == f"{os.getpid()}\n", short(r))
    ck("...and no .takeover folder is left", not Path(str(lock_dir(h)) + ".takeover").exists())
    locks.release_lock("crew-reviewer", CrewPaths.at(h))
    ck("release_lock removes the pid file and the folder", not lock_dir(h).exists())

# stale lock (dead pid), taken over
h = newhome("lock-stale-py")
lock_dir(h).mkdir(parents=True)
(lock_dir(h) / "pid").write_text(f"{dead_pid()}\n")
r = py_lock(h)
ck("a lock whose pid is dead is stale and taken over", r[0] == "ok" and (lock_dir(h) / "pid").read_text() == f"{os.getpid()}\n", short(r))
locks.release_lock("crew-reviewer", CrewPaths.at(h))
# a pid file that is not a pid is stale too
for text in ("garbage\n", "0\n"):
    h = newhome("lock-junk")
    lock_dir(h).mkdir(parents=True)
    (lock_dir(h) / "pid").write_text(text)
    r = py_lock(h)
    ck(f"a pid file holding {text.strip()!r} is stale", r[0] == "ok", short(r))
    locks.release_lock("crew-reviewer", CrewPaths.at(h))


# the "no pid yet" grace: an empty lock folder is waited on for 10 x 0.2 s, then called stale
def grace_py():
    h = newhome("grace-py")
    lock_dir(h).mkdir(parents=True)
    r = py_lock(h)
    locks.release_lock("crew-reviewer", CrewPaths.at(h))
    return r


# a pid that shows up during the grace: a live holder after all
def appears():
    h = newhome("appear-py")
    lock_dir(h).mkdir(parents=True)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def later():
        time.sleep(0.5)
        (lock_dir(h) / "pid").write_text(f"{sleeper.pid}\n")

    th = threading.Thread(target=later)
    th.start()
    t0 = time.time()
    r = py_lock(h)
    res = (1 if r[0] == "locked" else 0, r[3].line + "\n" if r[0] == "locked" else "", time.time() - t0)
    th.join()
    sleeper.kill()
    sleeper.wait()
    return res, sleeper.pid


# a takeover folder nobody removes (a killed command): cleared after 25 x 0.2 s
def stuck_takeover():
    h = newhome("stuck-py")
    lock_dir(h).mkdir(parents=True)
    (lock_dir(h) / "pid").write_text(f"{dead_pid()}\n")
    Path(str(lock_dir(h)) + ".takeover").mkdir()
    t0 = time.time()
    r = py_lock(h)
    ok = r[0] == "ok"
    locks.release_lock("crew-reviewer", CrewPaths.at(h))
    return ok, time.time() - t0, Path(str(lock_dir(h)) + ".takeover").exists()


with concurrent.futures.ThreadPoolExecutor(4) as ex:
    fp, ap, sp = ex.submit(grace_py), ex.submit(appears), ex.submit(stuck_takeover)
    rp = fp.result()
    ap_res, ap_pid = ap.result()
    sp_res = sp.result()
ck("no pid yet: the taker waits about 2 s, then takes the lock", rp[0] == "ok" and 1.6 <= rp[1] < 8, short(rp[:2]))
ck("a pid that appears during the wait is respected: the taker refuses with the text, early",
   ap_res[0] == 1 and ap_res[1] == StationLockedError("crew-reviewer", str(ap_pid)).line + "\n" and ap_res[2] < 1.8, short(ap_res))
ck("a takeover folder left by a killed command is cleared after about 5 s", sp_res[0] and 4 <= sp_res[1] < 12 and not sp_res[2], short(sp_res))

# the takeover re-checks the holder: a lock another command took meanwhile is left alone
h = newhome("lock-recheck")
dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
ld = lock_dir(h)
ld.parent.mkdir(parents=True, exist_ok=True)
ld.mkdir()
(ld / "pid").write_text(f"{dead.pid}\n")
real_read_pid = locks._read_pid
seen = {"n": 0}


def second_taker(lock):
    seen["n"] += 1
    return str(dead.pid) if seen["n"] == 1 else str(os.getpid())


locks._read_pid = second_taker
try:
    r = py_lock(h)
finally:
    locks._read_pid = real_read_pid
ck("a stale lock a third command took before the takeover is left alone", r[0] == "locked" and (ld / "pid").read_text() == f"{dead.pid}\n" and ld.is_dir(), short(r[:2]))
ck("...and no .takeover folder is left", not Path(str(ld) + ".takeover").exists())

# a corrupt pid file is a dead holder, not a crash
def attempt(fn):
    """fn() or the exception it raised, so a broken fix shows as a FAIL line and not a crash."""
    try:
        return fn()
    except Exception as ex:
        return ex


ck("pid_alive says no to a number no process can have", attempt(lambda: locks.pid_alive(99999999999999999999)) is False)
h = newhome("lock-hugepid")
ld = lock_dir(h)
ld.parent.mkdir(parents=True, exist_ok=True)
ld.mkdir()
(ld / "pid").write_text("99999999999999999999\n")
r = attempt(lambda: py_lock(h))
ck("a lock whose pid file holds a huge number is stale and taken over", isinstance(r, tuple) and r[0] == "ok" and (ld / "pid").read_text() == f"{os.getpid()}\n", short(r))
locks.release_lock("crew-reviewer", CrewPaths.at(h))

# python-only lock behaviour
h = newhome("lock-py")
locks.station_lock("crew-releaser", CrewPaths.at(h))
ck("station_lock makes the stations folder when the crew home has none", lock_dir(h, "crew-releaser").is_dir())
ck("taking a lock this process holds again does nothing", locks.station_lock("crew-releaser", CrewPaths.at(h)) is None)
with locks.held_station_lock("crew-releaser", CrewPaths.at(h)):
    pass
ck("the context manager leaves a lock that was already held, held", lock_dir(h, "crew-releaser").is_dir())
locks.release_lock("crew-releaser", CrewPaths.at(h))
with locks.held_station_lock("crew-builder", CrewPaths.at(h)):
    inside = (lock_dir(h, "crew-builder") / "pid").read_text()
ck("the context manager holds the lock inside the block, with this pid", inside == f"{os.getpid()}\n")
ck("...and lets it go after", not lock_dir(h, "crew-builder").exists())
try:
    with locks.held_station_lock("crew-builder", CrewPaths.at(h)):
        raise RuntimeError("boom")
except RuntimeError:
    pass
ck("...and lets it go when the block raises", not lock_dir(h, "crew-builder").exists())
locks.station_lock("crew-a", CrewPaths.at(h))
locks.station_lock("crew-b", CrewPaths.at(h))
locks.release_locks()
ck("release_locks releases every lock this process holds", not lock_dir(h, "crew-a").exists() and not lock_dir(h, "crew-b").exists())
# at exit
code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from tares.crew import locks, CrewPaths; "
        f"locks.station_lock('crew-x', CrewPaths.at({str(h)!r})); print((CrewPaths.at({str(h)!r}).lock_dir('crew-x') / 'pid').read_text().strip())")
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
ck("a Python command that exits releases its lock (atexit)", r.returncode == 0 and r.stdout.strip().isdigit() and not lock_dir(h, "crew-x").exists(), short((r.stdout, r.stderr)))
# a forked child must not release its parent's lock
if hasattr(os, "fork"):
    locks.station_lock("crew-fork", CrewPaths.at(h))
    pid = os.fork()
    if pid == 0:
        locks.release_locks()
        os._exit(0)
    os.waitpid(pid, 0)
    ck("a forked child's release_locks leaves its parent's lock", lock_dir(h, "crew-fork").is_dir())
    locks.release_locks()

# liveness
ck("pid_alive: this process", locks.pid_alive(os.getpid()))
ck("pid_alive: a process that has ended", not locks.pid_alive(dead_pid()))
ck("pid_alive: 0 and negative numbers are never holders", not locks.pid_alive(0) and not locks.pid_alive(-5))
if os.name != "nt":
    ck("pid_alive: a process we may not signal (PermissionError) is alive", locks.pid_alive(1))
else:
    ck("pid_alive on Windows: own pid alive, ended pid not", locks._pid_alive_windows(os.getpid()) and not locks._pid_alive_windows(dead_pid()))
# On Windows os.kill(pid, 0) would end the process: the Windows branch must never reach it.
real_kill, real_win, real_is_win = os.kill, locks._pid_alive_windows, locks._is_windows
seen = []


def no_kill(pid, sig):
    raise AssertionError("os.kill called on the Windows path")


os.kill = no_kill
locks._pid_alive_windows = lambda pid: seen.append(pid) or True
locks._is_windows = lambda: True
try:
    ck("pid_alive on Windows asks the Windows API and never calls os.kill", locks.pid_alive(4242) is True and seen == [4242])
    ck("a holder pid is judged by the same path on Windows", locks._holder_alive("4242") and seen == [4242, 4242])
finally:
    os.kill, locks._pid_alive_windows, locks._is_windows = real_kill, real_win, real_is_win
src = Path(locks.__file__).read_text(encoding="utf-8")
ck("locks.py calls os.kill once, after the Windows branch has returned", len(re.findall(r"^\s+os\.kill\(", src, re.M)) == 1 and src.index("return _pid_alive_windows(pid)") < src.index("    os.kill(pid, 0)"))

# ====================================================================================
print("\n== a file with a byte order mark ==")
hb_ = newhome("bom")
Path(hb_).mkdir(parents=True)
bomfile = Path(hb_) / "bom.json"
bomfile.write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}\n')
ck("read_json reads a file that starts with a byte order mark", attempt(lambda: state.read_json(bomfile)) == {"a": 1}, short(attempt(lambda: state.read_json(bomfile))))
hb2 = newhome("bom-crew")
Path(hb2).mkdir(parents=True)
cfgp = CrewPaths.at(hb2).crew_cfg
cfgp.write_bytes(b"\xef\xbb\xbf" + b'{"human": {"name": "Ana"}}\n')
attempt(lambda: state.merge_crew_defaults(CrewPaths.at(hb2)))
ck("merge_crew_defaults reads a crew.json with a byte order mark and keeps the person's value", attempt(lambda: state.read_json(cfgp)["human"]["name"]) == "Ana", short(attempt(lambda: state.read_json(cfgp, None))))

print("\n== import ==")
probe = ("import sys, time\nsys.path.insert(0, %r)\nbefore = set(sys.modules)\nt = time.time()\nimport tares.crew\ndt = time.time() - t\n"
         "std = set(sys.stdlib_module_names) | set(sys.builtin_module_names) | {'__main__', '_virtualenv'}\n"
         "extra = sorted({m.split('.')[0] for m in set(sys.modules) - before} - std - {'tares'})\n"
         "heavy = [m for m in sys.modules if m.split('.')[0] in ('duckdb','fastapi','uvicorn','starlette','httpx','requests','psutil','pydantic','numpy','pandas','anthropic','openai','filelock') or m in ('tares.daemon','tares.cli','tares.store')]\n"
         "import json\nprint(json.dumps({'extra': extra, 'heavy': heavy, 'secs': dt, 'tares': sorted(m for m in sys.modules if m.startswith('tares'))}))\n") % str(ROOT)
r = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, cwd=str(T))
info = json.loads(r.stdout) if r.returncode == 0 else {}
ck("importing tares.crew works in a bare interpreter", r.returncode == 0, r.stderr[-400:])
ck("importing tares.crew pulls in no duckdb, fastapi, tares.daemon or other heavy module", info.get("heavy") == [], short(info.get("heavy")))
ck("...and nothing outside the standard library", info.get("extra") == [], short(info.get("extra")))
ck("...and only the tares.crew modules and tares itself", set(info.get("tares", [])) <= {"tares", "tares.crew", "tares.crew.errors", "tares.crew.paths", "tares.crew.state", "tares.crew.locks", "tares.crew.perms"},
   short(info.get("tares")))
ck("...in well under a second", info.get("secs", 99) < 1.0, str(info.get("secs")))
init_src = (ROOT / "tares" / "__init__.py").read_text(encoding="utf-8")
ck("tares/__init__.py itself imports nothing heavy", "import" not in init_src.replace('"""', ""), init_src[:200])
pkg = sorted(p.name for p in (ROOT / "tares" / "crew").iterdir() if p.suffix == ".py")
ck("the package has the modules the ticket names", {"__init__.py", "paths.py", "state.py", "locks.py"} <= set(pkg), str(pkg))
sv = ROOT / "tares" / "crew" / "schemas" / "schema_version.json"
ck("schemas/schema_version.json is shipped (TR-1064) and holds the schema version as JSON", sv.is_file() and json.loads(sv.read_text(encoding="utf-8")).get("schema_version") == 1, str(sv))
pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
ck("the wheel ships the whole tares package, so tares.crew with it", re.search(r'packages = \["tares"', pyproject) is not None)

# ====================================================================================
shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
