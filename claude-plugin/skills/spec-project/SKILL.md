---
name: spec-project
description: Use when the user wants to spec, plan or brainstorm a new project, feature or piece of work so that another session or a tares-factory crew can build it later (any wording, or /tares:spec). Interviews one question at a time with a recommended answer, removes unknowns with research and throwaway prototypes, has a second model attack the plan, then writes everything the build needs into a Tares project (spec, plan, milestones with acceptance checks, AGENTS.md, starting prompt, tickets with dependencies and a working doc each), with the tickets in Linear or GitHub Issues when the user wants them there. A session opened to contribute to someone else's project (answer area owner questions, check tickets) follows the section "Contributor" instead.
---

# Spec a project into Tares

You are the spec session. Your job ends when a fresh Claude Code session, or a tares-factory
crew, told only the project's name, can read everything it needs from Tares and build it without
asking the user anything you could have written down. Cheaper models may build the tickets, so
nothing may be left for them to guess. You do not build anything here.

**Light mode** (the `session` skill sends you here for `tares factory session "<idea>"`): a small
project planned and built in the same session. Follow the section "Light mode" at the end instead
of steps 1 to 9.

**Contributor mode** (a session whose first message says "You are contributing to <project> led by
<lead>"): you are not the project's lead. Follow the section "Contributor" below instead of
steps 1 to 9.

## 0. Read what is already decided

- Call `read_global_docs` first: the AGENTS.md and memory every project shares hold the user's
  standing rules and facts. Follow them and do not ask again for what they answer.
- For a project a tares-factory crew will build, call `read_grants` and `get_crew_settings`:
  the user's standing permissions and how far the crew may go alone. Only ask about what is
  different for this project; save a new grant with `add_grant`, quoting the user's own words.
- If the project builds on a repo that exists, **read the repo before asking about it**: layout,
  stack, how it builds and tests today, its own AGENTS.md or README.
- When this session runs in a folder that holds several repos (`tares factory spec` started in a
  workspace), the project may span them, for example an API, its UI and its chart. Read each, and
  ask which ones the project changes, recommending the ones the idea touches.

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
- where the code lives: every repo the project changes, each with its folder on this machine, its
  GitHub name (`owner/name`) and branch, and the git rules (push or not, PRs or not)
- how we will know it works, and where it is checked (a performance target needs the machine it
  is measured on)
- how it ships: where it runs, how a release is done and checked, how it is rolled back
- whether it will run on a tares-factory crew, or be built by one session (see step 8)

**Area owners.** On an older Tares with no `list_areas` tool (factory-protocol), skip this part:
one person owns every repo. Otherwise, once you know which repos the
project changes, call `list_areas` and note the
owner of each one (`{repo, person: {id, name}}`). If a repo has none, ask the user who owns it
(one question, recommending a person from `list_people`) and call `set_area(repo, person)`. Setting
the owner of a repo that has one, or of a repo for someone else, needs an admin: when you are not
one, tell the user an admin sets the owner. Tickets go to these owners (step 6). Nobody signs off
before the hand-over: an owner is asked only what the spec needs from them, with `ask_area`.
`ask_area`, `list_questions` and `answer_question` come with an enterprise licence, like `list_areas`: when
they are not tools, ask the user what you need to know yourself and skip the rest of this part and
the open-questions step below.

When a question is about a repo whose area owner is not the user (what a module does, whether a
change is acceptable, how it is deployed), do not guess and do not make the user relay it. Once the
project exists (after `create_project`, below), call
`ask_area(repo, text, project)` with the question written so the owner can answer without this
conversation. On a private project, first put the other owners on it with
`set_project_access(project, add_members=[<their emails>])`, because `ask_area` is refused (403) for
an owner who is not on the project. Tell the user "waiting for <name>", and carry on with the other questions. Before
you plan (step 3), call `list_questions(project=<name>)`, fold each answered one into the spec
(name the answerer: "Kiran said ..."), and say which are still open. Plan around an open one only
as an explicit assumption in the spec.

**As soon as the user agrees the goal** (usually the first question), call `create_project` with a
short unique name and the one-line goal, and tell the user its name in one line. From then on the
project, and this session, show on Tares while you work; use that name as `project` in every call
after this. Nothing else goes to Tares yet.

Push back on vague answers. Write nothing more to Tares until the user agrees the idea is clear
enough to write down.

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
  show, or a URL and what it returns. The crew runs these itself when the milestone's last ticket
  merges. Add a check `for: "person"` only for what a person must see or try themselves (a screen,
  a flow in the console), worded for them, not as a command; those are the only checks the
  person is asked to accept.
- **Tickets** are an hour or two of agent work each, one PR each, in order. Each belongs to a
  milestone and names the tickets it depends on. A ticket a cheaper model cannot finish without
  guessing is too big or too vague: split it or say more.
- **Several repos:** a ticket changes one repo, since one PR lives in one repo. A change that needs
  two repos is two tickets joined by `depends_on`, the one that provides first (the API endpoint
  before the UI that calls it), and both working docs say what crosses the line between them (the
  route, the field names, the version). Each repo gets its own builder, so tickets in different
  repos run side by side unless a dependency holds one back.

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

Ask (one question, with your recommendation): "Should the tickets live in Linear, GitHub Issues,
or only in Tares?"

- **Linear:** Tares needs Linear connected (Tares console, Linear connection); Tares does the
  writing, so this session needs no Linear tools. The Linear project must exist: ask for its URL or
  name, or offer to make it once the user has.
- **GitHub Issues:** one repo holds the issues, marked by an issue label. Tares needs a GitHub
  credential (Tares console, GitHub connection) that can write that repo's issues; this session
  needs no `gh` for it. Ask which repo, recommending the repo that holds the plan.
- Linear and GitHub own a ticket's status, assignee and dependencies once it exists. `set_stage`,
  `add_check`, the working doc and `repo` still work in Tares.
- **Tares only:** nothing else to set up.

## 6. Write the project into Tares

Write for a reader who was not here: no "as discussed", no references to this conversation.

1. The project was made when the goal was agreed (step 1). If the goal changed since, the spec's
   first line says the goal now agreed.
2. `write_doc` kind `spec`: what is built and why, who it is for, scope in and out, constraints,
   decisions made here (with the user's words where they decided), assumptions still open.
3. `write_doc` kind `plan`: the milestones in order with their goal and the tickets in each.
4. `write_doc` kind `agents`, the AGENTS.md of the build: where the code lives (each repo's
   absolute path or clone URL, GitHub name and branch), layout, exact commands to install, build,
   run, test, lint, per repo when they differ; conventions; the
   git rules; what not to touch; and the **release section**: how to release, where it runs, how
   to check it went out, how to roll back, as exact commands. Propose the release section and get
   the user's yes. Ask for anything you do not know; a builder cannot guess it.
   When the project's CI is local (`get_integrations` shows the `ci` slot as `local`), call
   `set_required_checks` with the test and lint commands from that AGENTS.md, exactly as a
   builder runs them. A pull request counts as passing when each is recorded on its head commit.
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
  Linear tickets, with `gh issue edit` and `gh issue close` for GitHub tickets, `write_ticket`
  otherwise); when a milestone's tickets are done, run its crew checks and record each with
  `record_milestone_check`
- if a ticket is already in progress when the session starts, an earlier session stopped on it:
  `pick_up` first and finish that ticket instead of starting it over
- to keep the working doc's `## Progress` current after each step that works
- to record every check it runs with `add_check` (the command, what it showed, the commit, and
  for each test it adds, the test it saw fail when it broke the code), never one it did not run
- to keep what it learns about the project with `remember_for_project`
- where questions go: a `note` doc in the project when something is unclear

**Built by a tares-factory crew:** the stations' own skills say how to work, so the starting
prompt only says what this project is:

- what the project is, in two lines, and that the spec, plan, milestones, tickets and AGENTS.md
  are on this Tares project
- each repo it changes: folder on this machine, GitHub name, branch
- that builders ask the crew's orchestrator, not the user, and record what they assume with
  `assume`; they do not leave `note` docs
- Do not include the single-session instructions above.

## 8. Check, then hand over

Call `list_docs`, `list_milestones` and `list_tickets` and confirm: a spec, a plan, an AGENTS.md
with a release section, a starting prompt, every milestone has checks, every ticket has a
milestone and a working doc with all sections, and only the first tickets are ready. Fix anything
missing. Then tell the user, briefly:

- the project name and what it holds (counts, not a dump), and the assumptions worth a glance
- where the tickets live, with the Linear or GitHub link if any
- **the open questions** (only when `list_questions` is a tool). Call `list_questions(state="open", project=<name>)`. For each one, say
  who it is for ("Q2 for Kiran: is the cart in scope?"), and that they answer it from their own
  machine with `tares factory spec "<project>"` (the exact project name, in quotes). The project
  is not ready to start while one is open: it shows on the user's home under "You are waiting on".
  Fold each answer into the spec when it comes.
- **now or later.** Ask: "Start this now, or keep it for later?" If the user says later (a backlog
  item, not to build yet), call `set_later(project=<name>)` and tell them it is off their home,
  under Later in All projects, until they start it (Start now on the project page, or the
  hand-over). Then stop: no step 9.
- the next step:
  - one session: the command to start the build, in a terminal in the repo:
    `tares factory session <name>` (it opens Claude bound to the project, with the Tares plugin; a
    plain `claude` has no Tares tools)
  - a crew: go on to step 9.

## 9. Hand the project to the crew (crew-built projects only)

Ask: "Hand this to the orchestrator now?" On yes:

1. `hand_over(project, repos=[{"path": <absolute folder>, "name": "owner/name"}, ...])`, every
   repo the tickets change, the one holding the plan first; add `autonomy="L3-review"` only when
   the user wants this project stricter than the crew. Tares records it, the project page shows
   its repos, and the orchestrator finds it in `crew_projects`. The tool says which orchestrator
   is running.
2. Send that orchestrator (`list_crew` names it: `<crew>-orchestrator`) the `[TF:SHIP]` message the tool returns,
   with SendMessage, so it starts now rather than on its next tick.
3. Tell the user the orchestrator has it and will start the builders; they talk to it with
   `tares factory crew attach orchestrator`, and watch the crew on the Crew page.

If `hand_over` says no orchestrator is running, tell the user to run `tares factory crew up` in a
terminal (starting stations hands them the user's permissions, so the user starts them, not you).
The orchestrator finds the project on Tares when it starts; no message is needed.

## Contributor

For a session opened by `tares factory spec <project>` by someone who is not the project's lead,
with the first message "You are contributing to <project> led by <lead>". The project's spec and
tickets exist; you ask nothing about the idea. Your job is to answer the questions for this person
as an area owner and to check the tickets in their repos. Use `<project>` as `project`
in every call, and the person's own words in anything you write.

1. Call `read_global_docs`, then `get_doc` for the `spec` and the `plan`, and read them.
2. Call `my_work(project=<project>)` and read `person` (who this is) and `spec_questions` (the
   open questions addressed to this person in this project only). An error 404 ("no project 'x' you
   can see: use a project id or its exact name") means the name or id is wrong or the project is
   not shared with this person: stop
   and tell the user, do not retry with another spelling. Question numbers (Q1, Q2)
   restart in each project, so never answer a question from a list that was not asked for this
   project. Do not match people by display name. Ask the user each one, one at a time, with your
   recommended answer and why, so they can say yes.
3. On their answer, call `answer_question(id, answer, project)` with their words. Say which
   questions are left.
4. Call `list_tickets(project=<project>)` and keep the tickets whose `repo` is one that
   `list_areas` gives to the `person` from `my_work` (match by `person.id`, not by name). Show them
   (title, milestone, depends on, a line from the working doc) and ask what to change. Apply each
   edit where the ticket lives: `write_ticket` (and `write_doc` for its working doc) for Tares
   tickets, Linear's `save_issue` for Linear ones, `gh issue edit` for GitHub ones. A ticket that
   changes a repo this person does not own is not theirs to edit: tell the lead instead. Editing a
   Tares ticket or its working doc (`write_ticket`, `write_doc`) needs a key with the `admin`
   scope, and an ordinary key (read and ingest) is refused with 403 "this credential lacks the
   'admin' scope". Linear and GitHub edits do not go through the Tares key, so they are not refused
   this way. On that 403, do not retry and do not go around it: write the change the person wants, ticket by ticket, in your reply and
   tell the user to pass it to <lead>, who makes it. Answering still works.
5. When the questions are answered and the tickets checked, say so, and that <lead> can hand the
   project over: nothing else waits for this person.

## Light mode: plan a small project and build it in this session

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
     `write_ticket` creates them there, as in the last two steps of the full spec. No milestones, no
     working docs, no second-model attack.
5. **Size check**, once the ticket list exists: more than about five tickets, or tickets in more
   than one repo, is big enough for the crew. Then ask: "This looks big enough for the crew.
   Switch to the full spec and hand it over?" (recommend yes). On yes, carry on from step 2 of the
   full flow in this session, keeping everything written so far (add milestones with checks,
   working docs, the second-model attack, the hand-over in step 9). On no, or when it is small:
   say the plan is done and go back to the `session` skill to build it here.

