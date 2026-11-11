"""Reviving, restarting and retiring stations, stopping the crew, halting and resuming it (TR-955).

Ports these functions of `claude-plugin/bin/factory`, with the same behaviour and the same printed text:
cmd_revive, cmd_restart, cmd_retire, tell_tares_ended, cmd_down, cmd_halt, cmd_resume, rearm_text, watch_pid,
and the part of stop_watch that cmd_down needs. Where this module and the bash differ, the bash is right;
every difference is listed under "Differences from bash" below.

Standard library plus the crew modules and psutil (no duckdb, fastapi or daemon).

What is not here: the pace (pace.py, TR-962). The watch itself is watch.py (TR-958); watch_pid and stop_watch call it.
rearm_text reads the pace with pace.read.

Differences from bash
  * A missing station argument (`factory revive` with nothing after it) is refused with
    "tares factory: usage: factory revive <station> [--fresh] [prompt]". Bash prints the shell's own
    "<script>: line N: 1: usage: ..." and exits 1.
  * A Python watch is asked to stop with a stop file and given time to finish its round before it is terminated
    (watch.stop); a process that is gone or may not be stopped is left alone, as bash's `kill ... || true` does.
"""
from __future__ import annotations

import contextlib
import subprocess
import sys
import time

from . import claude, launch, locks, pace, stations, state, watch
from .client import TaresClient, TaresHTTPError, uri
from .errors import CrewError, SessionListUnreadableError
from .paths import ME, CrewPaths

REVIVE_USAGE = "usage: factory revive <station> [--fresh] [prompt]"
RESTART_USAGE = f"usage: {ME} restart <station>"
RETIRE_USAGE = "usage: factory retire <station>"


# ---------- the watch (the real one is watch.py, TR-958) ----------


def watch_pid(paths: CrewPaths):
    """The pid of the background watch when it runs, else None."""
    return watch.running_pid(paths)


def stop_watch(paths: CrewPaths, say) -> None:
    """Stop the background watch and drop its pid file; see watch.stop."""
    watch.stop(paths, say)


# ---------- what a restart tells a station ----------


def rearm_text(client: TaresClient) -> str:
    """What a restart tells a station about its schedule. The pace is read here, as bash does for a command that
    has not read it yet (a watch round has its own reading in pace.step). Bash reads it in a command substitution,
    so a reading taken here is not remembered: a restart that read nothing before writes no pace file."""
    had = pace.has_read(client.paths)
    reading = pace.last(client.paths) if had else pace.read(client)
    if not had:
        pace.unremember(client.paths)
    if launch.idle_wake_on(client):
        return ("arm no tick of your own (idle_wake is on: the factory wakes you when there is work and holds "
                "your tick; if you hold one, delete it with CronList and CronDelete)")
    if reading.paced:
        return ("re-arm your schedule if you have one, at 45 minutes while the factory paces the crew before "
                "the plan's limit")
    return "re-arm your schedule if you have one"


# ---------- reviving ----------


def _usage(text: str):
    raise CrewError(text)


def _stop_session(job_id: str, binary: str) -> None:
    """`claude stop <id>`, whatever comes of it (bash: `|| true`)."""
    try:
        claude.run_claude(["stop", job_id], binary)
    except (OSError, subprocess.SubprocessError):
        pass


def _remove_session(job_id: str, binary: str) -> None:
    try:
        claude.run_claude(["rm", job_id], binary)
    except (OSError, subprocess.SubprocessError):
        pass


def revive(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str) -> None:
    """factory revive <station> [--fresh] [prompt]: same name, same env; the conversation resumed, or a new one."""
    state.require_crew(paths)
    if not args or not args[0]:
        _usage(REVIVE_USAGE)
    station = stations.station_name(args[0], paths)
    rest = list(args[1:])
    fresh = bool(rest) and rest[0] == "--fresh"
    if fresh:
        rest = rest[1:]
    get = lambda key: state.stations_get(station, key, paths=paths)  # noqa: E731
    role = get("role")
    if not role:
        raise CrewError(f"{station} is not a recorded station")
    parent, project, repo = get("parent"), get("project"), get("repo")
    cwd = get("cwd") or paths.raw
    model, effort, session = get("model"), get("effort"), get("sessionId")
    old_id = get("id")
    skill = launch.parent_role(parent, paths) if role == "helper" else role
    prompt = rest[0] if rest else ""
    again = (f"Reload the skills factory-protocol and station-{skill}, re-read your report and your work on "
             f"Tares, {rearm_text(client)}, then carry on where your report left off.")
    if not prompt and fresh:
        prompt = ("You were started again under the same name, in a new conversation: the last one kept "
                  "refilling its context right after each compaction, so Claude Code stopped it. Nothing from "
                  f"it carries over except your report and what is on Tares. {again} Read big files, logs and "
                  "diffs in parts.")
    if not prompt:
        prompt = f"You were revived by the factory under the same name. {again}"
    locks.station_lock(station, paths)
    if get("status") == "retired":
        raise CrewError(f"{station} was retired; factory spawn starts it again")
    if claude.is_alive(station, "", claude_bin, paths):
        raise CrewError(f"{station} is running; message it instead")
    launch.launch(client, station, role, parent, project, repo, cwd, model, effort, prompt,
                  "" if fresh else session, claude=claude_bin)
    # Resuming starts a copy under a new id; drop the stopped original so the agent view shows one row,
    # and keep it in the station's history so Tares and status show it as an earlier session.
    new_id = get("id")
    if old_id and old_id != new_id:
        _remove_session(old_id, claude_bin)
        _keep_earlier(paths, station, old_id, session)
    state.event("restarted-fresh" if fresh else "revived", station, new_id, paths=paths)


def _keep_earlier(paths: CrewPaths, station: str, old_id: str, session: str) -> None:
    """Add the replaced session to the station's `earlier` list in stations.json."""
    try:
        data = state.stations_load(paths)
        row = data.get(station)
        if not isinstance(row, dict):
            return
        earlier = row.get("earlier")
        if earlier is None or earlier is False:
            earlier = []
        if not isinstance(earlier, list):
            return
        row["earlier"] = earlier + [{"id": old_id, "sessionId": session, "replacedAt": state.utc_stamp()}]
        state.write_json(paths.stations, data)
    except (CrewError, OSError):
        pass


def restart(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str) -> None:
    """factory restart <station> [--fresh]: stop it and start it again with freshly generated settings."""
    state.require_crew(paths)
    if not args or not args[0]:
        _usage(RESTART_USAGE)
    station = stations.station_name(args[0], paths)
    job_id = state.stations_get(station, "id", paths=paths)
    if not job_id:
        raise CrewError(f"{station} is not a recorded station")
    locks.station_lock(station, paths)
    try:
        claude.active_json(claude_bin)
    except SessionListUnreadableError:
        raise SessionListUnreadableError(f"{claude.UNREAD}; not stopping {station} without being able to see it stop") from None
    _stop_session(job_id, claude_bin)
    for _ in range(_stop_wait(client.env)):
        if not claude.is_alive(station, "", claude_bin, paths):
            break
        time.sleep(1)
    if claude.is_alive(station, "", claude_bin, paths):
        raise CrewError(f"{station} did not stop within {_stop_wait_text(client.env)}s; try again")
    revive(client, paths, [station, *args[1:]], claude_bin=claude_bin)


def _stop_wait_text(env) -> str:
    return env.get("FACTORY_STOP_WAIT") or "10"


def _stop_wait(env) -> int:
    """How many one-second looks `seq "${FACTORY_STOP_WAIT:-10}"` gives; none for a number below 1 or for text."""
    try:
        return max(int(float(_stop_wait_text(env))), 0)
    except (ValueError, OverflowError):
        return 0


# ---------- retiring ----------


def retire(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str) -> None:
    """factory retire <station>: stop it for good (stations.json keeps it, as retired)."""
    state.require_crew(paths)
    if not args or not args[0]:
        _usage(RETIRE_USAGE)
    station = stations.station_name(args[0], paths)
    job_id = state.stations_get(station, "id", paths=paths)
    if not job_id:
        raise CrewError(f"{station} is not a recorded station")
    locks.station_lock(station, paths)
    _stop_session(job_id, claude_bin)
    _remove_session(job_id, claude_bin)
    state.stations_put(station, {"status": "retired"}, paths)
    with contextlib.suppress(FileNotFoundError):
        paths.pace_station_file(station).unlink()   # what the pace told it (TR-674)
    state.event("retired", station, job_id, paths=paths)
    tell_tares_ended(client, paths, station)
    client.say(f"retired {station}")


def tell_tares_ended(client: TaresClient, paths: CrewPaths, station: str) -> None:
    """Tares learns a station ended only from its session's last line, which a stopped or killed session never
    sends: say it here, or its row stays Working or Quiet. Never fails the retire; a station that never
    reported to Tares (404) is fine. The crew goes along: other crews have a station of the same name."""
    if not client.tares:
        client.tares = client.tares_url()
    token = client.tares_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers["Content-Type"] = "application/json"
    url = f"{client.tares}/api/crew/{uri(station)}/end?crew={uri(state.crew_name(paths))}"
    try:
        code = client._request("POST", url, headers, b'{"reason":"retired"}', 3).status_code
    except TaresHTTPError:
        code = 0
    if code not in (200, 404):
        client.say(f"could not tell Tares at {client.tares} that {station} ended; mark it ended on the Crew page")


# ---------- stopping the crew ----------


def down(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str) -> None:
    """factory crew down: stop the watch, retire every active station, withdraw the questions that are moot."""
    state.require_crew(paths)
    stop_watch(paths, client.say)   # first, so it doesn't revive what is being stopped
    if not paths.stations.is_file():
        client.say("no stations recorded")
        return
    for name, row in list(state.stations_load(paths).items()):
        if isinstance(row, dict) and row.get("status") == "active":
            retire(client, paths, [name], claude_bin=claude_bin)
    # with no crew left to relay them, a finished project's questions are moot, and so is "stop the
    # crew?", asked with or without a project: it names this command (TR-599)
    for item_id in _finished_questions(client.desk_json()):
        _withdraw(client, item_id, "the crew stopped and the project is finished")
    for item_id in _stop_questions(client.desk_json()):
        _withdraw(client, item_id, "the person stopped the crew")
    client.say(f"all stations stopped. Tares keeps the projects, tickets and the desk; {ME} crew up starts fresh sessions.")


def _truthy(value) -> bool:
    return value is not None and value is not False


def _item_id(item: dict) -> str:
    return state.jq_raw(item.get("id"))


def _finished_questions(desk: list) -> list:
    """`.[] | select(.project_finished) | .id`"""
    return [_item_id(i) for i in desk if isinstance(i, dict) and _truthy(i.get("project_finished"))]


def _stop_questions(desk: list) -> list:
    """The ids of the items whose question names `crew down` or says "stop the crew" (in any case). jq stops
    on a question that is not text, and then nothing is withdrawn."""
    found = []
    for item in desk:
        if not isinstance(item, dict):
            return []
        q = item.get("question")
        q = "" if not _truthy(q) else q
        if not isinstance(q, str):
            return []
        if "crew down" in q or "stop the crew" in q.lower():
            found.append(_item_id(item))
    return found


def _withdraw(client: TaresClient, item_id: str, reason: str) -> None:
    try:
        client.tares_http("POST", f"/api/desk/{item_id}/withdraw", '{"reason":"%s"}' % reason)
    except CrewError:
        pass


# ---------- halting and resuming ----------


def halt(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str = "") -> None:
    """factory crew halt [reason]: write the HALT file."""
    state.require_crew(paths)
    reason = args[0] if args else ""
    state.halt_write(reason or "halted by the human", paths=paths)
    state.event("halt", "", reason, paths=paths)
    client.say(f"crew halted: stations finish their current step and start nothing risky (merges, pushes, "
               f"deploys, tags, new sessions). {ME} crew resume lifts it.")
    client.say(f"tell the orchestrator too ({ME} crew attach orchestrator) so it sends HALT to every station")


def resume(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str = "") -> None:
    """factory crew resume: remove the HALT file."""
    state.require_crew(paths)
    state.resume(paths)
    state.event("resume", paths=paths)
    client.say("crew resumed")


# ---------- the commands ----------


def _command(fn, *, local_only: bool):
    """cmd_x(argv, env) for fn(client, paths, args, claude_bin=...). argv is everything after `tares factory`:
    the command word, or `crew` and its subcommand, then the arguments. A refusal prints its line to stderr
    and exits 1, as bash's die does."""
    def run(argv: list, env: dict) -> int:
        words = 2 if argv[:1] == ["crew"] else 1
        paths = CrewPaths.from_env()
        try:
            binary = claude.claude_bin(env)   # bash looks for claude before it does anything else
            state.crew_name(paths)
            client = TaresClient(paths, env)
            try:
                if local_only:   # these work while Tares is down: stopping the crew never waits for it
                    state.merge_crew_defaults(paths)
                    client.tares_on()
                    client.keep_token()
                else:
                    client.prime()
                fn(client, paths, list(argv[words:]), claude_bin=binary)
            finally:
                client.close()
        except CrewError as e:
            sys.stdout.flush()
            print(e.line, file=sys.stderr)
            return 1
        return 0
    run.__name__ = f"cmd_{fn.__name__}"
    run.__doc__ = fn.__doc__
    return run


cmd_revive = _command(revive, local_only=False)
cmd_restart = _command(restart, local_only=False)
cmd_retire = _command(retire, local_only=True)
cmd_down = _command(down, local_only=True)
cmd_halt = _command(halt, local_only=True)
cmd_resume = _command(resume, local_only=True)
