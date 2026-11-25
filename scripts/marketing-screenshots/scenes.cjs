// The shots of the marketing library: [slug, path, feature, shows, opts].
// opts.sample: the path is a console sample (?sample=, behind the taresSamples flag), curated fake data.
// Without it the path is the seeded "Bakery orders" project (scripts/seed_factory_demo.py) on the
// scratch cell, also fake but stored and served by the real daemon. {project} is its id.
// opts.dark, opts.click (button names), opts.clickText, opts.scrollText, opts.full (whole page).
const s = (sample) => ({ sample })
module.exports = [
  ['01-home', '/?sample=busy', 'Home', 'Home for a developer: what waits for you across projects, ranked, with the crew at work beside it', s('busy')],
  ['02-home-team', '/?sample=threePeople', 'Home', 'Home on a team cell: the same list, with who each item waits on', s('threePeople')],
  ['03-projects', '/projects?sample=busy', 'Projects', 'Every project the factory builds, with its state, milestone and ticket progress', s('busy')],
  ['04-project', '/projects/uc_sample?sample=building', 'Project', 'One project while it builds: milestones, tickets on the move, and the decisions it needs from you', s('building')],
  ['05-project-ready', '/projects/uc_sample?sample=ready', 'Project', 'A project with every milestone merged, ready for you to close', s('ready')],
  ['06-tickets', '/tickets?sample=many', 'Tickets', 'All tickets across projects, by whose turn it is', s('many')],
  ['07-ticket-review', '/projects/uc_sample/tickets/T99?sample=changes', 'Ticket', 'One ticket after review: the findings by file and line, what the reviewer verified by breaking it, the checks that ran', s('changes')],
  ['08-ticket-merged', '/projects/uc_sample/tickets/T99?sample=merged', 'Ticket', 'A merged ticket with its full record: review rounds, checks, assumptions the builder made', s('merged')],
  ['09-reviews', '/reviews?sample=l3', 'Reviews', 'Pull requests waiting on a human, and the ones the crew has in hand', s('l3')],
  ['10-merge-queue', '/projects/uc_sample/github?sample=queue-mixed', 'Merge queue', 'The merge queue: each pull request with review, CI and guardrail state, in merge order', { sample: 'queue-mixed', clickText: ['Merge queue'] }],
  ['11-crew', '/crew?sample=busy', 'Crew', 'The crew: orchestrator, builders, reviewer and releaser, what each is doing now', s('busy')],
  ['12-crew-team', '/crew?sample=team', 'Crew', 'A crew shared by a team', s('team')],
  ['13-docs', '/docs?sample=busy', 'Docs', 'The rules and memory the crew reads before every task', s('busy')],
  ['14-docs-memory', '/docs?sample=pmem', 'Docs', 'A project memory: what the crew learned about the code, line by line, with who added it', s('pmem')],
  ['15-brain', '/brain?sample=all', 'Brain', 'Repositories the crew indexes so it can search your code and history', s('all')],
  ['16-settings', '/settings?sample=busy', 'Settings', 'Factory settings: the tools it connects, the crew, models and the standing permissions you gave', s('busy')],
  ['17-setup', '/setup?sample=empty', 'Setup', 'Guided setup: tickets, review, CI, memory, monitoring, guardrails and runtime, one slot each', s('empty')],
  ['20-live-project', '/projects/{project}', 'Project', 'A seeded project, Bakery orders, as the daemon serves it: plan, milestones and ticket states', {}],
  ['21-live-ticket', '/projects/{project}/tickets/T1', 'Ticket', 'Ticket T1 of Bakery orders: changes asked on the first review, passed on the second, then merged', {}],
  ['23-live-crew', '/crew', 'Crew', 'The seeded crew reporting in, with the builder on Bakery orders', {}],
  ['d1-home-dark', '/?sample=busy', 'Home', 'Home, dark theme', { sample: 'busy', dark: true }],
  ['d2-project-dark', '/projects/uc_sample?sample=building', 'Project', 'One project, dark theme', { sample: 'building', dark: true }],
  ['d3-ticket-dark', '/projects/uc_sample/tickets/T99?sample=changes', 'Ticket', 'A reviewed ticket, dark theme', { sample: 'changes', dark: true }],
]
