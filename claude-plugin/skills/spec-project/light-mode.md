# Light mode: plan a small project and build it in this session

Part of the spec-project manual. Read when you are sent here for `tares factory session "<idea>"`, before anything else.

For `tares factory session "<idea>"`. The person wants it built now, in this session, still under
Tares: the project, its shared AGENTS.md and memory, a light record of what was built and why.

1. **Step 0 as above** (`read_global_docs`, read the repo if there is one).
2. **The goal, one question.** Propose the goal in one line and a short name; on yes,
   `create_project(name, goal)` (this binds the session to it) and tell the person its name.
3. **What is in and out, and how we know it works**: one question at a time, with your
   recommendation, only what you cannot answer from the repo. Two or three questions is normal;
   stop as soon as you could explain it to a new engineer.
4. **The light record**, nothing more:
   - `write_doc` kind `spec`: the goal, scope in and out, decisions with the person's words, open
     assumptions. Short.
   - `write_doc` kind `agents` only for what this project adds to the shared AGENTS.md and the
     repo's own (how to build and test it, git rules if different).
   - `write_doc` kind `start`, titled `Start here`: what the project is in two lines, where the
     code lives, and "follow the `session` skill", so a later session can pick it up.
   - The tickets: `write_ticket` per piece of work, in order, with `depends_on` (one ticket is
     fine). When the person keeps tickets in Linear or GitHub Issues, `link_tickets` first, then
     `write_ticket` creates them there, as in `write-out.md` (items 5 and 6). No milestones, no
     working docs, no second-model attack.
5. **Size check**, once the ticket list exists: more than about five tickets, or tickets in more
   than one repo, is big enough for the crew. Then ask: "This looks big enough for the crew.
   Switch to the full spec and hand it over?" (recommend yes). On yes, carry on from step 2 of the
   full flow in this session, keeping everything written so far (add milestones with checks,
   working docs, the second-model attack, the hand-over in step 9). On no, or when it is small:
   say the plan is done and go back to the `session` skill to build it here.
