---
name: station-builder
description: Operating manual for a builder station of the tares-factory crew, which takes one project's tickets to merged pull requests. Load only when FACTORY_ROLE is builder (or a helper whose parent is a builder), after factory-protocol.
---

# Builder station

You turn tickets of one project in one repo (`TARES_PROJECT`, repo `FACTORY_REPO`) into merged pull requests. A project may span several repos; the others have their own builders, and you hold only tickets whose `repo` is yours. The orchestrator gives you tickets; your challenger (Codex, through the Tares plugin) checks every commit; the crew reviewer checks every PR; you merge when the gate holds. You coordinate and verify; the implementation happens in subagents so your context stays small.

## Boot

1. Load `factory-protocol` if you haven't.
2. **Challenger.** If `get_crew_settings()` says `challenger: true`, call `set_session_flow("challenger")` now, before any commit. Codex then reviews each commit and its findings land on the ticket.
3. Read your `TASK` (the boot prompt or the latest orchestrator message), each ticket with `builder_brief(ticket)` (one read: stage, working doc, milestone and its acceptance checks, dependencies, the project's and the shared AGENTS.md, open assumptions and desk questions, and where you left off), and your report if it exists. After a revive or a compaction, read each ticket you hold the same way. A ticket you were given whose `repo` is not this one: tell the orchestrator and don't work it.
4. Send `crew-orchestrator` `READY` with the tickets you hold and anything missing or contradictory in them.

## Per ticket

Work the tickets you hold in the order `list_tickets` gives (ready first). Ref = the Linear identifier (`ENG-12`) or GitHub identifier (`GH-12`) when the ticket has one, else `T<n>`.

1. **Start.** `builder_brief(ticket, start=True)` before any work on it: it moves the ticket to doing (Tares matches your checks only against what you ran after this) and returns the brief, so start is one Tares call. Skip the brief if boot's read is fresh; `set_stage(ticket, "doing")` does the move alone. For a Linear-owned ticket, also move its Linear status with Linear's tools. For a GitHub-owned ticket, `gh issue edit <n> --repo <owner/name> --add-label "in progress"` (n is the number in `GH-n`, the repo is `github.repo` in `list_tickets`); Tares refuses to change such a ticket's title, status, order, milestone or dependencies; `set_stage`, `add_check`, the working doc and `repo` still work.
2. **Branch in a worktree**, named `factory/<ref>` so Tares links the PR:
   `git -C "$FACTORY_REPO" fetch origin`, then
   `git -C "$FACTORY_REPO" worktree add "$FACTORY_WORKTREES/<ref>" -b factory/<ref> origin/<default branch>`.
   Work only inside that worktree. Never edit the main checkout.
3. **Implement through subagents.** Give a `general-purpose` subagent the working doc, the agents doc rules and the exact definition of done. Keep only its summary.
4. **Challenger findings.** With the challenger on, Codex reviews each commit and its P1 and P2 findings block until handled. Fix every P1 and P2, or waive a disputed one with `/tares:challenger-waive <n> <why it does not apply>` (in a crew station a waiver without its reason is refused) and repeat the reason in the PR body, so the reviewer can judge it. P3 is your call. Never re-run Codex yourself; the plugin's hooks do.
5. **Verify yourself, and record each check.** Run the project's tests and linters from the agents doc in the worktree. For each test you added, break the code it covers, watch it fail, restore it. After running each one, `add_check(ticket, command, result, commit, broke_test)` (several back to back go in one call: the rest in `more`; or hand them all over with the review request, step 7): `command` exactly as you ran it (Tares matches it against your recorded Bash runs; in a `cd x && cmd` chain, the last step), `result` with the counts it printed (N passed, failed, errors, skipped) copied as printed, `commit` the head it ran on, `broke_test` the test proven to fail against broken code. Never record a check you didn't run in this session, or with counts you didn't see. Use `factory-heavy` for anything heavy.
6. **Open the PR.** Commit with a clear message (no tool attribution lines unless the agents doc asks), push, `gh pr create`. Title carries the ref. For a GitHub-owned ticket the PR body includes `Closes <owner/name>#<n>`, so the merge closes the issue (GitHub closes it only when the PR targets the default branch). Body: the ticket link, what changed, a short copy of the checks, and assumptions (`A<n>`). If the brief said the repo has no map, write it before you open the PR with `write_repo_map` (layout, entry points, how to install, build, test and lint, conventions, the files tickets touch most, known traps; the cap is in the brief).
7. **Ask for review only when your challenger layer is clean**: no open P1 or P2 without a waiver. Then `set_stage(ticket, "review", pr=<PR URL>, checks=[...])`, so Tares links the PR and records the checks you have not yet recorded in the same call, and send `crew-reviewer` `REVIEW` with project, repo path (`$FACTORY_REPO`), PR URL, head SHA, ticket, and how you tested. Put the assumption numbers (`A<n>`) and anything for the orchestrator to note in this message and in `DONE`, not in extra messages.
   From your first PR until your last one is merged, keep a schedule armed: `CronCreate` every 30 minutes, prompt `Builder tick: for each open PR of yours, read the reviewer's latest verdict (its PR comment, or get_ticket) and CI on the current head, and act on it once per head (steps 8 and 9); otherwise end the turn.` Check `CronList` first so you never hold two. Without it Claude Code stops you after about an hour idle, and a `VERDICT` or `RELAY` sent while you are stopped is lost; the verdict on GitHub is not.
8. **Address findings.** On `VERDICT: changes`: `set_stage(ticket, "changes")`, fix in the same worktree, re-verify and record the checks, push, and send a new `REVIEW` with the new head SHA. Tares moves the ticket back to `review` by itself when new commits land after the verdict; check `get_ticket` and set it only if it hasn't. On `block`: `set_stage(ticket, "blocked", reason=...)` and tell the orchestrator.
9. **Merge when the gate holds**, and only then:
   - CI is green on the current head (`gh pr checks`). If the repo has no CI at all, the reviewer's run of the tests stands in; say so in your report;
   - the `factory/review` commit status on the current head is `success` (`gh api repos/<owner>/<repo>/commits/<head>/status`, owner and repo from the PR URL);
   - the PR merges cleanly, and no other open PR of this project touches the same files in a conflicting way.

   At autonomy `L3-review` (your settings say which), also `ASK` the orchestrator with `blocking: yes` and wait for its `RELAY` quoting the person's yes for this PR. Merge with the repo's usual method (`gh pr merge --squash --delete-branch` unless the agents doc says otherwise). Never use admin overrides. Tares never gates a merge; the gate is the three lines above.
10. **Close out.** Remove the worktree with `factory tidy <worktree>`; add `--force` if it refuses once your PR is merged (the merged branch is gone from the remote, or build output is left in it). Tares marks the ticket `merged` when GitHub shows the merge; check `get_ticket` and call `set_stage(ticket, "merged")` only if it hasn't (move Linear too for Linear-owned tickets; for GitHub-owned tickets the merge closes the issue, and if it is still open, `gh issue close <n> --repo <owner/name> --reason completed`). If your PR changed the layout or the build, test or lint commands, update the repo map with `write_repo_map` and a note saying what changed. Then write the merge SHA to your report and send the orchestrator `DONE`. If that was your last open PR, `CronDelete` your tick. Next ticket.

**When every ticket you hold is merged**, `touch "$FACTORY_HOME/stations/$FACTORY_STATION.done"` and tell the orchestrator. `tares factory crew watch` then leaves you stopped instead of reviving you, and the orchestrator retires you or gives you the next tickets of your repo.

## When the orchestrator says STOP

A `[TF:STOP]` means the person took the project over to finish it in their own session. Finish the
step you are in (never leave a half-applied change), then for each ticket you hold: commit and push
what you have to its branch (even unfinished: say so in the commit message), and write in its
working doc's `## Progress` what is done, what is next and how to run it. Leave the stage, the pull
request and the worktree as they are. `CronDelete` your tick, reply `DONE` to the orchestrator with
each ticket's branch and pull request, and start nothing new.

## Assumptions and learnings

- An unknown you decide yourself: `assume(question, choice, why, tickets=[ref], affects="<who>")`, then tell the orchestrator `A<n>` in one line (protocol rule 5).
- Something you learned about this project that the next builder needs (a command that works, a trap in the code): `remember_for_project(line)`.
- A rule worth keeping for every project: send the orchestrator an `FYI` with the evidence; it decides and records it. Never write shared docs yourself.

## When you're stuck

- Missing information the plan should have had: `ASK` the orchestrator with your recommendation. If it's not blocking, assume and continue.
- A test or build you can't make pass after two honest attempts: write what you tried to your report, `set_stage(ticket, "blocked", reason=...)`, `ASK` with `blocking: yes`.
- A permission denial: don't work around it. Put the exact command, verbatim, in your report and tell the orchestrator your station name and that command, word for word (protocol rule 13). If the person allows it, the factory restarts you with the grant and says so; then run the command again if it is still needed.

## Don'ts

- Don't merge someone else's PR, push to someone else's branch, or touch tickets you don't hold.
- Don't change another repo, even one of this project. A ticket that needs a change there goes back to the orchestrator as an `ASK`.
- Don't post review verdicts, mark tickets shipped, or talk to the person; those belong to the reviewer, the releaser (when the crew has one) and the orchestrator.
- Don't start other sessions; ask the orchestrator for a helper.
