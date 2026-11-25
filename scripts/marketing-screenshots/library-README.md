# Tares console screenshots

Screens of the Tares software factory console: the place a developer sees what a crew of Claude Code
sessions (orchestrator, builders, reviewer, releaser) is building, and answers what waits on them.
`manifest.json` has one entry per image: what it shows, the feature, and the fields below. Read the
entry before using an image.

## What is in here

| Files | Data | Use |
|---|---|---|
| `01` to `17` | Sample data built into the console | Whole-product coverage. Show the screen, not the numbers. |
| `20` to `23` | A demo project, "Bakery orders", stored and served by a real Tares | One project told end to end: plan, a ticket that got changes asked then passed, the crew on it. |
| `d1` to `d3` | Sample data, dark theme | Same screens in dark mode. |

All images are 3200x2000 (a 1600x1000 window at 2x).

## Rules for writing from these

- **Every image is illustrative.** Project names (project-x, Bakery orders), people (Lee, Dana, Sam),
  repos (example-org, acme), figures, costs and times are made up and do not agree across shots. Never
  quote them as results, customers or measured speed-ups.
- **No real customer and no real crew run is in this library.** If an article needs one, ask for real
  shots; they need a person to take them and mask names.
- All the screens shown are in the released product (v1.60.0). The shots come from a build a little
  ahead of it, so a label may read slightly differently in the release.
- Tares is described as a software factory on top of Claude Code: it does not replace the coding tool,
  it runs a crew of sessions with review and merge rules a person sets.

## Good pairings for an article

- **"A crew, not a chatbot"**: 11-crew, 01-home.
- **"Review you can trust"**: 07-ticket-review (findings by file and line, what the reviewer broke on
  purpose to check the tests), 21-live-ticket (changes asked, then passed), 10-merge-queue.
- **"You stay in charge"**: 01-home (what waits for you), 10-merge-queue ("Yes, merge" on a pull
  request), 16-settings (who merges, how many builders).
- **"The crew remembers"**: 13-docs, 14-docs-memory, 15-brain.
- **"From spec to merged"**: 04-project, 20-live-project, 06-tickets.
