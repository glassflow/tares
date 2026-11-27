"""Does what a Codex station asks reach the person's desk, and their answer the station? A by-hand probe
(TR-1593), not in CI: it needs a signed-in `codex`, and it starts a real Tares cell (cell.py).

    TARES_PROBE_CODEX=/path/to/codex .venv/bin/python tests/factory/codex_probe/probe_asks.py [--out DIR] [--only NAME ...]

A Builder is started by the product's own start against that cell, with no `claude` on PATH, and told to run
one command its sandbox does not allow: a `touch` in a folder of the probe's own under ~/.cache (not the
workspace, not TMPDIR, not /tmp, which the sandbox lets it write). Nobody is connected to the station. The
watch's Codex round (`tares.crew.codex_round.step`) is then run by hand, as the watch runs it, and the probe
plays the person: it answers the desk item through the console's own route, with the person's key and no
station's name.

  approved      the item is on the real desk with the command; the person's Approve reaches the station on the
                next round, the command runs and the turn ends with nobody connected
  declined      the person's Decline: the command does not run, and the turn goes on to its end
  no_longer_asked
                the station is restarted while its item is open: the item is withdrawn on the cell, and an
                Approve given after that reaches nobody

Three model turns, and the restart's.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import probe_mcp_station as base  # noqa: E402
from appserver import codex_bin  # noqa: E402
from cell import CellError  # noqa: E402
from probe_launch import finish, path_without_claude  # noqa: E402

from tares.crew import codex as cx  # noqa: E402
from tares.crew import codex_asks, codex_round  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

FAR = os.path.expanduser("~/.cache/tares-asks-probe")   # a folder of the probe's own that the sandbox does not cover
SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def round_(paths) -> str:
    """One round of the watch's Codex step; what it said."""
    out = io.StringIO()
    with contextlib.redirect_stderr(out):
        codex_round.step(paths)
    return out.getvalue()


def waits(paths, station: str, want: str, seconds: float = 180) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cs.turn_state(paths, station) == want:
            return True
        time.sleep(1)
    return False


def asks(w: dict, name: str) -> tuple:
    """A Builder whose first turn asks to touch a file under FAR: (paths, client, station, the file)."""
    os.makedirs(FAR, exist_ok=True)
    target = os.path.join(FAR, f"{name}-{os.getpid()}.txt")
    paths, client, station = base.crew(w)
    err = base.start(w, client, station, (
        f"Run exactly this one shell command: touch {target}\n"
        "It writes outside your sandbox, so it needs approval: request the approval and wait for the answer. "
        "Do not try another way and do not change the command. If it is declined, do not retry. "
        "Then answer with the one word done."))
    if err:
        raise CrewError(err)
    return paths, client, station, target


def desk(w: dict, state: str = "open") -> list:
    return w["cell"].api("GET", f"/api/desk?state={state}", key=w["cell"].key).get("desk") or []


def person_says(w: dict, label: str, words: str):
    """The person's answer in the console: their key, no station's name. The error text, or None."""
    try:
        w["cell"].api("POST", f"/api/desk/{label}/reply", {"words": words}, key=w["cell"].key)
    except CellError as e:
        return str(e)
    return None


@scenario
def approved(out):
    w = base.world()
    paths, client, station, target = asks(w, "approved")
    blocked = waits(paths, station, "blocked")
    said = round_(paths)
    items = [d for d in desk(w) if d.get("station") == station]
    kept = codex_asks.kept(paths, station)
    not_yet = not os.path.exists(target)
    again = round_(paths)
    label = items[0]["label"] if items else ""
    station_reply = None
    try:   # a station's own answer: the cell must refuse it
        w["cell"].api("POST", f"/api/desk/{label}/reply", {"words": "A"}, key=w["cell"].key,
                      headers={"X-Tares-Station": station, "X-Tares-Role": "builder"})
    except CellError as e:
        station_reply = str(e)
    err = person_says(w, label, "A") if label else "no item"
    given = round_(paths)
    rested = waits(paths, station, "done")
    return finish({
        "the station's turn stopped on what it asked, with nobody connected": blocked,
        "one item is on the real desk, with the station and the command as it was asked":
            len(items) == 1 and f"touch {target}" in items[0]["question"] and "exec </dev/null" not in json.dumps(items[0])
            and items[0]["options"] == ["Approve", "Decline"],
        "nothing ran while it waited, and a second round filed nothing more": not_yet and len(kept) == 1
            and len([d for d in desk(w) if d.get("station") == station]) <= 1 and "on the desk" not in again,
        "the cell refuses an answer from the station itself": bool(station_reply) and "403" in station_reply,
        "the person's Approve reached the station on the next round": err is None and "(accept)" in given,
        "the command then ran, and the turn ended with nobody connected": rested and os.path.exists(target),
    }, said=said.strip()[-200:], given=given.strip()[-200:], station_reply=station_reply,
        question=(items[0]["question"] if items else "")[:300], context=(items[0].get("context") if items else "")[:400])


@scenario
def declined(out):
    w = base.world()
    paths, client, station, target = asks(w, "declined")
    blocked = waits(paths, station, "blocked")
    round_(paths)
    items = [d for d in desk(w) if d.get("station") == station]
    err = person_says(w, items[0]["label"], "B") if items else "no item"
    given = round_(paths)
    rested = waits(paths, station, "done")
    return finish({
        "the turn stopped on what it asked, and the item was filed": blocked and len(items) == 1,
        "the person's Decline reached the station": err is None and "(decline)" in given,
        "the command did not run, and the turn went on to its end": rested and not os.path.exists(target),
    }, given=given.strip()[-200:])


@scenario
def no_longer_asked(out):
    w = base.world()
    paths, client, station, target = asks(w, "gone")
    blocked = waits(paths, station, "blocked")
    round_(paths)
    items = [d for d in desk(w) if d.get("station") == station]
    label = items[0]["label"] if items else ""
    cs.stop(paths, station)
    said = round_(paths)
    withdrawn = [d for d in desk(w, "withdrawn") if d.get("label") == label]
    late = person_says(w, label, "A") if label else "no item"
    after = round_(paths)
    return finish({
        "the turn stopped on what it asked, and the item was filed": blocked and bool(label),
        "with the station stopped the item is withdrawn on the cell": len(withdrawn) == 1 and "no longer asks" in said,
        "an Approve given after that is refused by the cell, and nothing is handed on or run":
            bool(late) and "409" in late and "(accept)" not in after and not os.path.exists(target),
    }, said=said.strip()[-200:], late_answer=late)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    os.environ["PATH"] = path_without_claude()
    os.environ.pop("CLAUDE_BIN", None)
    todo = [(n, f) for n, f in SCENARIOS.items() if not args.only or n in args.only]
    results = {}
    try:
        for name, fn in todo:
            try:
                results[name] = fn(args.out)
            except Exception as e:  # noqa: BLE001 - a probe says what broke and goes on
                results[name] = {"pass": False, "error": f"{type(e).__name__}: {e}"}
            r = results[name]
            print("PASS" if r.get("pass") else "FAIL", name, r.get("error") or "")
            for k, v in (r.get("checks") or {}).items():
                print(f"      {'ok ' if v else 'NO '} {k}")
    finally:
        w = base.WORLD
        if w:
            for n in range(1, w["n"] + 1):
                home = CrewPaths.at(os.path.join(w["root"], f"crew{n}"))
                for station in list(json.load(open(home.stations, encoding="utf-8"))):
                    with contextlib.suppress(CrewError):
                        cs.stop(home, station)
            w["cell"].stop()
            shutil.rmtree(w["root"], ignore_errors=True)
        shutil.rmtree(FAR, ignore_errors=True)
    with open(os.path.join(args.out, "asks.json"), "w", encoding="utf-8") as f:
        json.dump({"codex": cx.codex_version(codex_bin()), "results": results}, f, indent=1, ensure_ascii=False)
    return 0 if all(results[name].get("pass") for name, _ in todo) else 1


if __name__ == "__main__":
    sys.exit(main())
