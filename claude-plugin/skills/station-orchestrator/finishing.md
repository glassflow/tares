# Finishing a project

Part of the station-orchestrator manual. Read when a project you lead has every ticket merged (or canceled) and every milestone's checks passing, or when the person says "it's done" or "wrap it up" (that is the start of this, not a substitute for it: ask the desk item, so their words are recorded).

1. Ask on the desk, as one item: "Is <project> done?", with what was delivered in two lines (milestones, merged PRs, where it runs) and what is still open, if anything. Recommend yes only when nothing is open. Options: **yes, it's done** and **no, there is more**.
2. Record their answer with `desk_answer`, their words as typed. On a yes, `finish_project(project, decision="D<n>")`: Tares keeps their words, marks the project done and takes it off `crew_projects`. On a no, ask what is missing and write it as tickets.
3. Retire the project's builders still recorded (`tares factory retire <station>`), and check it left no worktrees (`tares factory tidy` any that remain).
4. If `finish_project` says the crew holds no other project, ask on the desk, with `project` set to the project you just finished: "No project is left. Stop the crew? Run `tares factory crew down` in your terminal." Recommend yes: idle stations still wake on their schedules. You can't stop the crew yourself; the person runs it, and `crew down` withdraws this question.
