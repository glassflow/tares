# A project the person takes over

Part of the station-orchestrator manual. Read when a `[TF:TAKEOVER]` arrives, or `my_work` shows a project under `releasing`.

The person may take a project back from the crew to build it in their own session (`tares factory session <project>`). Tares records it with their words and takes the project off `crew_projects` at once; it shows under `releasing`, with the stations that still hold its tickets (`holders`). Release it gracefully; nothing the builders did is thrown away:

1. Assign nothing more in that project and start no builder for it.
2. Send each of its builders (the `holders`, and any `<project>-build-*` in `tares factory roster`) `[TF:STOP]` with the project: finish the current step, commit and push what they have to their branch, write their progress in the working doc of each ticket they hold, then reply `DONE` with the branch and pull request of each ticket.
3. When each has replied (or after 15 minutes without a reply, or when it is gone), `tares factory retire <station>` it. Leave its branches, pull requests and worktree where they are.
4. `release_project(project)`. Tares clears the tickets' holders; the person's session owns the project.
5. Reply to the session named in the `TAKEOVER` (its `session:` line) with `[TF:RELEASED]`, listing each ticket the builders left in progress with its branch and pull request. Write it to your report. Tell the person nothing else about it unless they ask.

Note in your report which step you are on, so a compaction mid-takeover resumes at the right step.
