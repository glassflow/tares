---
name: ship
description: Hand a project planned on Tares to the tares-factory crew, so its orchestrator starts builders in the project's repos. Use when the user runs /tares:ship, says "hand this to the crew" or "ship it to the orchestrator", or at the end of a spec session when they agree to the hand-over.
---

# Ship: hand a project to the crew

You are in the user's own session, not a crew station. The crew is one always-on team (`crew-orchestrator`, `crew-reviewer`, and `crew-releaser` when the user asked for one) that the user started with `tares factory crew up`. Handing a project over is one record on Tares; the orchestrator finds it there, reads the plan, starts builders inside the project's repos and replies with what it started. You start nothing yourself.

## 1. What to hand over

- **Project**: the Tares project this session's spec wrote (`TARES_PROJECT`), or ask the user, recommending the project whose `start` doc names this repo.
- **Repos**: every repo the project's tickets change. For each, the absolute folder it is cloned in on this machine (`git -C <folder> rev-parse --show-toplevel`) and its GitHub name (`gh repo view --json nameWithOwner -q .nameWithOwner` inside it). The repo that holds the plan first. Look at the tickets' `repo` field (`list_tickets`): each `owner/name` there needs a folder. When this session runs in a folder that holds several repos, they are its subfolders; otherwise ask the user where each is cloned. Each must have a GitHub remote, since builders open PRs there, and be trusted in Claude Code (`claude` run there once).
- **Autonomy**: only if the user wants this project stricter than the crew (`L3-review`, every merge waits for their yes). A looser level is not set here: the user changes the crew setting.
- **Plan present**: `list_milestones` and `list_tickets` return milestones and tickets. If not, say so and stop; offer the spec.

Show the user these in a few lines and ask for a yes, recommending it.

## 2. Hand over

1. Call `hand_over(project, repos=[{path, name}, ...], autonomy=...)` (autonomy only when stricter). The project page then shows "Handed to the crew" with the time and its repos, and the orchestrator finds it in `crew_projects`.
2. Check the orchestrator is running: `ListAgents` lists `crew-orchestrator`, or `list_crew` shows it working, idle or waiting.
3. If it is, `SendMessage` to `crew-orchestrator` the `[TF:SHIP]` message `hand_over` returned, so it starts now rather than on its next tick. Tell the user the orchestrator will reply here with the builders it started, and that they answer the crew's questions in the orchestrator's session (`tares factory crew attach orchestrator`).
4. If it is not running, tell the user to run, **in their own terminal**, `tares factory crew up`; the orchestrator picks the project up from Tares when it boots. Explain in one line why you can't run it for them: starting the stations hands them the user's permissions, so Claude Code requires the user to do it.

## Don'ts

- Don't start builders or any other session; the orchestrator does.
- Don't hand over twice to change something: a second `hand_over` adds repos, so call it again only to add one that was missing.
