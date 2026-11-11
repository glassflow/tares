"""An install without the console (a git install builds with no ui/dist, TR-1170) still starts.

`tares up` says in one line how to get the console, the daemon answers the console's address with a 404 that
says the same, and nothing raises. With a built console, neither says it.
"""
import io, os, sys, tempfile, types
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

import tares.cli as cli
from tares import _ui

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def up_output(tmp, dist):
    os.environ["TARES_UI_DIST"] = str(dist)
    cli.run_daemon = lambda: None
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._up(types.SimpleNamespace(host="127.0.0.1", port=9999, data_dir=str(tmp), open=False))
    return buf.getvalue()


with tempfile.TemporaryDirectory() as t:
    root = Path(t)
    empty, built = root / "empty", root / "built"
    empty.mkdir()
    built.mkdir()
    (built / "index.html").write_text("<!doctype html>\n", encoding="utf-8")

    ck("an empty folder is no console", not _ui.console_built(empty))
    ck("a missing folder is no console", not _ui.console_built(root / "nope"))
    ck("a folder with index.html is a console", _ui.console_built(built))
    os.environ["TARES_UI_DIST"] = str(built)
    ck("TARES_UI_DIST wins", _ui.ui_dist() == built)
    os.environ.pop("TARES_UI_DIST")
    ck("without it, the packaged or source console is used", _ui.ui_dist().name in ("console", "dist"), str(_ui.ui_dist()))

    out = up_output(root / "h1", empty)
    hint = [ln for ln in out.splitlines() if _ui.NO_CONSOLE_HINT in ln]
    ck("tares up on an install without the console says how to get it, once", len(hint) == 1, out)
    ck("...and still reports the address", "tares: console at http://127.0.0.1:9999" in out, out)
    ck("...in plain words: no em dash", "—" not in _ui.NO_CONSOLE_HINT)
    ck("...and it names the three ways to get one", all(w in _ui.NO_CONSOLE_HINT for w in ("pip install tares", "npm run build", "TARES_UI_DIST")))
    out = up_output(root / "h2", built)
    ck("with a console, tares up says nothing about it", "no console" not in out, out)

    from tares import daemon
    daemon.UI_DIST = empty
    r = daemon._serve_ui("")
    body = r.body.decode()
    ck("the daemon answers / with a 404 and the hint", r.status_code == 404 and "console not built" in body and "npm run build" in body, f"{r.status_code} {body}")
    ck("...also for a client-side route", daemon._serve_ui("sources/abc").status_code == 404)
    daemon.UI_DIST = built
    r = daemon._serve_ui("")
    ck("with a console it serves index.html", r.status_code == 200 and Path(r.path) == built / "index.html", str(getattr(r, "path", r)))

# The image builds from a hand-listed file set: the build hook must be in it, and the image requires the console.
dockerfile = (Path(__file__).resolve().parent.parent / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
copies = [ln.split()[1:-1] for ln in dockerfile.splitlines() if ln.startswith("COPY ") and "--from" not in ln]
ck("the Dockerfile copies hatch_build.py next to pyproject.toml", any("hatch_build.py" in c and "pyproject.toml" in c for c in copies), str(copies))
ck("...and requires the console for the image build", "ENV TARES_REQUIRE_CONSOLE=1" in dockerfile and
   dockerfile.index("ENV TARES_REQUIRE_CONSOLE=1") < dockerfile.index("RUN pip install"))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
