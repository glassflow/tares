# The software factory on Tares

One crew of Claude Code sessions on your machine builds every project you hand it. You plan a
project with Claude, the crew takes it from tickets to pull requests merged on main, and Tares
keeps the plan, the ledger, the reviews and the questions for you. You talk to one session, the
orchestrator.

The crew:

- **orchestrator**: hands out work, answers the others, brings you what needs you. The only one
  you talk to.
- **reviewer**: reviews every pull request to a fixed bar and posts the verdict.
- **builders**: one per milestone and repo of a project, started by the orchestrator. They merge
  their own pull requests once the reviewer passes them and CI is green.
- **releaser**: only if you ask for one. Without it, tickets end at merged on main.

## What you need

- Claude Code, signed in, on a plan that allows background sessions.
- `gh`, signed in (`gh auth login`), with write access to the repos the crew works in.
- `git`, `jq` and `curl`.
- Tares: `pip install tares`, or from a checkout `scripts/factory-dev.sh install`, then
  `source .venv/bin/activate`.
- Optional: the Codex CLI, if you want builders to run the Codex challenger on every commit.

If another version of the `tares` plugin is installed from a marketplace, where Claude Code loads
it, it takes the place of this one and the stations run without their skills. `tares factory doctor`
names it; if stations report missing skills, remove it, for example `claude plugin uninstall
tares@tares` (add `--scope project`, run in that project's folder, for a project install).

## 1. Start Tares, or sign in to Tares Cloud

On Tares Cloud there is nothing to start. Sign in once:

```
tares login
```

Your browser opens, you sign in, and you pick the workspace the crew works in (Tares 1.44.0 or
later). This machine gets its own key on it. Every `tares factory` command and every station then
use that workspace and its MCP endpoint, with no environment variables to set; `tares factory
doctor` says it found them through the saved login. `tares login` again switches workspaces (bring
the crew down and up to move it), and `tares logout` removes the key. `TARESD_URL` and
`TARES_AUTH_TOKEN`, when set, win over the saved login. Skip to step 2.

To run Tares on your own machine instead:

```
tares up
```

It serves the console at http://127.0.0.1:8787. On another port, set `TARESD_URL` (for example
`export TARESD_URL=http://127.0.0.1:8797`) in every terminal where you run `tares factory`.

Then, in another terminal, start the MCP endpoint every station shares:

```
tares mcp --transport streamable-http
```

It listens at http://127.0.0.1:8788/mcp. With auth on, start it with `TARES_AUTH_TOKEN` set to your
token. Stations reach Tares only through it, so `crew up`, `spawn` and `spec` stop and say so when
it does not answer. Elsewhere, set `TARES_FACTORY_MCP_URL` to where yours runs. On Tares Cloud the
cell serves it at `https://<cell>.tares-glassflow.com/mcp`, and there is nothing to start.

## 2. Bring up the crew

```
tares factory crew up
```

The crew lives in a folder of its own, `~/factory`, whichever folder you run this from
(`FACTORY_CREW_HOME` moves it). Its stations run there, so Claude Code must trust it once: the
first `crew up` offers to open Claude Code there; pick "Yes, I trust this folder", then `/exit`.

Then it asks how far the crew may go, in a box like Claude Code's: one question per tab, the
recommended answer preselected, ↑/↓ to choose, Enter to select, ←/→ to go back, and a last tab
with all your answers before anything is saved:

- **Merging**: the crew merges once its reviewer approves and tests pass (recommended), or every
  merge waits for your yes.
- **Releasing**: no by default; the crew stops when work is merged to main.
- **Codex check**: a second AI checking every commit; no by default.
- **Builders**: how many may work at the same time.
- **Grants**: anything you always allow the crew, in your own words.

The answers are kept on Tares; a second machine asks nothing. In a script, or with `--yes`, the
same questions come as numbered prompts. The Crew page
(http://127.0.0.1:8787/crew) lists the crew as it starts.

`crew up` also starts the watch in the background, which brings back any station that stops (Claude
Code stops idle sessions after about an hour) and gives a station a permission you allowed in
Tares; its log is `~/factory/watch.log`, and `crew down` stops it. `tares factory doctor` checks everything the crew needs.

`tares factory crew freeze` keeps the crew on today's plugin kit: the watch still tells you when the
checkout has a newer one, but moves no station onto it. A station that starts or revives uses a copy
of the kit as it was frozen; a station already running keeps reading the checkout until it restarts.
`tares factory crew unfreeze` lets the stations follow the checkout again.

## 3. Plan a project

In the repo the project builds on, or in a folder that holds its repos:

```
tares factory spec "what you want to build"
```

This opens Claude with `/tares:spec`. It interviews you one question at a time and has a second
model attack the plan. Then it writes the plan into a Tares project:

- the spec;
- milestones with checks;
- tickets, each with the repo it changes and what it depends on, and a working doc each;
- an AGENTS.md.

It ends by asking whether to hand the project to the crew. On yes, the orchestrator picks it up.

## Or build it in one session

Some projects don't need a crew. Work on one in your own Claude Code session, still on Tares
(the shared AGENTS.md and memory apply, and the session's record lands on the project):

```
tares factory session <project>        # a planned project
tares factory session "<idea>"         # plan a small one and build it right there
tares factory session                  # asks which
```

It opens Claude with the Tares plugin, bound to the project. A plain `claude` has no Tares tools,
so start sessions that work on a project this way.

- **A planned project**: the session reads its starting prompt and works the ready tickets,
  picking up first any ticket an earlier session left in progress.
- **A project the crew has**: it asks whether to take it over. On yes, Tares keeps your words, the
  crew stops working on it (its builders push what they have and write their progress), and the
  session carries on from their branches and pull requests. Answer no to leave it with the crew.
- **A new idea**: the session asks a few questions, writes a light record (goal, short spec,
  tickets) into a new project and builds it. If the plan turns out big (several repos, many
  tickets), it offers the full spec and the hand-over to the crew instead.

When it is done, the session asks you whether the project is finished and keeps your answer.

## 4. Watch it build, answer its questions

- **Crew page:** who is on the crew, what each member is doing, the projects it holds. Click a
  member for its tickets and its last lines.
- **What it spends:** the Crew page shows how much of your Claude plan is left (the 5-hour and
  weekly limits, with when each resets) and which projects used the current 5-hour window. Each
  station shows how near it is to compacting; click it and **Read it** for its context window by
  category, as `/context` shows it. Settings, Usage has tokens and an estimated API cost per
  project for the last week. The readings come from the crew's machine: each station's status
  line after every reply, `/usage` every 10 minutes from the watch (`FACTORY_PLAN_EVERY` seconds
  changes it, 0 stops it), and `/context` when you ask. None of them makes a model call. You can
  read them yourself with `tares factory plan` and `tares factory context <station>`. The plan's
  limits are there on a Pro or Max login only; with an API key there are tokens and cost.
- **Project page:** the repos, the milestones and tickets with their stage, pull request and
  verdict, the reviews, assumptions and decisions.
- **Questions for you** reach the orchestrator's session, one at a time, with a recommendation.
  Answer there:

  ```
  tares factory crew attach orchestrator
  ```

  Your answer is kept on Tares in your own words. Say it holds from now on, and it becomes a
  standing grant.
- **A permission a station was refused** comes to the desk with the station and the exact
  command. Allow it on the project page or the Crew page: check the sentence that becomes the
  grant, choose this project or every project (a crew station such as the reviewer works for
  every project, so its grant is for every project), and click **Allow for <station>**. The
  crew's watch restarts that station with the permission once it is between turns, in the same
  conversation, and it runs the command again. No terminal needed. A station you have open with
  `crew attach` waits until you leave it.

## When a project is done

When its last milestone is done, the orchestrator asks you on the desk whether the project is
finished. Your yes marks it done on Tares: the project page says Done with your words, the Crew page
lists it as finished, and the crew stops working on it. You can also mark one done yourself:

```
tares factory finish <project> "your words"
```

Handing it to the crew again opens it. With no project left, stop the crew (below).

## Stop and clean up

- `tares factory crew halt "why"` stops merges, pushes and new sessions until `tares factory crew resume`.
- `tares factory crew down` stops every station. Tares keeps the projects, tickets and answers;
  `tares factory crew up` starts fresh sessions that carry on from them.
- The crew's own files are in `~/factory` (`FACTORY_CREW_HOME` to move it). Deleting that folder
  forgets the stations, not the projects.
