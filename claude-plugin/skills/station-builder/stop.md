# When the orchestrator says STOP

Part of the station-builder manual. Read on a `[TF:STOP]`.

A `[TF:STOP]` means the person took the project over to finish it in their own session. Finish the
step you are in (never leave a half-applied change), then for each ticket you hold: commit and push
what you have to its branch (even unfinished: say so in the commit message), and write in its
working doc's `## Progress` what is done, what is next and how to run it. Leave the stage, the pull
request and the worktree as they are. `CronDelete` your tick, reply `DONE` to the orchestrator with
each ticket's branch and pull request, and start nothing new.
