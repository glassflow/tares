"""Does a Codex station behave, after its start, the way the crew's station code takes it to (TR-1593)? A
by-hand probe, not in CI: it needs a signed-in `codex` and spends six small turns.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_station.py [--out DIR] [--only NAME ...]

Everything is done by the product code: `tares.crew.codex.start` starts the station and hands it its first
prompt, `tares.crew.codex_station` reads its turn state, hands it the next prompt and stops it. Nothing stays
connected to the station between two calls. The same cases against a fake Codex are
tests/test_crew_codex_station.py.

Scenarios:
  turns_by_visits        the first prompt runs as a turn with nobody connected: the station reads "working" during
                         it and "done" after; a second prompt is one more turn on the same conversation, and the
                         answer shows it was the same conversation
  no_prompt_into_a_turn  a prompt for a station in a turn is refused and never reaches it; stopping the station
                         in the middle of its command interrupts the turn, ends the app-server and leaves no
                         command behind
  carried_on_after_stop  the station is stopped and started again: its conversation is loaded again and knows
                         what was said before
  blocked_on_approval    a turn that stops on an approval reads "blocked" and is not between turns; nothing ran;
                         the station can still be stopped
  killed_mid_command     the app-server is killed in the middle of a command: the command goes on, is found
                         among what ran under the app-server at the watch's last look, and is ended. What the
                         environment alone would have found is written down too (on macOS: not a system program)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from appserver import codex_bin  # noqa: E402
from probe_launch import STATION, Crew, finish, path_without_claude  # noqa: E402

from tares.crew import codex as cx  # noqa: E402
from tares.crew import codex_station as cs  # noqa: E402
from tares.crew.errors import CrewError, NotBetweenTurnsError  # noqa: E402

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def wait_for(paths, want: str, seconds: float = 120) -> tuple:
    """(reached, the states seen on the way, seconds it took), by visits a second apart."""
    seen, began = [], time.time()
    while time.time() - began < seconds:
        st = cs.turn_state(paths, STATION)
        if not seen or seen[-1] != st:
            seen.append(st)
        if st == want:
            return True, seen, round(time.time() - began, 1)
        time.sleep(1)
    return False, seen, round(time.time() - began, 1)


def answers(paths) -> list:
    """What the station said, turn by turn, read from its conversation by a visitor."""
    thread = cs._recorded_thread(paths, STATION)
    try:
        with cs.visit(paths, STATION) as wire:
            found = cs._thread(wire, thread, turns=True)[0] or {}
    except CrewError:
        return []
    out = []
    for turn in found.get("turns") or []:
        said = [i.get("text") or "" for i in turn.get("items") or [] if i.get("type") == "agentMessage"]
        out.append({"status": turn.get("status"), "last_answer": (said[-1] if said else "")[:80]})
    return out


def running(needle: str) -> list:
    ps = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, errors="replace").stdout
    return [line.split()[0] for line in ps.splitlines() if needle in line and "probe_station.py" not in line]


@scenario
def turns_by_visits(out):
    c = Crew()
    try:
        err, row = c.start(prompt="Remember the word PLUM. Run the shell command `sleep 6` once, then answer with: ready")
        if err:
            return finish({"the start succeeded and handed over the first prompt": False}, error=err)
        thread = c.row().get("sessionId")
        during = cs.turn_state(c.paths, STATION)
        reached, seen, took = wait_for(c.paths, "done")
        updated = cs.updated(c.paths, STATION)
        got = cs.hand(c.paths, STATION, "Answer with the one word I asked you to remember, and nothing else.")
        reached2, seen2, took2 = wait_for(c.paths, "done")
        said = answers(c.paths)
        how = cs.stop(c.paths, STATION)
        return finish({
            "the first prompt ran as a turn with nobody connected": bool(thread) and reached and len(said) >= 1
                and said[0]["status"] == "completed",
            "the station read working during its turn": during == "working",
            "and done after it": reached,
            "when it last did anything is a time near now": isinstance(updated, (int, float)) and abs(time.time() - updated) < 300,
            "the second prompt was one more turn on the same conversation": got["thread"] == thread and not got["replaced"]
                and reached2 and len(said) == 2 and said[1]["status"] == "completed",
            "its answer shows it was the same conversation": len(said) == 2 and "PLUM" in said[1]["last_answer"].upper(),
            "at rest, the station ended when asked": how == "stopped" and not cs.is_alive(c.paths, STATION),
        }, states_seen=seen, seconds_of_the_first_turn=took, states_seen_second=seen2, answers=said, stop=how)
    finally:
        c.end(wait=3)
        c.remove()


@scenario
def no_prompt_into_a_turn(out):
    c = Crew()
    try:
        err, _ = c.start(prompt="Run the shell command `sleep 41.4142` once, then answer with: slept")
        if err:
            return finish({"the start succeeded": False}, error=err)
        for _ in range(60):   # until the command itself runs
            if running("sleep 41.4142"):
                break
            time.sleep(1)
        command_ran = bool(running("sleep 41.4142"))
        try:
            cs.hand(c.paths, STATION, "Answer with: this prompt must not arrive")
            refused = "not refused"
        except NotBetweenTurnsError as e:
            refused = str(e)
        began = time.time()
        how = cs.stop(c.paths, STATION)
        took = round(time.time() - began, 1)
        time.sleep(2)
        left = running("sleep 41.4142")
        thread = c.row().get("sessionId")
        return finish({
            "the station was in the middle of its command": command_ran,
            "a prompt for it was refused": "is in a turn" in refused,
            "stopped in the middle of the command, the app-server ended when asked": how == "stopped" and not cs.is_alive(c.paths, STATION),
            "no command of the turn is left running": left == [],
        }, refusal=refused, stop=how, seconds_to_stop=took, commands_left=len(left), conversation=bool(thread))
    finally:
        for pid in running("sleep 41.4142"):
            subprocess.run(["kill", pid])
        c.end(wait=3)
        c.remove()


@scenario
def carried_on_after_stop(out):
    c = Crew()
    try:
        err, _ = c.start(prompt="Remember the word QUINCE. Answer with: noted")
        if err:
            return finish({"the first start succeeded": False}, error=err)
        thread = c.row().get("sessionId")
        wait_for(c.paths, "done")
        first_pid = (c.row().get("codex") or {}).get("pid")
        how = cs.stop(c.paths, STATION)
        err2, row2 = c.start(prompt="Answer with the one word I asked you to remember, and nothing else.", resume=thread)
        reached, seen, _ = wait_for(c.paths, "done")
        said = answers(c.paths)
        return finish({
            "the station was stopped and started again as another process": how == "stopped" and err2 is None
                and (row2.get("codex") or {}).get("pid") not in (None, first_pid),
            "it carries on the conversation it had": row2.get("sessionId") == thread and reached,
            "and knows what was said before the stop": bool(said) and "QUINCE" in said[-1]["last_answer"].upper(),
        }, second_error=err2, answers=said, turns_in_the_conversation=len(said))
    finally:
        cs.stop(c.paths, STATION)
        c.end(wait=3)
        c.remove()


@scenario
def blocked_on_approval(out):
    c = Crew()
    try:
        plan = c.plan()
        plan["settings"]["thread"]["approvalPolicy"] = "untrusted"   # so a plain command asks first, as in TR-1576
        err, _ = c.start(plan, prompt="Run the shell command `touch approval-probe.txt` once, then answer with: touched")
        if err:
            return finish({"the start succeeded": False}, error=err)
        reached, seen, took = wait_for(c.paths, "blocked", 90)
        time.sleep(3)
        ran = os.path.exists(os.path.join(c.work, "approval-probe.txt"))
        between = cs.at_rest(c.paths, STATION)
        try:
            cs.hand(c.paths, STATION, "Answer with: this prompt must not arrive")
            refused = False
        except NotBetweenTurnsError:
            refused = True
        how = cs.stop(c.paths, STATION)
        return finish({
            "the turn stopped on an approval, and the station read blocked": reached,
            "nothing ran unanswered": not ran,
            "a blocked station is not between turns, and takes no prompt": not between and refused,
            "it can still be stopped": how in ("stopped", "killed") and not cs.is_alive(c.paths, STATION),
        }, states_seen=seen, seconds_until_blocked=took, stop=how)
    finally:
        c.end(wait=3)
        c.remove()


@scenario
def killed_mid_command(out):
    c = Crew()
    try:
        err, _ = c.start(prompt="Run the shell command `sleep 43.4343` once, then answer with: slept")
        if err:
            return finish({"the start succeeded": False}, error=err)
        for _ in range(60):
            if running("sleep 43.4343"):
                break
            time.sleep(1)
        pid = (c.row().get("codex") or {}).get("pid")
        cs.note_children(c.paths, STATION)   # the watch's look at a station that runs, once a round
        os.kill(pid, 9)
        time.sleep(2)
        still = running("sleep 43.4343")
        note = cs._children_file(c.paths, STATION)
        kept = note.read_text() if note.exists() else ""
        note.unlink(missing_ok=True)
        by_environment = sorted({cs._name(p) for p in cs.leftovers(c.paths, STATION)})   # with no note to go by
        note.write_text(kept)
        found = cs.leftovers(c.paths, STATION)
        names = sorted({cs._name(p) for p in found})
        ended = cs.end_leftovers(c.paths, STATION, grace=5)
        time.sleep(1)
        return finish({
            "killed, the app-server left its command running": bool(still) and not cs.is_alive(c.paths, STATION),
            "the command is found among what ran under the app-server at the watch's last look":
                any("sleep" in n for n in names),
            "ended, nothing of the station is left running": bool(ended) and running("sleep 43.4343") == []
                and cs.leftovers(c.paths, STATION) == [],
        }, programs_found=names, programs_ended=ended, found_by_the_environment_alone=by_environment)
    finally:
        for left in running("sleep 43.4343"):
            subprocess.run(["kill", left])
        c.left_behind.append(os.path.realpath((c.row().get("codex") or {}).get("socket", "") or "/nonexistent"))
        c.end(wait=3)
        c.remove()


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
    for name, fn in todo:
        try:
            results[name] = fn(args.out)
        except Exception as e:  # noqa: BLE001 - a probe says what broke and goes on
            results[name] = {"pass": False, "error": f"{type(e).__name__}: {e}"}
        r = results[name]
        print("PASS" if r.get("pass") else "FAIL", name, r.get("error") or "")
        for k, v in (r.get("checks") or {}).items():
            print(f"      {'ok ' if v else 'NO '} {k}")
    with open(os.path.join(args.out, "station.json"), "w", encoding="utf-8") as f:
        json.dump({"codex": cx.codex_version(codex_bin()), "results": results}, f, indent=1, ensure_ascii=False)
    return 0 if all(results[name].get("pass") for name, _ in todo) else 1


if __name__ == "__main__":
    sys.exit(main())
