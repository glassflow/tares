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

## 1. Start Tares

```
tares up
```

It serves the console at http://127.0.0.1:8787. On another port, set `TARESD_URL` (for example
`export TARESD_URL=http://127.0.0.1:8797`) in every terminal where you run `tares factory`.

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

Keep it alive in a spare terminal: `tares factory crew watch`. `tares factory doctor` checks
everything the crew needs.

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

## 4. Watch it build, answer its questions

- **Crew page:** who is on the crew, what each member is doing, the projects it holds. Click a
  member for its tickets and its last lines.
- **Project page:** the repos, the milestones and tickets with their stage, pull request and
  verdict, the reviews, assumptions and decisions.
- **Questions for you** reach the orchestrator's session, one at a time, with a recommendation.
  Answer there:

  ```
  tares factory crew attach orchestrator
  ```

  Your answer is kept on Tares in your own words. Say it holds from now on, and it becomes a
  standing grant.

## Stop and clean up

- `tares factory crew halt "why"` stops merges, pushes and new sessions until `tares factory crew resume`.
- `tares factory crew down` stops every station. Tares keeps the projects, tickets and answers;
  `tares factory crew up` starts fresh sessions that carry on from them.
- The crew's own files are in `~/factory` (`FACTORY_CREW_HOME` to move it). Deleting that folder
  forgets the stations, not the projects.
