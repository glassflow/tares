"""The crew's read-only views: desk, status, roster, attach and logs (TR-957).

Ports cmd_desk, cmd_status, cmd_roster, cmd_attach and cmd_logs of claude-plugin/bin/factory, with the
read-only helpers they lean on (the kit fingerprint, the grants and Claude Code version checks, the pace
line, the watch's pid, adopt_unrecorded). Each prints its lines in a fixed order.

Helpers that a later ticket owns (the kit freeze, the Claude Code move, the pace) are read-only copies
here, so the default output of a command that runs in Python is the same as bash's. The ticket that ports
the owner moves them next to it and keeps this file calling them.

`settings` and a bare `crew` stay in bash: write_settings is ported with TR-953.

Untested on real Windows: the views read process command lines through psutil (watch_pid, attached) and
`attach` and `logs` start Claude Code through subprocess there, where bash `exec`s it.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

from . import claude, contract, state
from .cc_update import cc_behind, cc_retry_in, station_cc  # moved there by TR-963, TR-964; status and roster read them
from .client import TaresClient, TaresHTTPError, cksum
from .errors import CrewError, SessionListUnreadableError
from .kit import Frozen, frozen_phys, kit_version, plugin_root, stale_kit  # noqa: F401  (moved there by TR-965; status, roster and doctor read them)
from .lifecycle import watch_pid  # moved there by TR-955; the status view reads it
from .paths import ME, CrewPaths
from .regrant import regranted  # moved there by TR-960; the status view reads it
from .watch_revive import stuck_on_context  # moved there by TR-959; status and roster read it

LETTERS = "ABCDEFGH"
DESK_END = f"Answer in the orchestrator's session, one at a time: {ME} crew attach orchestrator"
ROW = "%-30s %-12s %-14s %-8s %-8s %s\n"


# ---------- small helpers ----------


def _say(message: str) -> None:
    sys.stdout.flush()  # keep stdout and stderr in the order bash prints them when both go to one pipe
    print(f"{ME}: {message}", file=sys.stderr)


def _truthy(value) -> bool:
    """jq's truthiness: everything but null and false."""
    return value is not None and value is not False


def _text(value) -> str:
    """A value inside a jq string interpolation: a string as is, anything else as compact JSON."""
    return value if isinstance(value, str) else state.dumps_compact(value)


def _field(obj, key: str, default: str = "") -> str:
    """`jq -r '.key // default'`."""
    value = obj.get(key) if isinstance(obj, dict) else None
    return state.jq_raw(value) if _truthy(value) else default


def _tsv(value) -> str:
    """One value as jq's @tsv prints it."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    return state.dumps_compact(value)


def _start(env: dict, *, local_only: bool):
    """What bash does before a command: a crew name that cannot be one stops here; a command that only
    touches this machine's sessions works with Tares down, the others need it."""
    paths = CrewPaths.from_env()
    state.crew_name(paths)
    client = TaresClient(paths, env)
    if local_only:
        state.merge_crew_defaults(paths)
        client.tares_on()
        client.keep_token()
    else:
        client.prime()
    return paths, client


def _guarded(fn):
    """Run fn(argv, env); a refusal prints its line to stderr and exits 1, as bash's `die` does. With --json
    it prints the error shape on stdout instead (contract.py)."""
    def run(argv: list, env: dict) -> int:
        try:
            return fn(argv, env)
        except CrewError as e:
            sys.stdout.flush()
            if not contract.refuse(e, argv):
                print(e.line, file=sys.stderr)
            return 1
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def _sessions_or_none(binary: str):
    """`claude agents --json` as a list; None when it cannot be read."""
    try:
        return claude.active_json(binary)
    except SessionListUnreadableError:
        return None


_NO_ROW = object()


def _turn_of(agents: list, sid):
    """The turn of the first live session with this id, _NO_ROW when none is live."""
    for row in agents:
        if row.get("id") == sid and not claude.gone(row):
            return claude.turn(row)
    return _NO_ROW


# ---------- the desk ----------


def desk_block(item: dict) -> str:
    """One desk item as the person reads it: its question, options and recommendation (desk_block)."""
    head = f"{_text(item.get('label'))}{' (blocks work)' if _truthy(item.get('blocking')) else ''}"
    if _truthy(item.get("project_name")):
        head += f" · {_text(item['project_name'])}"
    lines = [f"{head}: {_text(item.get('question'))}"]
    options = item.get("options")
    if isinstance(options, list):
        for i, value in enumerate(options):
            lines.append(f"  {LETTERS[i] if i < len(LETTERS) else i + 1}) {_text(value)}")
    why = f", since {_text(item['why'])}" if _truthy(item.get("why")) else ""
    lines.append(f"  recommended: {_text(item.get('recommendation'))}{why}")
    blocks = item.get("blocks")
    if _truthy(blocks) and blocks != "":
        lines.append(f"  blocks: {_text(blocks)}")
    assumptions = item.get("assumptions")
    if isinstance(assumptions, list) and assumptions:
        lines.append(f"  confirms: {', '.join(_text(a) for a in assumptions)}")
    if _truthy(item.get("command")):
        who = _text(item["station"]) if _truthy(item.get("station")) else None
        lines.append(f"  {who or 'a station'} was refused: {_text(item['command']).replace(chr(10), ' ')}")
        lines.append("  allow it in Tares, on the project page or the Crew page: the watch restarts "
                     f"{who or 'the station'} with it")
    return "\n".join(lines)


def cmd_desk(argv: list, env: dict) -> int:
    """`desk` and `crew desk`: the questions waiting for the person, in full."""
    paths, client = _start(env, local_only=False)
    state.require_crew(paths)
    items = client.desk_json()
    if contract.wants_json(argv):
        contract.emit({"items": items}, "desk")
        return 0
    if not items:
        print("Nothing waits for you on the desk.")
        return 0
    sys.stdout.write("".join(desk_block(item) + "\n\n" for item in items) + DESK_END + "\n")
    return 0


# ---------- the kit and the freeze: kit.py has them (TR-965); these names stay importable from here ----------


# ---------- grants, context, Claude Code versions ----------


def _active(paths: CrewPaths) -> list:
    return [s for s, row in state.stations_load(paths).items()
            if isinstance(row, dict) and row.get("status") == "active"]


# ---------- the watch and the pace ----------


def pace_status_line(client: TaresClient) -> str:
    """The pace in one line for crew status; "" when it is off or Tares cannot say."""
    if not client.tares_on():
        return ""
    try:
        p = state.loads_json(client.tares_http("GET", "/api/crew/pace"))
    except (TaresHTTPError, ValueError):
        return ""
    if not isinstance(p, dict) or p.get("on") is not True:
        return ""
    if p.get("known") is not True:
        return "pace: on, no reading of the plan limits yet"

    def pc(v):
        return "-" if v is None else f"{_text(v)}%"

    until = _text(p["until"]) if _truthy(p.get("until")) else "the reset"
    st = p.get("state")
    if st == "ok":
        how = "ok"
    elif st == "slow":
        how = f"slow, no new builders and the longest ticks, until {until}"
    elif st == "hold":
        how = f"hold, builders finish their step and wait, until {until}"
    else:
        how = f"at the limit, stopped stations wait for the reset, until {until}"
    return f"pace: {how} (5-hour {pc(p.get('session_pct'))}, week {pc(p.get('weekly_pct'))})"


# ---------- adopt_unrecorded ----------


def _job(sid: str) -> dict:
    try:
        return claude.job_state(sid)
    except CrewError:
        return {}


def _flag(flags, name: str) -> str:
    """`index(name) as $i | if $i then .[$i+1] else "" end`, as text."""
    if isinstance(flags, list) and name in flags:
        i = flags.index(name) + 1
        value = flags[i] if i < len(flags) else None
        return state.jq_raw(value) if value is not None else ""
    return ""


def adopt_unrecorded(paths: CrewPaths, client: TaresClient, binary: str) -> list | None:
    """Record a live station session stations.json does not list (a spawn whose output could not be
    read), so status, roster and the duplicate check see it. It asks Claude Code for the sessions itself;
    when they cannot be read nothing is recorded. It returns the list it read (adopting changes
    stations.json, not that list), so a command that needs the sessions next can use it instead of
    starting Claude Code again; None when there was no crew or the list could not be read."""
    if not paths.crew_cfg.is_file():
        return None
    live = _sessions_or_none(binary)
    if live is None:
        return None
    crew = state.crew_name(paths)
    pattern = re.compile("^" + re.escape(crew) + "-|-build-")
    rows = None
    for row in live:
        name = row.get("name")
        name = name if isinstance(name, str) else ""
        if not pattern.search(name) or claude.gone(row):
            continue
        sid = _tsv(row.get("id"))
        if not sid:
            continue
        # The folder as Claude Code writes it, not as jq @tsv prints it: bash compares two @tsv-escaped texts, so a
        # backslash path (Windows) matches there; here the repo path from Tares is raw, so cwd must be raw too (TR-1167).
        session, cwd = _tsv(row.get("sessionId")), _text(row.get("cwd")) if row.get("cwd") is not None else ""
        # A second live session under a recorded live station is a duplicate for status to show, not a replacement.
        recorded = state.stations_get(name, "id", paths=paths)
        if any(r.get("id") == recorded and not claude.gone(r) for r in live):
            continue
        project = repo = ""
        if name in (f"{crew}-orchestrator", f"{crew}-reviewer", f"{crew}-releaser"):
            role = name[len(crew) + 1:]
            if cwd != paths.raw:
                continue
        elif "-build-" in name:
            role, repo = "builder", cwd
            # Ours only when the name is this crew's builder name for a project of that repo.
            if rows is None:
                rows = client.project_repo_rows()
            for pname, prepo in rows:
                if prepo == cwd and name.startswith(state.builder_name(state.slugify(_text(pname)), "", paths)):
                    project = _text(pname)
                    break
            if not project:
                continue
        else:
            continue
        flags = _job(sid).get("respawnFlags")
        flags = flags if _truthy(flags) else []
        state.stations_put(name, {
            "role": role, "parent": state.crew_orchestrator(paths) if role == "builder" else "",
            "project": project, "repo": repo, "id": sid, "sessionId": session,
            "model": _flag(flags, "--model"), "effort": _flag(flags, "--effort"), "cwd": cwd,
            "settings": f"{paths.raw}/stations/{name}.settings.json", "status": "active",
            "startedAt": state.utc_stamp(), "adopted": True}, paths)
    return live


# ---------- status ----------


def cmd_status(argv: list, env: dict) -> int:
    """`status` and `crew status`."""
    paths, client = _start(env, local_only=False)
    state.require_crew(paths)
    js = contract.wants_json(argv)
    out = io.StringIO() if js else sys.stdout  # --json builds the document below; the text goes nowhere
    doc = {"crew": state.crew_name(paths), "orchestrator": state.crew_orchestrator(paths), "stations": [],
           "running": 0}
    binary = claude.claude_bin(env)
    adopt_unrecorded(paths, client, binary)
    if not paths.stations.is_file():
        _say(f"no stations yet; run {ME} crew up")
        if js:
            contract.emit(doc, "status")
        return 0
    agents = _sessions_or_none(binary)
    if agents is None:
        raise SessionListUnreadableError(claude.UNREAD)
    # Stations keep the Tares they were started with. After `tares login` to another workspace,
    # say so instead of letting the person think the crew moved.
    ran = state.jq_get(state.read_json(paths.station_file(state.crew_orchestrator(paths), "settings.json"), None),
                       ("env", "TARES_FACTORY_URL"))
    if ran and ran.rstrip("/") != client.tares_url():
        _say(f"the crew still runs against {ran}, not {client.tares_url()}: {ME} crew down, then {ME} crew up, "
             "to move it")
    frozen = Frozen(paths, client)
    kit = kit_version(plugin_root(paths, env))
    active = _active(paths)
    running = len(active)
    doc["running"] = running
    old = 0
    if running > 0:
        out.write(ROW % ("STATION", "ROLE", "MODEL", "STATE", "TEMPO", "DOING / NEEDS"))
    else:
        out.write(f"no stations running; {ME} crew up starts them\n")
    for s in active:
        role = state.stations_get(s, "role", paths=paths)
        if role != "helper" and stale_kit(paths, s, kit, frozen) and not paths.done_file(s).exists():
            old += 1
        sid = state.stations_get(s, "id", paths=paths)
        model = state.stations_get(s, "model", paths=paths)
        effort = state.stations_get(s, "effort", paths=paths)
        turn = _turn_of(agents, sid)
        st = "" if turn is _NO_ROW else state.jq_raw(turn)
        job = _job(sid)
        tempo = _field(job, "tempo", "-")
        detail = _field(job, "detail")
        needs = _field(job, "needs")
        if needs:
            detail = f"NEEDS: {needs}"
        if stuck_on_context(paths, binary, s, agents):
            detail = f"STUCK on autocompact: {ME} crew watch starts it fresh"
        if (st and state.stations_get(s, "sessionId", paths=paths) and not paths.done_file(s).exists()
                and regranted(paths, client, s)):
            if claude.attached(s, paths=paths):
                detail = "NEW PERMISSIONS: restarted with them once you leave its session"
            else:
                detail = "NEW PERMISSIONS: the watch restarts it with them between turns"
        out.write(ROW % (s, role, f"{model}/{effort}", st or "gone", tempo, detail[:70]))
        doc["stations"].append({"station": s, "role": role, "model": model, "effort": effort,
                                "state": st or "gone", "tempo": "" if tempo == "-" else tempo, "detail": detail})
    # two live sessions under one station name
    pattern = re.compile("^" + re.escape(state.crew_name(paths)) + "-|-build-")
    groups: dict = {}
    for r in _sessions_or_none(binary) or []:  # bash asks again here
        name = r.get("name")
        name = name if isinstance(name, str) else ""
        if pattern.search(name) and not claude.gone(r):
            groups.setdefault(name, []).append(_tsv(r.get("id")))
    doc["two_sessions"] = [{"name": n, "ids": groups[n]} for n in sorted(groups) if len(groups[n]) > 1]
    for name in sorted(groups):
        if len(groups[name]) > 1:
            out.write(f"TWO SESSIONS named {name}: {', '.join(groups[name])}; "
                      "stop the one stations.json does not list (tares factory roster)\n")
    is_frozen = frozen.on
    doc["plugin"] = {"older": old}
    if old == 0:
        pass
    elif is_frozen:
        out.write(f"plugin: {old} station(s) run an older version; the freeze holds them ({ME} crew unfreeze "
                  "lets the watch move each onto the checkout's)\n")
    else:
        out.write(f"plugin: {old} station(s) run an older version; {ME} crew watch moves each onto this one "
                  "when it's idle\n")
    target = claude.claude_version(binary)
    cc_old = cc_behind(paths, target, agents)
    cc_retry = sum(1 for s in active
                   if state.stations_get(s, "cc_failed", paths=paths) == target
                   and claude.older_than(station_cc(paths, s, agents), target))
    doc["claude_code"] = {"target": target, "older": cc_old, "retrying": cc_retry}
    if cc_old == 0:
        pass
    elif is_frozen:
        out.write(f"claude code: {cc_old} station(s) run an older Claude Code than {target}; the freeze stops the "
                  f"watch moving them ({ME} crew unfreeze lets it; {ME} crew update does it now; one that "
                  f"restarts starts on {target})\n")
    else:
        retry = f"; {cc_retry} retrying after coming back on an old spare" if cc_retry else ""
        out.write(f"claude code: {cc_old} station(s) run an older Claude Code than {target}; {ME} crew watch "
                  f"moves each onto it when it's idle ({ME} crew update does it now; {ME} roster shows each "
                  f"version){retry}\n")
    doc["halted"] = state.halt_reason(paths) if state.halted(paths) else None
    if state.halted(paths):
        out.write(f"HALTED: {state.halt_reason(paths)}\n")
    doc["frozen"] = None
    if is_frozen:
        newer = "" if kit == frozen.field("version") else " (a newer kit is available)"
        doc["frozen"] = {"kit": frozen.text(), "newer_kit": bool(newer)}
        out.write(f"FROZEN: the kit {frozen.text()}; stations that start or revive use it, running ones keep "
                  f"reading the checkout until they restart; the watch moves no station onto a newer kit{newer}\n")
    doc["idle_wake"] = bool(frozen.idle_wake)
    if frozen.idle_wake:
        out.write("idle_wake: on, the watch wakes a station when it has work and holds its tick (a liveness "
                  "wake after 15, 20 or 30 minutes of quiet, then 30, then every 45)\n")
    line = pace_status_line(client)
    doc["pace"] = line or None
    if line:
        out.write(line + "\n")
    pid = watch_pid(paths)
    doc["watch"] = {"running": bool(pid), "pid": int(pid) if str(pid).isdigit() else None}
    if pid:
        out.write(f"watch: running (pid {pid}), it revives any station that stops and gives one a permission "
                  f"you allowed in Tares; log in {paths.shown('watch.log')}\n")
    else:
        out.write(f"watch: not running; {ME} crew up starts it (or run {ME} crew watch yourself)\n")
    items = client.desk_json()
    # a finished project's questions only matter to a crew that is still running (TR-559)
    if running <= 0:
        items = [i for i in items if not _truthy(i.get("project_finished"))]
    doc["desk"] = {"waiting": len(items), "next": items[0] if items else None}
    if items:
        out.write(f"desk: {len(items)} waiting for you; the next one ({ME} crew desk shows all; answer in the "
                  "orchestrator's session):\n")
        out.write("".join("  " + ln + "\n" for ln in desk_block(items[0]).split("\n")))
    else:
        out.write("desk: nothing waits for you\n")
    doc["projects"] = []
    for p in client.crew_projects():
        total = p.get("tickets_total")
        ms = p.get("milestone")
        if _truthy(ms):
            tail = f", on {_text(ms.get('name'))} {_text(ms.get('done'))}/{_text(ms.get('total'))}"
        elif isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
            tail = ", all done"
        else:
            tail = ", no tickets yet"
        out.write(f"project {_text(p.get('name'))}: {_text(p.get('tickets_done'))}/{_text(total)} tickets done{tail}\n")
        doc["projects"].append({"name": p.get("name"), "tickets_done": p.get("tickets_done"), "tickets_total": total,
                                "milestone": ({"name": ms.get("name"), "done": ms.get("done"), "total": ms.get("total")}
                                              if _truthy(ms) and isinstance(ms, dict) else None)})
    out.write(f"Crew page: {client.tares}/crew\n")
    doc["crew_page"] = f"{client.tares}/crew"
    if js:
        contract.emit(doc, "status")
    return 0


# ---------- roster ----------


def cmd_roster(argv: list, env: dict) -> int:
    """`roster`: compact, for stations. One line per running station: name, role, project, turn, Claude Code."""
    paths, client = _start(env, local_only=True)
    js = contract.wants_json(argv)
    if not js:
        sys.stdout.write(f"crew: {state.crew_name(paths)} (orchestrator: {state.crew_orchestrator(paths)})\n")
    state.require_crew(paths)
    binary = claude.claude_bin(env)
    listed = adopt_unrecorded(paths, client, binary)
    doc = {"crew": state.crew_name(paths), "orchestrator": state.crew_orchestrator(paths), "stations": []}
    if not paths.stations.is_file():
        if js:
            contract.emit(doc, "roster")
        else:
            print("no stations")
        return 0
    # the list adopt_unrecorded read is still the list (a Claude Code start is most of this command's time);
    # when it could not be read, ask once more, as before
    agents = listed if listed is not None else _sessions_or_none(binary)
    if agents is None:
        raise SessionListUnreadableError(claude.UNREAD)
    stations = state.stations_load(paths)
    # Messages go to a station by its session title. An orchestrator that runs under another title
    # (the crew was renamed since it started) gets none of them.
    title = ""
    for row in stations.values():
        if isinstance(row, dict) and row.get("status") == "active" and row.get("role") == "orchestrator":
            for a in agents:
                if a.get("id") == row.get("id") and not claude.gone(a):
                    title = state.jq_raw(a.get("name"))
                    break
            if title:
                break
    if title and title != state.crew_orchestrator(paths):
        _say(f"Orchestrator session title is {title}, expected {state.crew_orchestrator(paths)}: "
             "messages to it will not arrive")
    # the fifth column is the Claude Code each station's process runs, flagged when it is older than the one installed
    target = claude.claude_version(binary)
    lines = []
    for s, row in stations.items():
        if not isinstance(row, dict) or row.get("status") != "active":
            continue
        turn = _turn_of(agents, row.get("id"))
        st = state.jq_raw(turn) if turn is not _NO_ROW and _truthy(turn) else "gone"
        v = station_cc(paths, s, agents)
        if claude.older_than(v, target):
            v = f"{v} (older than {target}"
            if state.stations_get(s, "cc_failed", paths=paths) == target:
                v += f", retrying in {cc_retry_in(paths, s)}"
            v += ")"
        project = _field(row, "project")
        lines.append(f"{s}\t{_field(row, 'role', 'null')}\t{project}\t{st}\t{v}\n")
        doc["stations"].append({"station": s, "role": row.get("role") if isinstance(row.get("role"), str) else None,
                                "project": project, "turn": st, "claude_code": v})
    if js:
        contract.emit(doc, "roster")
    else:
        sys.stdout.write("".join(lines))  # in one write, so a reader that stops early never breaks a pipe
    return 0


# ---------- attach and logs ----------


def _through(verb: str, argv: list, env: dict) -> int:
    """Open or follow a station's session: `claude <verb> <id>`, which replaces this process as bash's exec does."""
    paths, _client = _start(env, local_only=True)
    state.require_crew(paths)
    word = (argv[2:3] if argv[:1] == ["crew"] else argv[1:2])
    if not word:
        raise CrewError(f"usage: {ME} crew {verb} <role|station>")
    word = word[0]
    station = f"{state.crew_name(paths)}-{word}" if word in ("orchestrator", "reviewer", "releaser") else word
    sid = state.stations_get(station, "id", paths=paths)
    if not sid:
        raise CrewError(f"unknown station {word}")
    binary = claude.claude_bin(env)
    cmd = claude.claude_command(binary, [verb, sid], env=env)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name != "nt" and isinstance(cmd, list):
        try:
            os.execvpe(cmd[0], cmd, env)
        except OSError as e:
            raise CrewError(f"could not run {binary}: {e.strerror or e}") from e
    try:
        return subprocess.call(cmd, env=env)
    except OSError as e:
        raise CrewError(f"could not run {binary}: {e.strerror or e}") from e


def cmd_attach(argv: list, env: dict) -> int:
    """`attach <role|station>` and `crew attach ...`."""
    return _through("attach", argv, env)


def cmd_logs(argv: list, env: dict) -> int:
    """`logs <role|station>` and `crew logs ...`."""
    return _through("logs", argv, env)


COMMANDS = {
    "desk": _guarded(cmd_desk),
    "status": _guarded(cmd_status),
    "roster": _guarded(cmd_roster),
    "attach": _guarded(cmd_attach),
    "logs": _guarded(cmd_logs),
}
