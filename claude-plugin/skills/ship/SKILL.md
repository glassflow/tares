---
name: ship
description: Hand a planned project to the tares-factory crew, so its orchestrator starts a builder in the project's repo. Use when the user runs /tares-factory:ship, says "hand this to the crew" or "ship it to the orchestrator", or at the end of a spec session when they agree to the hand-over.
---

# Ship: hand a project to the crew

You are in the user's own session, not a crew station. The crew is one always-on team (`crew-orchestrator`, `crew-reviewer`, `crew-releaser`) that the user started with `factory crew up`. Handing a project over means one message to `crew-orchestrator`; it reads the plan, starts the first builder inside the project's repo and replies with what it started. You start nothing yourself.

## 1. What to hand over

- **Project**: the Tares project name when Tares is connected (the one this session's spec wrote, `TARES_PROJECT`, or ask the user, recommending the project whose `start` doc names this repo). Without Tares: `project` in `.factory/factory.json`, or the repo folder name.
- **Repos**: the absolute path of this git repository (`git rev-parse --show-toplevel`), and of every other repo the plan's tickets name in a `repo:` line (when `.factory/factory.json` names a `workspace` folder, look for them there first; otherwise ask the user where each is cloned). Each must have a GitHub remote, since builders open PRs there, and be trusted in Claude Code (`claude` run there once).
- **Autonomy**: only if the user wants this project stricter than the crew (for example `L3-review`, every merge waits for their yes). A looser level is not set here: the user changes the crew setting.
- **Plan present**: with Tares, `list_tickets` returns tickets; without, `.factory/docs/plan.md` and `.factory/tickets/` exist. If there is no plan, say so and stop; offer the spec.

Show the user these in three lines and ask for a yes, recommending it.

## 2. Hand over

1. With Tares connected, call `hand_over(project, repo)` with this repo (Tares records one repo per hand-over today; the message below carries all of them). The project page then shows "Handed to the crew" with the time.
2. Check the orchestrator is running: `ListAgents` lists `crew-orchestrator`.
3. If it is, `SendMessage` to `crew-orchestrator`:

   ```
   [TF:SHIP] <project>: hand over to the crew
   project: <project>
   repo: <absolute repo path>          (one line per repo, this one first)
   autonomy: <only if stricter than the crew>
   Planned with <tares spec | factory spec>; <n> milestones, <m> tickets.
   ```

   Tell the user the orchestrator will reply here with the builder it started, and that they answer the crew's questions in the orchestrator's session (`factory crew attach orchestrator`).
4. If it is not running, keep the hand-over for later: write the same message to `${FACTORY_CREW_HOME:-~/factory}/handovers/<project slug>.md` (create the folder if needed). Tell the user to run, **in their own terminal**, `factory crew up`; the orchestrator picks the hand-over up when it boots. Explain in one line why you can't run it for them: starting the stations hands them the user's permissions, so Claude Code requires the user to do it.

## Don'ts

- Don't start builders or any other session; the orchestrator does.
- Don't send the hand-over twice. If the orchestrator already holds the project (its reply, or `list_crew` shows a builder for it), say so instead.
