"""tares.crew reads and writes the crew's files exactly as the bash `claude-plugin/bin/factory` does.

The real script is the oracle: its functions are cut out of it by text (slugify, crew_name, event,
stations_put, merge_crew_defaults, station_lock, ...) into a small bash harness, run on a throwaway crew
home (a path with a space in it), and compared with the Python port: files bash wrote read by Python
and the other way round, byte for byte where bash would write the same thing; slugify and crew name
rules and error text; FACTORY_NOW; the per-station lock in both directions (a bash holder refuses
Python, a Python holder refuses bash, a stale lock is taken over either way, the "no pid yet" grace);
ensure_crew_home; and that importing tares.crew pulls in nothing but the standard library.

Without bash or jq the oracle cases are skipped with a message; the rest still runs.

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

FACTORY = ROOT / "claude-plugin" / "bin" / "factory"
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

HAVE_BASH = bool(shutil.which("bash") and shutil.which("jq"))
if os.name == "nt":
    HAVE_BASH = False

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


# ---------- the bash harness: functions cut out of the real script ----------

SRC = FACTORY.read_text(encoding="utf-8").split("\n")


def func_text(name):
    start = next((i for i, ln in enumerate(SRC) if ln.startswith(name + "() {")), None)
    assert start is not None, f"{name} not found in {FACTORY}"
    i = start
    while True:
        ln = SRC[i].rstrip()
        cont = i == start or SRC[i - 1].rstrip().endswith("\\")
        opens_with_comment = i == start and re.match(r"^\w+\(\) \{ #", ln)
        if cont and ln.endswith("}") and not ln.endswith("{") and not opens_with_comment:
            break
        if ln == "}":
            break
        i += 1
    return "\n".join(SRC[start:i + 1])


def assign_text(name):
    ln = next((ln for ln in SRC if ln.startswith(name + "=")), None)
    assert ln is not None, f"{name}= not found in {FACTORY}"
    return ln


ASSIGNS = ["CREW_HOME", "CREW_CFG", "STATIONS", "HALT_FILE", "FROZEN_KIT", "NEWER_FILE", "FREEZING",
           "CC_NEWER_FILE", "CHECKOUT_FILE", "EVENTS", "TOKEN_FILE", "CREW_NAME_RULE", "CREW_NAME_CACHE",
           "LOCKED", "PACE_FILE", "WAKE_DEFAULT"]
FUNCS = ["die", "say", "ccfg", "crew_name_ok", "crew_name", "crew_orchestrator", "slugify", "now_s", "event",
         "merge_crew_defaults", "ensure_crew_home", "require_crew", "stations_get", "stations_put",
         "station_lock", "release_locks", "pace_station_file", "pace_station_read", "pace_station_put",
         "wake_file", "wake_read", "wake_update", "crew_key", "cmd_halt", "cmd_resume"]

HARNESS = T / "harness.sh"
if HAVE_BASH:
    parts = ["set -euo pipefail", 'ME="tares factory"', f'TEMPLATES="{TEMPLATES}"']
    parts += [assign_text(a) for a in ASSIGNS]
    parts += [func_text(f) for f in FUNCS]
    parts += [
        'hold() { station_lock "$1"; echo held; exec sleep 120; }',
        'take() { station_lock "$1"; echo "$$ $(cat "$CREW_HOME/stations/$1.lock/pid")"; }',
        'show() { printf "%s" "$(stations_get "$@")"; }',
        'slug() { local x s; for x in "$@"; do s=$(slugify "$x"; printf X); printf "%s\\0" "${s%X}"; done; }',
        'names() { for x in "$@"; do if crew_name_ok "$x"; then printf "1"; else printf "0"; fi; done; }',
        'name() { crew_name; }',
        'echo_assigns() { printf "%s\\n" "$CREW_HOME" "$CREW_CFG" "$STATIONS" "$HALT_FILE" "$FROZEN_KIT" "$NEWER_FILE" '
        '"$FREEZING" "$CC_NEWER_FILE" "$CHECKOUT_FILE" "$EVENTS" "$TOKEN_FILE" "$PACE_FILE" "$(pace_station_file x)" '
        '"$(wake_file x)" "$CREW_HOME/stations/x.done" "$CREW_HOME/stations/x.lock" "$CREW_NAME_RULE"; }',
        'cat_assign() { printf "%s" "$WAKE_DEFAULT"; }',
        '"$@"',
    ]
    HARNESS.write_text("\n".join(parts) + "\n", encoding="utf-8")


def bash(home, *args, extra_env=None, timeout=60):
    """Run a harness function with the crew home `home`: (returncode, stdout bytes, stderr text)."""
    e = dict(os.environ)
    e["FACTORY_CREW_HOME"] = str(home)
    e.update(extra_env or {})
    r = subprocess.run(["bash", str(HARNESS), *args], env=e, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace")


def bash_ok(home, *args, **kw):
    rc, out, err = bash(home, *args, **kw)
    assert rc == 0, f"bash {args} failed ({rc}): {err}"
    return out


def read_bytes(p):
    return Path(p).read_bytes()


def skip_oracle(what):
    print(f"  skip {what}: bash or jq is missing")


# ====================================================================================
print("\n== names of the files ==")
if HAVE_BASH:
    h = newhome("names")
    got = bash_ok(h, "echo_assigns").decode("utf-8").split("\n")[:-1]
    cp = CrewPaths.at(h)
    want = [str(cp.home), str(cp.crew_cfg), str(cp.stations), str(cp.halt_file), str(cp.frozen_kit), str(cp.newer_file),
            str(cp.freezing), str(cp.cc_newer_file), str(cp.checkout_file), str(cp.events), str(cp.token_file),
            str(cp.pace_file), str(cp.pace_station_file("x")), str(cp.wake_file("x")), str(cp.done_file("x")),
            str(cp.lock_dir("x")), CREW_NAME_RULE]
    for g, w in zip(got, want):
        ck(f"path {Path(w).name or w[:20]} is the one bash uses", g == w, f"bash {g!r} python {w!r}")
    ck("every path bash names was compared", len(got) == len(want), f"{len(got)} vs {len(want)}")
    ck("WAKE_DEFAULT: python and bash default wake state agree",
       json.loads(bash_ok(h, "cat_assign")) == state.WAKE_DEFAULT)
    with env(HOME="/hm", FACTORY_CREW_HOME=None):
        ck("default crew home is $HOME/factory", CrewPaths.from_env().raw == "/hm/factory")
    with env(FACTORY_CREW_HOME=""):
        ck("an empty FACTORY_CREW_HOME counts as unset", CrewPaths.from_env().raw == f"{HOME}/factory")
else:
    skip_oracle("file names")

# ====================================================================================
print("\n== slugify ==")
SLUGS = ["", "Hello World", "  lead", "trail  ", "---a---", "a__b", "UPPER lower 123", "Ünïcödé", "日本語",
         "mixed 日本 text", "x/y\\z", "a\nb", "-", "!!!", "Kelvin \u212a sign", "İstanbul", "ß", "a'b\"c", "tab\there",
         "emoji \U0001F600 ok", "a-b-c", "--", "ABC-def_GHI.jkl", "  ", "0", "é", "CAPS", "\u00a0nbsp\u00a0",
         "Fix: the (big) bug #42!", "x" * 200, "a.b.c", "[brackets]", "ZZZ-ZZZ", "tilde~caret^", "\u2028line sep"]
if HAVE_BASH:
    # bash's tr lower-cases by the locale. Where it works on bytes (GNU tr, or LC_ALL=C) only A-Z change, which
    # is what Python does. macOS's tr in a UTF-8 locale also folds two non-ASCII letters to ASCII (the Kelvin
    # sign to k, a dotted capital I to i); those two inputs are left out of the default-locale run.
    LOCALE_FOLDED = {"Kelvin \u212a sign", "\u0130stanbul"}
    for loc in (None, "C"):
        e = {"LC_ALL": loc} if loc else {}
        out = bash_ok(newhome("slug"), "slug", *SLUGS, extra_env=e).decode("utf-8").split("\0")[:-1]
        bad = [(s, o, state.slugify(s) + "\n") for s, o in zip(SLUGS, out)
               if o != state.slugify(s) + "\n" and not (loc is None and s in LOCALE_FOLDED)]
        ck(f"slugify equals bash on {len(SLUGS)} inputs (locale {loc or 'default'})", len(out) == len(SLUGS) and not bad, short(bad))
else:
    skip_oracle("slugify")
ck("slugify has no trailing newline (bash callers get that from $(...))", state.slugify("A b") == "a-b")

# ====================================================================================
print("\n== crew name ==")
NAMES = ["crew", "a", "0", "my-crew", "Bad_Name", "-x", "UPPER", "a b", "x-build-y", "build", "a-build", "build-a",
         "a-b-build-c", "a--b", "-", "a" * 31, "a" * 32, "buildx", "xbuild", "ünï", "ab\n"]
if HAVE_BASH:
    flags = bash_ok(newhome("nameok"), "names", *NAMES).decode()
    mine = "".join("1" if state.crew_name_ok(n) else "0" for n in NAMES)
    ck("crew_name_ok equals bash on every name", flags == mine, f"bash {flags} python {mine}")


def name_case(label, cfg_text=None, env_name=None):
    """Compare crew_name (and the error text) with bash for one setup."""
    h = newhome("name")
    h.mkdir()
    if cfg_text is not None:
        (h / "crew.json").write_text(cfg_text, encoding="utf-8")
    e = {"FACTORY_CREW_NAME": env_name} if env_name is not None else {}
    rc, out, err = bash(h, "name", extra_env=e)
    with env(FACTORY_CREW_NAME=env_name):
        try:
            py = ("ok", state.crew_name(CrewPaths.at(h)))
        except CrewError as ex:
            py = ("err", ex.line + "\n")
    ck(f"crew name: {label}", (py == ("ok", out.decode().rstrip("\n"))) if rc == 0 else (py == ("err", err)),
       f"bash rc={rc} out={out!r} err={err!r}; python {py!r}")


if HAVE_BASH:
    for n in NAMES:
        name_case(f"env {n!r}", env_name=n)
    name_case("no env, no file")
    name_case("empty env falls to the file", '{"crew_name": "fromfile"}', "")
    name_case("env wins over the file", '{"crew_name": "fromfile"}', "fromenv")
    name_case("file name", '{"crew_name": "fromfile"}')
    name_case("bad file name names the file", '{"crew_name": "Bad Name"}')
    name_case("number as the file name", '{"crew_name": 5}')
    name_case("true as the file name", '{"crew_name": true}')
    name_case("null file name", '{"crew_name": null}')
    name_case("empty file name", '{"crew_name": ""}')
    name_case("file that is not JSON", "{nope")
    name_case("nested crew_name is not read", '{"x": {"crew_name": "no"}}')
    name_case("unicode file name is refused", '{"crew_name": "ünï"}')
    rc, out, err = bash(newhome("orch"), "crew_orchestrator", extra_env={"FACTORY_CREW_NAME": "acme"})
    with env(FACTORY_CREW_NAME="acme"):
        ck("crew_orchestrator equals bash", out.decode().strip() == state.crew_orchestrator(CrewPaths.at(newhome("x"))))
else:
    skip_oracle("crew name")
ck("the rule text is the one in the script", CREW_NAME_RULE in "\n".join(SRC))

# ====================================================================================
print("\n== FACTORY_NOW ==")
with env(FACTORY_NOW="1700000000"):
    ck("now_s honours FACTORY_NOW", state.now_s() == 1700000000)
    if HAVE_BASH:
        ck("bash gives the same", bash_ok(newhome("now"), "now_s", extra_env={"FACTORY_NOW": "1700000000"}).decode().strip() == "1700000000")
with env(FACTORY_NOW=None):
    ck("without it, the clock", abs(state.now_s() - time.time()) < 3)
with env(FACTORY_NOW=""):
    ck("an empty FACTORY_NOW is the clock, as bash's :- has it", abs(state.now_s() - time.time()) < 3)
with env(FACTORY_NOW="soon"):
    try:
        state.now_s()
        ck("a FACTORY_NOW that is not a number is refused", False)
    except CrewError as ex:
        ck("a FACTORY_NOW that is not a number is refused", "FACTORY_NOW" in str(ex))
ck("utc_stamp is `now | todate`", state.utc_stamp(1700000000) == "2023-11-14T22:13:20Z")
if HAVE_BASH:
    out = subprocess.run(["jq", "-n", "-r", "1700000000 | todate"], capture_output=True, text=True).stdout.strip()
    ck("utc_stamp equals jq's todate", out == state.utc_stamp(1700000000), out)

# ====================================================================================
print("\n== ensure_crew_home, merge_crew_defaults ==")
if HAVE_BASH:
    for label, e in (("git name, gh login", {}),
                     ("gh fails", {"FAKE_GH": "fail"}),
                     ("no git name", {"GIT_CONFIG_GLOBAL": str(EMPTYGIT)})):
        hb, hp = newhome("ensure-bash"), newhome("ensure-py")
        bash_ok(hb, "ensure_crew_home", extra_env=e)
        with env(**e):
            state.ensure_crew_home(CrewPaths.at(hp))
        ck(f"ensure_crew_home ({label}): same folders", all((hp / d).is_dir() and (hb / d).is_dir() for d in ("stations", "reports", "worktrees")))
        ck(f"ensure_crew_home ({label}): crew.json is byte for byte bash's",
           read_bytes(hb / "crew.json") == read_bytes(hp / "crew.json"), short((read_bytes(hb / "crew.json"), read_bytes(hp / "crew.json"))))
    cfg = json.loads(read_bytes(hp / "crew.json"))
    ck("ensure_crew_home: no git name gives \"the user\"", cfg["human"]["name"] == "the user" and cfg["authorized"]["by"] == "the user", short(cfg))
    hb, hp = newhome("ens-b"), newhome("ens-p")
    bash_ok(hb, "ensure_crew_home")
    raw = read_bytes(hb / "crew.json")
    ck("crew.json: non-ASCII is written as is, not escaped", "Zoë Ørsted 名前".encode() in raw and b"\\u" not in raw)
    ck("crew.json: LF only, ends with one newline", b"\r" not in raw and raw.endswith(b"}\n") and not raw.endswith(b"\n\n"))
    ck("Python reads the file bash wrote", state.ccfg("human", "name", paths=CrewPaths.at(hb)) == "Zoë Ørsted 名前"
       and state.ccfg("human", "github", paths=CrewPaths.at(hb)) == "zoe-gh"
       and state.ccfg("stations", "orchestrator", "model", paths=CrewPaths.at(hb)) == "opus")
    state.ensure_crew_home(CrewPaths.at(hp))
    rc, out, _ = bash(hp, "ccfg", ".release.environments[0].name")
    ck("bash reads a Python crew.json (ccfg)", out.decode().strip() == "kind", short(out))
    for hh in (newhome("req"),):
        rc, _, err = bash(hh, "require_crew")
        try:
            state.require_crew(CrewPaths.at(hh))
            msg = None
        except CrewError as ex:
            msg = ex.line + "\n"
        ck("require_crew: same refusal text as bash", rc == 1 and msg == err, f"bash {err!r} python {msg!r}")

    # merge_crew_defaults: hand-written files get the template's defaults under them
    HAND = [
        ("keys in another order, an extra key, a nested override",
         '{"zzz": [1, {"a": null}], "human": {"github": "g", "name": "n é"}, "stations": {"builder": {"model": "haiku"}, "extra": 1}, "crew_name": "acme"}'),
        ("null replaces a default object", '{"review": null, "tares": {"url": "http://x y"}}'),
        ("a scalar replaces an object", '{"limits": 5, "release": {"environments": []}}'),
        ("already complete", None),
        ("empty object", "{}"),
        ("compact one-line file", '{"human":{"name":"A"}}'),
        ("CRLF line endings", '{\r\n  "human": {"name": "A"}\r\n}\r\n'),
        ("DEL and escapes", '{"human": {"name": "a\\u007fb\\n\\"q\\" \\ud83d\\ude00 \\u00e9"}}'),
    ]
    for label, text in HAND:
        hb, hp = newhome("merge-b"), newhome("merge-p")
        for h in (hb, hp):
            h.mkdir()
        if text is None:
            bash_ok(hb, "ensure_crew_home")
            text = read_bytes(hb / "crew.json").decode()
            hb2 = newhome("merge-b2")
            hb2.mkdir()
            hb = hb2
        for h in (hb, hp):
            (h / "crew.json").write_bytes(text.encode("utf-8"))
        bash_ok(hb, "merge_crew_defaults")
        state.merge_crew_defaults(CrewPaths.at(hp))
        ck(f"merge defaults ({label}): same bytes as bash", read_bytes(hb / "crew.json") == read_bytes(hp / "crew.json"),
           short((read_bytes(hb / "crew.json"), read_bytes(hp / "crew.json"))))
    # a file already merged is left alone: same inode and time
    hp = newhome("merge-same")
    state.ensure_crew_home(CrewPaths.at(hp))
    before = os.stat(hp / "crew.json")
    time.sleep(0.05)
    state.merge_crew_defaults(CrewPaths.at(hp))
    after = os.stat(hp / "crew.json")
    ck("merge defaults: an unchanged crew.json is not rewritten", (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns))
    hb = newhome("merge-same-b")
    bash_ok(hb, "ensure_crew_home")
    before = os.stat(hb / "crew.json")
    bash_ok(hb, "merge_crew_defaults")
    ck("(bash does the same)", os.stat(hb / "crew.json").st_ino == before.st_ino)
    # a missing crew.json is skipped; invalid JSON is refused with the same text
    hm = newhome("merge-none")
    hm.mkdir()
    state.merge_crew_defaults(CrewPaths.at(hm))
    ck("merge defaults: no crew.json, nothing happens", not (hm / "crew.json").exists())
    for label, text in (("not JSON", "{nope"), ("empty file", ""), ("an array", "[]"), ("null", "null"), ("a string", '"x"')):
        hb, hp = newhome("bad-b"), newhome("bad-p")
        for h in (hb, hp):
            h.mkdir()
            (h / "crew.json").write_text(text)
        rc, _, err = bash(hb, "merge_crew_defaults")
        err = err.strip().split("\n")[-1].replace(str(hb), "HOME")   # the line before it is jq's own complaint
        try:
            state.merge_crew_defaults(CrewPaths.at(hp))
            msg = None
        except CrewError as ex:
            msg = ex.line.replace(str(hp), "HOME")
        ck(f"merge defaults ({label}): refused with bash's text", rc != 0 and msg == err, f"bash {err!r} python {msg!r}")
        ck(f"merge defaults ({label}): the file is left as it was", (hp / "crew.json").read_text() == text)
else:
    skip_oracle("ensure_crew_home and merge_crew_defaults")

# ====================================================================================
print("\n== stations.json ==")
PUTS = [("crew-orchestrator", {"role": "orchestrator", "id": "x1", "cwd": "/p q/r"}),
        ("crew-orchestrator", {"id": "x2", "new": 1}),
        ("b", {"name": "é\u007f\"\\\n\t ü 日本", "empty": "", "e": {}, "l": []}),
        ("crew-orchestrator", {"nested": {"k": [1, 2, {"z": None}], "s": "x"}, "t": True, "f": False, "n": None, "num": 3, "neg": -7}),
        ("crew-orchestrator", {"role": "reviewer"}),
        ("c d", {"k": "space in the station name"})]
GETS = [("crew-orchestrator", ""), ("crew-orchestrator", ".role"), ("crew-orchestrator", ".id"), ("crew-orchestrator", ".nested.k"),
        ("crew-orchestrator", ".nested.s"), ("crew-orchestrator", ".t"), ("crew-orchestrator", ".f"), ("crew-orchestrator", ".n"),
        ("crew-orchestrator", ".num"), ("crew-orchestrator", ".role.deep"), ("crew-orchestrator", ".missing"),
        ("b", ".name"), ("b", ".empty"), ("b", ".e"), ("b", ".l"), ("zz", ".x"), ("zz", ""), ("c d", ".k")]


def keys_of(path):
    return tuple(k for k in path.split(".") if k)


if HAVE_BASH:
    hb, hp = newhome("st-b"), newhome("st-p")
    for h in (hb, hp):
        h.mkdir()
    ck("stations_get on a crew home with no stations.json is empty in both",
       bash_ok(hb, "show", "x", ".id") == b"" and state.stations_get("x", "id", paths=CrewPaths.at(hb)) == "")
    for i, (s, v) in enumerate(PUTS):
        bash_ok(hb, "stations_put", s, json.dumps(v))
        state.stations_put(s, v, CrewPaths.at(hp))
        ck(f"stations_put #{i} ({s}): same bytes as bash", read_bytes(hb / "stations.json") == read_bytes(hp / "stations.json"),
           short((read_bytes(hb / "stations.json"), read_bytes(hp / "stations.json"))))
    raw = read_bytes(hp / "stations.json")
    ck("stations.json: non-ASCII raw, DEL escaped, LF, one trailing newline",
       "日本".encode() in raw and b"\\u007f" in raw and b"\r" not in raw and raw.endswith(b"}\n"))
    bad = []
    for s, p in GETS:
        b = bash_ok(hb, "show", s, p).decode()
        py = state.stations_get(s, *keys_of(p), paths=CrewPaths.at(hb))
        if b != py:
            bad.append((s, p, b, py))
    ck(f"stations_get equals bash on {len(GETS)} fields of a file bash wrote", not bad, short(bad))
    bad = [(s, p) for s, p in GETS if bash_ok(hp, "show", s, p).decode() != state.stations_get(s, *keys_of(p), paths=CrewPaths.at(hp))]
    ck("stations_get equals bash on the file Python wrote", not bad, short(bad))
    # each side puts onto the file the other wrote, alternating, starting with either
    hm1, hm2 = newhome("mix1"), newhome("mix2")
    for h in (hm1, hm2):
        h.mkdir()
    for i, (s, v) in enumerate(PUTS):
        (bash_ok(hm1, "stations_put", s, json.dumps(v)) if i % 2 == 0 else state.stations_put(s, v, CrewPaths.at(hm1)))
        (state.stations_put(s, v, CrewPaths.at(hm2)) if i % 2 == 0 else bash_ok(hm2, "stations_put", s, json.dumps(v)))
    ck("puts alternating between bash and Python (either start) end in the bytes of all-bash",
       read_bytes(hm1 / "stations.json") == read_bytes(hm2 / "stations.json") == read_bytes(hb / "stations.json"))
    # stations_put on something that is not an object: both refuse and leave the file alone
    hn = newhome("st-bad")
    hn.mkdir()
    (hn / "stations.json").write_text('{"a": "text"}\n')
    rc, _, _ = bash(hn, "stations_put", "a", '{"k":1}')
    try:
        state.stations_put("a", {"k": 1}, CrewPaths.at(hn))
        py_refused = False
    except CrewError:
        py_refused = True
    ck("stations_put onto a non-object: both refuse", rc != 0 and py_refused)
    ck("...and leave the file alone", (hn / "stations.json").read_text() == '{"a": "text"}\n')
    (hn / "stations.json").write_text("{nope")
    ck("stations_get on a broken stations.json is empty in both",
       bash_ok(hn, "show", "a", ".k") == b"" and state.stations_get("a", "k", paths=CrewPaths.at(hn)) == "")
else:
    skip_oracle("stations.json")

# ====================================================================================
print("\n== events.jsonl ==")
EVENTS = [("start", "crew-orchestrator", "detail"), ("resume", "", ""), ("halt", "", "by the \"human\""),
          ("odd", "s\\t", "line\nbreak and \ttab"), ("uni", "stäti\u00f6n", "日本語 \U0001F600 é"), ("del", "x", "a\u007fb"),
          ("ctl", "x", "\u0001\u001f"), ("slash", "a/b", "</script> & <b>")]
AT = re.compile(r'^\{"at":"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",(.*)\}$')
if HAVE_BASH:
    hb, hp = newhome("ev-b"), newhome("ev-p")
    for h in (hb, hp):
        h.mkdir()
    for e, s, d in EVENTS:
        if d == "" and s == "":
            bash_ok(hb, "event", e)
        else:
            bash_ok(hb, "event", e, s, d)
        state.event(e, s, d, paths=CrewPaths.at(hp))
    lb = read_bytes(hb / "events.jsonl").decode("utf-8").split("\n")
    lp = read_bytes(hp / "events.jsonl").decode("utf-8").split("\n")
    ck("events: same number of lines", len(lb) == len(lp) == len(EVENTS) + 1 and lb[-1] == "" == lp[-1], f"{len(lb)} {len(lp)}")
    bad = []
    for b, p in zip(lb[:-1], lp[:-1]):
        mb, mp = AT.match(b), AT.match(p)
        if not (mb and mp and mb.group(1) == mp.group(1)):
            bad.append((b, p))
    ck("events: each line equals bash's except the time (compact, key order at, station, event, detail)", not bad, short(bad))
    ck("events: Python's time is a UTC stamp with the Z", all(AT.match(x) for x in lp[:-1]))
    jb = subprocess.run(["jq", "-c", "."], input=read_bytes(hp / "events.jsonl"), capture_output=True).stdout
    ck("bash's jq reads Python's events.jsonl and prints it back identically", jb == read_bytes(hp / "events.jsonl"))
    ck("Python parses the lines bash wrote", [json.loads(x)["event"] for x in lb[:-1]] == [e[0] for e in EVENTS])
    # a crew home that does not exist: nothing is created, nothing fails
    gone = newhome("ev-gone")
    bash_ok(gone, "event", "x")
    state.event("x", paths=CrewPaths.at(gone))
    ck("events: no crew home, no file and no folder made (bash and Python)", not gone.exists())
    # a crew home that cannot be written to never fails the caller
    ro = newhome("ev-ro")
    ro.mkdir()
    (ro / "events.jsonl").mkdir()   # a folder where the file should be
    try:
        state.event("x", paths=CrewPaths.at(ro))
        ck("events: an unwritable events.jsonl does not raise", True)
    except Exception as ex:
        ck("events: an unwritable events.jsonl does not raise", False, repr(ex))
    # many writers at once (threads and bash) never mix a line
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
    for _ in range(25):
        bash_ok(hc, "event", "b", "bash", "line")
    for t in ths:
        t.join()
    lines = read_bytes(hc / "events.jsonl").decode().split("\n")[:-1]
    ok = True
    for ln in lines:
        try:
            json.loads(ln)
        except ValueError:
            ok = False
    ck("events: 6 threads x 60 lines plus 25 from bash, every line whole", ok and len(lines) == 6 * 60 + 25, f"{len(lines)} lines, parse ok={ok}")
else:
    skip_oracle("events.jsonl")

# ====================================================================================
print("\n== HALT, pace, wake, token ==")
if HAVE_BASH:
    hb, hp = newhome("halt-b"), newhome("halt-p")
    for h in (hb, hp):
        h.mkdir()
        (h / "crew.json").write_text("{}\n")
    HALT = re.compile(r'^\{"at":"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",(.*)\}\n$')
    for reason in ("why é \"q\"", None):
        args = ["cmd_halt"] + ([reason] if reason is not None else [])
        rc, _, err = bash(hb, *args)
        assert rc == 0, err
        state.halt_write(reason if reason is not None else "halted by the human", paths=CrewPaths.at(hp)) if reason is not None else state.halt_write(paths=CrewPaths.at(hp))
        mb, mp = HALT.match(read_bytes(hb / "HALT").decode()), HALT.match(read_bytes(hp / "HALT").decode())
        ck(f"HALT ({reason!r}): same text as bash except the time", bool(mb and mp and mb.group(1) == mp.group(1)),
           short((read_bytes(hb / "HALT"), read_bytes(hp / "HALT"))))
        ck(f"HALT ({reason!r}): Python reads bash's, and reports it", state.halted(CrewPaths.at(hb)) and state.halt_reason(CrewPaths.at(hb)) == (reason or "halted by the human"))
        jr = subprocess.run(["jq", "-r", ".reason", str(hp / "HALT")], capture_output=True, text=True, encoding="utf-8").stdout.strip()
        ck(f"HALT ({reason!r}): bash's jq reads Python's reason", jr == (reason or "halted by the human"), jr)
    ck("halt event: bash's line and Python's equivalent have the same shape",
       json.loads(read_bytes(hb / "events.jsonl").decode().split("\n")[0])["event"] == "halt")
    bash_ok(hb, "cmd_resume")
    state.resume(CrewPaths.at(hp))
    ck("resume removes HALT in both, and Python's twice does not fail", not (hb / "HALT").exists() and not (hp / "HALT").exists()
       and state.resume(CrewPaths.at(hp)) is None and not state.halted(CrewPaths.at(hp)))

    # pace
    hb, hp = newhome("pace-b"), newhome("pace-p")
    for h in (hb, hp):
        (h / "stations").mkdir(parents=True)
    for s, st, hd in (("crew-builder", True, False), ("crew-builder", False, True), ("crew-reviewer", True, True)):
        bash_ok(hb, "pace_station_put", s, str(st).lower(), str(hd).lower())
        state.pace_station_put(s, st, hd, CrewPaths.at(hp))
        ck(f"pace put {s} stretched={st} held={hd}: same bytes", read_bytes(hb / "stations" / f"{s}.pace") == read_bytes(hp / "stations" / f"{s}.pace"),
           short((read_bytes(hb / "stations" / f"{s}.pace"), read_bytes(hp / "stations" / f"{s}.pace"))))
    bash_ok(hb, "pace_station_put", "crew-builder", "false", "false")
    state.pace_station_put("crew-builder", False, False, CrewPaths.at(hp))
    ck("pace put false false removes the file in both", not (hb / "stations/crew-builder.pace").exists() and not (hp / "stations/crew-builder.pace").exists())
    state.pace_station_put("crew-builder", False, False, CrewPaths.at(hp))
    for label, text in (("missing", None), ("partial", '{"held":true}'), ("extra key", '{"stretched":true,"held":false,"x":[1]}'),
                        ("not JSON", "{nope"), ("an array", "[1]"), ("empty", "")):
        for h in (hb, hp):
            f = h / "stations" / "s.pace"
            f.unlink(missing_ok=True)
            if text is not None:
                f.write_text(text)
        rc, out, _ = bash(hb, "pace_station_read", "s")
        py = state.pace_station_read("s", CrewPaths.at(hb))
        if label == "an array":
            # jq fails on `{...} + []` and bash prints nothing; Python answers with the defaults
            ck(f"pace read ({label}): bash prints nothing, Python the defaults", out == b"" and py == state.PACE_DEFAULT, f"bash {out!r} python {py!r}")
        else:
            ck(f"pace read ({label}): equals bash, text and key order", out.decode().strip() == state.dumps_compact(py), f"bash {out!r} python {py!r}")

    # wake
    hb, hp = newhome("wake-b"), newhome("wake-p")
    for h in (hb, hp):
        (h / "stations").mkdir(parents=True)
    bash_ok(hb, "wake_update", "crew-reviewer", '.n = 3 | .seen = ["a","é 日本"] | .clock = $now | .released = true', "--argjson", "now", "1700000000")
    w = state.wake_read("crew-reviewer", CrewPaths.at(hp))
    w.update(n=3, seen=["a", "é 日本"], clock=1700000000, released=True)
    state.wake_write("crew-reviewer", w, CrewPaths.at(hp))
    ck("wake write: same bytes as bash's wake_update", read_bytes(hb / "stations/crew-reviewer.wake") == read_bytes(hp / "stations/crew-reviewer.wake"),
       short((read_bytes(hb / "stations/crew-reviewer.wake"), read_bytes(hp / "stations/crew-reviewer.wake"))))
    ck("wake read: Python reads bash's file", state.wake_read("crew-reviewer", CrewPaths.at(hb))["seen"] == ["a", "é 日本"])
    bash_ok(hp, "wake_update", "crew-reviewer", ".n += 1")
    ck("wake read: bash updates Python's file", state.wake_read("crew-reviewer", CrewPaths.at(hp))["n"] == 4)
    for label, text in (("missing", None), ("partial", '{"n":9}'), ("extra key", '{"zz":1,"n":2}'), ("not JSON", "{nope"), ("an array", "[]")):
        f = hb / "stations" / "w.wake"
        f.unlink(missing_ok=True)
        if text is not None:
            f.write_text(text)
        rc, out, _ = bash(hb, "wake_read", "w")
        py = state.wake_read("w", CrewPaths.at(hb))
        if label == "an array":
            ck(f"wake read ({label}): bash prints nothing, Python the defaults", out == b"" and py == state.WAKE_DEFAULT, f"bash {out!r} python {py!r}")
        else:
            ck(f"wake read ({label}): equals bash, text and key order", out.decode().strip() == state.dumps_compact(py),
               f"bash {out!r} python {state.dumps_compact(py)!r}")

    # the crew's key
    hb = newhome("tok")
    hb.mkdir()
    tok = hb / ".tares-token"
    tok.write_text("  abc\tdef \n\nghi\n")
    ck("crew_key: whitespace removed, as bash's tr -d", state.crew_key(CrewPaths.at(hb)) == bash_ok(hb, "crew_key").decode().strip() == "abcdefghi")
    ck("crew_key: TARES_FACTORY_TOKEN_FILE wins", state.crew_key(CrewPaths.at(hb), {"TARES_FACTORY_TOKEN_FILE": str(T / "none")}) == ""
       and bash_ok(hb, "crew_key", extra_env={"TARES_FACTORY_TOKEN_FILE": str(T / "none")}).decode().strip() == "")
    state.write_token("s3cret-é", CrewPaths.at(hb))
    ck("write_token: the token and a newline", read_bytes(tok) == "s3cret-é\n".encode())
    if os.name != "nt":
        ck("write_token: readable by the person only (0600)", (os.stat(tok).st_mode & 0o777) == 0o600, oct(os.stat(tok).st_mode))
    ck("bash reads the token Python wrote", bash_ok(hb, "crew_key").decode().strip() == "s3cret-é")
else:
    skip_oracle("HALT, pace, wake, token")

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
        except FileNotFoundError:
            pass
        except ValueError as ex:
            torn.append(ex)


state.atomic_write_text(f, big + "\n")
rt = threading.Thread(target=reader)
rt.start()
for i in range(60):
    state.atomic_write_text(f, (big if i % 2 else '{"small": 1}') + "\n")
stop.set()
rt.join()
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


def start_holder(kind, h, s="crew-reviewer"):
    """A live process that holds the station's lock: kind "bash" or "python". Returns the Popen."""
    e = {**os.environ, "FACTORY_CREW_HOME": str(h)}
    if kind == "bash":
        p = subprocess.Popen(["bash", str(HARNESS), "hold", s], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    else:
        code = (f"import sys, time; sys.path.insert(0, {str(ROOT)!r}); from tares.crew import locks, CrewPaths; "
                f"locks.station_lock({s!r}, CrewPaths.at({str(h)!r})); print('held', flush=True); time.sleep(120)")
        p = subprocess.Popen([sys.executable, "-c", code], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    line = p.stdout.readline().decode().strip()
    assert line == "held", f"{kind} holder did not take the lock: {line!r} {p.stderr.read(500)!r}"
    return p


def kill(p):
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
for kind in ("bash", "python") if HAVE_BASH else ("python",):
    h = newhome(f"lock-{kind}-holder")
    holder = start_holder(kind, h)
    pidfile = (lock_dir(h) / "pid").read_text()
    ck(f"{kind} holder writes its own pid and a newline into <lock>/pid", pidfile == f"{holder.pid}\n", repr(pidfile))
    r = py_lock(h)
    ck(f"a {kind}-held lock refuses Python with the typed error and bash's text",
       r[0] == "locked" and r[1] == MSG.format(pid=holder.pid, s="crew-reviewer") and r[3].holder == str(holder.pid) and isinstance(r[3], CrewError), short(r))
    ck(f"...at once, without waiting", r[2] < 1.5, f"{r[2]:.2f}s")
    ck(f"...and the line carries the tares factory: prefix", r[3].line == "tares factory: " + MSG.format(pid=holder.pid, s="crew-reviewer"))
    if HAVE_BASH:
        rc, _, err = bash(h, "station_lock", "crew-reviewer")
        ck(f"a {kind}-held lock refuses bash with the same text Python raises", rc == 1 and err == r[3].line + "\n", f"rc={rc} {err!r}")
    ck(f"the refused commands left the {kind} holder's lock alone", (lock_dir(h) / "pid").read_text() == pidfile)
    kill(holder)
    # the holder died without releasing: a stale lock; Python takes it over
    r = py_lock(h)
    ck(f"a stale lock left by a killed {kind} holder is taken over by Python", r[0] == "ok" and (lock_dir(h) / "pid").read_text() == f"{os.getpid()}\n", short(r))
    ck("...and no .takeover folder is left", not Path(str(lock_dir(h)) + ".takeover").exists())
    if HAVE_BASH:
        rc, _, err = bash(h, "station_lock", "crew-reviewer")
        ck("a lock Python holds refuses bash with the same text, naming Python's pid",
           rc == 1 and err == StationLockedError("crew-reviewer", str(os.getpid())).line + "\n", f"rc={rc} {err!r}")
    locks.release_lock("crew-reviewer", CrewPaths.at(h))
    ck("release_lock removes the pid file and the folder", not lock_dir(h).exists())
    if HAVE_BASH:
        out = bash_ok(h, "take", "crew-reviewer").decode().split()
        ck("after release bash takes the lock, with its own pid in the file", len(out) == 2 and out[0] == out[1], str(out))
        ck("bash released its lock when it exited", not lock_dir(h).exists())

if HAVE_BASH:
    # stale lock by bash's rule, taken over by bash (dead pid)
    h = newhome("lock-stale-bash")
    lock_dir(h).mkdir(parents=True)
    (lock_dir(h) / "pid").write_text(f"{dead_pid()}\n")
    out = bash_ok(h, "take", "crew-reviewer").decode().split()
    ck("bash takes over a stale lock (dead pid)", len(out) == 2 and out[0] == out[1], str(out))
    h = newhome("lock-stale-py")
    lock_dir(h).mkdir(parents=True)
    (lock_dir(h) / "pid").write_text(f"{dead_pid()}\n")
    r = py_lock(h)
    ck("Python takes over a stale lock (dead pid)", r[0] == "ok" and (lock_dir(h) / "pid").read_text() == f"{os.getpid()}\n", short(r))
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
    def grace_bash():
        h = newhome("grace-bash")
        lock_dir(h).mkdir(parents=True)
        t0 = time.time()
        out = bash_ok(h, "take", "crew-reviewer").decode().split()
        return ("ok", time.time() - t0) if len(out) == 2 else ("bad", out)

    def grace_py():
        h = newhome("grace-py")
        lock_dir(h).mkdir(parents=True)
        r = py_lock(h)
        locks.release_lock("crew-reviewer", CrewPaths.at(h))
        return r

    # a pid that shows up during the grace: a live holder after all
    def appears(kind):
        h = newhome(f"appear-{kind}")
        lock_dir(h).mkdir(parents=True)
        sleeper = subprocess.Popen(["sleep", "60"])

        def later():
            time.sleep(0.5)
            (lock_dir(h) / "pid").write_text(f"{sleeper.pid}\n")

        th = threading.Thread(target=later)
        th.start()
        t0 = time.time()
        if kind == "bash":
            rc, _, err = bash(h, "station_lock", "crew-reviewer")
            res = (rc, err, time.time() - t0)
        else:
            r = py_lock(h)
            res = (1 if r[0] == "locked" else 0, r[3].line + "\n" if r[0] == "locked" else "", time.time() - t0)
        th.join()
        sleeper.kill()
        sleeper.wait()
        return res, sleeper.pid

    # a takeover folder nobody removes (a killed command): cleared after 25 x 0.2 s
    def stuck_takeover(kind):
        h = newhome(f"stuck-{kind}")
        lock_dir(h).mkdir(parents=True)
        (lock_dir(h) / "pid").write_text(f"{dead_pid()}\n")
        Path(str(lock_dir(h)) + ".takeover").mkdir()
        t0 = time.time()
        if kind == "bash":
            out = bash_ok(h, "take", "crew-reviewer").decode().split()
            ok = len(out) == 2
        else:
            r = py_lock(h)
            ok = r[0] == "ok"
            locks.release_lock("crew-reviewer", CrewPaths.at(h))
        return ok, time.time() - t0, Path(str(lock_dir(h)) + ".takeover").exists()

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        fb, fp = ex.submit(grace_bash), ex.submit(grace_py)
        ab, ap = ex.submit(appears, "bash"), ex.submit(appears, "python")
        sb, sp = ex.submit(stuck_takeover, "bash"), ex.submit(stuck_takeover, "python")
        rb, rp = fb.result(), fp.result()
        (ab_res, ab_pid), (ap_res, ap_pid) = ab.result(), ap.result()
        sb_res, sp_res = sb.result(), sp.result()
    ck("no pid yet: bash waits about 2 s, then takes the lock", rb[0] == "ok" and 1.6 <= rb[1] < 8, short(rb))
    ck("no pid yet: Python waits as long as bash did, then takes the lock", rp[0] == "ok" and 1.6 <= rp[1] < 8, short(rp[:2]))
    ck("a pid that appears during the wait is respected: bash refuses, early", ab_res[0] == 1 and "pid " + str(ab_pid) in ab_res[1] and ab_res[2] < 1.8, short(ab_res))
    ck("a pid that appears during the wait is respected: Python refuses with the same text, early",
       ap_res[0] == 1 and ap_res[1] == StationLockedError("crew-reviewer", str(ap_pid)).line + "\n" and ap_res[2] < 1.8, short(ap_res))
    ck("a takeover folder left by a killed command: bash clears it after about 5 s", sb_res[0] and 4 <= sb_res[1] < 12 and not sb_res[2], short(sb_res))
    ck("a takeover folder left by a killed command: Python waits as long and clears it", sp_res[0] and 4 <= sp_res[1] < 12 and not sp_res[2], short(sp_res))
else:
    skip_oracle("lock timing against bash")

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
src = Path(locks.__file__).read_text()
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
probe = ("import sys, time\nsys.path.insert(0, %r)\nt = time.time()\nimport tares.crew\ndt = time.time() - t\n"
         "std = set(sys.stdlib_module_names) | set(sys.builtin_module_names) | {'__main__', '_virtualenv'}\n"
         "extra = sorted({m.split('.')[0] for m in sys.modules} - std - {'tares'})\n"
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
init_src = (ROOT / "tares" / "__init__.py").read_text()
ck("tares/__init__.py itself imports nothing heavy", "import" not in init_src.replace('"""', ""), init_src[:200])
pkg = sorted(p.name for p in (ROOT / "tares" / "crew").iterdir() if p.suffix == ".py")
ck("the package has the modules the ticket names", {"__init__.py", "paths.py", "state.py", "locks.py"} <= set(pkg), str(pkg))
sv = ROOT / "tares" / "crew" / "schemas" / "schema_version.json"
ck("schemas/schema_version.json is shipped (TR-1064) and holds the schema version as JSON", sv.is_file() and json.loads(sv.read_text()).get("schema_version") == 1, str(sv))
pyproject = (ROOT / "pyproject.toml").read_text()
ck("the wheel ships the whole tares package, so tares.crew with it", re.search(r'packages = \["tares"', pyproject) is not None)

# ====================================================================================
shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
