"""Reviving, restarting and retiring stations, stopping the crew, halting and resuming it (TR-955).

Ports these functions of `claude-plugin/bin/factory`, with the same behaviour and the same printed text:
cmd_revive, cmd_restart, cmd_retire, tell_tares_ended, cmd_down, cmd_halt, cmd_resume, rearm_text, watch_pid,
and the part of stop_watch that cmd_down needs. Where this module and the bash differ, the bash is right;
every difference is listed under "Differences from bash" below.

Standard library plus the crew modules and psutil (no duckdb, fastapi or daemon).

What is not here: the pace (pace.py, TR-962). The watch itself is watch.py (TR-958); watch_pid and stop_watch call it.
rearm_text reads the pace with pace.read.

A station on Codex (TR-1593)
  revive, restart and retire work for a station recorded on Codex: its app-server is started, handed a prompt
  and ended by tares.crew.codex_station, and no `claude` is looked for when the crew runs on Codex (`proc`).
  See _restart_codex for what a restart is there. The rest of this note is about Claude Code.

Differences from bash
  * A missing station argument (`tares factory revive` with nothing after it) is refused with
    "tares factory: usage: tares factory revive <station> [--fresh] [prompt]". Bash prints the shell's own
    "<script>: line N: 1: usage: ..." and exits 1.
  * A Python watch is asked to stop with a stop file and given time to finish its round before it is terminated
    (watch.stop); a process that is gone or may not be stopped is left alone, as bash's `kill ... || true` does.
"""
from __future__ import annotations

import contextlib
import subprocess
import sys
import time

from . import claude, codex_station, jsonrun, launch, locks, pace, proc, report, stations, state, watch
from .client import TaresClient, TaresHTTPError, uri
from .errors import ClaudeNotFoundError, CrewError, GuardOffError, NotBetweenTurnsError, SessionListUnreadableError
from .paths import ME, CrewPaths

REVIVE_USAGE = "usage: tares factory revive <station> [--fresh] [prompt]"
RESTART_USAGE = f"usage: {ME} restart <station>"
RETIRE_USAGE = "usage: tares factory retire <station>"


# ---------- the watch (the real one is watch.py, TR-958) ----------


def watch_pid(paths: CrewPaths):
    """The pid of the background watch when it runs, else None."""
    return watch.running_pid(paths)


def stop_watch(paths: CrewPaths, say) -> None:
    """Stop the background watch and drop its pid file; see watch.stop."""
    watch.stop(paths, say)


# ---------- what a restart tells a station ----------


REPORT_READ = ("re-read your report (a short state file; for older events use `tares factory report grep`, "
               "never cat or tail a file)")  # every reminder to a station says this, word for word


def rearm_text(client: TaresClient) -> str:
    """What a restart tells a station about its schedule. The pace is read here, as bash does for a command that
    has not read it yet (a watch round has its own reading in pace.step). Bash reads it in a command substitution,
    so a reading taken here is not remembered: a restart that read nothing before writes no pace file."""
    had = pace.has_read(client.paths)
    reading = pace.last(client.paths) if had else pace.read(client)
    if not had:
        pace.unremember(client.paths)
    if not client.tares_on():   # idle_wake is Tares's to say: a station revived while it is down cannot be told
        return ("re-arm your schedule if you have one, unless the factory tells you idle_wake is on (Tares did "
                "not answer when you were started, so it could not be read)")
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
    again = (f"Reload the skills factory-protocol and station-{skill}, {REPORT_READ} and your work on "
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
        raise CrewError(f"{station} was retired; tares factory spawn starts it again")
    if proc.is_alive(station, "", claude_bin, paths):
        raise CrewError(f"{station} is running; message it instead")
    if _codex_station(paths, station):
        # TR-1594. A station stopped for what a turn ran is started again by a person only (the watch leaves it
        # down): that turn is not held against it again. And with --fresh a person gives up a conversation whose
        # last turn was never compared; the watch and `restart` do not get here with --fresh for such a station.
        codex_station.seen_by_a_person(paths, station)
        if fresh:
            codex_station.give_up_unverified(paths, station, "revive --fresh")
        # what it had started and left running when it died: ended, so nothing runs twice, and said
        prompt = _with_cut_off(prompt, codex_station.end_leftovers(paths, station))
    launch.launch(client, station, role, parent, project, repo, cwd, model, effort, prompt,
                  "" if fresh else session, claude=claude_bin)
    # Resuming starts a copy under a new id; drop the stopped original so the agent view shows one row,
    # and keep it in the station's history so Tares and status show it as an earlier session.
    new_id = get("id")
    if old_id and old_id != new_id:
        _remove_session(old_id, claude_bin)
        _keep_earlier(paths, station, old_id, session)
    state.event("restarted-fresh" if fresh else "revived", station, new_id, paths=paths)


def _with_cut_off(prompt: str, names: list) -> str:
    """`prompt` with a first paragraph that names the programs the station had left running, when there were
    any: they were ended, and what they did is not known."""
    if not names:
        return prompt
    return (f"Before anything else: when your process ended, {len(names)} program(s) you had started were still "
            f"running ({', '.join(names)}), and the factory ended them. What they did is not known. Look at the "
            "state they may have changed (the working tree, the remote, your pull request, Tares) before you run "
            f"any of it again.\n\n{prompt}")


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


def restart(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin: str,
            only_between_turns: bool = False, prompt_wait=0) -> None:
    """factory restart <station> [--fresh]: stop it and start it again with freshly generated settings.
    With `only_between_turns` (a wake) the station is looked at again under its lock, right before it is
    stopped, and left running (NotBetweenTurnsError) when it has started a turn or been attached since.
    A station waiting on a person (a question or a permission prompt) is stopped only when it has waited
    `prompt_wait` seconds (`claude.quiet`'s `wait`: 0 at once, None the quiet wait of 30 minutes, `math.inf`
    never)."""
    state.require_crew(paths)
    if not args or not args[0]:
        _usage(RESTART_USAGE)
    station = stations.station_name(args[0], paths)
    if _codex_station(paths, station):
        return _restart_codex(client, paths, station, list(args[1:]), claude_bin, only_between_turns)
    job_id = state.stations_get(station, "id", paths=paths)
    if not job_id:
        raise CrewError(f"{station} is not a recorded station")
    locks.station_lock(station, paths)
    try:
        claude.active_json(claude_bin)
    except SessionListUnreadableError:
        raise SessionListUnreadableError(f"{claude.UNREAD}; not stopping {station} without being able to see it stop") from None
    if only_between_turns and (claude.attached(station, paths)
                               or not claude.between_turns(station, claude_bin, paths, wait=prompt_wait)):
        raise NotBetweenTurnsError(f"{station} is in a turn; not stopping it")
    _stop_session(job_id, claude_bin)
    for _ in range(_stop_wait(client.env)):
        if not claude.is_alive(station, "", claude_bin, paths):
            break
        time.sleep(1)
    if claude.is_alive(station, "", claude_bin, paths):
        raise CrewError(f"{station} did not stop within {_stop_wait_text(client.env)}s; try again")
    revive(client, paths, [station, *args[1:]], claude_bin=claude_bin)


def _codex_station(paths: CrewPaths, station: str) -> bool:
    """A recorded station whose runtime is Codex."""
    return bool(state.stations_get(station, "role", paths=paths)) and proc.on_codex(station, paths)


def _restart_codex(client: TaresClient, paths: CrewPaths, station: str, rest: list, claude_bin,
                   only_between_turns: bool) -> None:
    """restart for a station on Codex. A station that runs, is between turns and would be started with exactly
    what it runs on now is handed the prompt as its next turn, and its app-server is left alone: on Codex a
    prompt needs no restart. Anything else is a real restart: the app-server is stopped (a running turn is
    interrupted first) and the station is revived, with freshly made settings, in its own conversation or,
    with --fresh, a new one. `only_between_turns` (a wake) leaves a station in a turn alone
    (NotBetweenTurnsError). A station whose stop finds that its last turn ran something the guard has no
    record of is not started again (GuardOffError): `tares factory revive` does that, as for one the watch
    stopped, and a restart of a station that is down for that already is refused the same way. `--fresh`
    goes on only when this call's own stop compared the last turn: when the stop could not, when an earlier
    one could not, or when the station was not running (nobody to ask), the station is left down and the
    error names the two ways on. A restart without `--fresh` goes on in all three: its start compares first,
    in the same conversation."""
    locks.station_lock(station, paths)
    fresh = bool(rest) and rest[0] == "--fresh"
    prompt = (rest[1:] if fresh else rest)[:1]
    prompt = prompt[0] if prompt else ""
    alive = codex_station.is_alive(paths, station)
    resting = alive and codex_station.at_rest(paths, station)
    if only_between_turns and alive and not resting:
        raise NotBetweenTurnsError(f"{station} is in a turn; not stopping it")
    if resting and prompt and not fresh and _launched_as_now(client, paths, station):
        codex_station.hand(paths, station, prompt)
        state.event("prompted", station, paths=paths)
        return
    stopped = ""
    if alive:
        stopped = codex_station.stop(paths, station)
        if codex_station.is_alive(paths, station):
            raise CrewError(f"{station} did not stop; try again")
    # TR-1594. Looked at whether this call stopped the station or found it down: `revive` below takes its
    # caller for a person who has seen why the station is down, and a restart is not that.
    off = codex_station.guard_off(paths, station)
    if off:   # stopped for what it ran past the guard, now or before: it stays down until `revive`
        raise GuardOffError(station, off)
    if fresh:
        # A new conversation gives the old one up, so its last turn must have been compared, by this call's
        # own stop. A station that was not running has nobody to ask: that is a comparison that could not be
        # made, like one that failed, and is written down the same way.
        thread = state.stations_get(station, "sessionId", paths=paths)
        if thread and stopped in ("", "not running") and not codex_station.unverified(paths, station):
            codex_station.mark_unverified(paths, station, thread, "the station was not running, so there was "
                                                                  "nobody to ask what its last turn ran")
        pending = codex_station.unverified(paths, station)
        if pending:   # not compared, so not known to be in order: not given up for a new conversation
            raise CrewError(
                f"{station} is down and is not started in a new conversation: what its last turn ran could not be "
                f"compared with the guard's record ({pending.get('why')}). `tares factory revive {station}` starts "
                f"it in its own conversation, where the comparison is made first; `tares factory revive {station} "
                "--fresh` starts it in a new one without the comparison")
    revive(client, paths, [station, *rest], claude_bin=claude_bin)


def _launched_as_now(client: TaresClient, paths: CrewPaths, station: str) -> bool:
    """Would a start now give the station exactly what it runs on? False when that cannot be told (the launch
    cannot be had), so a restart then starts it anew, or says why it cannot."""
    get = lambda key: state.stations_get(station, key, paths=paths)  # noqa: E731
    try:
        plan = launch.launch_plan(client, station, get("role"), get("parent"), get("project"), get("repo"),
                                  get("model"), get("effort"))
        now = launch.cx.fingerprint(plan, launch.kit_version(launch.kit_dir(client)))
    except CrewError:
        return False
    return now == codex_station.info(paths, station).get("launched")


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
    on_codex = _codex_station(paths, station)
    job_id = state.stations_get(station, "id", paths=paths)
    if not job_id and not on_codex:
        raise CrewError(f"{station} is not a recorded station")
    locks.station_lock(station, paths)
    if on_codex:   # its own app-server: a running turn is interrupted, then the process is ended
        job_id = f"codex app-server {codex_station.stop(paths, station)}"
        codex_station.give_up_unverified(paths, station, "retire")   # retired for good: nothing waits on it
    else:
        _stop_session(job_id, claude_bin)
        _remove_session(job_id, claude_bin)
    state.stations_put(station, {"status": "retired"}, paths)
    with contextlib.suppress(FileNotFoundError):
        paths.pace_station_file(station).unlink()   # what the pace told it (TR-674)
    state.event("retired", station, job_id, paths=paths)
    tell_tares_ended(client, paths, station)
    client.say(f"retired {station}")
    parked = report.undelivered(paths, station)
    if parked:
        client.say(f"warning: {station}'s report holds {len(parked)} line{'s' if len(parked) != 1 else ''} under "
                   "## Undelivered; `report grep --section` skips a retired station, so read them in the file and pass "
                   f"them on ({paths.reports_dir / (station + '.md')})")


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
    # the person's own questions only: on a shared cell another person's are not ours to withdraw (TR-1355)
    for item_id in _stop_questions(client.desk_json(mine=True)):
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


def _command(fn, *, local_only: bool, name: str = "", offline_ok: bool = False):
    """cmd_x(argv, env) for fn(client, paths, args, claude_bin=...). argv is everything after `tares factory`:
    the command word, or `crew` and its subcommand, then the arguments. A refusal prints its line to stderr
    and exits 1, as bash's die does. With --json (TR-1283) it runs the same and prints one result document
    named `name` on stdout (jsonrun.py). `offline_ok`: the command goes on with Tares down, from what an earlier
    start kept (a revive or restart starts from the station's launch copy)."""
    def body(argv: list, env: dict) -> int:
        words = 2 if argv[:1] == ["crew"] else 1
        paths = CrewPaths.from_env()
        # bash looks for claude before it does anything else; a Codex crew needs none. A crew that has recorded
        # no station yet is asked after Tares was: its first station may be about to start on Codex.
        no_claude = None
        try:
            binary = proc.binary(env, paths)
        except ClaudeNotFoundError as e:
            if launch.crew_runtime(paths):
                raise
            binary, no_claude = "", e
        state.crew_name(paths)
        client = TaresClient(paths, env)
        try:
            if no_claude is not None and proc.planned_runtime(client) != proc.CODEX:
                raise no_claude
            if local_only:   # these work while Tares is down: stopping the crew never waits for it
                state.merge_crew_defaults(paths)
                client.tares_on()
                client.keep_token()
            else:
                client.prime(offline_ok=offline_ok)
            fn(client, paths, list(argv[words:]), claude_bin=binary)
        finally:
            client.close()
        return 0

    def text_fail(e: CrewError) -> None:
        sys.stdout.flush()
        print(e.line, file=sys.stderr)

    def run(argv: list, env: dict) -> int:
        return jsonrun.run(argv, name or fn.__name__, lambda a: body(a, env), text_fail=text_fail)
    run.__name__ = f"cmd_{fn.__name__}"
    run.__doc__ = fn.__doc__
    return run


cmd_revive = _command(revive, local_only=False, name="revive", offline_ok=True)
cmd_restart = _command(restart, local_only=False, name="restart", offline_ok=True)
cmd_retire = _command(retire, local_only=True, name="retire")
cmd_down = _command(down, local_only=True, name="crew down")
cmd_halt = _command(halt, local_only=True, name="crew halt")
cmd_resume = _command(resume, local_only=True, name="crew resume")
