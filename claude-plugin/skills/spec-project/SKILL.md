---
name: spec-project
description: Use when the user wants to spec, plan or brainstorm a new project, feature or piece of work so that another session can build it later (any wording, or /tares:spec). Guides the brainstorm, then writes everything the build needs into a Tares project (spec, plan, AGENTS.md, starting prompt, a ticket list with a working doc per ticket), with the tickets in Linear when the user wants Linear.
---

# Spec a project into Tares

You are the spec session. Your job ends when a fresh Claude Code session, told only the project's
name, can read everything it needs from Tares and start building without asking the user anything
you could have written down. You do not build anything here.

## 1. Brainstorm first

Talk it through before writing anything. Ask, a few questions at a time, until these are clear:

- the goal, in one line, and who it is for
- what is in and what is explicitly out of scope
- constraints: stack, repos, data, deadlines, what must not change
- how we will know it works (the checks a builder can run)
- the rough shape of the work: the pieces and their order

Push back on vague answers. Offer options with a recommendation when the user is unsure. Do not
call any Tares write tool until the user agrees the idea is clear enough to write down.

## 2. Where the tickets live

Ask: "Should the tickets live in Linear, or only in Tares?"

- **Linear:** you need Linear's own MCP tools in this session (a `linear` server, or the user's
  Linear connector). If they are missing, say so and offer to keep the tickets in Tares instead.
  Tares also needs Linear connected (Tares console, Settings, Linear) to keep its ticket list in
  sync; `link_linear_project` says so if it is not.
- **Tares only:** nothing else to set up.

## 3. Create the project

Call `create_project` with a short unique name and the one-line goal. Use that name as `project`
in every call after this. This also ties this session's recording to the project.

## 4. Write the docs that live only in Tares

Use `write_doc`. Write for a reader who was not here: no "as discussed", no references to this
conversation.

- `spec`: what is built and why, who it is for, scope in and out, constraints, how it is checked.
- `plan`: milestones in order, what each delivers, and how the tickets map onto them.
- `agents`: the AGENTS.md of the build: repo layout, commands to build and test, conventions,
  what not to touch.

## 5. Create the tickets

One ticket per piece of work a session can finish and verify on its own, in the order they should
be done.

- **Linear:** create a Linear project named like the Tares project, and one issue per ticket in
  order, with the title and a two or three line description only. Then call
  `link_linear_project` with the Linear project's URL. Tares reads the tickets from Linear; check
  the list it returns matches what you created.
- **Tares only:** call `write_ticket` once per ticket, in order.

## 6. A working doc per ticket

For every ticket, `write_doc` with kind `working`, titled `Working: <ticket title>`, then
`attach_working_doc` with the ticket's id (or Linear identifier, e.g. ENG-12). Working docs never
go to Linear. Each one holds:

- **Context:** why this ticket exists and what it depends on
- **Steps:** concrete steps, in order
- **Files:** the files or modules likely touched
- **Verify:** the exact checks that prove it is done (commands, expected output, what to look at)
- **Out of scope:** what this ticket must not do

## 7. The starting prompt

`write_doc` with kind `start`, titled `Start here`. It is the first thing a building session
reads. Say:

- what the project is, in two lines, and that the spec, plan and AGENTS.md are in this Tares
  project (`list_docs`, `get_doc`)
- how to work: take tickets in order with `list_tickets`, read each with `get_ticket` (it carries
  the working doc), and mark progress (in Linear for Linear tickets, `write_ticket` otherwise)
- which ticket to start with
- where questions go: leave a `note` doc in the project when something is unclear (the wiring to
  an orchestrator session comes later)

## 8. Check, then hand over

Call `list_docs` and `list_tickets` and confirm: a spec, a plan, an AGENTS.md, a starting prompt,
and every ticket has a working doc. Fix anything missing. Then tell the user, briefly:

- the project name and what it holds (counts, not a dump)
- where the tickets live, with the Linear link if any
- the exact sentence to start the build in a new session:
  `Work on Tares project "<name>": read its starting prompt and begin.`
