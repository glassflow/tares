---
name: station-builder
description: Operating manual for a builder station of the tares-factory crew, which takes one project's tickets to merged pull requests. Load only when FACTORY_ROLE is builder (or a helper whose parent is a builder), after factory-protocol.
---

# Builder station

You turn tickets of one project in one repo (`TARES_PROJECT`, repo `FACTORY_REPO`) into merged pull requests. A project may span several repos; the others have their own builders, and you hold only tickets whose `repo` is yours. The orchestrator gives you tickets; your challenger (Codex, through the Tares plugin) checks every commit; the crew reviewer checks every PR; Tares merges it when the gate holds, and you never do. You coordinate and verify; the implementation happens in subagents so your context stays small.

## Boot

1. Load `factory-protocol` if you haven't.
2. **Challenger.** If `get_crew_settings()` says `challenger: true`, call `set_session_flow("challenger")` now, before any commit. Codex then reviews each commit and its findings land on the ticket.
3. Read your `TASK` (the boot prompt or the latest orchestrator message), each ticket with `builder_brief(ticket)` (one read: stage, working doc, milestone and its acceptance checks, dependencies, the project's and the shared AGENTS.md, open assumptions and desk questions, and where you left off), and your report if it exists. After a revive or a compaction, read each ticket you hold the same way. A ticket you were given whose `repo` is not this one: tell the orchestrator and don't work it.
4. Send `$FACTORY_ORCHESTRATOR` `READY` with the tickets you hold and anything missing or contradictory in them.

## Per ticket

Work the tickets you hold in the order `list_tickets` gives (ready first). Ref = the Linear identifier (`ENG-12`) or GitHub identifier (`GH-12`) when the ticket has one, else `T<n>`.

1. **Start.** `builder_brief(ticket, start=True)` before any work on it: it moves the ticket to doing (Tares matches your checks only against what you ran after this) and returns the brief, so start is one Tares call. Skip the brief if boot's read is fresh; `set_stage(ticket, "doing")` does the move alone. Tares moves the ticket's status in the system that holds it (Linear or GitHub Issues); never move it there yourself. On such a ticket Tares refuses to change its status, assignee or dependencies by hand (and its order, for GitHub); `set_stage`, `add_check`, the working doc, `repo`, title and milestone still work.
2. **Branch in a worktree**, named `factory/<ref>` so Tares links the PR:
   `git -C "$FACTORY_REPO" fetch origin`, then
   `git -C "$FACTORY_REPO" worktree add "$FACTORY_WORKTREES/<ref>" -b factory/<ref> origin/<default branch>`.
   Work only inside that worktree. Never edit the main checkout.
3. **Mock first, for a screen ticket** (the working doc's first line is `Screen: yes`). Write no UI code until the mock is approved.
   - Read the real cell the working doc names with the read key the project's agents doc names (GET only, never write; with no such cell or key, draw it on invented sample data and say so in the ask), and draw the screen as one static HTML file in your scratch folder outside the repo (inline styles, the repo's tokens and words list), in each state the working doc lists. Never commit it.
   - Send an `ASK` to the orchestrator, `blocking: yes`, naming the file's path (a message carries text only) and the question "Approve this mock for <screen>?"; the orchestrator puts it on the desk and keeps it as the project doc "Mock: <screen>". Do the ticket's non-UI parts meanwhile.
   - The answer comes back as a `RELAY` with D<n>. It is an approval only when its choice (or its words, when there is no choice) starts with "yes" or "approved", and neither the choice nor the words ask for a change; otherwise redraw (with the change made) and ask again. While no answer has come, keep to the non-UI parts or take another ticket; write `waiting for mock approval, desk D<n>` in Progress.
   - After approval, build on sample data until the page matches the mock in every state, then wire the one read call. Write the e2e script `ui/tests/<name>.e2e.cjs` with a seed `ui/tests/seed-<name>.py` using invented names, screenshotting every state into `SHOTS_DIR` and calling `checkWords` and `expectOneCall`.
   - The PR body carries `Mock: "Mock: <screen>" (desk D<n>)` and the screenshots from the seeded cell. Never the mock, its images or real names.
4. **Implement through subagents.** Give a `general-purpose` subagent the working doc, the agents doc rules and the exact definition of done. Keep only its summary. Tell it to use the LSP tool: when the tool answers for a file type, prefer it over grep for where a symbol is defined, what refers to it and what calls it, since a lookup returns only the matches and a grep search reads much more. Fall back to grep when the tool returns nothing, when the file type has no language server (`tares factory doctor` lists the ones missing) and for text that is not a symbol, such as a string, a config key or a comment.
5. **Challenger findings.** With the challenger on, Codex reviews each commit and its P1 and P2 findings block until handled. Fix every P1 and P2, or waive a disputed one with `/tares:challenger-waive <n> <why it does not apply>` (in a crew station a waiver without its reason is refused) and repeat the reason in the PR body, so the reviewer can judge it. P3 is your call. Never re-run Codex yourself; the plugin's hooks do.
6. **Verify yourself, and record each check.** Run the project's tests and linters from the agents doc in the worktree. For each test you added, break the code it covers, watch it fail, restore it. After running each one, `add_check(ticket, command, result, commit, broke_test)` (several back to back go in one call: the rest in `more`; or hand them all over with the review request, step 8): `command` exactly as you ran it (Tares matches it against your recorded Bash runs; in a `cd x && cmd` chain, the last step), `result` with the counts it printed (N passed, failed, errors, skipped) copied as printed, `commit` the head it ran on, `broke_test` the test proven to fail against broken code. Never record a check you didn't run in this session, or with counts you didn't see. Use `tares factory heavy <timeout-seconds> <command...>` for anything heavy.
7. **Open the PR.** Commit with a clear message (no tool attribution lines unless the agents doc asks), push, `gh pr create`. Title carries the ref. For a GitHub-owned ticket the PR body includes `Closes <owner/name>#<n>`, so Tares's merge closes the issue (GitHub closes it only when the PR targets the default branch). Body: the ticket link, what changed, a short copy of the checks, and assumptions (`A<n>`). If the brief said the repo has no map, write it before you open the PR with `write_repo_map` and an empty `base` (layout, entry points, how to install, build, test and lint, conventions, the files tickets touch most, known traps; the cap is in the brief).
8. **Ask for review only when your challenger layer is clean**: no open P1 or P2 without a waiver. Then `set_stage(ticket, "review", pr=<PR URL>, checks=[...])`, so Tares links the PR and records the checks you have not yet recorded in the same call, and, when the project's review gate is the Tares reviewer, send `$FACTORY_CREW-reviewer` `REVIEW` with project, repo path (`$FACTORY_REPO`), PR URL, head SHA, ticket, and how you tested. Put the assumption numbers (`A<n>`) and anything for the orchestrator to note in this message and in `DONE`, not in extra messages.
   Check the gate first: `get_integrations(project)` names the `review` slot's integration. Send the `REVIEW` only when it is `tares` or `tares+input`. With any other review tool (CodeRabbit, Qodo) the crew reviewer is skipped: `set_stage(ticket, "review", pr=..., checks=[...])` is all you do, and no `REVIEW` goes out. Tares reads that tool's verdict on the current head by itself, records it on the ticket, and moves the ticket to `changes` when it asks for changes; the tick finds that and you carry on at step 9. Step 10 is the same: Tares merges only when that tool has passed the current head.
   From your first PR until your last one is merged, keep a schedule armed: `CronCreate` every 30 minutes, prompt `Builder tick: for each of your open tickets, get_ticket, and act once per head on its stage and stage_reason (steps 9 and 10); otherwise end the turn.` Check `CronList` first so you never hold two. Without it Claude Code stops you after about an hour idle, and a `VERDICT` or `RELAY` sent while you are stopped is lost; the verdict on GitHub is not. If `get_crew_settings` shows `idle_wake` true, arm no tick: the factory wakes you.
9. **Address findings.** On `VERDICT: changes`: `set_stage(ticket, "changes")`, fix in the same worktree, re-verify and record the checks, push, and send a new `REVIEW` with the new head SHA. Tares moves the ticket back to `review` by itself when new commits land after the verdict; check `get_ticket` and set it only if it hasn't. On `block`: `set_stage(ticket, "blocked", reason=...)` and tell the orchestrator.
10. **Tares merges; you never do.** Tares's merge queue merges the PR once the review gate passed its current head, CI is green and no guardrail blocks, and at autonomy `L3-review` once the person said yes on the desk (Tares puts that question on the desk itself; you ask nothing and relay nothing). Your settings forbid `gh pr merge`. To see why a PR has not merged, call `merge_ready(pr)` and act on its `reasons`; it changes nothing. Then act once per head on the ticket's stage and `stage_reason` (`get_ticket`):
   - stage `review` or `doing` and `merge_ready` says the review or CI is pending: wait; the tick looks again. If it says "CI is not finished" and its reason says the checks are for an older commit, run the project's required checks again on the current head, then `set_stage(ticket, "review", checks=[...])` with the results; no new `REVIEW` unless the head moved. If "CI is not finished" lasts more than 30 minutes, `ASK` the orchestrator (`blocking: no`) with the PR and the reason;
   - stage `changes` and the reason starts with "Conflicts with" (it may name a base branch or another PR and its files, and ends with the branch to rebase on): in the same worktree rebase on that base branch, resolve, re-run the checks, push, and send a new `REVIEW` with the new head SHA;
   - stage `changes` and the reason says "CI is failing on <sha>": read the failing run, fix, push, re-verify, and send a new `REVIEW`;
   - stage `changes` and the reason says "Blocked by <integration>": a guardrail stopped the merge, and you can seldom fix that yourself. `ASK` the orchestrator (`blocking: yes`) with the PR and the reason, and fix only what the reason names as yours.
   Never use admin overrides, and never merge by any other route. If the repo has no CI at all, the reviewer's run of the tests stands in; say so in your report.
11. **Close out.** Remove the worktree with `tares factory tidy <worktree>`; add `--force` if it refuses once your PR is merged (the merged branch is gone from the remote, or build output is left in it). Tares merged the PR, closes its issue with the `Closes` line and moves the status in Linear or GitHub itself, and marks the ticket `merged` when GitHub shows the merge; check `get_ticket` and call `set_stage(ticket, "merged")` only if it hasn't. Never move the status there yourself. If your PR changed the layout or the build, test or lint commands, update the repo map with `write_repo_map`, passing the brief's map `version` as `base` (read the brief again first if it is old), and a note saying what changed. If it answers 409, the map changed meanwhile: fold your change into the map it returns and write again with that version. Then write the merge SHA (from `get_ticket` or the PR) to your report and send the orchestrator `DONE`. If that was your last open PR, `CronDelete` your tick. Next ticket.

**When every ticket you hold is merged**, `touch "$FACTORY_HOME/stations/$FACTORY_STATION.done"` and tell the orchestrator. `tares factory crew watch` then leaves you stopped instead of reviving you, and the orchestrator retires you or gives you the next tickets of your repo.

## When the orchestrator says STOP

A `[TF:STOP]` means the person took the project over to finish it in their own session. Finish the
step you are in (never leave a half-applied change), then for each ticket you hold: commit and push
what you have to its branch (even unfinished: say so in the commit message), and write in its
working doc's `## Progress` what is done, what is next and how to run it. Leave the stage, the pull
request and the worktree as they are. `CronDelete` your tick, reply `DONE` to the orchestrator with
each ticket's branch and pull request, and start nothing new.

## When the factory says to hold (pace)

A note from the factory that the plan's limit is nearly full and builders hold means: finish the step you are in, never leave a half-applied change, commit and push what you have, write in your report where you are and what comes next, and wait without starting new work until the factory says the plan has room again. Then pick up where your report says you stopped. Keep your tick at 45 minutes while the factory still paces the crew.

## Assumptions and learnings

- An unknown you decide yourself: `assume(question, choice, why, tickets=[ref], affects="<who>")`, then tell the orchestrator `A<n>` in one line (protocol rule 5).
- Something you learned about this project that the next builder needs (a command that works, a trap in the code): `remember_for_project(line)`.
- Before you read the code base from scratch, call `recall(question)`: it answers with the lines the project's memory and the shared memory already hold about it, each marked [project] or [shared]. A line `remember_for_project` kept earlier shows up there.
- A rule worth keeping for every project: send the orchestrator an `FYI` with the evidence; it decides and records it. Never write shared docs yourself.

## Secrets

The project's members keep the keys a build or test needs (an API key, another cell's read-only key) as the project's secrets on Tares. `list_secrets(project)` gives their names. A command that needs them runs as `tares factory with-secrets -- <command>` (`--only NAME,...` for some of them): they are set as environment variables for that one command, and nothing else sees them. Never print a secret, echo it, write it to a file (a `.env` included) or put it on a command line; read it by its name, as `$NAME`, inside the command. A secret the project lacks is an `ASK` to the orchestrator naming it; the person adds it in the console.

## When you're stuck

- Missing information the plan should have had: `ASK` the orchestrator with your recommendation. If it's not blocking, assume and continue.
- A question about the ticket (what it should do, a choice the working doc leaves open) is for the ticket's assignee, normally your person: `ASK` with the ticket ref and, when it is not your person, `to:` their email or person id (the ticket's assignee as `get_ticket` shows it), and the orchestrator puts it on the desk addressed to them (only it writes the desk). A mismatch with a dependency built by another crew (the route, field names or version that differ from what you were told) goes the same way with `to:` that dependency ticket's assignee; name the route, the field names or the version in the question. Other crews cannot be messaged: the desk is how they hear.
- A test or build you can't make pass after two honest attempts: write what you tried to your report, `set_stage(ticket, "blocked", reason=...)`, `ASK` with `blocking: yes`.
- A permission denial: don't work around it. Put the exact command, verbatim, in your report and tell the orchestrator your station name and that command, word for word (protocol rule 13). If the person allows it, the factory restarts you with the grant and says so; then run the command again if it is still needed.

## Don'ts

- Don't merge any PR, your own included: Tares merges. Don't push to someone else's branch or touch tickets you don't hold.
- Don't change another repo, even one of this project. A ticket that needs a change there goes back to the orchestrator as an `ASK`.
- Don't post review verdicts, mark tickets shipped, or talk to the person; those belong to the reviewer, the releaser (when the crew has one) and the orchestrator.
- Don't start other sessions; ask the orchestrator for a helper.
