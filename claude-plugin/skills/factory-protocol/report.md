# Your report

Part of the factory-protocol skill. Read when you boot, after a compaction or a revive, and before you write your report.

`$FACTORY_HOME/reports/<your station>.md` is your **current state**, not your history: what a fresh session of your station needs to carry on, and nothing it can read on Tares. Keep it under 4 KB and never over 8 KB (`tares factory report log` warns past that). Its sections:

- `## Now`: what you are doing and the next step, in a few lines.
- `## Waiting on`: each thing you still owe or wait for, one line with its ref (`TR-12 assign once Tares syncs it`, `D92 retry desk_answer next tick`, `PR 491 merge is Kiran's`).
- `## Desk` (orchestrator): the desk item you are asking and when you last showed it.
- `## Undelivered`: messages a peer did not get. Always keep this section: the watch and the releaser read it.
- `## Bouncing` (orchestrator): each bouncing ticket and the ladder step you answered.
- `## Taken` (releaser): each `RELEASE` you took and has not shipped.

Rewrite a section when it changes and drop a line once it is done. The report never grows by appending, and a tick where nothing changed writes nothing. After a compaction or a revive, Read the report whole (one call), then your work on Tares: that is your whole recovery.

## History goes to the log

What happened (an event, a result, a decision) goes to your log, one line each, with `tares factory report log "<what, refs, evidence>"`. It dates the line into `reports/log/<station>/<day>.md`. Never log a tick where nothing changed.

To look something up, ask Tares first: a ticket's history and checks (`get_ticket`), decisions and desk items (`list_decisions`, `desk_list`), milestone checks (`list_milestones`), assumptions (`list_assumptions`), the project's timeline (`project_timeline`). What Tares does not hold (a CI run id, a command you ran) is searched with `tares factory report grep "<regex>" [--station <station>] [--days <n>]`, which prints at most 40 lines and 4 KB, newest first.

Never `cat`, `tail`, `sed` or `grep` a report or a log file, and never append to one by hand. Never open a log, or any file that keeps growing (a table you add to), with Read: Claude Code attaches every file you Read again after each compaction. Such a table belongs in your log.
