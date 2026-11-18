# A screen ticket: mock first

Part of the station-builder manual. Read when a ticket's working doc starts with `Screen: yes`, before any UI work, and again when the mock's `RELAY` comes back.

Write no UI code until the mock is approved.
- Read the real cell the working doc names with the read key the project's agents doc names (GET only, never write; with no such cell or key, draw it on invented sample data and say so in the ask), and draw the screen as one static HTML file in your scratch folder outside the repo (inline styles, the repo's tokens and words list), in each state the working doc lists. Never commit it.
- Send an `ASK` to the orchestrator, `blocking: yes`, naming the file's path (a message carries text only) and the question "Approve this mock for <screen>?"; the orchestrator puts it on the desk and keeps it as the project doc "Mock: <screen>". Do the ticket's non-UI parts meanwhile.
- The answer comes back as a `RELAY` with D<n>. It is an approval only when its choice (or its words, when there is no choice) starts with "yes" or "approved", and neither the choice nor the words ask for a change; otherwise redraw (with the change made) and ask again. While no answer has come, keep to the non-UI parts or take another ticket; write `waiting for mock approval, desk D<n>` in Progress.
- After approval, build on sample data until the page matches the mock in every state, then wire the one read call. Write the e2e script `ui/tests/<name>.e2e.cjs` with a seed `ui/tests/seed-<name>.py` using invented names, screenshotting every state into `SHOTS_DIR` and calling `checkWords` and `expectOneCall`.
- The PR body carries `Mock: "Mock: <screen>" (desk D<n>)` and the screenshots from the seeded cell. Never the mock, its images or real names.
