Every edition screen follows this list. `cd ui && npm run test:words` enforces it.

The list is `words.json` next to this file. It covers the `factory` and `sre` editions; the `platform`
edition is today's console and keeps its own words. The replacement words below are proposals until
Ashish confirms them in the pull request.

A word on the list never appears on an edition screen: not in a heading, a button, a label, a
placeholder, a constant, sample data kept in a `.ts` or `.tsx` file or an API answer a screen reads.
Whole words only, any case, plural included. A few ordinary phrases are allowed (`allow` in
`words.json`): "open source", "View PR", "View all", "view the" and "run the tests", "runs on". Avoid
the verb "run" instead of relying on them: write "Try its checks", not "Run its checks".

Two limits of the check. "runs on" is allowed as a phrase, so "3 runs on main" passes even though
"runs" is the noun there; word it as "3 sessions on main". Only whole words match, so "running",
"ran" and "triggered" pass; write those in plain words too. `selftest` pins both.

## Software Factory

| Internal name | What the person sees | Example |
|---|---|---|
| source | connection | "Connect GitHub to see pull requests." |
| view | name the page: Home, Projects, Crew | "Open the Projects page." |
| trigger | rule | "This rule starts a session when a ticket moves to Ready." |
| run | session | "Lee's session on TR-12 has waited 6 days." |
| dispatch, firing, delivery | notification | "3 notifications went out today." |
| wiring | setup | "Setup is not finished." |
| catalog | connections | "These are the connections this crew can use." |
| entity | name it: repo, PR, ticket | "The repo has 2 open PRs." |
| ingest | receive | "The crew can receive pushes from GitHub." |
| station | crew member: lead, builder, reviewer | "The reviewer has 2 PRs waiting." |

## AI SRE

| Internal name | What the person sees | Example |
|---|---|---|
| source | connection | "Datadog is connected." |
| view | name the page: Home, Services, Incidents | "Open the Incidents page." |
| trigger | check rule | "This check rule looks at error rates every minute." |
| run | check (a watcher), investigation (an investigator) | "The check on service-a happened 17 seconds ago." |
| dispatch, firing, delivery | alert sent | "An alert was sent to #oncall 8 minutes ago." |
| wiring | setup | "Setup is not finished." |
| catalog | connections | "These are your connections." |
| entity | service | "service-a has 1 open incident." |
| ingest | receive | "Tares can receive alerts from your monitoring." |
| station | not used | |

## Copy rules

- Plain words for what happened. Name the symptom, then the cause.
- Durations in words, never raw minutes or ids: "17 seconds ago", "8 minutes ago", "6 days",
  "1 day, 18 hours". `durationWords(seconds)` in `words.ts` formats them: one unit, floored; when that
  unit's count is 1 the next unit follows if it is not zero.
- Say who an item is from: "Lee opened this PR", "from the reviewer".
- Put what needs the person first, and say how long it has waited.
- No em dashes in any rendered string. Use a comma, a colon or a new sentence.
- Building blocks (connections, rules, agents, the catalog) are only under Settings, Advanced.
- Avoid the verb "run": "try its checks", "the check happened".

## Marking text the list does not apply to

- `data-words-exempt` on the element holding raw tool output, raw log rows, or a document body that a
  person or an agent wrote (Docs, Runbooks, the Crew page's "Show what it did").
- `data-user-text` on a name a person chose (a project title, a repo name).
- `// words-ok` at the end of a line for a literal that is not shown to people, such as an API field
  value.

Nothing else is exempt. The static check (`ui/tests/words.check.cjs`) reads every string in
`ui/src/editions`, `ui/src/components/edition` and `ui/src/pages/dev`, in `.ts` and `.tsx` files only
(sample data in a JSON or other file is not read, so keep it in a `.ts` file); the browser scripts call
`checkWords` and `expectOneCall` from `ui/tests/words.cjs`; API tests use `tares/words.py`
(`python -m tares.words --skip name,goal,title,detail,said,text` reads JSON on stdin).
