"""The kit and its freeze in Python (tares/crew/kit.py, TR-965): `crew freeze`, `crew unfreeze` and the kit part of
the watch's round.

Each check runs in a throwaway crew home with a small kit of its own (TARES_PLUGIN_DIR), the Python fake Claude Code
(tests/factory/fake_claude.py) and a fake Tares that keeps what a PUT of the settings saves. The restart of a station
is replaced by a recorder, and so is the watch's restart of itself, so nothing here starts a session or signals a
process. The bash and the Python run side by side in the bash suite tests/factory/freeze_test.sh and the parity harness.

The test runs in a fresh HOME with no keyring: nothing of the person's is read or written.

Run: .venv/bin/python tests/test_crew_kit.py
"""
import contextlib
import http.server
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
FACTORY_DIR = HERE / "factory"

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-kit-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
CREW = SCRATCH / "crew"
STATE = SCRATCH / "claude"
KITDIR = SCRATCH / "checkout" / "claude-plugin"
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"),
    FACTORY_PAUSE="0", FACTORY_NO_WATCH="1", FACTORY_CREW_HOME=str(CREW), FAKE_CLAUDE_STATE=str(STATE),
    TARES_PLUGIN_DIR=str(KITDIR), FACTORY_STATION="python-test",
)
(SCRATCH / "home").mkdir()
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FACTORY_DIR))

import fake_claude  # noqa: E402
from tares.crew import cc_update, claude, dispatch, kit, launch as L, lifecycle, locks, state, watch, watch_revive  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402


def _lf(path, text):
    """Write `text` with LF endings on every platform: the crew reads its state files with no newline translation,
    and Path.write_text would write CRLF on Windows."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

P = F = 0
CP = CrewPaths.at(CREW)
WORK = SCRATCH / "work"
WORK.mkdir()


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def make_launcher(folder: Path, script: Path) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        f = folder / "claude.cmd"
        f.write_bytes(f'@"{sys.executable}" "{script}" %*\r\n'.encode("ascii", "replace"))
    else:
        f = folder / "claude"
        f.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        f.chmod(0o755)
    return str(f)


BIN = os.environ["CLAUDE_BIN"] = make_launcher(SCRATCH / "bin", FACTORY_DIR / "fake_claude.py")


class FakeTares:
    """A Tares that keeps `kit.frozen` the way the real one does: a PUT merges it, a GET shows it back.
    keep=False: a Tares too old to know the field (the PUT answers 200 and changes nothing).
    put_status: the status a PUT answers (500 = it failed)."""

    def __init__(self, settings=None, keep=True, put_status=200, health=True, settings_status=200):
        self.settings = {"kit": {"frozen": None}, **(settings or {})}
        self.keep, self.put_status, self.health, self.settings_status = keep, put_status, health, settings_status
        self.requests: list = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, doc):
                data = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    return self._send(200 if outer.health else 503, {"ok": True})
                if self.path == "/api/crew/settings":
                    outer.requests.append("GET")
                    if outer.settings_status != 200:
                        return self._send(outer.settings_status, {"detail": "no"})
                    return self._send(200, {"settings": outer.settings})
                self._send(404, {"detail": "not found"})

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                outer.requests.append(("PUT", body))
                if outer.put_status != 200:
                    return self._send(outer.put_status, {"detail": "boom"})
                if outer.keep and "kit" in body:
                    outer.settings["kit"] = body["kit"]
                self._send(200, dict(outer.settings))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def puts(self) -> list:
        return [r[1] for r in self.requests if isinstance(r, tuple)]

    def __enter__(self):
        self.thread.start()
        import urllib.request
        with contextlib.suppress(OSError):   # a 503 is an answer too
            urllib.request.urlopen(self.url + "/health", timeout=10).read()   # serving, so shutdown() cannot race the start of the loop
        os.environ["TARESD_URL"] = self.url
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("TARESD_URL", None)


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def station(**over) -> dict:
    row = {"role": "builder", "status": "active", "parent": "crew-orchestrator", "project": "shop", "repo": "",
           "cwd": str(WORK), "model": "opus", "effort": "high", "id": "aaaa0001", "sessionId": "sess-1",
           "kit": "old-kit"}
    row.update(over)
    return row


def make_kit(files=None) -> None:
    shutil.rmtree(KITDIR.parent, ignore_errors=True)
    (KITDIR / "bin").mkdir(parents=True)
    (KITDIR / "skills" / "x").mkdir(parents=True)
    (KITDIR / "bin" / "factory").write_text("#!/bin/sh\necho kit\n", encoding="utf-8")
    (KITDIR / "skills" / "x" / "SKILL.md").write_text("skill one\n", encoding="utf-8")
    for rel, text in (files or {}).items():
        (KITDIR / rel).parent.mkdir(parents=True, exist_ok=True)
        (KITDIR / rel).write_text(text, encoding="utf-8")


def world(stations=None, rows=None) -> None:
    """A fresh crew home with these stations, the fake Claude Code listing these sessions, and a kit."""
    shutil.rmtree(CREW, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    (CREW / "stations").mkdir(parents=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", CREW / "crew.json")
    _lf(CREW / "stations.json", json.dumps(stations or {}, indent=2) + "\n")
    fake_claude.write_agents(STATE, rows or [])
    _lf(STATE / "version", "2.1.100\n")
    make_kit()
    kit.reset()
    watch_revive.last = watch_revive.Round()
    restarts.clear()
    upgrades.clear()


def stations_now() -> dict:
    return json.loads((CREW / "stations.json").read_text(encoding="utf-8"))


def events() -> list:
    try:
        lines = (CREW / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(x) for x in lines]


def kinds(name=None) -> list:
    return [(e["event"], e["station"], e["detail"]) for e in events() if name is None or e["event"] == name]


def row(st: str, id: str, name: str, version="2.1.100", **extra) -> dict:
    return fake_claude.agent_row(st, id, name, str(WORK), version=version, **extra)


def says(fn):
    """Run fn() with stderr kept: (its result, the lines the factory said; the others raw)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = fn()
    return res, [x[len("tares factory: "):] if x.startswith("tares factory: ") else x for x in err.getvalue().splitlines()]


def freeze_here(**kw):
    """kit.freeze as the command runs it: (what it raised or None, what it said)."""
    err = io.StringIO()
    raised = None
    with contextlib.redirect_stderr(err), TaresClient(CP, os.environ) as client:
        try:
            kit.freeze(client, CP, [], **kw)
        except CrewError as e:
            raised = e
    return raised, [x[len("tares factory: "):] if x.startswith("tares factory: ") else x for x in err.getvalue().splitlines()]


def unfreeze_here():
    err = io.StringIO()
    raised = None
    with contextlib.redirect_stderr(err), TaresClient(CP, os.environ) as client:
        try:
            kit.unfreeze(client, CP, [])
        except CrewError as e:
            raised = e
    return raised, [x[len("tares factory: "):] if x.startswith("tares factory: ") else x for x in err.getvalue().splitlines()]


restarts: list = []   # what upgrade_station handed to lifecycle.restart
upgrades: list = []   # the watch restarting itself


def fake_restart(client, paths, args, claude_bin=None):
    restarts.append(list(args))
    state.stations_put(args[0], {"kit": kit.kit_version(KITDIR)}, paths)


lifecycle.restart = fake_restart
real_restart_self = watch.restart_self
watch.restart_self = lambda paths, interval, env=None: upgrades.append(interval)


def tree(root: Path) -> dict:
    out = {}
    for base, _dirs, files in os.walk(root):
        for f in files:
            p = Path(base) / f
            out[str(p.relative_to(root)).replace(os.sep, "/")] = p.read_bytes()
    return out


def frozen_settings(version, at="2026-01-02T03:04:05Z", by="ada") -> dict:
    return {"kit": {"frozen": {"at": at, "by": by, "version": version}}}


# ---------------------------------------------------------------------------------------------
print("kit_version, plugin_root, kit_clean")
world()
v1 = kit.kit_version(KITDIR)
ck("a kit has a fingerprint, the same twice", v1.isdigit() and v1 == kit.kit_version(KITDIR))
(KITDIR / "skills" / "x" / "SKILL.md").write_text("skill two\n", encoding="utf-8")
v2 = kit.kit_version(KITDIR)
ck("a changed file changes it", v2 != v1)
(KITDIR / "skills" / "x" / ".hidden").write_text("x", encoding="utf-8")
(KITDIR / "skills" / "x" / "a.pyc").write_text("x", encoding="utf-8")
(KITDIR / "skills" / "x" / "b~").write_text("x", encoding="utf-8")
(KITDIR / "__pycache__").mkdir()
(KITDIR / "__pycache__" / "m.py").write_text("x", encoding="utf-8")
ck("dot files, bytecode, editor leftovers and __pycache__ do not count", kit.kit_version(KITDIR) == v2)
ck("an unreadable folder fingerprints as nothing and does not raise", kit.kit_version(SCRATCH / "nope") == kit.kit_version(SCRATCH / "nope"))
ck("plugin_root is TARES_PLUGIN_DIR", kit.plugin_root(CP, os.environ) == KITDIR)
ck("a folder that is no git checkout is clean", kit.kit_clean(KITDIR) is True)
ck("frozen_phys is the real path of the crew home plus /frozen-kit", kit.frozen_phys(CP) == os.path.realpath(CREW) + "/frozen-kit")

# ---------------------------------------------------------------------------------------------
print("freeze: the happy path")
world({"crew-orchestrator": station(role="orchestrator", id="aaaa0002")})
with FakeTares() as ft:
    raised, said = freeze_here()
    ck("freeze does not raise", raised is None, str(raised))
    ck("the copy is the kit as it was", tree(CP.frozen_kit) == tree(KITDIR) and (CP.frozen_kit / "bin" / "factory").is_file())
    ck("the checkout is written down", CP.checkout_file.read_text().strip() == str(KITDIR))
    ck("no marker, no half-made copy", not CP.freezing.exists() and not Path(f"{CP.frozen_kit}.new").exists())
    put = ft.puts()
    ck("one PUT, with when, who and the kit version", len(put) == 1 and sorted(put[0]["kit"]["frozen"]) == ["at", "by", "version"]
       and put[0]["kit"]["frozen"]["by"] == "python-test" and put[0]["kit"]["frozen"]["version"] == kit.kit_version(CP.frozen_kit), str(put))
    ck("Tares has it", ft.settings["kit"]["frozen"]["version"] == kit.kit_version(KITDIR))
    ck("a freeze event", kinds("freeze") == [("freeze", "", kit.kit_version(KITDIR))], str(kinds()))
    ck("it says what the freeze means and how to lift it",
       len(said) == 1 and said[0].startswith("kit frozen since ") and " by python-test: stations that start or revive use this kit; "
       "running ones keep reading the checkout until they restart, and the watch moves none onto a newer kit. tares factory crew unfreeze lifts it."
       in said[0], str(said))
    old_at = ft.settings["kit"]["frozen"]["at"]
    time.sleep(1.1)
    raised, said = freeze_here()
    ck("freezing twice says so and keeps the first time", raised is None and len(said) == 1 and said[0].startswith("the kit is already frozen since ")
       and said[0].endswith("; tares factory crew unfreeze lifts it") and ft.settings["kit"]["frozen"]["at"] == old_at, str(said))
    ck("...no second PUT, no second event", len(ft.puts()) == 1 and len(kinds("freeze")) == 1)
    _lf(CP.newer_file, "1\n")
    _lf(CP.cc_newer_file, "2.1.9\n")
    raised, said = unfreeze_here()
    ck("unfreeze with no station on the copy removes it", raised is None and not CP.frozen_kit.exists() and not CP.checkout_file.exists(), str(raised))
    ck("...clears the setting and the remembered newer versions", ft.settings["kit"]["frozen"] is None and not CP.newer_file.exists()
       and not CP.cc_newer_file.exists() and ft.puts()[-1] == {"kit": {"frozen": None}})
    ck("...says so, and records it", said == ["kit unfrozen: the watch moves each station onto the checkout's kit when it's idle"]
       and len(kinds("unfreeze")) == 1, str(said))
    raised, said = unfreeze_here()
    ck("unfreezing what is not frozen says so and changes nothing", raised is None and said == ["the kit is not frozen"]
       and len(ft.puts()) == 2 and len(kinds("unfreeze")) == 1, str(said))

# ---------------------------------------------------------------------------------------------
print("freeze: failures leave nothing behind")
world()
with FakeTares(put_status=500) as ft:
    raised, said = freeze_here()
    ck("a PUT that fails is an error naming Tares", raised is not None and "could not save the freeze on Tares at " in str(raised.line), str(raised))
    ck("...with no copy, no checkout file, no marker, no event", not CP.frozen_kit.exists() and not CP.checkout_file.exists()
       and not CP.freezing.exists() and kinds("freeze") == [])
    ck("...and Tares is not frozen", ft.settings["kit"]["frozen"] is None)
world()
with FakeTares(keep=False) as ft:
    raised, _ = freeze_here()
    ck("a Tares that does not keep the version is an error", raised is not None and "did not keep the freeze; it needs a newer version" in raised.line, str(raised))
    ck("...with nothing left behind", not CP.frozen_kit.exists() and not CP.checkout_file.exists() and not CP.freezing.exists() and kinds("freeze") == [])
world()
with FakeTares(settings_status=500):
    raised, _ = freeze_here()
    ck("Tares that cannot give the settings is an error", raised is not None and "could not read the crew settings from Tares at " in raised.line, str(raised))
    ck("...with nothing left behind and no PUT", not CP.frozen_kit.exists() and not CP.freezing.exists())
    raised, _ = unfreeze_here()
    ck("unfreeze says the same", raised is not None and "could not read the crew settings from Tares at " in raised.line)
world()
with FakeTares(), patched(kit, "copy_kit", lambda src, dst: (_ for _ in ()).throw(OSError("disk full"))):
    raised, _ = freeze_here()
    ck("a copy that fails is an error", raised is not None and raised.line.endswith(f"could not copy the kit to {CP.frozen_kit}"), str(raised))
    ck("...with no stray copy, marker or Tares setting", not CP.frozen_kit.exists() and not Path(f"{CP.frozen_kit}.new").exists()
       and not CP.freezing.exists() and not CP.checkout_file.exists())
world()
with FakeTares(settings=frozen_settings("1"), put_status=500) as ft:
    raised, _ = unfreeze_here()
    ck("an unfreeze whose PUT fails is an error and keeps the setting", raised is not None
       and "could not lift the freeze on Tares at " in raised.line and ft.settings["kit"]["frozen"] is not None, str(raised))
world()
with FakeTares():
    CP.home.mkdir(exist_ok=True)
    shutil.rmtree(CREW)
    raised, _ = freeze_here()
    ck("freeze needs a crew", raised is not None, "no error without a crew")

# ---------------------------------------------------------------------------------------------
print("freeze: a copy still in use")
world({"s1": station(plugin=kit.frozen_phys(CP))})
CP.frozen_kit.mkdir()
(CP.frozen_kit / "bin").mkdir()
(CP.frozen_kit / "bin" / "factory").write_text("#!/bin/sh\necho kit\n", encoding="utf-8")
(CP.frozen_kit / "skills" / "x").mkdir(parents=True)
(CP.frozen_kit / "skills" / "x" / "SKILL.md").write_text("skill one\n", encoding="utf-8")
with FakeTares() as ft:
    raised, said = freeze_here()
    ck("a station on the old copy and a checkout that did not change: the copy is reused", raised is None
       and ft.settings["kit"]["frozen"]["version"] == kit.kit_version(CP.frozen_kit), str(raised))
world({"s1": station(plugin=kit.frozen_phys(CP))})
CP.frozen_kit.mkdir()
(CP.frozen_kit / "bin").mkdir()
(CP.frozen_kit / "bin" / "factory").write_text("#!/bin/sh\necho OLD kit\n", encoding="utf-8")
with FakeTares() as ft:
    raised, _ = freeze_here()
    ck("...but a changed checkout is refused, naming the way out", raised is not None and "stations still run from the kit copy of an earlier freeze, "
       "and the checkout has changed since: wait until tares factory crew watch has moved them (tares factory crew status), then freeze again"
       in raised.line, str(raised))
    ck("...the old copy and its station stay, the marker goes, Tares is untouched", (CP.frozen_kit / "bin" / "factory").read_text().startswith("#!/bin/sh\necho OLD")
       and not CP.freezing.exists() and ft.settings["kit"]["frozen"] is None and ft.puts() == [])

# ---------------------------------------------------------------------------------------------
print("the copy: read-only files, links, folders")
world()
(KITDIR / "skills" / "x" / "ro.md").write_text("ro\n", encoding="utf-8")
(KITDIR / "skills" / "x" / "ro.md").chmod(0o444)
(KITDIR / "skills" / "ro-dir").mkdir()
(KITDIR / "skills" / "ro-dir" / "f").write_text("f", encoding="utf-8")
(KITDIR / "skills" / "ro-dir").chmod(0o555)
try:
    os.symlink("SKILL.md", KITDIR / "skills" / "x" / "link.md")
    has_links = True
except (OSError, NotImplementedError):
    has_links = False
with FakeTares() as ft:
    raised, _ = freeze_here()
    ck("a kit with read-only files and folders freezes", raised is None and (CP.frozen_kit / "skills" / "x" / "ro.md").read_text() == "ro\n", str(raised))
    mode = (CP.frozen_kit / "skills" / "x" / "ro.md").stat().st_mode
    ck("...the copy is writable, so it can be removed", sys.platform == "win32" or mode & stat.S_IWUSR, oct(mode))
    if has_links:
        ck("...links stay links", (CP.frozen_kit / "skills" / "x" / "link.md").is_symlink())
    raised, _ = unfreeze_here()
    ck("...and the copy goes on unfreeze", raised is None and not CP.frozen_kit.exists(), str(raised))
(KITDIR / "skills" / "ro-dir").chmod(0o755)
(KITDIR / "skills" / "x" / "ro.md").chmod(0o644)

# ---------------------------------------------------------------------------------------------
print("unfreeze: a copy in use stays")
world({"s1": station(plugin=kit.frozen_phys(CP))})
CP.frozen_kit.mkdir()
_lf(CP.checkout_file, f"{KITDIR}\n")
with FakeTares(settings=frozen_settings("1")) as ft:
    raised, said = unfreeze_here()
    ck("unfreeze keeps the copy while a station runs from it, and says so", raised is None and CP.frozen_kit.is_dir() and CP.checkout_file.exists()
       and said == ["kit unfrozen: the watch moves each station onto the checkout's kit when it's idle, and removes "
                    f"{CP.frozen_kit} once none runs from it"], str(said))

# ---------------------------------------------------------------------------------------------
print("freezing, frozen_copy_in_use, reap_frozen_copy")
world({"s1": station(plugin=kit.frozen_phys(CP)), "s2": station(status="retired", plugin="elsewhere")})
ck("no marker: not freezing", kit.freezing(CP) is False)
CP.freezing.write_text("", encoding="utf-8")
ck("a fresh marker: freezing", kit.freezing(CP) is True)
old = time.time() - 400
os.utime(CP.freezing, (old, old))
ck("a marker of 400 seconds is a freeze that died", kit.freezing(CP) is False)
ck("an active station on the copy keeps it in use", kit.frozen_copy_in_use(CP) is True)
state.stations_put("s1", {"plugin": "/elsewhere"}, CP)
ck("a station elsewhere does not", kit.frozen_copy_in_use(CP) is False)
state.stations_put("s2", {"plugin": kit.frozen_phys(CP)}, CP)
ck("a retired station does not count", kit.frozen_copy_in_use(CP) is False)
CP.frozen_kit.mkdir()
(CP.frozen_kit / "f").write_text("x", encoding="utf-8")
for f in (CP.newer_file, CP.cc_newer_file, CP.checkout_file):
    f.write_text("x\n", encoding="utf-8")
state.stations_put("s1", {"plugin": kit.frozen_phys(CP)}, CP)
kit.reap_frozen_copy(CP)
ck("the copy stays while a station runs from it", CP.frozen_kit.is_dir())
state.stations_put("s1", {"plugin": "/elsewhere"}, CP)
CP.freezing.write_text("", encoding="utf-8")
kit.reap_frozen_copy(CP)
ck("...and while a freeze is under way", CP.frozen_kit.is_dir())
os.utime(CP.freezing, (old, old))
kit.reap_frozen_copy(CP)
ck("it goes when no one needs it, with the files that go with it", not CP.frozen_kit.exists() and not CP.newer_file.exists()
   and not CP.cc_newer_file.exists() and not CP.checkout_file.exists())
kit.reap_frozen_copy(CP)
ck("reaping what is not there is fine", True)

# ---------------------------------------------------------------------------------------------
print("Frozen, upgrades_frozen, kit_dir")
world()
with FakeTares(settings=frozen_settings("77")):
    with TaresClient(CP, os.environ, lambda m: None) as c:
        fz = kit.Frozen(CP, c)
        ck("Frozen reads the settings", fz.on and fz.field("version") == "77" and fz.field("by") == "ada"
           and fz.text() == "since 2026-01-02T03:04:05Z by ada")
        ck("upgrades_frozen is true", kit.upgrades_frozen(CP, c) is True)
        ck("upgrades_frozen(fresh=True) asks again", kit.upgrades_frozen(CP, c, fresh=True) is True)
        ck("kit_dir with no copy is the checkout", kit.kit_dir(CP, c, KITDIR) == str(KITDIR))
        CP.frozen_kit.mkdir()
        ck("kit_dir with a copy and a freeze is the copy", kit.kit_dir(CP, c, KITDIR) == kit.frozen_phys(CP))
        ck("launch.kit_dir(client) is the same", L.kit_dir(c) == kit.frozen_phys(CP))
        ck("launch.plugin_flags(client) names the copy", kit.frozen_phys(CP) in " ".join(L.plugin_flags(c)))
with FakeTares():
    with TaresClient(CP, os.environ, lambda m: None) as c:
        ck("a copy and no freeze: the checkout", kit.kit_dir(CP, c, KITDIR) == str(KITDIR) and not kit.Frozen(CP, c).on)
        ck("Frozen with no `at` says Tares does not say", kit.Frozen(CP, c).text() == "(Tares does not say since when)")
with FakeTares(settings_status=500):
    with TaresClient(CP, os.environ, lambda m: None) as c:
        ck("Tares unreadable and a copy there: frozen", kit.Frozen(CP, c).on and kit.kit_dir(CP, c, KITDIR) == kit.frozen_phys(CP))
shutil.rmtree(CP.frozen_kit)
with FakeTares(settings_status=500):
    with TaresClient(CP, os.environ, lambda m: None) as c:
        ck("Tares unreadable and no copy: not frozen", not kit.Frozen(CP, c).on)
CP.frozen_kit.mkdir()
_lf(CP.checkout_file, f"{KITDIR}\n")
ck("a factory run from the copy means the checkout", kit.map_checkout(CP, Path(kit.frozen_phys(CP))) == KITDIR)
FP = kit.frozen_phys(CP)
ck("_within: the copy itself, a file in it, a sibling that shares the prefix, and a ../ out of it",
   kit._within(FP, FP) and kit._within(Path(FP) / "bin" / "factory", FP) and not kit._within(FP + "2", FP)
   and not kit._within(Path(FP) / ".." / "x", FP))
ck("a run from a sibling of the copy means itself", kit.map_checkout(CP, Path(FP + "2")) == Path(FP + "2"))
ck("...a path elsewhere means itself", kit.map_checkout(CP, Path("/x/y")) == Path("/x/y"))
CP.checkout_file.write_text("/does/not/exist\n", encoding="utf-8")
ck("...unless the checkout is gone", kit.map_checkout(CP, Path(kit.frozen_phys(CP))) == Path(kit.frozen_phys(CP)))

# ---------------------------------------------------------------------------------------------
print("tell_newer_kit")
world()
with FakeTares(settings=frozen_settings("100")):
    with TaresClient(CP, os.environ, lambda m: None) as c:
        msgs: list = []
        c.say = msgs.append
        fz = kit.Frozen(CP, c)
        kit.tell_newer_kit(CP, c, fz, "100")
        ck("the same kit as the frozen one says nothing", msgs == [] and kinds("kit-newer") == [])
        kit.tell_newer_kit(CP, c, fz, "200")
        ck("a newer kit is said, with the way out", msgs == ["kit 200 is newer than the frozen kit 100; not moving stations "
                                                           "(tares factory crew unfreeze lets them follow it)"], str(msgs))
        ck("...recorded once", kinds("kit-newer") == [("kit-newer", "", "200")] and CP.newer_file.read_text() == "200\n")
        kit.tell_newer_kit(CP, c, fz, "200")
        ck("...and not again for the same kit", len(msgs) == 1 and len(kinds("kit-newer")) == 1)
        kit.tell_newer_kit(CP, c, fz, "300")
        ck("another kit is said again", len(msgs) == 2 and len(kinds("kit-newer")) == 2)
        kit.tell_newer_kit(CP, c, fz, "100")
        ck("going back to the frozen kit forgets it", not CP.newer_file.exists())
with FakeTares(settings={"kit": {"frozen": {"at": "x", "by": "y"}}}):
    with TaresClient(CP, os.environ, lambda m: None) as c:
        msgs = []
        c.say = msgs.append
        kit.tell_newer_kit(CP, c, kit.Frozen(CP, c), "5")
        ck("a freeze that recorded no version says nothing", msgs == [])

# ---------------------------------------------------------------------------------------------
print("step: the kit part of the round")


def round_(settings=None, health=True, env=None):
    """kit.step once against a fake Tares: (its result, what it said, the Tares)."""
    with FakeTares(settings, health=health) as ft:
        res, said = says(lambda: kit.step(CP, env, BIN))
    return res, said, ft


IDLE = [row("idle", "aaaa0001", "shop-build-m1")]
world({"shop-build-m1": station()}, IDLE)
res, said, _ = round_()
ck("the first round: the watch's own kit is the kit, nothing is newer, a stale quiet station is moved", res is True
   and len(restarts) == 1 and restarts[0][0] == "shop-build-m1", str((res, said, restarts)))
ck("...in its own conversation, with the plugin-updated prompt", "The Tares plugin was updated, so the factory restarted you on it, in this "
   "same conversation" in restarts[0][1] and "station-builder" in restarts[0][1], restarts[0][1] if restarts else "")
ck("...says what it does, and records upgraded", any("shop-build-m1 runs an older version of the plugin; restarting it on this one now that it's idle, "
   "in the same conversation" == m for m in said) and kinds("upgraded") == [("upgraded", "shop-build-m1", kit.kit_version(KITDIR))], str((said, kinds())))
res, said, _ = round_()
ck("the next round: it is on the kit now, nothing to do", res is False and len(restarts) == 1, str((res, restarts)))

world({"shop-build-m1": station(kit=kit.kit_version(KITDIR))}, IDLE)
res, said, _ = round_()
ck("a station on the current kit is left alone", res is False and restarts == [] and said == [], str((res, said)))

world({"shop-build-m1": station()}, IDLE)
CP.done_file("shop-build-m1").write_text("", encoding="utf-8")
state.stations_put("shop-build-m1", {"role": "builder"}, CP)
res, _, _ = round_()
ck("a finished builder is not moved", res is False and restarts == [])
world({"shop-build-m1": station(role="helper")}, IDLE)
res, _, _ = round_()
ck("a helper is not moved", res is False and restarts == [])
world({"shop-build-m1": station(sessionId="")}, IDLE)
res, _, _ = round_()
ck("a station with no conversation to resume is not moved", res is False and restarts == [])
world({"shop-build-m1": station()}, [row("working", "aaaa0001", "shop-build-m1")])
res, _, _ = round_()
ck("a station that is not quiet is not moved", res is False and restarts == [])
world({"shop-build-m1": station()}, IDLE)
with patched(state, "halted", lambda paths: True):
    res, _, _ = round_()
ck("a halted crew moves nothing", res is False and restarts == [])
world({"shop-build-m1": station()}, IDLE)
watch_revive.last = watch_revive.Round()
watch_revive.last.down = True
res, _, _ = round_()
ck("a round with a station down moves nothing", res is False and restarts == [])
world({"shop-build-m1": station()}, IDLE)
watch_revive.last = watch_revive.Round()
watch_revive.last.started = {"shop-build-m1"}
res, _, _ = round_()
ck("a station the revive step just started is not moved", res is False and restarts == [])
world({"shop-build-m1": station()}, IDLE)
watch_revive.last = watch_revive.Round()
watch_revive.last.unread = True
res, _, _ = round_()
ck("a round whose session list could not be read moves nothing", res is False and restarts == [])
world({"shop-build-m1": station()}, IDLE)
make_kit()
res, _, _ = round_()
ck("(control) the same world moves the station", res is True and len(restarts) == 1)

world({"shop-build-m1": station()}, IDLE)
res, said, ft = round_(settings=frozen_settings("999"))
ck("a frozen crew moves no station", res is False and restarts == [], str((res, restarts)))
ck("...tells the newer kit once", [m for m in said if "is newer than the frozen kit 999" in m] != [] and len(kinds("kit-newer")) == 1, str(said))
res, said, ft = round_(settings=frozen_settings("999"))
ck("...and not again", len(kinds("kit-newer")) == 1 and not [m for m in said if "is newer than" in m], str(said))

world({"shop-build-m1": station()}, IDLE)
res, said, _ = round_(health=False)
ck("Tares down: nothing moves, it says so, and the round still counts as a kit round (bash: the if, not its result)", res is True and restarts == [] and
   any("Tares doesn't answer; shop-build-m1 moves onto the new version once it does" == m for m in said), str((res, said)))

world({"shop-build-m1": station()}, IDLE)
orig = kit.upgrade_station


def froze_since(paths, client, station_name, kit_v, *, claude_bin):
    """The crew froze between the first look and the move."""
    with FakeTares(settings=frozen_settings("1")):
        return orig(paths, client, station_name, kit_v, claude_bin=claude_bin)


with patched(kit, "upgrade_station", froze_since):
    res, _, _ = round_()
ck("a crew that froze since the round began restarts no one", restarts == [], str(restarts))

# a station that is not in the session list is down: no move this round, whatever the revive step saw (bash: the
# round's own down check)
world({"shop-build-m1": station(), "shop-build-m2": station(id="bbbb0002", sessionId="sess-2")}, IDLE)
watch_revive.last = watch_revive.Round()
res, _, _ = round_()
ck("a station down that the revive step did not see holds the move back", res is False and restarts == [], str((res, restarts)))
world({"shop-build-m1": station(), "shop-build-m2": station(id="bbbb0002", sessionId="sess-2")}, IDLE + [row("idle", "bbbb0002", "shop-build-m2")])
res, _, _ = round_()
ck("(control) both up: one moves", res is True and len(restarts) == 1, str((res, restarts)))

# upgrade_station asks again before it restarts
world({"shop-build-m1": station()}, [row("working", "aaaa0001", "shop-build-m1")])
with FakeTares() as ft:
    with TaresClient(CP, os.environ, lambda m: None) as c:
        rc = kit.upgrade_station(CP, c, "shop-build-m1", kit.kit_version(KITDIR), claude_bin=BIN)
ck("a station that turned busy since the pick is not restarted: rc 4, no event", rc == 4 and restarts == [] and kinds("upgraded") == [], str((rc, restarts, kinds())))
world({"shop-build-m1": station()}, IDLE)
with FakeTares() as ft:
    with TaresClient(CP, os.environ, lambda m: None) as c:
        rc = kit.upgrade_station(CP, c, "shop-build-m1", kit.kit_version(KITDIR), claude_bin=BIN)
ck("(control) an idle one is: rc 0, restarted, upgraded recorded", rc == 0 and len(restarts) == 1 and len(kinds("upgraded")) == 1, str((rc, restarts, kinds())))

# the watch restarts itself, once the kit has held for a round
world({"shop-build-m1": station(kit=kit.kit_version(KITDIR))}, IDLE)
watch.LOOP.once = False
watch.LOOP.interval = 7
res, said, _ = round_()
ck("the first round: no restart", upgrades == [])
(KITDIR / "skills" / "x" / "SKILL.md").write_text("skill THREE\n", encoding="utf-8")
res, said, _ = round_()
ck("a kit that just changed is not an update yet", upgrades == [] and restarts == [], str((upgrades, restarts)))
res, said, _ = round_()
ck("...one that held for a round restarts the watch, with its interval", upgrades == [7]
   and "the plugin was updated; the watch restarts on the new version" in said, str((upgrades, said)))
world({"shop-build-m1": station(kit=kit.kit_version(KITDIR))}, IDLE)
watch.LOOP.once = True
round_()
(KITDIR / "skills" / "x" / "SKILL.md").write_text("skill FOUR\n", encoding="utf-8")
round_()
round_()
ck("`--once` never restarts the watch", upgrades == [], str(upgrades))
watch.LOOP.once = False

# the checkout being edited
world({"shop-build-m1": station()}, IDLE)
with patched(kit, "kit_clean", lambda root: False):
    res, said, _ = round_()
    res2, said2, _ = round_()
ck("a dirty checkout moves no station, and says so once", res is False and res2 is False and restarts == []
   and len([m for m in said if "has uncommitted changes; no station moves onto a new version until it is clean" in m]) == 1
   and not [m for m in said2 if "uncommitted" in m], str((said, said2)))

# the frozen copy goes when nobody needs it
world({"shop-build-m1": station(kit=kit.kit_version(KITDIR))}, IDLE)
CP.frozen_kit.mkdir()
round_()
ck("an unfrozen round removes a copy no station runs from", not CP.frozen_kit.exists())
world({"shop-build-m1": station(kit=kit.kit_version(KITDIR), plugin=kit.frozen_phys(CP))}, IDLE)
CP.frozen_kit.mkdir()
round_()
ck("...but not one a station runs from", CP.frozen_kit.is_dir())
res, said, _ = round_()
ck("a station on the copy is stale once unfrozen: it is moved", res is True and restarts and restarts[0][0] == "shop-build-m1", str((res, restarts)))

# ---------------------------------------------------------------------------------------------
print("the watch's updates step and cc_update")
calls_ = []
with patched(kit, "step", lambda paths: True), patched(cc_update, "step", lambda paths, kit_upgraded=False: calls_.append(kit_upgraded)):
    watch._updates_step(CP)
ck("a round that moved a station on a kit makes no Claude Code move", calls_ == [True], str(calls_))
calls_.clear()
with patched(kit, "step", lambda paths: False), patched(cc_update, "step", lambda paths, kit_upgraded=False: calls_.append(kit_upgraded)):
    watch._updates_step(CP)
ck("...otherwise it does", calls_ == [False])
calls_.clear()


def boom(paths):
    raise RuntimeError("kaput")


with patched(kit, "step", boom), patched(cc_update, "step", lambda paths, kit_upgraded=False: calls_.append(kit_upgraded)):
    _, said = says(lambda: watch._updates_step(CP))
ck("a kit part that fails does not stop the Claude Code part", calls_ == [False] and any("the kit step failed: RuntimeError: kaput" in m for m in said), str((calls_, said)))
# a round whose kit part moved a station makes no Claude Code move
(STATE / "stamp-version").write_text("1\n", encoding="utf-8")
cc_rows = [row("idle", "aaaa0001", "shop-build-m1", version="1.0.0")]


def cc_round(kit_upgraded):
    world({"shop-build-m1": station(kit=kit.kit_version(KITDIR))}, cc_rows)
    (STATE / "version").write_text("1.1.0\n", encoding="utf-8")
    cc_update._cc_seen = None
    with FakeTares() as ft:
        return says(lambda: cc_update.step(CP, claude_bin=BIN, kit_upgraded=kit_upgraded))[1]


said = cc_round(True)
ck("a round that moved a station onto a new kit makes no Claude Code move", not any("older Claude Code" in m for m in said), str((said, restarts)))
said = cc_round(False)
ck("(control) the same world without a kit move: the idle station moves onto the new Claude Code", any("runs an older Claude Code than 1.1.0" in m for m in said), str(said))
world()
with FakeTares(settings=frozen_settings("1")):
    with TaresClient(CP, os.environ, lambda m: None) as c:
        ck("cc_update.upgrades_frozen is the kit's", cc_update.upgrades_frozen(CP, c) is True)
with FakeTares():
    with TaresClient(CP, os.environ, lambda m: None) as c:
        ck("...and false when not frozen", cc_update.upgrades_frozen(CP, c) is False)

# ---------------------------------------------------------------------------------------------
print("watch.restart_self")


class _OS:
    """The os module, except for its name (a Windows run on any platform) and execve."""

    def __init__(self, name, execve=None):
        self.name = name
        self.execve = execve

    def __getattr__(self, attr):
        return getattr(os, attr)


execs: list = []
world()
# The environment the new watch would get: a scratch home too, or a patch that missed would start a watch on ~/factory
env_ = {"PATH": os.environ.get("PATH", ""), "TARES_FACTORY_ENGINE": "python", "HOME": os.environ["HOME"],
        "USERPROFILE": os.environ["HOME"], "FACTORY_HOME": str(CREW), "FACTORY_CREW_HOME": str(CREW)}


def record_exec(path, argv, env):
    _crew_guard.check_exec([path, *argv], env)   # the environment it would exec with must not reach the real home
    execs.append((path, argv, env))


with patched(watch, "os", _OS("posix", record_exec)):
    real_restart_self(CP, 7, env_)
ck("on a platform with exec the process becomes the new watch", len(execs) == 1 and execs[0][0] == execs[0][1][0], str(execs))
ck("...an absolute program, the watch command and the interval it ran with", os.path.isabs(execs[0][1][0])
   and execs[0][1][1:] == ["-m", "tares.crew", "crew", "watch", "--interval", "7"], str(execs))
ck("...with the environment it was given", execs[0][2] == env_, str(execs[0][2]))

world()
locks.station_lock("crew-reviewer", CP)
lock_pid = CP.lock_dir("crew-reviewer") / "pid"
seen: list = []
with patched(watch, "os", _OS("posix", lambda path, argv, env: seen.append(lock_pid.exists()))):
    real_restart_self(CP, 7, env_)
ck("a station lock is let go before the exec, which runs no atexit", seen == [False], str(seen))
locks.station_lock("crew-reviewer", CP)


def failing_execve(path, argv, env):
    raise OSError("no such program")


try:
    with patched(watch, "os", _OS("posix", failing_execve)):
        real_restart_self(CP, 7, env_)
    failed = None
except OSError as e:
    failed = e
ck("a failed exec raises, so follow_engine keeps this watch", failed is not None, str(failed))
back = lock_pid.read_text().strip() if lock_pid.exists() else "no pid file"
ck("...and the watch takes back the lock it let go", back == str(os.getpid()), back)
locks.release_locks()

spawned: list = []
world()


def fake_spawn(paths, argv, env):
    _crew_guard.check_exec(argv, env)
    spawned.append(argv)
    return 4242, 1234.5


try:
    with patched(watch, "os", _OS("nt")), patched(watch, "_spawn", fake_spawn):
        real_restart_self(CP, 3, env_)
    left = None
except SystemExit as e:
    left = e.code
ck("with no exec (Windows) a new watch is started with the interval and this one leaves", spawned and spawned[0][-2:] == ["--interval", "3"] and left == 0, str((spawned, left)))
pf = watch.read_pidfile(CP)
ck("...and the pid file names the new watch, so this one's exit leaves it alone", pf is not None and pf[0] == 4242, str(pf))

# ---------------------------------------------------------------------------------------------
print("the commands")
ck("freeze and unfreeze are ported", dispatch.PORTED[("crew", "freeze")] is not None and dispatch.PORTED[("crew", "unfreeze")] is not None)
world({"crew-orchestrator": station(role="orchestrator")})
with FakeTares() as ft:
    rc, said = says(lambda: dispatch.PORTED[("crew", "freeze")](["ignored", "args"], dict(os.environ)))
    ck("`crew freeze` freezes, ignoring any arguments", rc == 0 and ft.settings["kit"]["frozen"] is not None and CP.frozen_kit.is_dir(), str((rc, said)))
    rc, said = says(lambda: dispatch.PORTED[("crew", "unfreeze")]([], dict(os.environ)))
    ck("`crew unfreeze` lifts it", rc == 0 and ft.settings["kit"]["frozen"] is None and not CP.frozen_kit.exists(), str((rc, said)))
with FakeTares(put_status=500) as ft:
    rc, said = says(lambda: dispatch.PORTED[("crew", "freeze")]([], dict(os.environ)))
    ck("a failing freeze exits non-zero with the message", rc != 0 and any("could not save the freeze on Tares" in m for m in said), str((rc, said)))

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
