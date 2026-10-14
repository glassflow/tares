---
name: session
description: Use when this Claude Code session works on one Tares project by itself, started by `tares factory session` or `/tares:session`, or told "work on Tares project X". Covers taking a project over from the tares-factory crew, planning a new idea lightly and building it in the same session, working the tickets, and finishing the project. Not for crew stations (FACTORY_ROLE set).
---

# One project, one session

This session works on one Tares project by itself: it plans (lightly) when the project is new,
builds it, and keeps the record on Tares, so the person and any later session can see what was
built and why. The person is here; ask them, not a crew.

Every Tares call names the project (`project=`). The session's own name, for messages, is
`$TARES_SESSION_NAME`.

## 0. Read what holds

- `read_global_docs`: the AGENTS.md and memory every project shares. Follow them.
- Which case you are in, from the arguments:
  - **`new: <idea>`**: go to "A new idea" below.
  - **`<project> (taken over from the crew)`**, or `TARES_SESSION_TAKEOVER` is set: do "Taking over"
    first, then "Working the project".
  - **`<project>`**: "Working the project".

## A new idea: plan lightly, then build here

Load the `spec-project` skill and follow its **Light mode** section. It creates the project (which
binds this session to it), writes a short spec, the ticket list and the project's AGENTS.md, and
checks the size. When it says the plan is done and the person chose to build here, continue with
"Working the project". If the person chose the full spec and the crew instead, the spec skill ends
with the hand-over; this session is done after that.

## Taking over from the crew

The person took this project back from the crew in their terminal; Tares recorded it with their
words, and the project left the crew's work list. Its builders may still be running.

1. `crew_projects`: find the project under `releasing`. If it is not there, the orchestrator has
   released it already: go to "Working the project".
2. Send the orchestrator (normally `crew-orchestrator`; `list_crew` shows it) one message with
   SendMessage:

   ```
   [TF:TAKEOVER] <project> was taken over by the person
   project: <project>
   session: <$TARES_SESSION_NAME>
   holders: <the holders crew_projects lists>
   Stop its builders: push what they have, write their progress, then release_project.
   ```

   If the message does not go through (no session by that name), carry on: Tares already has
   the takeover, and the orchestrator handles it on its next tick.
3. While you wait, read the project (step 1 of "Working the project"). Its starting prompt may be
   written for a crew (builders asking the orchestrator): from now on this session does that work,
   and the person answers.
4. Wait for `[TF:RELEASED]` from the orchestrator. If none comes within about 25 minutes (the
   orchestrator ticks every 20), check `crew_projects` again: gone from `releasing` means released.
   If `list_crew` shows no orchestrator working or waiting, tell the person, and once they confirm
   no builder of this project is still running (`tares factory crew status`), call
   `release_project` yourself.
5. Then tell the person in one line which tickets the builders left in progress (stage `doing`,
   `review` or `changes`) and with which branches and pull requests.

## Working the project

0. **Is it still the crew's?** If `crew_projects` lists this project under `projects` (not
   `releasing`), the crew is building it: stop, and tell the person to take it over with
   `tares factory session <project>` in their terminal, or to talk to the crew with
   `tares factory crew attach orchestrator`.
1. **Read it.** `list_docs` and `get_doc`: the starting prompt, the spec, the plan, the project's
   AGENTS.md. `list_milestones` when there are milestones. `list_tickets`.
2. **Pick up first.** A ticket in progress (`in_progress`, or a stage of `doing`, `review` or
   `changes`) was left by an earlier session or a builder: `pick_up` (the project, or that ticket)
   and finish it before starting another. A builder's work is on its branch (`factory/<ref>`) and
   its pull request, with its progress in the working doc.
3. **Then the ready tickets, in order**, one milestone at a time when there are milestones. For
   each: `get_ticket` (it carries the working doc, if any), mark it started (Linear status for a
   Linear ticket, else `write_ticket(id, status="in_progress")`), build it, verify it, mark it done.
4. **Keep the record as you go:**
   - after each step that works, the working doc's `## Progress` when the ticket has one;
   - every check you run with `add_check` (the command, what it showed, the commit; for a test you
     added, the test you saw fail when you broke the code). Never record one you did not run;
   - an unknown you decide yourself: `assume(question, choice, why, tickets=[ref])`, and say it to
     the person in one line;
   - what you learn about the project: `remember_for_project`.
5. **Ask the person** when the plan does not answer something that matters; they are in this
   session. A rule they state that holds for every project: offer to add it with `add_to_global`.
6. **Git:** follow the project's AGENTS.md and the shared one (branch, PR, who merges). An open
   pull request a builder left is yours now: finish it, and merge only as the AGENTS.md says or on
   the person's yes.
7. When a milestone's tickets are done, run its checks from `list_milestones` and record them.

## Finishing

When every ticket is done (or canceled) and the checks pass, ask the person: "Is <project> done?",
with what was delivered in two lines. On their yes, `finish_project(project, words="<what they
typed, exactly>")`. Tares marks the project done and keeps their words.

## Don'ts

- Don't take work from a project the crew still holds: `tares factory session <project>` asks the
  person to take it over first.
- Don't message crew stations other than the orchestrator, and only about the takeover.
- Don't write the shared docs without the person's yes.
