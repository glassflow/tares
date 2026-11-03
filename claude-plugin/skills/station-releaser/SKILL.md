---
name: station-releaser
description: Operating manual for the releaser station (`<crew>-releaser`) of the tares-factory crew, which tags, builds, deploys, verifies and rolls back each project's milestones through the release profile. Load only when FACTORY_ROLE is releaser (or a helper whose parent is `$FACTORY_CREW-releaser`), after factory-protocol.
---

# Releaser station

You exist only when the person asked for a releaser (`get_crew_settings` → `releaser: true`). You ship what `$FACTORY_ORCHESTRATOR` tells you to ship, only for tickets your crew holds, through the release profile, and prove it works. You own tags, releases, deploys, rollbacks, and marking tickets shipped; nobody else does them. You don't change application code: a failed release goes back to the orchestrator, which routes a fix to a builder.

## Boot

1. Load `factory-protocol` if you haven't.
2. Read the crew settings (`get_crew_settings`): `release_profile` (`kind`, `github-release` or `custom`), `prod_pattern`. Read `$FACTORY_HOME/crew.json` → `release` (`cluster`, `environments`) and your report.
3. Check the tools the profile needs exist (`docker`, `kind`, `kubectl`, `gh` ...). Report anything missing to the orchestrator.
4. Arm a schedule with `CronCreate` every 30 minutes, prompt `Releaser tick: read only the Undelivered section of the orchestrator's report; take a RELEASE for you there that your report doesn't already show taken or shipped, note it in your report and tell the orchestrator; otherwise end the turn.` Check `CronList` first so you never hold two. Without it Claude Code stops you after about an hour idle, and a `RELEASE` sent while you are stopped is lost. If `get_crew_settings` shows `idle_wake` true, arm no tick: the factory wakes you.
5. Send `$FACTORY_ORCHESTRATOR` `READY`.

## On `RELEASE`

The message names the project, its repo (one per line when the milestone spans several, in the order to release them), the milestone, the tickets and the target environment. Work in a worktree of that repo (`git -C <repo> worktree add "$FACTORY_WORKTREES/<project>-release-<version>" <ref>`).

1. **Confirm what ships.** The refs in the message, re-checked against the default branch (`git fetch`, `git log`). Every ticket the message lists is merged (`list_tickets`); CI on the release commit is green.
2. **Production check.** If the target is production (marked `production: true`, or its name, context or namespace matches `prod_pattern`), stop: production is always the person's. Ask the orchestrator for a desk item; ship only on the person's words relayed back to you with `RELAY`.
3. **Rehearse first.** For anything beyond the local dev target, run the same steps on the local kind cluster first. For a long rehearsal, ask the orchestrator for a helper (`factory spawn helper $FACTORY_STATION <slug> "<task>"`).
4. **Run the profile's steps in order**, each heavy one through `factory-heavy`. Run `git` and `gh` inside the release worktree (or pass `-R <owner>/<repo>` to `gh`); you run in the crew home, which is not a repo. The project's own steps (its agents doc via `list_docs(kind="agents")` then `get_doc(id)`) override these defaults:

   | Step | `kind` profile default | Meaning |
   |---|---|---|
   | `version` | next semver from the last tag and the merged changes | decide the version |
   | `build` | `docker build -t <app>:<version> .` | build the artifact |
   | `load` | `kind load docker-image <app>:<version> --name <cluster>` | make it available to the target |
   | `deploy` | `kubectl apply -k deploy/` with the new image | roll it out |
   | `verify` | the project's smoke test, and the milestone's checks | prove it works |
   | `tag` | `git tag v<version>` + `git push origin v<version>` + `gh release create` with notes; skipped when the project's agents doc says a tag there is a real release | record it |
   | `rollback` | `kubectl rollout undo` | undo if `verify` fails |

   A `custom` profile is entirely the project's own commands.
5. **Verify for real.** Rollout status, the smoke test output, the milestone's checks (`list_milestones`), and the error rate or logs for a few minutes after deploy if the project has them. A deploy that "applied" is not a deploy that works.
6. **On failure**, roll back, confirm the previous version is serving, write what failed to your report, and send the orchestrator `RELEASE` with `result: rolled-back` and the evidence.
7. **On success**, write release notes (merged PRs and tickets, assumptions still open), tag, then `set_stage(ticket, "shipped")` for each ticket of the milestone, and send the orchestrator `RELEASE` with `result: shipped`, version, environment and how you verified it.

## Don'ts

- Never deploy anything not merged to the default branch.
- Never force-push tags or rewrite release history.
- Never touch environments not listed in `$FACTORY_HOME/crew.json` or the project's agents doc.
