# Software factory for teams: acceptance runbook

A live check that two people can run the software factory on one Tares: one leads a change that
touches two repos, the other owns one of them, and each builds their own part with their own crew.
It follows the guide at https://docs.glassflow.ai/tares/software-factory/teams (on the glassflow-docs
`teams` branch until it is deployed; the run needs it live, or the branch open beside you), and the
names below are that guide's.

Who: Ashish (lead, owns the API repo) and Kiran (owns the web repo). Repos: `glassflow/factory-test-api`
and `glassflow/factory-test-web`. Time: about an hour and a half, most of it waiting on builders and
on the orchestrators' 20 minute ticks.

Run it on a dev cloud cell, never on production. Do not run it on a cell with real projects: it
adds people, areas and a private project.

## Before you start

This step is Ashish's. The commands are here as text; nobody runs them from this file.

1. Build a Tares image from the `teams` branch of `glassflow/tares` and deploy it to a dev cloud
   cell. The image must contain the key-scope fix (TR-636 follow-up): a key with only the `read` and
   `ingest` scopes, bound to a person, can answer a question put to it and sign off its repo.
2. Deploy the control plane from the `teams` branch of tares-cloud. Cells first, then the control
   plane.
3. Open the cell's address in a browser, add `/health`, and check it reports the new version.
   Signing in is checked in the next section.
4. Check that the repos exist and both people can push a branch and open a pull request in each.
   Each repo needs a CI check that passes, or the builders have nothing to merge on.
5. Clone the repos where the orchestrators find them: both on Ashish's machine, and
   `glassflow/factory-test-web` on Kiran's. The hand-over records each repo's folder on Ashish's
   machine, so clone `factory-test-web` at the same folder path on Kiran's. A repo with no clone
   there makes Kiran's orchestrator put a "where is it cloned" question on the desk and leave the
   repo's tickets alone.

Do not go on until the cell runs the `teams` image. The rest of this file fails in confusing ways on
an older one.

## People

Ashish and Kiran each sign in from their own machine:

```bash
tares login
```

Then, on the console, open **Settings, Access, people and API keys** and check **People** lists
both, each with an email.

The commands below need the cell's address. Read it from the login file into `CELL`, and do not
export `TARESD_URL` or `TARES_FACTORY_URL` anywhere: with either set, `tares factory crew up`
ignores the saved login and runs on whatever key it finds in `~/factory/.tares-token`.

```bash
CELL=$(jq -r .cell_url ~/.tares/login.json)
```

Check what each login may do, from each machine:

```bash
curl -s "$CELL/api/whoami" -H "Authorization: Bearer $(jq -r .key ~/.tares/login.json)"
```

The answer shows the person and the scopes. Each login must be bound to its person and carry the
`admin` scope: the crew runs on it, and the crew's writes (`assign_ticket`, `set_stage`, the desk)
need `admin`. Ashish's lead steps need it too.

Ashish then adds Lee, who is on no project: **Settings, Access, Add person**. The key picker lists
only people who exist. He then makes two more keys under **API keys** on the same page, each bound
to a person by the **person** picker:

| Key | Person | Scopes | Used for |
|---|---|---|---|
| `kiran-contrib` | Kiran | `read`, `ingest` | Kiran's contributor session, to show the fix works |
| `lee-outsider` | Lee (a third person, on no project) | `read`, `ingest` | the outsider checks at the end |

Copy each token when it is shown; it is shown once. Keep Lee's in `$OUTSIDER_KEY` and Kiran's in
`$CONTRIB_KEY`, in Ashish's terminal. Ashish sends Kiran's token to Kiran over a private channel,
not into the report. Run `whoami` with each and check the person and the two scopes.

## Areas

On **Settings, Areas** (or `set_area` in Claude Code), set the owners:

| Repo | Owner |
|---|---|
| `glassflow/factory-test-api` | Ashish |
| `glassflow/factory-test-web` | Kiran |

The page lists both, each with its owner. A repo with tickets needs an owner before a project can be
handed over, so do this before the spec.

## Spec

Ashish, in a folder that holds both test repos, runs the spec:

```bash
tares factory spec "add a nickname field to the API and show it on the web page"
```

Tell the spec session two things:

- The change touches both repos: a new `nickname` field on the API, and the web page showing it.
- The project is private. Kiran is the only member besides Ashish.

When the spec skill needs to know how the web page gets its data, it asks Kiran with
`ask_area(repo="glassflow/factory-test-web", ...)` and carries on with the API part. On a private
project the question is refused unless Kiran is a member, so the spec session adds Kiran first with
`set_project_access(add_members=...)`. If it was refused, ask it to do that and ask again. Check on
the project page that Q1 is listed as open and addressed to Kiran.

The session ends by writing two tickets: the API ticket for Ashish, and the web ticket for Kiran,
which depends on the API ticket. Note the project name and its id (the id is in the project page's
address); the rest of this file calls them `<project>` and `<project-id>`.

## Contributor

Kiran, on their own machine, in a second terminal (the crew has not started yet), sets the narrow
key and runs:

```bash
export TARES_AUTH_TOKEN=$CONTRIB_KEY
tares factory spec --project <project>
```

Because Kiran is not the project's lead, this opens a contributor session. It needs no git repo in
the folder. In it, Kiran first proves the session took the narrow key: ask it to call
`ask_area(project="<project>", repo="glassflow/factory-test-api", text="test")`. The answer must be
"this credential lacks the 'admin' scope". If the question is accepted instead, the session is on the
login's `admin` key: stop and note it, because the next two steps prove nothing. Do not try
`hand_over` as a probe: on the wrong key it would hand the project over before Ashish does.
Then Kiran:

1. Answers Q1 (`answer_question`). Only the person a question is addressed to can answer it.
2. Reads the tickets in `factory-test-web`, and says yes with `sign_off(repo="glassflow/factory-test-web")`.
   Only the repo's current owner can sign off.

Both calls must work with this narrow key. If either says "this credential lacks the 'admin'
scope", the cell image is older than the fix and Before you start was not finished.

On the project page, Q1 shows as answered and `factory-test-web` shows **Signed off**.

Then Kiran closes this terminal. That is not enough on its own: the session wrote the narrow key to
`~/factory/.tares-token`, and the crew's stations read only that file. The next step, Build, runs
`tares factory crew up` from a clean terminal (no `TARES_AUTH_TOKEN`, no `TARESD_URL`), which copies
the saved login key over it. If the cell refuses the login key, `crew up` prints "warning: Tares at
... refuses key ...; the crew keeps the key it has" and leaves the narrow key in place: run
`tares login` again and repeat. Do not start a crew from the contributor terminal before that.

Ashish then hands over from the spec session. The hand-over is refused while Kiran's repo has no
sign-off, and goes through once it has one. `factory-test-api` is Ashish's own repo and needs no
sign-off.

## Build

Each person starts a crew with their own name, on their own machine, with only the saved login (no
`TARES_AUTH_TOKEN` in that terminal):

```bash
export FACTORY_CREW_NAME=ashish   # Kiran uses kiran
tares factory crew up
```

The stations are named `ashish-orchestrator`, `ashish-reviewer` and so on, and
`kiran-orchestrator` and the rest. Run `tares factory roster` to see them. Each orchestrator ticks
every 20 minutes, so allow up to 20 minutes for each hand-off below.

Then wait:

1. Ashish's orchestrator sees the API ticket in `my_work` and gives it to a builder. The API
   pull request is reviewed and merged.
2. Kiran's orchestrator sees nothing ready at first, because the web ticket depends on the API
   ticket. Once the API ticket is merged, it shows up in Kiran's `ready` list, and Kiran's
   orchestrator assigns it on its next tick.
3. Kiran's builder opens the web pull request and it is reviewed and merged.

Nobody tells Kiran's crew that the API is done.

While the API ticket is still open, make the desk item for check 4. In Ashish's orchestrator
session, ask it to put one question on the desk about the web ticket, addressed to Kiran: `desk_add`
with `to` set to Kiran's email (a name is refused), `project` set to `<project>` and `tickets` set to
the web ticket, for example "Should the web page show an empty nickname as blank?". Do check 4
before Kiran's orchestrator answers it: an answered item leaves the open desk, and that orchestrator
ticks every 20 minutes.

## Expected results

Check each of these and write down pass or fail. Each says where to look.

1. **Both crews show.** The console **Crew** page lists the `ashish` crew and the `kiran` crew, each
   with its person and machine, the viewer's own first. `tares factory roster` on each machine lists
   only its own stations.
2. **Kiran's orchestrator never touches the API ticket.** Open the API ticket's own page and unfold
   **History**. No line is by a `kiran-` station, and no `kiran-` builder holds it.
3. **The web ticket waits, then moves by itself.** While the API ticket is open, the web ticket is
   not in Kiran's `my_work` `ready` list. After the API pull request merges, it is, and a `kiran-`
   builder holds it after Kiran's next orchestrator tick.
4. **The desk addresses the right person.** The desk question from Build shows under **For Kiran**
   in the desk notice at the top of the **Crew** page when Ashish views it. When Kiran views it, the
   question is the only one, so there is no heading: the notice reads "1 question waiting for you".
   Ashish's own `desk_list(mine=true)` does not show it, and Kiran's does.
5. **An outsider cannot see the project.** Lee's key sees a hidden row for the project (its id, name
   and lead, marked hidden, and nothing about its tickets), and cannot open it. Run these in order:

   ```bash
   curl -s "$CELL/api/projects" -H "Authorization: Bearer $OUTSIDER_KEY"
   curl -s -o /dev/null -w "%{http_code}\n" "$CELL/api/projects/<project-id>" \
     -H "Authorization: Bearer $OUTSIDER_KEY"
   curl -s -o /dev/null -w "%{http_code}\n" "$CELL/api/projects/<project-id>" \
     -H "Authorization: Bearer $(jq -r .key ~/.tares/login.json)"
   ```

   The first lists the project with `"hidden": true`. The second answers `404`. The third, with
   Ashish's login, answers `200`: that is the control, so a `404` above means the visibility rule,
   not a wrong address. Lee's MCP `list_projects` leaves the project out.
6. **A non-member cannot answer or sign off.** Lee's key gets past the scope check, so the project's
   visibility decides:

   ```bash
   curl -s -w "\n%{http_code}\n" -X POST "$CELL/api/projects/<project-id>/signoffs" \
     -H "Authorization: Bearer $OUTSIDER_KEY" -H "Content-Type: application/json" \
     -d '{"repo": "glassflow/factory-test-web"}'
   ```

   The answer is `404`, the same as a project that does not exist. Answering Q1 the same way, at
   `/api/projects/<project-id>/questions/Q1/answer` with `{"answer": "x"}`, is also `404`. Check after
   both that Q1 is still answered by Kiran and the sign-off is still Kiran's.
7. **Every history line names a person, except GitHub's.** On both tickets, unfold **History**: every
   line the crew wrote shows a person next to the station, for example "Kiran (kiran-orchestrator)"
   and "Ashish (ashish-reviewer)". Lines by `github` ("GitHub shows the pull request merged", "new
   commits pushed after the verdict") have no person; that is expected. No other line shows a bare
   station.
8. **The hand-over waited for the sign-off.** The project page shows "Handed to the crew" as a
   relative time, so compare exact times from the API:

   ```bash
   curl -s "$CELL/api/projects/<project-id>/signoffs" \
     -H "Authorization: Bearer $(jq -r .key ~/.tares/login.json)"
   curl -s "$CELL/api/projects/<project-id>/crew" \
     -H "Authorization: Bearer $(jq -r .key ~/.tares/login.json)" | jq .handover.at
   ```

   The `at` of the `factory-test-web` sign-off is earlier than `handover.at`. The **Sign-offs** fold
   of the project page shows no override reason.

If a check fails, copy the exact output, the ticket and the time into the report, and stop there.
Do not fix anything on the cell while the run is open.

## Cleanup

1. On each machine, stop the crew: `tares factory crew down`.
2. In a terminal on Ashish's machine, once both tickets are merged: `tares factory finish <project>`.
   It is refused while a ticket is still open, and says the project is not one the crew holds if an
   orchestrator already finished it; that is fine.
3. In `glassflow/factory-test-api` and `glassflow/factory-test-web`, close any open pull request and
   delete the branches the builders made. Revert the merged change on the default branch if the next
   run needs a clean repo.
4. On **Settings, Access, people and API keys**, revoke `kiran-contrib`, `lee-outsider` and the
   sign-in keys made only for this run.
5. Leave the people and areas, or remove them under **Settings, Areas**, as the next run wants.
