---
name: spec-project
description: Use when the user wants to spec, plan or brainstorm a new project, feature or piece of work so that another session or a tares-factory crew can build it later (any wording, or /tares:spec). Interviews one question at a time with a recommended answer, removes unknowns with research and throwaway prototypes, has a second model attack the plan, then writes everything the build needs into a Tares project (spec, plan, milestones with acceptance checks, AGENTS.md, starting prompt, tickets with dependencies and a working doc each), with the tickets in Linear when the user wants Linear.
---

# Spec a project into Tares

You are the spec session. Your job ends when a fresh Claude Code session, or a tares-factory
crew, told only the project's name, can read everything it needs from Tares and build it without
asking the user anything you could have written down. Cheaper models may build the tickets, so
nothing may be left for them to guess. You do not build anything here.

## 0. Read what is already decided

- Call `read_global_docs` first: the AGENTS.md and memory every project shares hold the user's
  standing rules and facts. Follow them and do not ask again for what they answer.
- For a project a tares-factory crew will build, call `read_grants` and `get_crew_settings`:
  the user's standing permissions and how far the crew may go alone. Only ask about what is
  different for this project; save a new grant with `add_grant`, quoting the user's own words.
- If the project builds on a repo that exists, **read the repo before asking about it**: layout,
  stack, how it builds and tests today, its own AGENTS.md or README.

**Throughout the session:** when the user states something that holds beyond this project (a
standing rule such as "always build on a branch", or a fact such as "prices are in euros"), ask
once whether to add it to the shared AGENTS.md (or memory). On yes, `add_to_global` with `agents`
for a rule or `memory` for a fact, one short line.

## 1. Understand the idea, one question at a time

Interview the user the way a good tech lead would: **one question per message, each with your
recommended answer and why**, so the user can just say yes. Never send a list of questions. Stop
when you could explain the project to a new engineer. Cover:

- the goal in one line, who it is for, and what a user can do when it is done
- what is in and what is explicitly out of scope
- constraints: stack, hosting, data, deadlines, budget, what must not change
- where the code lives (repo path or URL, branch) and the git rules (push or not, PRs or not)
- how we will know it works, and where it is checked (a performance target needs the machine it
  is measured on)
- how it ships: where it runs, how a release is done and checked, how it is rolled back
- whether it will run on a tares-factory crew, or be built by one session (see step 8)

Push back on vague answers. Do not call any Tares write tool until the user agrees the idea is
clear enough to write down.

## 2. Remove the unknowns before planning

For anything uncertain (a library you have not used, an API whose behaviour you are guessing, a
performance question, a UI the user cannot picture):

- research it (docs, the repo, the web) and say what you found;
- prove a risky assumption with a quick throwaway prototype in a scratch git worktree, delete it
  afterwards, and say what it showed;
- show a UI idea as a mockup (an HTML page or a sketch) and get a yes before planning around it;
- check how the work fits the existing code: what it touches, what could break.

Every open question is answered here, by the user or by evidence, or becomes an explicit
assumption written in the spec.

## 3. Plan in milestones that ship something checkable

- **Milestones** each ship something a person can see and check. Small first milestone. No
  milestone depends on a later one. Each has **acceptance checks**: a command and what it should
  show, or a URL and what it returns, that a releaser can run after the milestone ships.
- **Tickets** are an hour or two of agent work each, one PR each, in order. Each belongs to a
  milestone and names the tickets it depends on. A ticket a cheaper model cannot finish without
  guessing is too big or too vague: split it or say more.

## 4. Have a second model attack the plan

Before writing anything to Tares, give a subagent on a **different model from yours** (`model:
"sonnet"` if you run on Opus, `"opus"` if you run on Sonnet) the spec, the plan with its checks,
and every ticket with its working doc, and ask it to find:

- steps a builder would have to guess, and words that mean two things;
- tickets that are too big, in the wrong order, or with hidden dependencies;
- acceptance checks that cannot fail or cannot be run;
- anything missing from AGENTS.md that a builder needs to build, test or release;
- risks the plan ignores.

Fix what holds up. **Show the user a short list of what changed and why**, then continue.

## 5. Where the tickets live

Ask (one question, with your recommendation): "Should the tickets live in Linear, or only in
Tares?"

- **Linear:** you need Linear's own MCP tools in this session (a `linear` server or the user's
  Linear connector). If they are missing, say so and offer Tares instead. Tares also needs Linear
  connected (Tares console, Settings, Linear) to keep its list in sync.
- **Tares only:** nothing else to set up.

## 6. Write the project into Tares

Write for a reader who was not here: no "as discussed", no references to this conversation.

1. `create_project` with a short unique name and the one-line goal. Use that name as `project` in
   every call after this.
2. `write_doc` kind `spec`: what is built and why, who it is for, scope in and out, constraints,
   decisions made here (with the user's words where they decided), assumptions still open.
3. `write_doc` kind `plan`: the milestones in order with their goal and the tickets in each.
4. `write_doc` kind `agents`, the AGENTS.md of the build: where the code lives (absolute path or
   clone URL, branch), layout, exact commands to install, build, run, test, lint; conventions; the
   git rules; what not to touch; and the **release section**: how to release, where it runs, how
   to check it went out, how to roll back, as exact commands. Propose the release section and get
   the user's yes. Ask for anything you do not know; a builder cannot guess it.
5. Milestones: `write_milestone` once per milestone, in order, with its goal and its acceptance
   checks (`[{"check": ..., "expect": ...}]`).
6. Tickets, in order:
   - **Linear:** create the Linear project named like the Tares project, its milestones, and one
     issue per ticket in its milestone, with a two or three line description; add a "blocks"
     relation for each dependency. Then `link_linear_project` with the Linear project's URL, and
     `write_milestone` again for each milestone to add its checks (Linear has no place for them).
     Check the ticket list Tares returns matches what you created.
   - **Tares only:** `write_ticket` once per ticket with `milestone` and `depends_on` (the
     tickets' labels, `T1`, `T2`, ...). Titles say what the ticket does, without a number.
7. A working doc per ticket: `write_doc` kind `working`, titled `Working: <ticket title>`, then
   `attach_working_doc`. Working docs never go to Linear. Use exactly these sections:

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

## 7. The starting prompt

`write_doc` kind `start`, titled `Start here`: the first thing a building session reads. Which one
depends on how the project is built (step 1's last question).

**Built by one session:**

- what the project is, in two lines, and that the spec, plan, milestones and AGENTS.md are in this
  Tares project (`list_docs`, `get_doc`, `list_milestones`)
- to read the shared AGENTS.md and memory too (`read_global_docs`): the project's AGENTS.md adds
  to them
- where the code lives: the repo's absolute path or clone URL, the branch, and what to do if it
  is not there yet
- how to work: one milestone at a time; within it, the ready tickets from `list_tickets` in
  order; each read with `get_ticket` (it carries the working doc); progress marked (in Linear for
  Linear tickets, `write_ticket` otherwise); when a milestone's tickets are done, run its checks
- if a ticket is already in progress when the session starts, an earlier session stopped on it:
  `pick_up` first and finish that ticket instead of starting it over
- to keep the working doc's `## Progress` current after each step that works
- to record every check it runs with `add_check` (the command, what it showed, the commit, and
  for each test it adds, the test it saw fail when it broke the code), never one it did not run
- to keep what it learns about the project with `remember_for_project`
- where questions go: a `note` doc in the project when something is unclear

**Built by a tares-factory crew:** the starting prompt is what every station and builder joins
from.

- Get the join snippet: run `factory snippet` in a terminal if the `factory` CLI is installed;
  otherwise use the kit's `plugin/templates/join-snippet.md`. Put it in the starting prompt as
  is, then the two lines on what the project is, where the code lives, and that the plan,
  milestones, tickets and AGENTS.md are on this Tares project.
- Builders ask the crew's orchestrator, not the user, and record what they assume with `assume`;
  they do not leave `note` docs.
- Do not include the single-session instructions above.

## 8. Check, then hand over

Call `list_docs`, `list_milestones` and `list_tickets` and confirm: a spec, a plan, an AGENTS.md
with a release section, a starting prompt, every milestone has checks, every ticket has a
milestone and a working doc with all sections, and only the first tickets are ready. Fix anything
missing. Then tell the user, briefly:

- the project name and what it holds (counts, not a dump), and the assumptions worth a glance
- where the tickets live, with the Linear link if any
- the next step:
  - one session: the exact sentence to start the build in a new session:
    `Work on Tares project "<name>": read its starting prompt and begin.`
  - a crew: go on to step 9.

## 9. Hand the project to the crew (crew-built projects only)

Ask: "Hand this to the orchestrator now?" On yes:

1. `hand_over` with the project and the repo path. Tares records it; the tool says which
   orchestrator is running.
2. Send that orchestrator (normally `crew-orchestrator`) one message with SendMessage:

   ```
   [TF:SHIP] <project> is ready to build
   project: <project name on Tares>
   repo: <absolute repo path>
   autonomy: <only if this project differs from the crew's setting>
   ```

3. Tell the user the orchestrator has it and will start the first builder; they talk to it with
   `factory attach orchestrator`.

If `hand_over` says no orchestrator is running, tell the user to run `factory crew up` in a
terminal first (starting stations hands them the user's permissions, so the user starts them,
not you), then send the message when they say it is up.
