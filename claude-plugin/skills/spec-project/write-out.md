# Write the project into Tares: links, milestones, tickets and working docs

Part of the spec-project manual. Read when you reach items 5 to 7 of step 6, before you write a milestone, a ticket or a working doc. Items 1 to 4 of step 6 are in SKILL.md; the numbers continue from them.

5. Link and milestones. When the tickets live in Linear or GitHub, `link_tickets` first: for
   Linear `link_fields={"project": "<the Linear project's URL or name>"}`; for GitHub
   `link_fields={"repo": "owner/name", "issue_label": "tares:<project>"}`, the label cut to 50
   characters. If it answers `error 429: GitHub rate limit reached`, nothing is linked: wait about
   5 minutes and call it again. Then `write_milestone` once per milestone, in order, with its goal
   and its acceptance checks (`[{"check": ..., "expect": ...}]`, plus `"for": "person"` on a check
   only a person can do). For Linear and GitHub it creates the milestone there; the checks stay in
   Tares.
6. Tickets: `write_ticket` once per ticket, in dependency order, with `milestone` (the name
   written just above) and `depends_on` (the labels of tickets already written, `T1`, `T2`, or
   `GH-3`, `ENG-12`). Titles say what the ticket does, without a number. It creates the ticket in
   Tares, Linear or GitHub alike, and a repeat of the same title finds it again. Then
   `list_tickets` and check it holds what you created.
   - A ticket in Linear or GitHub takes no `status`, `assignee` or `position` when it is created.
     The person sets the assignee in the tracker afterwards, if the owner of its repo should
     have it (`list_people` gives the email, and the `github` login); this session cannot.
     `assign_ticket` is the orchestrator's, and only hands a ticket to a crew station.
   - A Tares ticket also takes `assignee` (the email or id of its repo's area owner).
   - **Every ticket carries `repo`, wherever it lives, even in a project with one repo:** it is
     how a ticket finds its area owner and how a builder knows where to work.
7. A working doc per ticket: `write_doc` kind `working`, titled `Working: <ticket title>`, then
   `attach_working_doc`. Working docs never go to Linear or GitHub. Use exactly these sections:

   ```markdown
   ## Goal
   What this ticket delivers, in one or two lines.

   ## Context and files
   Why it exists, what it builds on (its dependencies), and the files or modules likely touched.

   ## Steps
   1. Concrete steps in order, naming files and functions.

   ## Definition of done
   - Observable results that must hold.

   ## Out of scope
   - What this ticket must not do.

   ## How to check
   - `<command>` -> <expected output>

   ## Progress
   (The builder keeps this current: what is done, what is next, how to run what it needs.)
   ```
