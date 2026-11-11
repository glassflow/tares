#!/usr/bin/env python3
"""Runs the looping watch of the freeze checks for the `freeze_*` scenarios of parity.py: a watch that keeps
running while the kit changes under it, which a step that waits for its command to end cannot do.

    freeze_support.py <crew command and arguments>     the command itself, as `python -m tares.crew`
    freeze_support.py @frozen-loop                     a frozen crew's watch meets a newer kit
    freeze_support.py @copy-loop                       a watch started from the frozen copy outlives it

The two @ words start `crew watch --interval 1` as a process of its own (in the scenario's crew and Tares), change the
kit (a line appended to a skill file), wait, stop the watch, and print what it looked at as one JSON object, so the
scenario's checks read it.

"Runs from" is the plugin folder a watch was started on. The watch is `python -m tares.crew crew watch` whatever the
plugin, so it is read from the TARES_PLUGIN_DIR the process runs with (psutil), which is where the engine takes its
plugin from.

Standard library and psutil (the engine's own dependency).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

ENGINE_CMD = [sys.executable, "-m", "tares.crew"]


def count_events(crew: Path, name: str) -> int:
    try:
        lines = (crew / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for x in lines if x.strip() and json.loads(x).get("event") == name)


def change_kit(kit: Path, tag: str) -> None:
    with open(kit / "skills" / "factory-protocol" / "SKILL.md", "a", encoding="utf-8", newline="\n") as f:
        f.write(f"<!-- {tag} -->\n")


SLOW = 100   # half-second looks (50 s) for what a watch has to start, run a round and finish on a busy machine


def wait_for(fn, tries: int = 40) -> bool:
    for _ in range(tries):
        if fn():
            return True
        time.sleep(0.5)
    return False


def start_watch(out: Path, env: dict) -> subprocess.Popen:
    env = {**env, "PYTHONUNBUFFERED": "1"}
    with open(out, "wb") as f:
        return subprocess.Popen(ENGINE_CMD + ["crew", "watch", "--interval", "1"], stdout=f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, cwd=str(ROOT), start_new_session=True)


def stop_watch(p: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except OSError:
            pass
        try:
            p.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def runs_from(p: subprocess.Popen) -> str:
    """The plugin folder the watch runs from ("" when it cannot be told)."""
    import psutil
    try:
        proc = psutil.Process(p.pid)
        return proc.environ().get("TARES_PLUGIN_DIR", "")
    except psutil.Error:
        return ""


def frozen_loop(env: dict) -> dict:
    """A frozen crew, every station up and idle on the old kit, and a watch that is running: the kit changes."""
    from tares.crew import kit as kitmod
    crew, kit = Path(env["FACTORY_CREW_HOME"]).resolve(), Path(env["TARES_PLUGIN_DIR"])
    tmp = crew.parent
    v0 = kitmod.kit_version(kit)
    out = tmp / "watch.out"
    p = start_watch(out, env)
    try:
        time.sleep(2)
        change_kit(kit, "v1")
        reported = wait_for(lambda: count_events(crew, "kit-newer") == 1, SLOW)
        time.sleep(4)
        text = out.read_text(encoding="utf-8", errors="replace")
        facts = {
            "reported": reported,
            "events": count_events(crew, "kit-newer"),
            "said": text.count(f"is newer than the frozen kit {v0}; not moving stations"),
            "restart_said": "the watch restarts on the new version" in text,
            "alive": p.poll() is None,
            "upgraded": count_events(crew, "upgraded"),
        }
    finally:
        stop_watch(p)
    facts["way_out"] = "crew unfreeze" in out.read_text(encoding="utf-8", errors="replace")
    return facts


def copy_loop(env: dict) -> dict:
    """A watch started from the frozen copy of the kit, left running past the unfreeze (TR-686)."""
    crew, kit = Path(env["FACTORY_CREW_HOME"]).resolve(), Path(env["TARES_PLUGIN_DIR"])
    snap = crew / "frozen-kit"
    tmp = crew.parent
    out = tmp / "copywatch.out"
    p = start_watch(out, {**env, "TARES_PLUGIN_DIR": str(snap)})
    try:
        gone = wait_for(lambda: not snap.exists(), SLOW)
        alive = p.poll() is None
        before = runs_from(p) if alive else ""
        change_kit(kit, "v5")
        restarted = wait_for(lambda: "the watch restarts on the new version"
                             in out.read_text(encoding="utf-8", errors="replace"), SLOW)
        time.sleep(2)
        after_alive = p.poll() is None
        after = runs_from(p) if after_alive else ""
        text = out.read_text(encoding="utf-8", errors="replace")
        facts = {
            "copy_gone": gone,
            "alive": alive,
            "from_copy": before == str(snap),
            "restarted": restarted,
            "alive_after": after_alive,
            "from_checkout": after == os.path.realpath(kit),
            "from_copy_after": after == str(snap),
            "no_such_file": "No such file" in text,
        }
        # The restarted watch moves a station onto the new kit. Let that move end before the watch is stopped, so a
        # station lock is not left behind by a stop that comes in the middle of it.
        wait_for(lambda: count_events(crew, "upgraded") >= 1, SLOW)
        wait_for(lambda: not list((crew / "stations").glob("*.lock")), SLOW)
        time.sleep(1)
    finally:
        stop_watch(p)
    return facts


def main(argv: list) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    rest = argv
    if rest[0] == "@frozen-loop":
        print(json.dumps(frozen_loop(dict(os.environ)), sort_keys=True))
        return 0
    if rest[0] == "@copy-loop":
        print(json.dumps(copy_loop(dict(os.environ)), sort_keys=True))
        return 0
    return subprocess.run(ENGINE_CMD + rest, env=dict(os.environ), cwd=os.getcwd()).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
