"""The help text of `tares factory`: what `help`, `-h`, `--help` and no argument print, and what a command
that does not exist prints before it exits 1. Moved here from the old bash crew (removed in TR-975), where it was
the `usage` function; the orchestrator and station lines name `tares factory`, since there is no bare `factory`.
"""
from __future__ import annotations

USAGE = """\
tares factory: one crew of connected Claude Code sessions for all your projects, kept on Tares

  tares factory crew up [--yes] [--no-watch] [roles...]
                                            start the crew from the crew home (orchestrator first),
                                            and the watch in the background; the first run asks the
                                            crew settings
  tares factory crew status                 the stations, the questions for you, a line per project
  tares factory crew attach <role|station>  open a station in this terminal (answer the crew's
                                            questions in the orchestrator)
  tares factory crew desk                   the questions waiting for you, each in full
  tares factory crew logs <role|station>    a station's recent output
  tares factory crew watch [--once]         (crew up runs it in the background) revive stations
                                            that stopped, under the same name; start
                                            fresh any that Claude Code stopped for thrashing on
                                            autocompact; after an update of the plugin or of Claude
                                            Code, move each station, helpers too, onto it when
                                            it's idle (same conversation, the orchestrator last; a
                                            station that keeps coming back on an old spare is
                                            started again in a new conversation, then retried
                                            after a pause, and shows as retrying; the crew setting
                                            claude_update off stops this move); restart a station
                                            between its turns when you allow it something new in
                                            Tares (same conversation, its fresh settings have it);
                                            a frozen crew's watch moves no station onto a newer kit
                                            or Claude Code and is only told of one; between
                                            rounds, read the plan's limits and the context
                                            the Crew page asks for
                                            with the idle_wake setting on, wake an idle station only
                                            when Tares has work for it (and after a quiet spell, to
                                            check), and hold its tick instead of the station
                                            with the pace setting on, slow the crew before the
                                            plan's limits (no new builders, longest ticks, builders
                                            wait), and revive a station that stopped at the limit in
                                            its own conversation once the limit has reset
  tares factory crew update [--wait <s>]    move every station onto the Claude Code installed now,
                                            right away: each when it is idle (one someone has open
                                            waits), the orchestrator last, same conversation; then
                                            checks each runs it, one session per name; gives up on a
                                            busy one after --wait seconds (default 1800, 0 = never);
                                            run from a station, it leaves that station to the watch
                                            when that station is behind, else to restart;
                                            a frozen crew moves too, since you asked; with pace
                                            on, neither moves one while a plan limit is full
  tares factory crew restart [--wait <s>]   the same, for stations that already run it too
  tares factory crew halt ["<reason>"]      stop risky actions on every station until resume
  tares factory crew resume                 lift a halt
  tares factory crew freeze                 keep the crew on today's plugin kit: a station that starts
                                            or revives uses a copy of it, a running one keeps reading
                                            the checkout until it restarts, and the watch moves none
                                            onto a newer kit or Claude Code (it still tells you one
                                            is there)
  tares factory crew unfreeze               lift the freeze; the watch moves each station onto the
                                            checkout's kit when it's idle
  tares factory crew down                   stop the watch and every station (Tares keeps the record)
  tares factory spec ["<idea>"]             plan a project with Claude and /tares:spec, in the repo it
                                            builds on or a folder of its repos; it ends with the
                                            hand-over to the crew. Name a project someone else leads
                                            (`--project <name>`) to answer their questions
  tares factory session [<project> | [--new] "<idea>"]
                                            work on a project in your own Claude session, bound to
                                            it on Tares: a planned one (take it over from the crew
                                            if it has it), or a new idea planned lightly and built
                                            right there (--new: even when it is one word); no
                                            argument asks which
  tares factory projects                    every project handed to the crew: progress, the milestone
                                            it is on, its builders and repos
  tares factory finish <project> ["words"]  mark a project done yourself (the orchestrator otherwise
                                            asks you on the desk when its last milestone is done)
  tares factory restart <role|station> [--fresh]
                                            restart a station with fresh settings, same conversation
                                            (--fresh: a new conversation; its state is on Tares)
  tares factory context <role|station>      read a station's /context (its window by category) for
                                            the Crew page; the watch does it when you ask there
  tares factory plan                        read your plan's limits (/usage) for the Crew page; the
                                            watch does it every 10 minutes
  tares factory doctor                      check everything the crew needs
  tares factory settings <station> [role]   print the settings a station launches with

  used by the orchestrator, inside its session:
  tares factory roster                      stations, roles, projects, states, Claude Code version (tab separated)
  tares factory spawn builder [--autonomy L3-review] <project> <slug> <repo path> "<task>"
                                            one builder per milestone and repo of a project
  tares factory spawn helper <parent-station> <slug> "<task>"
  tares factory revive <station> [--fresh] ["<prompt>"]

  used by every station:
  tares factory tidy [--force] <path>...    remove a worktree or scratch folder, only inside the
                                            crew's own folders (instead of rm, which can stop a
                                            station on a prompt); --force for your own worktree
                                            with uncommitted or unpushed work
  tares factory retire <station>
  tares factory heavy <seconds> <command...>
                                            run a heavy command (a docker build, a big scan) alone on
                                            the machine, killed after <seconds>

Tares is the workspace `tares login` signed in to, else $TARESD_URL (default http://127.0.0.1:8787).
The crew's key is the saved login's; for a Tares of your own with auth on, TARES_AUTH_TOKEN.
The crew home is ~/factory (FACTORY_CREW_HOME overrides).
The crew is named `crew` unless you name it (FACTORY_CREW_NAME, or crew_name in crew.json): its stations are
<crew>-orchestrator, <crew>-reviewer and <crew>-releaser, so two crews can be told apart.
"""
