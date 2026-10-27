# Software factory for teams: acceptance runbook

A live check that two people can run the software factory on one Tares: one leads a change that
touches two repos, the other owns one of them, and each builds their own part with their own crew.
It uses the guide at https://docs.glassflow.ai/tares/software-factory/teams, and the names below
are that guide's.

Who: Ashish (lead, owns the API repo) and Kiran (owns the web repo). Repos: `glassflow/factory-test-api`
and `glassflow/factory-test-web`. Time: about an hour, most of it waiting on builders.

Run it on a dev cloud cell, never on production. Do not run it on a cell with real projects: it
adds people, areas and a private project.

## Before you start

This step is Ashish's. The commands are here as text; nobody runs them from this file.

1. Build a Tares image from the `teams` branch of `glassflow/tares` and deploy it to a dev cloud
   cell. The image must contain the key-scope fix (TR-636 follow-up): a key with only the `read` and
   `ingest` scopes, bound to a person, can answer a question put to it and sign off its repo.
2. Deploy the control plane from the `teams` branch of tares-cloud. Cells first, then the control
   plane.
3. Check the cell reports the new version, and that each person can sign in:

   ```bash
   curl -s "$TARESD_URL/health"
   ```

4. Check that the repos exist and both people can push a branch and open a pull request in each.
   Each repo needs a CI check that passes, or the builders have nothing to merge on.

Do not go on until the cell runs the `teams` image. The rest of this file fails in confusing ways on
an older one.

## People

Ashish and Kiran each sign in from their own machine:

```bash
tares login
```

Then, on the console, open **Settings, Access, people and API keys** and check **People** lists
both, each with an email.

Check what each key may do:

```bash
curl -s "$TARESD_URL/api/whoami" -H "Authorization: Bearer $TARES_AUTH_TOKEN"
```

The answer shows the person and the scopes. Ashish's key needs the `admin` scope, since a lead
makes the project, asks the owner and hands over. Kiran's key can have `read` and `ingest` only.
That is what the contributor step checks.

Also make a third person, Lee, who is on no project, and a `read` key bound to Lee. This is the
outsider key for the checks at the end. Keep its token in `$OUTSIDER_KEY`.

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
`ask_area(repo="glassflow/factory-test-web", ...)` and carries on with the API part. Check on the
project page that Q1 is listed as open and addressed to Kiran. If Ashish made the project `team`
instead of `private`, set it now with `set_project_access`. The question is refused until Kiran is
a member.

The session ends by writing two tickets: the API ticket for Ashish, and the web ticket for Kiran,
which depends on the API ticket. Note the project name; the rest of this file calls it `<project>`.

## Contributor

Kiran, on their own machine, in any folder, runs:

```bash
tares factory spec --project <project>
```

Because Kiran is not the project's lead, this opens a contributor session. It needs no git repo in
the folder. Kiran:

1. Answers Q1 (`answer_question`). Only the person a question is addressed to can answer it.
2. Reads the tickets in `factory-test-web`, and says yes with `sign_off(repo="glassflow/factory-test-web")`.
   Only the repo's current owner can sign off.

Both calls must work with Kiran's ordinary key. If either says "this credential lacks the 'admin'
scope", the cell image is older than the fix and Before you start was not finished.

On the project page, Q1 shows as answered and `factory-test-web` shows **Signed off**.

Ashish then hands over from the spec session. The hand-over is refused while Kiran's repo has no
sign-off, and goes through once it has one. `factory-test-api` is Ashish's own repo and needs no
sign-off.

## Build

Each person starts a crew with their own name, on their own machine:

```bash
export FACTORY_CREW_NAME=ashish   # Kiran uses kiran
tares factory crew up
```

The stations are named `ashish-orchestrator`, `ashish-reviewer` and so on, and
`kiran-orchestrator` and the rest. Run `tares factory roster` to see them.

Then wait:

1. Ashish's orchestrator sees the API ticket in `my_work` and gives it to a builder. The API
   pull request is reviewed and merged.
2. Kiran's orchestrator sees nothing ready at first, because the web ticket depends on the API
   ticket. Once the API ticket is merged, it shows up in Kiran's `ready` list, and Kiran's
   orchestrator assigns it within one tick.
3. Kiran's builder opens the web pull request and it is reviewed and merged.

Nobody tells Kiran's crew that the API is done.

## Expected results

Check each of these and write down pass or fail. Each says where to look.

1. **Both crews show.** The console **Crew** page lists the `ashish` crew and the `kiran` crew, each
   with its person and machine, the viewer's own first. `tares factory roster` on each machine lists
   only its own stations.
2. **Kiran's orchestrator never touches the API ticket.** The API ticket's history on the project's
   Tickets page has no line for any `kiran-` station, and no `kiran-` builder holds it.
3. **The web ticket waits, then moves by itself.** While the API ticket is open, the web ticket is
   not in Kiran's `my_work` `ready` list. After the API pull request merges, it is, and a `kiran-`
   builder holds it within one tick.
4. **The desk addresses the right person.** A desk item about the web ticket shows **For Kiran** on
   the project's Desk page. Ashish's own `desk_list(mine=true)` does not show it.
5. **An outsider cannot see the project.** With Lee's key, the project is hidden:

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" "$TARESD_URL/api/projects/<project>" \
     -H "Authorization: Bearer $OUTSIDER_KEY"
   ```

   The answer is `404`. `GET /api/projects` lists the project only as a name and a lead, and Lee's
   MCP `list_projects` leaves it out.
6. **A reader cannot answer or sign off.** With Lee's key, `answer_question` for Q1 and
   `sign_off` for `factory-test-web` are refused with 403.
7. **Every history line names a person.** On both tickets, every line of the history shows a person
   next to the station, for example "Kiran (kiran-orchestrator)" and "Ashish (ashish-reviewer)".
   No line shows a bare station.
8. **The hand-over waited for the sign-off.** The project timeline shows the hand-over after
   Kiran's sign-off, and no override reason.

If a check fails, copy the exact output, the ticket and the time into the report, and stop there.
Do not fix anything on the cell while the run is open.

## Cleanup

1. On each machine, stop the crew: `tares factory crew down`.
2. Close the project from the spec session: `tares factory finish <project>`.
3. In `glassflow/factory-test-api` and `glassflow/factory-test-web`, close any open pull request and
   delete the branches the builders made. Revert the merged change on the default branch if the next
   run needs a clean repo.
4. On **Settings, Access, people and API keys**, revoke Lee's key and the sign-in keys made only for
   this run.
5. Leave the people and areas, or remove them under **Settings, Areas**, as the next run wants.
