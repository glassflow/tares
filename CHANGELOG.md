# Changelog

Notable changes to Tares (formerly NavFlow). Format follows [Keep a Changelog](https://keepachangelog.com/);
the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.61.0-rc.3] - 2026-11-27

### Added
- The Start checklist in the home data has its own steps for a person joining a factory someone else set up
  (TR-1314): choose how your crew works, install Tares, sign your machine in, start the crew and join a
  project. It also says what the team already set. Before, a new member got the owner's setup steps.
- `tares factory setup --json` prints the cell's factory setup (steps, integrations and their forms) as JSON for an app to draw, with `--progress` for how far setup is and `--repos` for the GitHub repositories the cell can see (TR-1414).
- `tares capabilities --json` prints one document with the engine version, the machine commands this engine has and what the Tares you are signed in to says at /health, so an app can check before it calls (TR-1415).
- `tares factory setup apply -` (also `setup --apply -`) saves setup choices without a browser: it reads one line of JSON
  from stdin with `save`, `keep` and `default` operations, applies them in order and prints one result per operation
  (schema `setup_apply.json`). An input it cannot run is refused before anything is sent, and an error shows `***` in
  place of a settings value of 4 characters or more. It needs a person's admin key (TR-1421).
- The cell can give a crew a GitHub token for one repository and say who to commit as: `GET /api/factory/github/token?repo=owner/name`, with a person's key (stations included). It answers for a repository that a project of that person names and that a stored github.com credential follows. A token from a GitHub App is short-lived and asks only for contents, pull requests, checks and metadata; a personal token goes to admin keys only and is handed out as stored. A token from a broker credential goes to a non-admin key only when its projects name every repository on that credential (TR-1422).
- A crew station can push, open pull requests and commit with the cell's GitHub token, with no `gh auth login` and no
  global git setup on the machine. When the cell gives tokens and holds a GitHub credential, a station's git asks
  `tares factory git-credential` for github.com repositories in place of the machine's own helpers, commits carry the
  name and email the cell answers with, and `gh` in a station runs through `tares factory gh`: a `gh` that already has
  a login keeps using it, and one without a login gets a fresh token for the repository the command names. When the
  cell gives no token for a repository, git fails for it without asking for a password. With an App credential, a push
  that changes files under `.github/workflows/` is refused, because the token has no workflows permission. On a cell
  without GitHub tokens, a station starts as before (TR-1425).
- A sessions table keeps each Claude Code session's line, tool call and coordination counts, written as each line is stored and caught up from the event history at start, so factory pages can read one row per session instead of counting over every line. Purging a source also drops its sessions rows, so shipping its lines again does not count them twice.
- `/metrics` has a new histogram, `tares_route_phase_seconds{route, phase}`, that splits the time of the ticket brief, the builder brief and merge-ready into the steps each one spends it on (for the briefs: `pr_refresh`, `ticket_read`, `recorded_commands`, `render` and the others; for merge-ready: `pr_read`, `review`, `ci`, `pr_files`, `guardrails`). A phase reports its own time, with the phases inside it left out, so the phases of a route add up to its time. Every series is on `/metrics` from the first scrape.
- Every claude_code transcript line is now also kept in a table of recent lines, numbered within its session, and each session says whether that table holds all of its lines. Nothing reads the table yet: it is the first step toward serving a session's recent lines without a scan of every stored line. A cell that is upgraded starts with every session marked as not complete, and a background pass every ten minutes updates the marks. Two metrics, tares_session_lines_rows and tares_session_lines_bytes, show its size.
- A second store for session data, built on SQLAlchemy and Alembic (TR-1493). It is the SQLite file `sessions.sqlite` next to the DuckDB file on a local install, and lives in the state Postgres when `TARES_STATE_DSN` is set. It holds no tables yet and nothing moves into it, so nothing you see changes.
- The session store now declares the five session tables (stations, states, project sessions, usage, meters) and a one-time copy from DuckDB for a local install (TR-1497). On Postgres the existing tables are taken over as they are, with no data change. Nothing reads or writes the new tables yet, so nothing you see changes.
- An hourly job archives the lines of sessions that have been idle for a week (TR-1506). It writes them as gzip JSON-lines files to a folder or an S3 bucket, reads each file back to check its size and MD5, and only then deletes the rows from the session store; a failed write keeps the rows and counts a failure. Set `TARES_ARCHIVE_URL` (`file:///path` or `s3://bucket/prefix/`), and for S3 `TARES_ARCHIVE_ENDPOINT`, `TARES_ARCHIVE_REGION`, `TARES_ARCHIVE_ACCESS_KEY_ID` and `TARES_ARCHIVE_SECRET_ACCESS_KEY`. Without a URL a cell on SQLite archives to `<data dir>/archive/` and a cell on Postgres archives nothing. A store that answers a different ETag than the file's MD5 (a multipart upload or SSE-KMS) fails the check, so those uploads are not archived. The metrics `tares_archive_sessions_total`, `tares_archive_bytes_total` and `tares_archive_failures_total` count the work. The image and CI install the `archive-s3` extra (`boto3`).
- Home lists what was answered today, folded under Waiting for you.
- The console has an embedded mode for the Tares desktop: opened with `?embed=desktop&theme=light|dark`, it leaves out its own header, theme switch and sign out, keeps its menu with search on top, and takes the desktop's light or dark theme for the rest of the tab. A browser visit is unchanged (TR-1553).
- The factory guard lets a builder push a tag that a standing project grant names, when the crew has no releaser (TR-1574). A grant carries the tag in a backticked clause such as `tag: glassflow/arche-design paper-tokens-v<N>`; the builder may then run `git tag <name> <sha> && git push origin <name>` in a repository whose `origin` is that repo. Every other tag, `git push --tags`, deleting or moving a tag, `gh release` and any tag from a crew with a releaser stay refused. The guard now also refuses `git tag -d`, `-f` and `-a`, `git push --delete <tag>`, `git push origin :<tag>` and a push of a name like `paper-tokens-v3`, which it let through before.
- The factory has a second runtime, Codex, next to Claude Code (TR-1592). With it chosen through the integrations API, `tares factory` starts a station as a Codex app-server of its own on a private socket in the crew home. The station gets a Codex folder of its own that holds a link to your login and nothing else of yours, so none of your Codex plugins, MCP servers or notify programs is loaded, and your account's connectors are switched off; a station that comes up with one anyway is stopped. It needs Codex 0.171.0 or newer, and a crew stays on the runtime it was started on. A Codex station does not get its first prompt and is not watched yet, so setup does not offer the choice. Claude Code crews are unchanged.
- A station on Codex now gets its prompts and can be restarted, revived, retired and stopped with the crew (TR-1593). Its first prompt is handed over at the start as a turn, and each later one (a wake, a note about pace or new rules) is the next turn on the same conversation, only while the station is between turns; a wake that finds it in a turn arrives after the turn, once. The watch revives a Codex station that died, in the conversation it had, ends any command it left running and tells the station so, and `crew status` shows Codex stations. Nothing stays connected to a station between two of these: the crew asks it and leaves. Not there yet: a station that stops on an approval waits until it is stopped, `roster` and `doctor` do not know Codex stations, and `crew up` still needs Claude Code. Claude Code crews are unchanged.
  What a station on Codex asks the person now reaches the desk, and their answer the station. A turn that stops to ask for a command its sandbox does not allow, for a file change outside its workspace, or with a question in words, gets a desk item with the station, the command and its folder; Approve lets that one call through, anything else declines it, and a question gets the person's words. Only an answer a person gave counts, and it is handed only to the request it was given for: a station that stopped asking has its item withdrawn. Nobody stays connected to the station for this; the watch does it once a round. A turn that failed, which leaves a station at rest as if it had finished, is now said, recorded and put on the desk.
  A request that asks the person several questions now gets one desk item for each, and each question is handed the words given to its own item; before, one reply was copied to all of them.
  After a crash a station on Codex is no longer given a new conversation unseen: when its conversation cannot be loaded and its last turn was never compared with the guard's record, it stays down until a person says `tares factory revive <station> --fresh`. A call that was let through and never reported an end (the app-server died while it ran) is written down as one whose outcome is not known, named in the station's next prompt, and refused by the guard as a repeat until the station has looked at what it left and said what it found with `tares factory outcome`. A call that waited for a yes it never got is known not to have run.
  A station on Codex is now recorded as it works, with nothing else to run: the watch's Codex step, which now comes first in each round, sends what each station did since the last look (TR-1595's recorder) before any other step can start another turn. Every stop records last, while the app-server still answers and again once it has ended: a restart, a retire, `crew down` and a station stopped for its guard all leave the commands that ended with their real ends, and the ones still running as outcome unknown. A closing Tares did not take is kept and sent by the next look. A crew on Claude Code is not touched.
  A call whose outcome is not known is named by its call id, in the prompt, the guard's refusal and `tares factory outcome <call>`, so settling one never moves the others. A stop's last recording that Tares could not take is now sent with the old conversation's end when the station is started in a new one, instead of being dropped.
  A station on Codex now gets its prompts and can be restarted, revived, retired and stopped with the crew (TR-1593). Its first prompt is handed over at the start as a turn, and each later one (a wake, a note about pace or new rules) is the next turn on the same conversation, only while the station is between turns; a wake that finds it in a turn arrives after the turn, once. The watch revives a Codex station that died, in the conversation it had, ends any command it left running and tells the station so, and `crew status` shows Codex stations. Nothing stays connected to a station between two of these: the crew asks it and leaves. A crew on Codex needs no Claude Code on the machine: `crew up` starts it from an empty crew home, asking for `codex`, a version stations start on and the person's Codex login instead of `claude`; `roster` lists each station with its turn and the Codex it runs; `doctor` checks `codex`, its version, the login, each station's own Codex folder and its guard, and makes none of Claude Code's checks. A Claude model named in crew.json (its template names opus and sonnet) is not handed to Codex, where a turn asked for it fails: the station runs on the model the account gets by default, and `crew up` and `doctor` say so. Not there yet: a station that stops on an approval waits until it is stopped, and `doctor` does not check a station's connection to the Tares MCP endpoint. Claude Code crews are unchanged.
- A station on Codex now runs the factory guard, and the crew checks that it does (TR-1594). The guard is the same rules a Claude Code station runs, started as `tares hook codex-guard`: it also closes a command's standard input, since Codex can type into a running command and no hook sees what it types, and it leaves a record of every run. A turn must leave that record before the model is asked; one that does not is interrupted with nothing run, and the station is stopped. The watch compares each command and MCP call of a station's last turn with the record every round, and stops a station that ran something the guard did not see. A stopped station stays down until `tares factory revive`. A repository's own `.codex/config.toml` can no longer add an MCP server or a hook to a station: Codex took a station's folder as trusted by itself and read it. Claude Code crews are unchanged.
  A last turn that cannot be listed with its items is not taken for one in order: the station's next prompt waits until it can be compared. The watch's comparison is made under the station's lock, so a station restarted meanwhile is not stopped for what its old process ran.
  A stop compares the station's last turn once more before it ends the app-server, so `restart --fresh` does not start a station again whose last turn ran something the guard did not see.
  When a stop cannot make that comparison the station's row says so, and until the turn is compared only the station's own conversation is started (the start compares first): `restart --fresh`, the watch and a start that finds the conversation gone leave it down, and a person gives the conversation up with `tares factory revive <station> --fresh` or by retiring the station. A station the watch stopped for what a turn ran is started again by `tares factory revive` without being stopped for that same turn again.
  `restart --fresh` goes on only when its own stop compared the last turn: it is refused for a station that is already down, marked or not, and `restart` does not start a station that is down for its guard.
- A Codex station's session is recorded like a Claude Code one (TR-1595). Once a watch round the crew looks at each Codex station and posts what is new to `/ingest/codex`; the cell turns it into the session lines it already keeps, so the session, its messages, commands, MCP calls and state show on the same pages. Nothing stays connected to the station.
  A command is recorded when it starts, from the line the guard writes before it lets the command through (that line now also holds the command and its folder), and again when it ends, with its real exit code, the folder it ran in and what it printed. A command that started and whose end was never written down, because the app-server died while it ran, is shown as started with an unknown outcome.
  A builder's `add_check` on Codex is matched against these commands as on Claude Code. A command that has not ended, or whose outcome is unknown, backs no claim.
  Tokens are read from the count Codex writes per reply. A count it did not give is stored as unknown, never as 0, and a reply of a model Tares has no price for has no cost.
- A station on Codex is connected to the crew's Tares MCP endpoint as itself: the same five identity headers a Claude Code station sends, and the crew's key added by the plugin's headers helper, which reads the key file. The key is in no environment, argument list, settings file or request; the settings hold the file's path. The server is marked required, so Codex does not start or join a conversation when the endpoint is down, and that refusal is never taken for a lost conversation. Before every prompt the crew asks the endpoint one read on the conversation's own connection: a key the cell refuses, or an endpoint that stopped answering, means no turn, and the caller is told why in the cell's words.
- A crew on Codex is paced from the Codex account's own limits (TR-1597). At the top of each round the watch asks one of its stations for them (`account/rateLimits/read`, which uses no model), sends the windows to Tares (PUT /api/crew/codex-limits) and reads the pace Tares works out (GET /api/crew/pace?runtime=codex). The 5-hour window and the week are told by a window's length, never by its place; a window of another length, or of one model, is shown and does not slow the crew. The thresholds are the crew's own (85, 90, 95). A reading that has not arrived, one older than 30 minutes, and an account that reports no limits (an API key) are each said as what they are, and none of them slows anything; Claude's plan is never used for a Codex crew.
  A Codex station whose turn the account's limit ended is still running, so the watch holds it where it is, records the turn and the reset time, and carries it on once after the reset, in the same conversation. A station that is down while the limit is full is not revived into it. What a Codex station is told about the pace names no Cron tool. `tares factory plan` on a Codex crew prints the account's windows, and the watch no longer looks for a Claude plan there.
  A Codex station whose last turn the account's limit ended is now held from every automatic prompt (an idle wake, new grants, a kit update, a note from the pace step), not only from the pace step's own: the hold is read from the turn itself as soon as it ends, whatever the reading says, so a model's own limit and a missing or stale reading hold it the same. A person's own restart is not held.
  Turning pacing off releases a Codex station held after a usage-limit failure, so automatic wakes can reach it again. Missing or stale readings still preserve the hold.
- A builder station on Codex now starts with its operating rules (TR-1598, first part). The rules keep one source, the plugin's skills, which a Claude Code station loads as before, unchanged. A Codex station has no skills, no subagents, no schedule of its own, no slash commands and no way to message another station, so the crew renders the same files for it (`tares/crew/role_text.py`, with what differs kept in `claude-plugin/runtimes/codex`) and hands the result over as part of the station's developer instructions, where it stays after a compaction. Nothing is written to the repository's AGENTS.md or to the person's `~/.codex`. A Codex builder reports through Tares: its ticket's stage, checks and working doc, and its report; a question that blocks goes to the desk by blocking the ticket, and a review is asked for by the stage alone. A rendering that still names something only Claude Code has, or a skill that changed where the Codex wording replaces it, stops the start with the passage named. The boot prompt and the idle-wake note of a Codex station name no skill and no schedule tool. The other roles have no rendering yet and start with their identity alone.
  The first start of a new Codex crew, which has no station recorded yet, now gets the Codex boot prompt: the runtime is taken from the cell's runtime slot when the crew has none of its own.
- Stations of a crew on Codex can now message each other (TR-1599). A station calls the Tares tool `send_to_station`; the watch reads the call from the sender's own session, so the sender is always the station that sent it, and hands the message to the recipient as a new turn between its turns. A station that is in a turn, stopped, or held at its account's limit gets its messages afterwards, once, also after a crash of the watch. A message to a station that is not in the crew comes back to its sender, and what a station sent is taken when it stops. A retire that cannot read the station's last messages is refused (the station is kept, and `--drop-mail` retires it anyway), so a retired station loses nothing it sent. A station's message is never taken as the person's answer or consent. Claude Code crews keep SendMessage. A message's text is quoted in the turn, so it cannot pass for another station's; a message to a station retired before it was delivered goes back to its sender; `crew down --drop-mail` stops a station whose last messages cannot be read; `send_to_station` is refused on a cell whose stations run on Claude Code. The orchestrator, reviewer and releaser of a Codex crew now run in the crew's worktrees folder, so their sandbox cannot write the crew home. A retire reads the station's last messages after its turn is interrupted, and settles a hand-over a round left unnoted before sending anything back; `send_to_station` goes by the calling station's own runtime, not the factory's default. A retire checks the station's turn really ended before that last look, and is refused when it cannot tell.
- `tares factory runtime` says what new crews start on and what the crew of this crew home runs on, with the Codex it found; `tares factory runtime codex` (or `claude_code`) sets it (TR-1600). A crew keeps the runtime it was started on, and the command says so. The console's Setup now offers Codex as the runtime, with the same note. `tares factory status` names the runtime (`runtime` in `--json`, for the crew and each station), and for a crew on Codex says which Codex runs and lists any station the crew stopped (its guard was off, or a turn could not be compared), with why and how to start it again. The console's Crew page shows each member's runtime and model, a Codex member's wait for an approval in words, a member the crew stopped with why (it said "Finished its work."), and a Codex member's usage with its cost unknown, never $0. The cell keeps which runtime and model a station's session runs on. New page: docs/codex.md (setup, the supported Codex version, what a station may do, the guard and its limits, recovery, troubleshooting).
- An accepted milestone shows a few lines on the Milestones page: what it delivered, the decisions taken on the way and what is left. Tares writes them once, with the cell's model when one is set, and "Write again" writes them anew. A model's text is shown only when every number in it is in the figures it was written from; otherwise fixed sentences say the same figures (TR-1645).
- The cell now keeps weekly figures per project: tickets merged and how long they took, time per stage and the part spent waiting on a person, milestones accepted, review rounds and findings, questions and cost by role. They are built once from the cell's history and then kept current in the background, ready for a report page to read. `POST /api/factory/report/rebuild` builds them again.
- The factory console has a Report page: pick a range (last 4 or 8 weeks, this quarter, or two dates) and a scope (your projects, a team, one project or the whole factory), and the address keeps both so a copied link opens the same view. The page frame and its six section headings are in place; their figures come in later releases.
- The Delivered section of the factory Report: tickets merged, milestones accepted, projects with a merge and the median time from start to merge for the range, each with its change on as many weeks before, the first-review pass rate and review rounds per ticket beside them, and a chart per week of tickets merged and of start to merge.
- Report has a Time section: how the time of the stages In progress, In review, Changes asked and Blocked divides, each stage split into waiting on a person and agent time, how long merged tickets took from start to merge, and what is stuck now (the oldest open blocking questions and the tickets longest in one stage). A question's link opens the project page at that question. A question of no project is listed on the Crew page, read only, for the person it waits for, and its link opens the page there. What is stuck now leaves out the questions of finished and Later projects, as it does their tickets.
- The Report page has its Review section: the share of tickets that passed their first review and the review rounds per ticket, for the range and week by week; the findings first raised in the range by kind and severity, fixed against still open, in a table you can sort by any column; each project's figures beside the same number of weeks before, lowest pass rate first; and the five finding kinds that came back on the most tickets in the last 30 days, each marked when a rule you can open was accepted for it.
- The factory Report page has a Cost section (TR-1671). It shows the spend for the chosen weeks and scope by week, project and role, the cost per merged ticket and per accepted milestone, the most expensive tickets and milestones, and what is tied to no ticket. Amounts are API-equivalent cost at list prices, not what a subscription charges, and which ticket a reply counts toward is worked out from who held the ticket at the time, so it is approximate.
- `python scripts/seed_report_demo.py --data-dir <dir>` fills a fresh cell with eight weeks of invented factory history on five sample projects, so Home and the coming Report can be shown to someone (TR-1674). It needs the daemon stopped, a second run adds nothing, and a data dir with other projects is refused unless you pass `--into-existing`. Start the daemon on it with `TARES_PR_SYNC_SECONDS=off`, because the sample pull requests do not exist on GitHub.
- `tares factory doctor` checks GitHub through the console (`github_console`) on a cell that gives GitHub tokens: it asks for a token for each github.com repository of the crew's projects and reads that repository on GitHub with it. When every repository is reached, the doctor no longer asks you to run `gh auth login`, and when the console also says who to commit as for every project folder, it no longer asks for a global git identity. A `gh` login of its own that does not work is still reported, since stations use it when it exists.
- The home data (`GET /api/home`) says whether you are joining a factory someone else already set up (TR-1719). A new `joiner` block gives the team's crew settings, how many other people have a crew here and whether your own crew is up.
- A person can join a team project themselves with Join this project, on its Overview or under People and access (TR-1720), so they no longer have to wait for its lead. A private project says whom to ask instead. Joining a project yourself doesn't give you its secrets until the lead adds you, and it doesn't make you a member of a project that is later made private. Joining needs an enterprise licence and a person's key that can write.
- Home shows a person who joins a factory someone else set up their own first steps (TR-1723): what the team already set, how their crew works, the commands for their machine and the team projects to join, with the team's crews beside them. The link to the first step, `/start#settings`, now opens the choices. Once the steps are done or hidden, Home is as before.
- The orchestrator, the reviewer and the releaser now have their rules on Codex, as the builder has had (TR-1599). They come from the same skills Claude Code reads, with what a Codex station does instead: it holds no schedule and is woken by the factory; the orchestrator's person answers on the desk in the console, not in the orchestrator's session; the reviewer is a station of its own and says, in what it did not verify, that no second model checked its review. A Codex orchestrator asks for approval to run `tares factory` commands that reach Tares (its sandbox has no network), settles a milestone's assumptions with decide_assumption, and puts a takeover's list on the desk.
- `tares factory spec` and `tares factory session` work on a crew that runs on Codex (TR-1599): they open your own Codex with the spec or session rules and the crew's Tares, given on the command line, so your ~/.codex is not changed. Run with no terminal, spec plans the idea in one go and records what it decided as assumptions. `tares factory second-look --file <file>` has a separate, fresh Codex session look at a review or a plan, read-only; it says which model it ran on, so a second look by the same model is never presented as a second model's. A Codex reviewer and a Codex spec session use it. `tares factory doctor` says when the challenger is on for a Codex crew, where none runs yet.

### Changed
- The Home, Projects, Tickets and project pages answer faster on a cell with many projects: each builds the team home once, reads the handovers, members, review gates and challenger lines for all projects together instead of one query per project or ticket, and resolves the caller once. Home and Projects at 25 projects ran 449 and 780 statements and now run 105 and 82; the pages say the same as before.
- The ticket list, the GitHub tab and the MCP ticket list no longer wait on GitHub: they show the pull request state kept so far, which the background sync keeps current. Opening one ticket, its review brief or its builder brief refreshes only that ticket's pull request, and waits for it at most 2.5 seconds, instead of refreshing every pull request in the project. A `factory/<ref>` branch is now linked to its ticket by that sync (within two minutes), not by opening the ticket.
- The brain decisions loop now backs off when lore does not answer. After a pass in which a call to lore failed, it waits twice as long before the next one, up to 30 times the usual minute, and goes back to a minute after a clean pass. Before, it retried every minute and each retry logged the same "could not put 50 documents" error again, about 50 times an hour (TR-1475).
  A document lore refuses (400, 413 or 422) is put on its own and left out, and does not slow the loop down.
- The factory reads the crew's protocol messages and the commands its sessions ran from two tables that are written as each transcript line arrives, not by parsing every stored line on each read. A cell that is upgraded fills them in the background at start, so the messages and commands of older lines appear as that finishes. The events are unchanged.
- The five session tables (stations, states, project sessions, usage, meters) are now read and written through the session store (TR-1498). A local install copies its rows from DuckDB once at start, checks the counts, and leaves DuckDB's own copies as they were; on Postgres the same tables are used where they are. Nothing you see changes.
- The station and project session pages read each session's times, line count and coordination calls from the sessions table, one row per session, instead of grouping over every line in the events table. The numbers shown are the same.
- The Crew pages stay the same size as a station collects sessions. A station row carries its current session, `earlier_count` and at most its 5 most recent earlier sessions, while its usage, coordination calls and projects still add up every session. The earlier ones are read a page at a time from `GET /api/factory/crew/{crew_id}/{station}/sessions`.
- The factory pages ask the database only for the projects they show, and the Projects list counts a finished project's tickets instead of loading them (TR-1503). On Postgres, tickets and ticket history gain an index, so a page that reads one project's tickets stays quick as the cell grows.
- A person's turns, the files a session touched and the last thing it said now come from small tables written as each transcript line arrives, not from a scan of every stored line on each read. A cell that is upgraded fills them in the background at start, so older turns and files appear as that finishes.
- A session whose lines are all in the session table is now read from the table, and from its archive files when the hourly job has moved some, instead of from the event store. The session view, a station's conversation and one tool call's output give the same answer as before. A session the table does not hold in full, or an archive file that cannot be read, still reads the event store and logs one line. Those routes now say where the lines came from in `lines_from`: `table`, `archive`, `both` or `events`.
- The factory performance work (the session store, handlers that do each piece of work once, lists that filter in the database) is brought up to date with main, which changed the status words and the Home page in the same files (TR-1535). A finished project's row on the Projects list now carries the same counts and words as any other project: merged, closed, queued and stuck apart, and "Done <date>" for when it finished.
- The console now takes the shared paper files from glassflow/arche-design with `npm run paper:sync`, and a check (`npm run test:paper`) stops hand edits to them (TR-1539). Nothing looks different.
- The console's buttons, dialogs, tabs, status labels and fields come from the shared paper kit.
- The console reads its role layer and motion from the shared paper files.
- The console's design checks come from the shared paper kit.
- The console's screens use the paper kit's controls directly.
- `tares factory crew up` asks a person only what neither they nor their team has set (TR-1721). When the team
  decided the autonomy or the number of builders, one line says so before the questions. A setting the team set,
  such as how the releaser ships, is no longer asked or saved again as the person's own. An answer looser than
  the team's value now shows what Tares says and asks that question again.
- On a crew that runs on Codex, a station's request beyond its sandbox (a fetch, a push, a write outside its workspace) is now decided by its rules, the person's grants among them, as a Claude Code station's auto mode decides it (TR-1599). Codex's own approval reviewer judges each request against a policy made of the station's rule sentences, and denies what no rule covers: no message in a station's conversation counts as the person's consent, whoever it claims to quote. Such a request waited for the person on the desk before. The sandbox is never widened by a grant: a grant made later reaches a running station when the factory restarts it in the same conversation with the new policy, as it does on Claude Code. The guard still runs first, so a halted crew and the guard's refusals stop a request whatever a rule allows. Every decision, allowed or refused, is recorded as a `codex-review` event and said by the watch. A refused request is asked of the person the way a Claude Code station asks for a command its permission check denied. Claude Code crews are unchanged.

### Fixed
- A cell that failed its first start on Postgres, kept running on its local session file for a while, and was then started on Postgres again could lose the sessions it had linked or updated in between. The five session tables are now copied afresh on that first start, so the rows written locally are all there.
- A cell just upgraded now shows right session numbers (lines, start time, coordination calls) at once: the session lists read the events scan until the first catch-up of the sessions table has finished, and the table after. A catch-up that fails leaves them on the scan until the next start.
- GET /api/crew no longer answers 500 when a station appears while the page is being read on Postgres. The four reads that build the stations list now run in one snapshot, so a station created in between shows on the next poll.
- A failed paper sync leaves nothing behind.
- The Claude Code usage read (`GET /api/claude-code/usage`) leaves out a private project you are not on: no row with its name, tokens or cost, in the 5-hour window or in the last days. The shares are worked out over what is left.
- The demo workspace (`scripts/seed_report_demo.py`) no longer shows a GitHub credential error on its pull request pages: its pull requests are marked as samples, so Tares shows what the seed wrote and does not read them from GitHub (TR-1691). Home reads the sample crew as running for an hour after seeding, where it said the crew was not heard from within minutes. The seed says so when it ends and prints the command that shows the crew as running again (`--refresh-crew`, which adds no rows, with Tares stopped). The sample stations now read "Idle". A demo data dir seeded earlier keeps its old behaviour, so seed an empty one to get both fixes.
- A browser test of a connection's page no longer fails now and then: it waits for the project's name before it reads the line that lists the projects.
- Home and the Crew page work out each project's share of the Claude plan over the cost of the projects you can see, so a private project you are not on adds nothing to the figures. A saved Home page is no longer shared between reads that see different projects.
- The crew read (`GET /api/crew`) no longer counts a private project you are not on in a station's usage, and it shows no session cost for a session that worked in one. Tokens and cost count for the same project as in the Report.
- The cost of a deleted private project no longer counts in the totals and shares on Home, Crew, Report and the Claude Code usage read. A crew station that worked only on it is no longer listed.
- The factory Projects list showed the day a project was finished in the server's own time zone, so on a
  machine away from UTC it could read a day early or late. It shows the UTC day, like the rest of the
  console.
- On a server away from UTC, the factory Docs page could show a memory line added minutes or hours ago as
  "today" instead of saying how long ago it was. The newest line is now dated by the UTC day in every time
  zone.
- Someone who cannot see a private project no longer reads its sessions through another person's station: the station's own read, its tool results, its earlier sessions and the crew list leave them out. The station's owner and the project's members see what they saw before.
- Project reads stay consistent after a project is deleted.
- A NUL byte in a session id no longer stops a cell on Postgres from filling the turns, commands and counts of its sessions, or from starting after a challenger line under that id. The NUL is now dropped from the id of a posted line, so SQLite and Postgres store it under the same session.
- More session reads follow project visibility, in the console and for the crew.
- A ticket's checks take their recorded text from the sessions the reader can open. Each check's
  state stays the same for every reader.
- The crew's messages on a ticket and in the builder brief come from the sessions the reader can open. The tickets a rule offer lists are those the reader can read, also after a private project is deleted. Home leaves out a rule offer about a private project that was deleted.
- Reads cost about what they did before after a private project is deleted: the event reads and the
  entity list no longer repeat their lookups on every query, and the entity list answers from saved
  counts again instead of counting the events on every request.
- Rule offers, review briefs and a ticket's challenger findings now follow the projects and sessions each reader can open.
- A station on Codex runs its commands in a plain shell instead of a login shell, so they keep the PATH the station was started with (TR-1600). On Debian and Ubuntu a login shell's /etc/profile sets PATH anew, and a `tares` installed in a virtualenv was then not found by the station's own commands, such as `tares factory report`.
- A station on Codex is now woken by the factory even when idle_wake is off, and what the factory tells it after its first start asks for nothing it does not have (TR-1598). It holds no schedule of its own, so with idle_wake off it was never woken again; a revive, a plugin update, a helper's start and a new grant told it to reload skills, re-arm a Cron tick or message its parent. `tares factory doctor` now checks a running Codex station's connection to the Tares MCP endpoint: each server connected, and one read going through with the station's key. Claude Code crews are unchanged. `tares factory outcome` took its own name for the call it was given, so a Codex station could not settle a call cut off by a crash; it now lists the calls and settles one by its call id. A Codex station whose sandbox refused a command (a fetch or a push) was told to wait for a grant that a Codex station never gets; it now asks for approval, which reaches the desk. A Codex station that works in a repository (a builder, a helper) can now write its report in the crew home without asking: its sandbox now also reaches the crew's reports folder, and nothing else of the crew home. The guard now sees a git command behind git's own options: `git -C <folder> push` went past a halted crew and the tag rules, on Claude Code crews too. A command cut off when a station's app-server was killed now shows on the cell with its outcome unknown after the station is revived; it showed as still running. A claimed check now matches a passing run of the claimed command as a step of a plain `a && b` chain that ended with exit code 0 (on Claude Code too); a newer chain that failed or did not end no longer lets an older pass stand. A Codex station whose `tares factory` command cannot reach Tares (its sandbox has no network) is now told to ask for approval, not that Tares is down.
  A resumed Codex conversation that is idle but still lists its last turn as in progress now settles its cut-off commands as outcome unknown after two reads, without waiting for another turn. Active turns and turns waiting on approval stay open.
  Quoted or escaped `&&` arguments are no longer treated as command separators when matching a claimed check against a passing chain.
- Two watches running at once on a crew that runs on Codex no longer record one decision of Codex's approval reviewer twice, or tell of one failed turn twice (TR-1600). Each step now reads and marks a station under its lock, the failed turn told of is kept in a file of the station's own (so a watch writing back the crew's stations file from an older read cannot undo it), and a station that another command is at work on is looked at in the next round.
- Claude Code sessions connected to a Tares cell lost the Tares tools now and then, with "connection closed" on a plain read. The MCP endpoint kept a session for every client and never let one go, so its memory grew until the container was killed. It now keeps none.

## [1.60.0] - 2026-11-20

Everything the crew waits on you for is in one place: Home and the feed list each decision with one tap to answer, and Slack sends you a direct message for each new one. On a licensed cell the Second Brain gets Repos, Connect a repo, Ask and Decisions pages, with decision folders read from your repos. Crew stations start lean, with only the MCP servers and plugins their role keeps and never looser than your own Claude Code settings, and their reports become small state files. The settings, connection and Explore screens have a new look and use the page's own words.

### Added
- A crew can now say which of your MCP servers and Claude Code plugins each station role keeps. `stations.<role>.mcp` and `.plugins` in crew.json hold the names (server names from ~/.claude.json, plugin ids from your enabled plugins), and a role with no list keeps none. `tares factory allow` shows them, one line per role, and `tares factory allow <role> mcp|plugins <names>` sets one (`--none` empties it). Helpers keep their parent role's lists, and `tares factory doctor` shows one line per role and warns about a name Claude Code does not have. Stations do not use the lists yet: that comes with the lean launch.
- A crew station now loads exactly the MCP servers and plugins its role keeps. The names in `stations.<role>.mcp` of `crew.json` are copied from your `~/.claude.json` into a file of their own for that station (`stations/<station>.mcp.json`, readable by you only, never printed or logged) and given to Claude Code with `--mcp-config`; a name your file does not have is skipped with one line. The ids in `stations.<role>.plugins` are enabled in the station's settings. A role with no lists starts with the Tares plugin and nothing else. A spec that carries its own `--setting-sources` is now forced to `project,local`, and `--no-chrome` is added when it is missing.
- `tares factory context <role>` now reads the station through the same lean launch it starts with, and prints a second line: the tokens loaded before any message, the tools per MCP server, the skills and whether auto-memory is on. It prints counts and server names only, never a setting, header or path. Before, it read the station with only its plugin, so the total did not match what a lean station loads.
- A station can keep its history out of its report: `tares factory report log "<text>"` adds a dated line to `reports/log/<station>/<day>.md`, and `tares factory report grep [--station <s>] [--days <n>] <regex>` searches the logs and reports newest first, at most 40 lines and 4 KB. Stations may run `report` without a permission prompt.
- `tares factory report archive` moves the old append-only station reports into `reports/log/<station>/`: a retired station's report becomes `final.md`, and a live station's becomes `archive-until-<day>.md` once it has written its new state file as `<station>.next.md`, which then takes its place. It is a dry run unless you pass `--apply`, it never deletes a file and never overwrites one (a name already taken gets `-2`, `-3`), it refuses to move anything while a station is in a turn, and it skips a report whose `## Undelivered` section the new one lacks. `report grep` also searches the archived files. It runs on Windows too.
- The watch wakes the orchestrator when a ticket becomes ready to start (including one whose dependencies just merged), a ticket bounces to a new ladder step, or a milestone is ready to check, once each. A Tares without `/api/crew/my-work` skips these signals.
- The watch wakes the orchestrator once when a held ticket (todo, doing or changes) has had no news for 45 minutes, and once per head when a ticket in changes has a holder that is not running. `/api/crew/my-work` now gives each `in_progress` ticket its `head` and `updated_at`; a Tares without them skips the stall signal.
- The watch wakes the orchestrator to ask an open desk item again, at most once an hour and never the moment the item is filed, and once when a project is listed under `releasing` (the person took it over). A project taken over again after a release wakes it again.
- A factory session on a cell with a ready second-brain repo is now told to call `search_brain` or `ask_brain` before it reads code, and to use grep only for repos that are not ready and to confirm an exact line. The spec skill asks the brain about the repo before asking you. A cell with no brain, or no ready repo, shows nothing new (TR-1202).
- The Second Brain menu row now opens Repos, a page with a card for every repo the brain knows. It shows what each repo is doing (queued, cloning, indexing, ready or not indexed), how far indexing has got, when the index last caught up with main, and lets an admin retry or remove a repo.
- The Second Brain has a Connect a repo page for admins (TR-1205). It lists the repos the GitHub connection can read, lets you tick the ones the brain should know and connects them in one go; anyone else sees who the admins are.
- The console has an Ask page for the second brain at Brain, Ask. Type a question and get a quick answer with numbered links to the cited lines on GitHub, and a Look deeper link that asks again for a slower, fuller answer. It names any repo still indexing, and says plainly when no repo is ready, the day's cap is reached or the brain is down (TR-1206).
- An admin can choose the decision folders of a connected brain repo (TR-1209). Tares suggests likely folders such as docs/adr from the repo tree, with a file count each, and the choice is stored per repo with who made it and when. `GET` and `PUT /api/brain/repos/{owner}/{name}/folders` read and set them, and connecting a repo accepts `folders`.
- Anyone on a cell with the enterprise licence and the brain can add a decision by hand (pasted text, or a Markdown, text or PDF file up to 5 MB, naming who decided; the text itself is at most 1 MB) at `POST /api/brain/decisions`, take back one they added (`DELETE /api/brain/decisions/{id}`, admins may delete any), and list every decision from every place with counts per place (`GET /api/brain/decisions`). Reading PDF files needs the new `ee` extra (`pypdf`); without it a PDF answers 501 and pasting still works.
- On a licensed team cell the Projects page now offers Yours and Team, with a count on each (TR-1277). A private project carries a lock, every row says who leads it and who is on it, and a private project you are not on never shows its name: Yours says in one line how many there are and who leads them, Team shows one quiet row each after the projects. `GET /api/factory/projects` carries these keys on a team cell only; a cell of one person, or one without a licence, sees the page as before. This brings back what Home's Projects block had before TR-1275 removed it.
- The Second Brain has a Decisions page (Knowledge, Decisions): every decision the brain knows, with its origin (a factory project, a GitHub repo or the Second Brain itself), who decided and when, 25 to a page, narrowed by Origin (with the cell's counts), Repo and Project dropdowns; the list route takes a `project` filter and answers the repos and projects that have decisions. Record a decision (top right) opens a form at `/brain/decisions/new` where anyone whose key can write adds one, pasted or as a Markdown, text or PDF file, and the person who added one or an admin deletes it. On a project, "Decisions and calls" is now "Crew questions", with the tabs "Answered by the crew" and "Answered by you".
- Decision folders on the Second Brain. Connect a repo shows, under each repo ticked, the folders of decisions Tares found in it (ADRs and the like, with their file counts), ticked to start with, and a field to add another; they are sent with Connect. A Ready repo card says how many decisions came from its folders, linked to the Decisions page for that repo, and an admin changes the folders with Decision folders.
- A person on a crew cell gets a direct message on Slack when something new waits for them: one per item, with what it asks, what it holds up and a link to it in the feed (`/feed?item=<id>`). Nothing is sent for what already waited when the cell starts reading, after more than an hour without a read, or while the person has it switched off. Each person switches it in Integrations, Notifications (`GET` and `PUT /api/me/slack`); it is on until they switch it off. Tares finds the person on Slack by their email, so the Slack app needs the `users:read.email` bot scope; without it nothing is sent and the switch says so. A message Slack refuses for good (a closed conversation, a deactivated user) is shown there too, until one goes through. A cell without Slack sends nothing.
- A link can open the feed at one item: `/feed?item=<id>` puts that card first and marks it "From your link". When the item no longer waits, the page says who answered it, with what, where and when, from the answers record of any day, or that it no longer waits, and shows the rest of the feed under it. `GET /api/answers` takes `item=<home item id>` and then looks back over every day unless `since` is given; a desk item is found under any of its ids.
- A crew member stuck on a prompt in its terminal (a permission prompt, a question or a dialog from Claude Code) waits for its crew's person on Home and in the feed: "Answer crew-releaser in its terminal", with the prompt's reason, and a link to the member on the Crew page. There is nothing to say yes or no with, since the answer is typed in the terminal. The item goes when the member works again; a new prompt is a new item. A member silent for 90 minutes reads as quiet and is not listed. On a project the person cannot see, the item names neither the project nor the reason.
- Factory setup starts with How the factory works: who is on the crew, the tools they use and the step that connects each one, and what happens to one goal. A new setup opens on it, and it stays first in the step list to come back to.
- The feed: a page at `/feed` that lists everything the crew waits on you for, one card per decision, answered with one tap and readable on a phone. Each card says the decision verb first ("Merge PR 336", "Hand Billing to the crew") and what it holds up. A pull request shows whether its checks pass and how far its base moved, and offers Merge when the merge queue asks for it; a milestone to check offers Accept. Home shows the same buttons. Every answer is now recorded with who gave it, what was said and where (`GET /api/answers`), so the feed lists what you answered today, from the console or the feed. A question with options can also be answered in words.

### Changed
- The orchestrator's tick reads a few hundred bytes when nothing moved. `my_work` carries a `fingerprint`, and `my_work(since=<fingerprint>)` answers `{"unchanged": true}` when nothing changed, whatever the desk holds, else a new `fingerprint` and only the parts that changed (`same` names the rest), with a desk item already seen cut to its label and question and `desk_closed` naming the ones gone. A `since` it does not know gives the whole answer. The REST answer of `/api/crew/my-work` is unchanged. The orchestrator and builder skills ask with `since` and call without it after a compaction or when the tool refuses it, so they still work on an older Tares.
- `review_brief` takes `known_map`, the repo map version an earlier brief showed: an unchanged map comes as one line instead of the whole map. `review_queue` now answers `{"fingerprint": ..., "queue": [...]}` and takes `since`: with the same fingerprint (minutes spent waiting do not count) it answers `{"unchanged": true}`. The reviewer's skill asks for both and falls back to the whole answer when the tool refuses or ignores them.
- `get_doc` answers a doc over 12,000 characters as its headings (each with its size) and the start of its first section, with a line saying how to read the rest, instead of the whole body. `get_doc(id, section="<heading>")` reads one section and `section="all"` the whole doc; a shorter doc comes whole as before. The orchestrator and releaser skills ask for the part they need, and still work against an older Tares that ignores `section` and answers the whole doc.
- A crew station now starts lean and never looser than your own Claude Code settings. It loads settings only from its project and local sources, skips the Chrome integration and the claude.ai connectors, and copies the deny and ask permissions and the auto mode soft_deny and environment entries of your `~/.claude/settings.json` next to its own. Your allow rules are not copied. A settings file that cannot be read stops the start, so a station never runs without your restrictions. The OTEL_* variables of your settings and of the cell's monitoring reach the station only through its process environment: they are never written to a file under the crew home and never printed, and a kept copy of the launch no longer holds them.
- Every crew station except the orchestrator (and the helpers under it) now starts with auto-memory off (`autoMemoryEnabled: false` in its settings file), which saves about 13,000 characters on every call. The orchestrator keeps its memory index. The station rules the memory used to carry are in the Tares docs.
- A crew station that keeps the Slack plugin, or an MCP server with slack in its name, now starts with the Slack write tools denied (T20): send, draft, schedule, canvas create and update, reaction, and any tool whose name after slack_ starts with a write verb (reply, upload, share, pin, join, archive and others in tares/crew/launch.py). Before, a station that kept Slack could post. A write tool whose name starts with none of those verbs is not denied by name. Reads (read, search, list, get) stay open. The rules are added to the station settings beside its own and the person's, and only when the role keeps Slack.
- Station skills keep the report as a small state file (under 4 KB, never over 8 KB) with fixed sections (`## Now`, `## Waiting on`, `## Desk`, `## Undelivered`, `## Bouncing`, `## Taken`). History goes to `tares factory report log`, and no skill tells a station to cat, tail, grep or append to a report. The rules sit in the new page `factory-protocol/report.md`.
- The reminder a revived or restarted station gets now tells it to read its report as a short state file and to use `tares factory report grep` for older events, never cat or tail. The releaser tick reads only the Undelivered section of the orchestrator's state file, and tests check that the watch finds a RELEASE only there. The skill check that keeps skills from telling a station to cat, tail or grep a report now catches more forms. `tares factory report grep --section <name>` lists only the lines under that section of each live report, the stations `stations.json` holds and has not retired (the regex is then optional), so the orchestrator can read the messages its peers could not deliver with `--section Undelivered` without the 40-line cap cutting them off.
- A station woken by the watch could not tell which signal woke it, and the orchestrator treated a wake as only the job that woke it. The wake prompt now names the keys that woke the station in one short line, and with idle_wake on the orchestrator runs the whole Tick section on every wake, a liveness wake included. Its Tick section and its liveness prompt say so.
- The crew setting `idle_wake` is now on unless it is set to false. A quiet station no longer wakes every
  15 to 30 minutes on its own tick: the watch wakes it when Tares shows work for it, and otherwise sends a
  liveness wake after 15 to 30 minutes, then 30, then every 45. A crew that set `idle_wake` to false keeps
  its ticks, and `factory doctor` now warns about it. Stations started before the change are told once,
  by the watch, to drop the tick they hold.
- The launch-plan command left out the telemetry and MCP server entries by naming them, so an entry a later release added, or one that could not be read, would have been printed (T47). It now prints only the args, env and settings entries and hides every other or unreadable one.
- Starting the crew with the watch switched off now says, in one line, that idle wake is on and the watch is off, so nothing wakes the stations until the watch runs. The factory-protocol skill says again that idle_wake is on by default and that a station re-arms its tick only when it is off.
- The crew's list tools answer small. `list_tickets` of a 100-ticket project fits in 8,000 bytes: it takes `milestone` and `open=True` to narrow the list and `limit` and `offset` to page it, says how to read the rest, leaves out the working doc, the assignee object and each link when they share one pattern (`ticket_url`), and `detail="full"` gives every ticket with every field as before. `list_docs` leaves the working docs out unless `kind="working"` is asked for and pages with `limit` and `offset`. `list_decisions` gives the newest 15 with each question cut to 200 characters, and `list_milestones` counts the checks of a finished milestone and cuts its goal; `detail="full"` on either gives the whole answer. The skills that read a finished milestone's checks ask for `detail="full"`, and still work on an older Tares that ignores it.
- Times on the console read the same for everyone (TR-1261). Hovering a "3h ago" now shows the moment as a UTC stamp, `2026-11-12 14:05Z`, instead of a date in your browser's language and time zone, and the marks under a chart list UTC stamps too. "3h ago" counts whole hours, so 90 minutes reads "1h ago", not "2h ago". How long ago a project secret was used reads like the rest of the console ("8 minutes ago", "just now" under a minute). Grouped numbers on the factory and SRE screens use a comma whatever the browser's language.
- Spacing, type size and weight on a few factory and SRE screens now follow what their code asked for (TR-1262). The old global stylesheet sat outside any cascade layer and overrode those classes, so: the tabs on a project page sit a little lower under the status label, the "Add a tool" buttons in SRE settings are 13px, some paragraphs lose an 8px margin they were never meant to have, and bold labels marked medium are medium. The platform pages look exactly as before.
- The Access box and People (you, the one person on a cell without an enterprise licence) on the settings pages have a new look: an open cell is said once, in Access, not again above the API keys, and Edit opens a form whose fields say what each is for, says an empty name or a refused email or GitHub login under its field, saves on Enter and cancels on Escape (TR-1392).
- The access key settings (Factory Settings > Access keys, and Settings > Access on the platform page) have a new look: each key says in words what it can do ("Can read and send data."), when it was last used and whose it is, a project key names its project, a new key is made in a form that explains each choice and says a missing name under the field, and revoking a key asks in a dialog (TR-1392).
- The GitHub settings (Factory Settings > GitHub, and Settings > GitHub on the AI SRE edition) have a new look: each account says whose it is, what it does and what uses it, labelled fields with hints, a refused name explained under the field, and deleting an account or creating the App with a warning asks in a dialog instead of the browser's own prompt (TR-1392).
- The model provider settings (Factory Settings > Models > Providers, and Settings > Model providers on the AI SRE edition) have a new look: each provider says where its key comes from and what its model list is, as sentences instead of a table, labelled fields with hints, a refused base URL explained under the field, removing a provider asks in a dialog, and the platform's own provider no longer offers an Edit that the daemon refuses (TR-1392).
- Agent tracing (Factory Settings > Observability, and Settings > Observability on the AI SRE edition) has a new look: a switch with a sentence saying whether traces are sent and where, labelled fields with hints, and a refused endpoint explained under the field. Tracing switched on to Rius without an API key now says that nothing arrives until there is one, instead of reading as on (TR-1392).
- The Slack settings (Factory Settings > Slack, and Settings > Slack on the AI SRE edition) have a new look: the bot token and the signing secret each say whether they are set and where from, the field has a label and a hint, and a refused token is explained under the field (TR-1392).
- Workspace settings on Tares Cloud have a new look: the overview, storage, included credit, the sign-in switch and delete are panels with labelled fields, the new storage size is typed with its range under it (a size outside the plan is said under the field), refusals are sentences, and the team is one list with each person's role, GitHub login and admin flag (TR-1393).
- Export connections has a new look: pick the connections to export, see which carry a secret, and get the file to copy or download. When an export is refused or fails, the page says why; before, the daemon's error answer was shown as if it were the file. A key that can read but not write is told it can't export before it tries. Changing the picks, or turning secrets on, drops a file made before, so the file on screen always matches what is picked. The downloaded file is now named tares-connections.yaml (TR-1394).
- Import connections has a new look. Replacing everything now asks first and says it can't be undone; before, choosing replace and pressing Import cleared everything at once. After an import the page says what came in, in the page's words, with a link to the connections. A key that can read but not write is told it can't import before it tries, and a refused import says the key can't import instead of showing the daemon's message (TR-1394).
- An old link to an agent, a rule or a notification that Tares no longer knows now says so in the page's words ("No such rule", "No such alert" on SRE) with a button to the projects; before, it named the internal kind ("trigger", "firing"). While Tares looks one up, it says what it is opening. An address the console does not know is titled "Page not found" (TR-1394).
- Connect Claude Code has a new look: three numbered steps, each with its own copy button that says when it copied, and when the browser has no clipboard (a Tares served over plain http) the page says so and the text can be copied by hand; before, a failed copy did nothing. When Tares can't say whether it needs a key, the page no longer tells you to leave it empty; it says what to do if the installer asks. Once Claude Code is connected, the page links to that connection under its own name; before, it always linked to claude_code, even when the connection has another name (TR-1394).
- A connection's page has a new look: its head says what kind of connection it is, how often Tares checks it, which projects it is in, whether it is healthy and when it last received something, and a failing connection says how many times in a row it failed and why. Labels, sampled fields and recent events are rows that fit a phone, with a bar for each field's coverage when it varies. The delete dialog names what depends on the connection as rules and agents, and a push connection's address is labelled "endpoint to post to", here and on the project page (TR-1394).
- Discover (finding connections in the Docker containers on Tares's machine) has a new look: each proposal is a row with a checkbox and a sentence saying what it would follow, a failed proposal says why under its own row, and when Docker can't be read or nothing is found, the page says so and offers adding one by hand. Adding again after a partial failure now sends only the ones that failed; before, it sent the ones already added as well, and they failed as duplicates. The page no longer leaves for the connections list on its own after adding; it says what was added and links there (TR-1394).
- The form for connecting Tares to a system (when adding a connection, and on its Configuration tab) has a new look: every field has a label in words and a hint, a missing field is named under it as a sentence, header and label rows have their own name and value fields, and secrets already stored say so and can be kept or removed. On a Postgres table, Discover's proposed columns now show, with "Apply to form"; before, they never appeared (TR-1394).
- Adding a connection has a new look: the kinds are grouped by how their events arrive (Tares reads it, it sends to Tares, documents you add), each described in plain words, and a kind Docker can't run on this machine says why instead of failing later. Picking a kind gives it its own address, so the back button returns to the list. A connection that sends to Tares shows its address and, on a cell that requires keys, its key with a copy button; if the key could not be made, the page says so and where to make one. Pasted YAML always adds and names what it added, with a link to each (TR-1394).
- The connections list (Advanced, Connections) has a new look and is titled Connections: each connection is one row that says in words what it is, how often Tares checks it, which projects it is in, whether it is healthy and when it last received something, with its last error under it. The filters have labels, Import and Export sit beside Add a connection instead of in a menu, and the page fits a phone without scrolling sideways (TR-1394).
- Connect speaks in the page's words: rules where it said triggers, notifications where it said dispatches and deliveries, add where it said subscribe and wire. The tabs are real tabs, so the arrow keys move between them. The MCP client is an ordinary dropdown, so the keyboard and screen readers can use it, and every snippet has its own copy button that still copies the real token while the screen shows it masked. The Webhook tab no longer points to the old Agents page; it says where a project's Subscribers are. When Tares cannot list its tools or rules, the page says so and offers to try again, instead of showing an empty list, and while the tools load it shows that it is loading (TR-1395).
- Explore speaks in the page's words: connections, not sources, and labels and values, not entities. The picked value is marked, each event says when it happened ("3 minutes ago" instead of "T-180s"), and the timeline names the window it covers. Adding a filter, choosing where to read from and choosing the window are ordinary dropdowns, so the keyboard and screen readers can use them. While Tares loads, the page shows that it is loading instead of saying there is nothing yet. A failed load offers to try again, and a failed read says so (TR-1395).
- Reads speaks in the page's words: each row says which client made the read (an agent over MCP, the REST API or this console), whether it was a read or stats, the window in words ("1 hour", not "1h"), and what it returned. Ages read in words, with the UTC time on hover. The client filter is an ordinary dropdown that opens on agents and offers every client, and search matches what was read or the client, in any case. When Tares cannot list its reads the page says so and offers to try again, instead of a bare error line; while they load it shows that it is loading; and with no reads at all it says so and links to Connect (TR-1395).
- All resources lists each kind under a tab in the page's words (Connections, Rules, or Check rules on SRE), with how many there are, in rows instead of a wide table: what each part is, its state in plain words, when it last did something, and the projects that use it as links. The tab is in the address, so Back returns to the kind you came from, and a new tab starts with its search and filter cleared. A failed load offers to try again (TR-1395).
- On a software factory cell, a project that is not a factory project (one made over the API, or before the cell switched editions) now opens on a page of its own: its name and status, what it is for, what it holds in the edition's words (connections, rules, agents, MCP servers, with any deleted outside the project marked), where its full setup lives, and Pause, Turn back on and Delete behind a dialog. An old project address with `?view=` on a factory project now lands on the factory page that holds the same thing: tickets, a ticket, the docs, People or Settings. The factory edition no longer opens the platform edition's project page (TR-1396).
- The console moves instead of jumping (TR-1431 to TR-1436): dialogs, menus and toasts arrive and leave with a short fade, a banner grows open and shrinks shut so the page below slides instead of jumping, rows glide to their new place when a list re-sorts, a status that changes washes once, the tab underline slides, and a page that takes a moment to load waits 200ms before showing its skeleton. With reduced motion set in the system, all of it is instant.
- The Slack message for something that waits for you is edited once it no longer waits, instead of staying as it was: "Answered by Alex from the feed", or "No longer waits for you." when no answer was recorded. More than three new in the same minute come as one message that lists them, and each line is struck through as its item goes. A message Slack refuses for a while (busy or down) is sent on the next read. A restart sends nothing again and still edits what it sent.
- Home and Waiting for you say "N crew answers to confirm" instead of "N calls to confirm", the words of the Crew questions tab they open.
- A repo card in the Second Brain now shows the file count lore reports for the repo (`files_in_index`) after each index and each sync, instead of a count the cell worked out from the files a push touched. With an older lore that does not send it, the card keeps the count it had before. A count of 0 from lore is shown as 0 files.
- Slack DMs open Home at the item that waits. /feed now opens Home.
- Every factory screen now says the same thing about the same state, and says whose turn it is. Ticket rows and cards, Reviews, the merge queue and `how_is_project` show who moves a ticket next: you, the crew, the checks, or another ticket it waits on. A yes reads "Next to merge" until the queue merges it. A project's count line reads like "5 merged, 2 queued, 1 stuck of 8", with a ticket closed without a merge counted apart from merged. Waiting reads "for you" everywhere, CI reads as checks, a reviewer's block reads "Reviewer stopped it", the item to close a project asks its lead "Close the project?" and tells anyone else who can close it, and `tares factory crew status` uses the console's words for a station. A stuck ticket is always on the desk.

### Fixed
- `tares factory report grep --days` no longer crashes: a superscript or other non-ASCII digit is refused with the usual message, and a huge number reads as every day instead of failing with an OverflowError.
- A wake could stop a station that had just started a turn, cutting the turn off. The restart now looks at the station again under its lock, right before stopping it, and leaves it running when it is in a turn or attached; the wake is tried again next round.
- An answer given on the console desk reaches the orchestrator within seconds instead of at the next watch round. The watch asks Tares one small question every few seconds (`GET /api/desk/answered`) and wakes the orchestrator only between its turns. A Tares without that route keeps the old behaviour.
- The `secrets` check on every pull request failed because of a test fixture on another person's unmerged branch. The job fetched every branch and scanned all of them. It now scans only the history that is checked out, which is main plus the pull request, so a branch nobody has merged can no longer turn other pull requests red.
- The pace note no longer stops a station that has just started a turn. The restart now looks again under the station's lock and leaves it running; the note is told on a later round.
- The crew watch no longer repeats a wake error every five seconds after a console answer: it waits 30 seconds and looks again. It also asks Tares only for answers newer than the last one it acted on, and Tares reads only those answers from its database instead of every answered item.
- A pull request whose draft was marked ready showed "CI failing" and could not merge, though the full run passed (T40). The merge gate counted the draft run's cancelled checks as well as the later passing ones. It now counts only the newest run of each check name, and the newest status of each commit status context. A cancelled run with no later run of its name still fails.
- The crew watch no longer stops a station that has started a turn when it restarts it for a kit update, for new grants, or to tell the orchestrator about strays. A restart for new grants that is refused this way is tried again next round instead of being marked done.
- A crew station started with `claude --bg` sent no telemetry when Claude Code's background service was already running, because that service gives a session the settings file's env and not the launcher's environment. The station's settings file now carries the telemetry variables that cannot hold a secret (the endpoint, exporters, protocol, resource attributes and the telemetry switches). Headers, keys, tokens and an address with credentials still never touch a file; the Tares monitoring headers come from `otelHeadersHelper`. A person's own `OTEL_EXPORTER_OTLP_HEADERS` still goes only in the launching process's environment, so a station on an already running service does not get it, and `tares factory launch` says so. `python tests/factory/otel_bg_probe.py file` is the by-hand check against the real `claude`.
- The merge gate could hide a failing check. Since only the newest run of each check name counts, a later run of the same name from another GitHub App replaced a failing run and the gate said "CI passing". Check runs are now told apart by the app that made them, so a failing run still fails until a later run from the same app replaces it.
- The merge queue brought every ready pull request that was behind its base up to date in the same pass, so all of them reran CI and, once the first merged and moved the base, all but one of those runs were thrown away. Now one pull request per repo and base is brought up to date at a time. It keeps the turn until its checks finish and, at L3-review, until the person's yes lets it merge; the others keep their place in the queue and say whose turn it is ("waits its turn"), and the next one goes when that one merges, is held, fails or leaves. A yes given for a commit still stands after the branch update when the update changed nothing in the pull request, so the person is not asked twice. A pull request that is not behind still merges at once, and projects whose CI is not GitHub's are unchanged.
- Every reminder a station gets to re-read its report (after an update, a kit change, a permission change, a pace hold, a revive or a session start) now says the same thing: the report is a short state file, use `tares factory report grep` for older events, never cat or tail it. Retiring a station now warns when its report still holds lines under `## Undelivered`, which nobody reads once it is gone. `report grep --section` read every report when all stations were retired, and now reads none; it counts `(none)` as empty, and no longer misses the headings after a code fence that was never closed. (T45)
- The merge queue's turn for bringing a branch up to date (one pull request per repo and base at a time) had two gaps. A pull request whose update never got its checks, or whose question the person never answered, held the turn for ever and every later pull request waited behind it. Now the turn is kept for two hours while the checks are awaited and four hours while the yes is awaited; after that the next pull request is brought up to date, and the first one keeps its place and carries on when its checks or its answer arrive. And a pull request that had just been updated gave up the turn on the next pass, so one sorted ahead of it that had since become ready was updated, and its CI run wasted, before the updated one merged. Now the updated pull request keeps the turn until it merges.
- A crew station's settings file could hold a secret from the person's telemetry variables when it sat in the value and not in the name: a query or fragment key such as `?api_key=...` or `#access_token=...` in an endpoint, a bare `user:pw@host` or `user@host` (alone or as a pair's value), or a pair such as `auth_token=...` in `OTEL_RESOURCE_ATTRIBUTES`. Those variables are now held back like headers and keys: the launching process gets them, no file does, and `tares factory launch` names them (never their values). `OTEL_EXPORTER_OTLP_CLIENT_KEY` holds a file path, not a secret, but its name says key, so it is held back too and `tares factory launch` says so; the client certificate path still goes in the file.
- A crew station could still write to Slack through a third-party Slack MCP server (a tool such as `conversations_add_message`) or through a write tool whose name the T20 deny rules did not list. The guard hook (`tares hook guard`) now refuses every tool of an MCP server with slack in its name (the Slack plugin included, any case) that is not on its read list (`SLACK_READ_TOOLS` in `claude-plugin/hooks/guard.py`: the Slack plugin's read, search, list and get tools and the common third-party reads such as `conversations_history`). A hook decides in every permission mode; Claude Code has no per-tool allow list for a server, and in auto mode a tool no rule names is left to a classifier. A read tool missing from the list is refused until it is added there. The server is found by its name, so a Slack server called something without slack in it is not held to this. A tool name that splits into server and tool more than one way (`mcp__a__b__c`) counts as a read only if every Slack reading of it is.
- A pull request marked ready while its draft run was still going stayed "CI failing" in the merge queue, and Tares asked for changes it did not need, until a new commit was pushed. Marking it ready starts a new CI run that cancels the draft run, and the draft run's cancelled jobs (some still under their unexpanded matrix names, which no later job shares) were counted as failures. Now a job cancelled in a run that a later run of the same workflow on the same commit replaced is no longer counted; a cancel with no later run still fails the checks, and while the later run has not finished the checks read as pending, not as passed.
- The idle wake prompt put a long or multi-line key in full, so one odd key could fill the prompt. Each key now shows on one line, cut at 80 characters. The merge queue's tests now cover a row that falls behind a second time while it holds the turn and the label of a turn that waits for approval, and its notes say a row keeps the turn until it merges or two hours after its last change. The merge gate no longer takes a passing job of the same name for a required job while the CI run that replaced a cancelled one is still queued. That waiting run stays in the checks when the list is cut to the required names, but only when a job it replaced is required or its workflow is: a rerun of an unrelated workflow, such as a preview or a deploy, does not hold the gate.
- A station stopped on a question or a permission prompt is no longer restarted by an idle wake, a pace note, a message from the strays scan or a desk answer wake, so the prompt is not lost: they wait until it answers or moves on. A change of grants restarts it at once only when an added grant names that station (the answer to its own refused command on the desk); any other change waits until it has been stopped on the prompt for 30 minutes, as the plugin kit move does. A station that is done is woken and regranted as before.
- The `sre-timeline` and `sre-services` browser tests failed about one run in three on an unrelated pull request, with a timeline list that held one item the test did not expect, or a row that said "Checked 4 minutes ago" where the test expected 3. The tests read the cell once, then the page a few seconds later, and the cell answers by its own clock, so a 10-minute or one-minute mark between the two reads changed the answer. Each test now compares the page with the answer the page was given, and the timeline test pins its pages to one instant, so no mark can fall between them. The test-only variable `TARES_E2E_CROSS_BOUNDARY=1` makes either suite wait for the next mark on purpose, to prove it.
- The console's AI SRE home browser test no longer fails now and then with "connection refused" right after "daemon up". Each section of the test starts its own daemon on one port; the test did not wait for the last one to exit, so the next one could not bind and the page was loaded from the dying daemon. It now waits for the process to exit, and "daemon up" counts only when the daemon it started answers.
- The account button at the top right was named "Account menu" while it shows your initials, so voice control could not reach it by what it shows (TR-1269). Its name now starts with the initials, for example "AK, account menu".
- A crew start with `--json` now fails with the code `crew_home_untrusted` and the crew home in `error.folder` when Claude Code has not trusted that folder, so an app can offer to trust it. On Windows the message names the folder with one kind of separator (TR-1407).
- The Second Brain Repos page no longer stays on Loading when the cell answers an empty body, and no longer breaks when the answer has no repo list: an empty body is a failed read with Retry, a missing list is no repos, and a row with no repo name is left out. The page now takes the menu's one read of the brain and reads again through it, so it makes no read of its own.
- A repo card on the Second Brain no longer shows the number of files the last push touched as the repo's file count: an update keeps the count from the first index, less the files a push deleted. The card's commit opens on GitHub, the time and file count sit on their own line, and the line about sessions using grep is gone. History on each card lists the first index and every update after a push to main, with what each changed or why it failed (GET /api/brain/repos/{owner}/{name}/updates, and `last_update` on each repo of GET /api/brain).
- When the Slack app cannot look people up by email, the Notifications setting keeps saying so until the scope is added, instead of clearing itself a minute later. Once the app is reinstalled with the scope, the notice goes on the next read.
- A brain index that the control plane stopped (no progress for a while, or past its time limit) showed "The index of owner/name failed: The index of owner/name stopped: ... Retry to start it again. Retry to start it again." (TR-1496). The cell now shows the control plane's sentence as it is.
- Notifications in Settings now says in one plain sentence why Slack refused a message, instead of Slack's raw error code (TR-1511). A refusal of the Slack app itself, such as a revoked bot token or a missing chat:write scope, now shows for everyone, not only for the person whose message met it. Switching messages off, or connecting Slack again, clears an old notice.
- With the brain off (no licence, or no lore set up), a failed sync that was due for a retry made the sync task wake every 50 ms until the brain came back, using a core for nothing. The task now sleeps until its next poll or push while the brain is off, and takes the due retry when it wakes with the brain on.
- When lore was down or failing while a push to a repo with chosen decision folders was imported, the push's decision documents stayed stale until another push, a restart or a folder change (TR-1514). The importer now keeps the files of that push and tries them again with no new push: after a minute, then after twice as long each time, up to half an hour. A success, a removed repo or a folder no longer chosen ends it.
- A one-tap answer read back to agents as an option letter with matched: false, so they doubted it. It now reads as the option chosen. An answer given by message is accepted as the person's own.

## [1.59.0] - 2026-11-17

Crew stations carry much less context: every skill fits under its size limit and a ticket read answers in a few KB. The Tickets page shows who each ticket is assigned to, and GitHub Issues picks its repository from a list.

### Added
- The Tickets page shows who each ticket is assigned to, on Board cards, List rows and Swimlanes, and filters by assignee: a person, Mine or No assignee. Swimlanes can group by assignee. `GET /api/factory/tickets` returns `assignee`, `assignee_raw`, `assignees` and `me`, and takes `assignee`.

### Changed
- A station that has Linear's own tools now reads one short page (`factory-protocol/linear.md`) before using them: `list_issues` with a `fields` list and a `limit` of 25 or less, `get_issue` with `fields`, at most one `list_issues(updatedAt=...)` look per tick, and one `save_issue` carrying every field. A Linear listing of 25 issues no longer carries every description into the station's context.
- The station-orchestrator skill is now under 16,000 characters. Rare procedures (a project takeover, finishing a project, a mock approval, a refused permission, the milestone assumptions) moved to pages next to it that the orchestrator reads when the situation comes up. Claude Code no longer cuts the skill after a compaction, so the orchestrator no longer reloads it every time.
- The station-builder skill is now under 15,000 characters. The steps for a screen ticket (the mock first) and for a stop from the orchestrator moved to pages next to it that the builder reads when the situation comes up. Claude Code no longer cuts the skill after a compaction, so the builder no longer reloads it every time.
- The station-reviewer skill is now under 8,000 characters. The design check for a screen ticket moved to a page next to it that the reviewer reads when a pull request changes a screen. Claude Code no longer cuts the skill after a compaction, so the reviewer no longer reloads it every time.
- The factory-protocol skill is now under 14,000 characters. The notes for a Tares before 1.56 (no people, no `my_work`) moved to a page next to it that a station reads only when those tools are missing. Claude Code no longer cuts the skill after a compaction, so stations no longer reload it every time.
- The spec-project skill is now under 16,000 characters. The contributor and light-mode procedures, the area owner notes and the write-out of milestones, tickets and working docs moved to pages next to it that a session reads only when it reaches them.
- A crew station reloads a skill after a compaction only when its re-attached copy says it was cut, or a rule it needs is missing, instead of after every compaction; this saves about 20,000 characters of context each time. A revive still loads both skills. A new test, `tests/test_skill_sizes.py`, keeps every SKILL.md at most 16,000 characters.
- A ticket read through the Tares tools (`get_ticket`) now answers in a few KB: a working doc over 4,000 characters comes as its Goal, Steps and Progress with the list of its other sections, and only the latest 8 messages and checks, the first 5 decisions and the open review findings come. `section="<heading>"` or `section="all"` reads more of the doc, `detail="status"` gives only where the ticket stands, `detail="full"` the whole answer as before, and `since=<fingerprint>` answers `{"unchanged": true}` when nothing moved. A skill that asks for a new option still works on an older Tares, which ignores it and sends the whole answer.
- The GitHub Issues connect form offers the repositories GitHub lets the cell see: the Repository box lists them, typing narrows the list, and any owner/name can still be typed. When GitHub lists nothing, the form says why.

## [1.58.0] - 2026-11-16

The Software Factory becomes modular: tickets, review, CI, memory, monitoring, guardrails and the runtime are integrations chosen once for the factory, and a factory is set up in one place, Set up your factory, with Integrations in the menu to change them later. On a licensed cell the second brain connects GitHub repos, keeps them indexed and answers the crew's questions about them with citations.

### Added
- `crew up --yes`, `crew down`, `halt`, `resume`, `freeze`, `unfreeze`, `update`, `restart`, and a station's `restart`, `revive` and `retire` accept `--json`: one result document on stdout (`schemas/result.json`) on success, the error document on failure. Without `--json` nothing changes (TR-1283).
- `tests/test_schema_compat.py` fails when a change to the crew's JSON schemas would break a reader of the last release and `schema_version` was not raised (TR-1280).
- `tares factory crew watch --restart` stops the background watch and starts a new one, and touches no station (TR-1279). `--if-running` does nothing when no watch runs, `--only-under <folder>` leaves a watch that runs from a program outside the folder alone, and `--json` prints `{"schema_version":1,"watch":{"action":...}}` (`watch.json`). It lets the desktop app move the watch onto a new engine version.
- A program can sign in to Tares Cloud, read the saved sign-in and sign out through `tares`, reading only JSON: `tares login --json` (one document per line), `tares login --show --json` and `tares logout --json`, with stable error codes (`login_timeout`, `login_refused`, `cell_too_old`, `keychain_unreadable`, `not_signed_in`) and schemas in `tares/crew/schemas/`. The key is never printed (TR-1282).
- The merge queue page now loads from one call, GET /api/projects/{uid}/merge-queue. It returns each pull request in line with its review, CI and guardrail lines, who it waits for, where a yes was given, and what was merged lately, all worded for people, so the screen only displays them (TR-1094).
- Each connect form on the Set up your factory screen has a Test connection button. It tries the settings in the form against the service and shows Passed or Failed with the integration's own sentence; nothing is saved by a test. An enterprise integration the cell has no licence for stays in the list, marked Not licensed, with the reason and a link to the enterprise licence page. (TR-1095).
- A licensed cell with `TARES_BRAIN_URL` and `TARES_BRAIN_KEY` can now connect GitHub repos to the brain (TR-1197): `GET /api/brain` lists them, `GET /api/brain/repos/available` shows the repos its GitHub connection can read, and admins connect, remove and retry repos. A connected repo waits as queued; indexing it comes later.
- Connecting a repo to the brain now starts its first index (TR-1198). The cell asks the control plane to clone and index the repo, and the repo moves from queued (with its place in line), through cloning and indexing (files done of files total), to ready. When it cannot be done, the repo shows failed with the reason in plain words, for example that GitHub refused the clone or that no GitHub connection on the cell can read the repo, and Retry starts it again. A cell needs `TARES_BRAIN_CP_URL` and `TARES_BRAIN_CP_TOKEN` for this; without them the repo says so instead of waiting. The index states are now queued, cloning, indexing, ready and failed (indexed is gone).
- Removing a repo from the brain now deletes everything the brain holds for it (TR-1199). The cell first cancels the repo's queued or running index at the control plane, then asks the brain to delete the repo and what it indexed from it, and only then takes the repo off the list. Removing runs in the background: if it takes longer than 45 seconds the request answers 202 and the repo shows `removing` until it is gone. If the brain is still indexing the repo, removing waits, with growing pauses, up to four minutes; when it gives up the repo stays on the list with `remove_error` "Still indexing. Try again in a minute.", and removing it again finishes the job. If the control plane cannot be asked, or does not say the index is gone (a wrong token, no brain index, no cancel route yet), nothing is deleted, the repo stays and the message says to try again; a cluster error there is asked again for a minute first. While a repo is being removed, Retry answers 409 and a second Remove joins the first.
- On a licensed cell, agents can ask the second brain a question about the connected repos and get an answer with citations. The new MCP tools `ask_brain`, `search_brain` and `get_brain_doc` use only repos that are ready and name each repo still indexing, so the agent knows to grep it. Quick answers are capped at 200 a day and deep answers at 20 (`TARES_BRAIN_ASK_DAILY`, `TARES_BRAIN_DEEP_DAILY`), and every call is kept with its station, repos, time and tokens (TR-1200).
- A repo on the brain now stays up to date (TR-1201). A push to its default branch is searchable within two minutes when the cell has the GitHub App; a cell with a token connection checks each repo once a minute and syncs when the branch moved. Several pushes in a row become one sync, and a push that arrives while the repo is still indexing is synced once it is ready. When a sync fails the repo stays ready and keeps answering from its last index; the next push or check tries again, and after three failures in a row the repo's card shows why.
- The Second Brain's pages (Repos, Decisions, Ask) are in the Software Factory menu only on a cell whose brain is set up; a cell without one shows none of them (TR-1203).
- Two pull requests that each added a line to `CHANGELOG.md` conflicted on it, so every merge made the next one rebase (TR-1249). Each change now adds a small file under `changelog.d/` (`changelog.d/<ticket>.md`: the category on the first line, the entry below it), `scripts/release.sh` collects the files into a new `CHANGELOG.md` section and deletes them, and a CI check asks for one on a pull request that changes `tares/` or `ui/src/`. The label `no changelog` skips the check.
- `tares login --cell <address> --key-stdin` saves a sign-in to a Tares that is not Tares Cloud, with a key read from standard input (TR-1285). It checks the version and the key and fails with `cell_unreachable`, `cell_too_old`, `key_refused` or `key_lacks_admin`; `tares logout` then deletes the login and leaves the key on the cell.
- Setting up a new factory asks how your crew works in the console (who merges, how many builders, releases on or off), and the commands to type in your terminal tick by themselves within seconds, with no reload, beside a Your crew panel that shows the machine, the orchestrator, the reviewer and the builders. Your choices are saved only when you confirm them, and who merges starts on your approval: letting the crew merge on its own asks first (TR-1312). `tares login` and `tares factory crew up` now tell the workspace which machine they run on, with a key that may only ingest. The report belongs to the person whose key sent it.
- The crew conversation shows the most common tool calls in a form made for them (TR-1317). A Bash card's header says what the command does, and the card shows the description, the command and its output with the exit status. A TodoWrite card is the plan as a checklist with a progress bar, its header counting the steps done. An Agent card shows the helper's type, and its brief and report read as markdown. A message names who it went to and reads as markdown. A Read card shows the file and line range, with the lines numbered beside the code. A Grep or Glob card says its pattern, where it looked and how many files it found, and lists them. Each has a Formatted / Raw switch, and Copy copies the call's input as it came. An input cut short in the list, and every other tool, shows the plain Input and Output blocks as before.
- tares factory doctor, crew status and the crew watch now report Claude processes the crew does not own (a station session left after retire, a second background service), a background service started from an SSH login on Windows, and orphaned crew processes older than an hour. They are reported, never stopped.
- Hook points for the guardrails (TR-861): Tares asks the project's guardrails before it acts, at `pre_build` (handing a project to the crew: a block refuses the hand-over with the reason and records nothing), `pre_merge` (merge_ready, now with the PR's files, base, review and CI state) and `pre_release` (the new `release_ready` MCP tool and `GET /api/factory/release-ready`, which the releaser calls before shipping and stops on when it says not ready). A block stops the action and a warning is only shown; a hook point is one entry in `HOOK_POINTS`. `/health` lists the `hook_points` capability.
- Guardrail pairs (TR-863): you can pair a hook point (`pre_build`, `pre_merge` or `pre_release`) with a guardrail integration, give it its integration settings, and say whether a "no" blocks or only warns (`on_fail`, block by default). The factory adds pairs that apply to every project, and a project's lead adds their own, through `/api/factory/guardrails/pairs` and `/api/projects/{uid}/guardrails/pairs`; secrets are never shown back, and a project pair never uses a factory pair's secret. A guardrail that does not answer in time, fails or is no longer installed follows its pair's `on_fail`, so an action is not let through by a guardrail that went quiet unless the pair says warn. A warning at the merge check is also written to the ticket's history, once. `merge_ready`, `release_ready` and the hand-over now list each guardrail's answer in `guardrails`. `/health` lists the `guardrail_pairs` capability.
- Crew stations take their rules from Tares and never start without rules (TR-864). A station's settings now hold the sentences the cell renders for its role, the team's and its project's grants among them, and the crew keeps a copy of each answer in `rules/<station>.json` in the crew home. With Tares down, or answering with an error, a station starts on that copy, and with no copy on the defaults shipped in the plugin (`claude-plugin/templates/default-rules.json`). A Tares that answers with no rules stops the start, and a cell without the `guardrail_rules` capability stops it with an upgrade message. The watch still restarts a station when its grants change, and its message quotes the rules the station did not have.
- The crew's memory now goes through the memory integration. Tares memory is built in: a line kept with `remember_for_project` or `add_to_global(doc="memory")` is written to the Tares doc first, then to the memory integration the cell uses. A line the integration does not take is kept and offered again every round of the background sync. The new `recall(question)` tool answers from the project's memory and the shared memory, best match first, each line marked [project] or [shared]; builders and the orchestrator call it before reading the code base from scratch.
- The monitoring slot now has three choices, off, Rius and any OTLP endpoint, stored in the same settings as agent tracing, so the console's tracing page and the integrations API never disagree (TR-866). A crew station's Claude Code sessions can report to the same backend: the telemetry variables carry the address and the station, role and project labels, and never a key. The key comes at start from the new `tares hook otel-headers` helper, which fetches it from the crew's Tares.
- A crew station's launch is now built on the cell by a runtime integration (Claude Code is the first), served at GET /api/factory/stations/{station}/launch and printed by `tares factory launch-plan`, so a later runtime is one more integration (TR-867).
- The project's Pull requests page has a Merge queue tab, next to the Pull requests tab: the pull requests in line to merge by repo and base branch, each with its review, CI and guardrail lines and what it waits on. At L3-review the person a merge question is for answers it there with "Yes, merge" or "No, hold"; it is the same question as on Overview and Calls, so answering closes it everywhere. The tab is in the address (`?tab=queue`), so a reload keeps it.
- The factory setup screen has one read, GET /api/factory/setup, that returns every step with what is connected, what can be connected and each form, so the console only shows it (TR-871).
- The console has a Set up your factory screen at /setup: a step per integration in a rail, and for the chosen step the integrations that are connected, the ones that can be connected, and one form drawn from each integration's settings schema, all from GET /api/factory/setup (TR-872).
- Integrations in the menu, right under Home: what the crew works with, grouped by what it does (Code, Tickets, Review, CI, Notifications, Runtime), with the same sections as Set up your factory. GitHub, Linear, Slack and Claude Code left Settings for it; their old Settings addresses open the right section. The CI step says what happens when nothing is chosen: GitHub Actions for a project with a repo on GitHub, local checks otherwise, and connecting GitHub Actions no longer asks whether the repository has CI.

### Changed
- On Windows, `tares factory doctor` and the plugin version check now tell a person to stop the crew before they upgrade or install `tares` by hand, because an upgrade while any tares command runs can leave the install broken. macOS and Linux text is unchanged (TR-1281).
- On a project's Docs and memory page the document now fills its card instead of a narrow centred column, so a wide screen no longer leaves empty space at both sides (TR-1245).
- The factory project page grew one long column, so sections such as Pull requests were easy to scroll past (TR-1245). A project now has its own sidebar with a page for each part: Overview, Tickets, Pull requests, Milestones, Decisions and calls, Docs and memory, People and access, and Settings, each with its own address and a count. While it shows, the main menu is hidden, as on Settings, and Back returns to the page before the project. Overview keeps only what needs you: what waits for you, a line for what waits for others, cards for the pull requests, milestones and decisions, and the tickets moving now. Tickets is the Tickets screen kept to the project, Decisions and calls opens on its Calls tab, and Docs and memory picks a document from a menu you can type to search, so the document has the page's width. Settings holds the icon, the plan, where the tickets live, the repos handed over, Later and Delete, and at phone width the sidebar is a row of buttons under the name. Old links to a section (`#people`, `#secrets`, `#milestones`, `#decisions`) open its page.
- The check on the crew's JSON schemas now also fails when a name leaves an object's `required` list or a `const` changes, unless `schema_version` was raised, and it reads the old schema files from git by their full path and names a file it cannot read (TR-1303).
- With `--json`, a lifecycle command that fails after it printed lines now carries them as `notes` in the error document (TR-1309), so a program can tell what had already happened, for example which stations `crew down` stopped before it refused. The key is absent when nothing was printed.
- crew lifecycle commands under --json now also write each progress line live to stderr as "tares factory: <msg>", flushed; the result document and its notes are unchanged (TR-1344).
- `crew update` no longer waits for a station a person has open in a terminal: it says so, leaves that station to the watch, and with `--json` lists it under `left_to_watch` in the result.
- `crew status` and `crew desk` (and their `--json`) list only the open desk items addressed to you. They ask Tares for `mine=1`, so on a shared cell they no longer count other people's items (TR-1355). `crew down` withdraws only your own "stop the crew?" questions, plus every finished project's questions. A Tares that rejects the parameter is asked again without it.
- The plugin's `bin/mcp-headers` and `bin/otel-headers` shell scripts are gone (TR-1358), so the plugin no longer ships a shell script with a `#!/bin/sh` line. `.mcp.json` and the monitoring setting already start `tares hook mcp-headers` and `tares hook otel-headers`, so nothing changes for a new session. A Claude Code session opened before tares 1.56 still starts the MCP header helper by the old path and loses its crew token until it is restarted.
- The crew guide (`claude-plugin/FACTORY.md`) no longer lists `jq` and `curl` under "What you need" (TR-1363). The crew has not used them since its hooks and header helpers moved to Python, so `git` is the only tool left on the list.
- Credentials now live only at the factory. Each slot (tickets, review, ci, guardrails) can hold several connected integrations with one default, and the integrations API can connect another, make one the default, or delete one that nothing uses. A project's own choice names a connected integration and carries only plain fields: a secret or an address in a project is refused, and so is an integration that is not connected. A guardrail pair holds no integration settings and uses the connection at the factory. On the first start after the upgrade, the secrets and addresses held by a project or a guardrail pair move to the factory connection of the same integration, and guardrail pairs that were then the same become one.
- The merge queue read says where a queued pull request stands in this project ("Next in this project", "2nd in this project") instead of naming the one ahead of it, and counts only this project's pull requests. An answer given from a Claude session no longer names the person the question was for, and an answer with no words reads "Held." instead of "Held: .".
- `tares factory` now starts every station from the launch the cell gives it (TR-870). Before, the command line built the arguments, the environment and the settings itself, and the cell built a copy of them (TR-867); now the command line asks the cell, puts in the values only this machine knows (its folders, the plugin, the crew's key file) and starts the session. A station started while the cell is down uses the launch kept from its last start, and says so; a station that has never started stops with "Tares is unreachable and this station has never started; start Tares first." A name that is not a station name (a slash, a space, a leading dot or hyphen, upper case, more than 120 characters) is refused with the rule, by the command line and by the cell. A project the cell does not list is no longer replaced by a default project: the station starts without one and a line says so.
- The factory menu is grouped into Work (Projects, Tickets, Reviews), Team (Crew, People) and Knowledge (Docs and memory, and the Second Brain's pages), each page with an icon. A group folds when you click its name, and stays folded in that browser.
- A factory is set up in one place, Set up your factory (/setup), instead of the home's first-run list and a second setup screen. It walks through how your crew works, GitHub, where tickets, reviews and checks live, and the commands to type in your terminal, which tick by themselves as your machine reports and stay there to copy again later. A new cell with no project opens on it, and a card on top of the menu shows how far it is until every step is done. Each integration step asks you to confirm the default (for example Use Tares) or connect another; Memory and Guardrails are left out while they offer no choice, and Monitoring until it is tested. The home no longer shows the first-run list or the connected tools. Where tickets live is now only the Tickets step: the separate choice on the home, which the crew never read, is gone.
- Setting up a factory ends once the crew is up: the terminal step is three commands, and the Setup card leaves the menu. Planning the first project is no longer a setup step that waits on a long conversation; the home's empty Projects list shows it instead, with `tares factory spec` to copy and what it needs. `tares factory spec` in an empty folder, or in a repo with no commit yet, now offers at a terminal to make it a private GitHub repo with a first commit and push it (with `gh`), and plan there. Without a terminal to ask at, the message names the exact commands. The New project page no longer points to a Get started list on Home.

### Fixed
- On Windows, `tares factory crew status` did not adopt a live station that the crew had not recorded when the folder `claude agents --json` lists for it and the folder Tares or the crew home records were the same place spelled differently (backslash against slash, letter case, a trailing separator). Both are now compared as paths (TR-1308).
- On Windows, a live station could read as stopped when the folder `claude agents --json` lists for it and the folder the crew recorded were the same place spelled differently (backslash against slash, another letter case, a trailing separator). The watch then skipped its grants restart, and the stopped-session checks in `find_live_job` and `is_alive` called it gone. The two folders are now compared as normalised paths (TR-1305). macOS and Linux were not affected, apart from a trailing separator.
- On Windows, a new crew home could be written with no GitHub login, or with a name and login ending in a carriage return, in `crew.json`. `gh` installed as `gh.cmd` was not started (Windows starts only a `gh.exe` by bare name), and Windows commands end their output with `\r\n` where only `\n` was stripped. `gh` is now run by the full path `shutil.which` finds and the trailing `\r` is stripped too (TR-1228).
- On Windows, two crew commands or threads writing an event at the same moment could mix their lines or lose one in `events.jsonl` (TR-1229). Each event is now written under a lock, so every line stays whole and none is dropped. macOS and Linux were not affected.
- The crew's messages were listed two to four times on a ticket (each REVIEW, VERDICT, FYI). The plugin's async hooks shipped the same transcript lines side by side, and Claude Code 2.1.3xx writes `api-request-blob` lines that copy the last assistant message with no timestamp, which Tares stored as new tool calls at the moment they arrived. One shipper per transcript now holds a lock (a turn's end waits up to 5 s for it), the `api-request*` lines are not shipped and are dropped at ingest, and the message list shows each tool call once.
- A project handed over from a person's session on a named crew went to the wrong crew: `hand_over` recorded it under `crew` and said `crew-orchestrator` was not running, while the person's own crew (for example `kiran-windows-crew`) was up and never got the `[TF:SHIP]` nudge. `tares factory spec` and `session` now tell the plugin the crew's name, and a session that names no crew (a plain `claude` with the plugin) hands over to the person's one running crew.
- `tares factory doctor` now warns when git has no global `user.name` or `user.email`. On a fresh Windows machine a builder's first commit in a repo that sets none of its own stopped with "Author identity unknown", and `crew up` named the person "the user"; the warn gives the two `git config --global` commands.
- The `windows-crew` check could fail on a slow Windows runner: shard 1's parity coverage report took 1055 s of its 1200 s limit. The report now has its own limit of 1800 s (`--coverage-timeout`, twice `--timeout` by default) and a shard job may run 60 minutes, so the required check does not fail for speed alone (TR-1230).
- On Windows, the upgrade advice no longer sends a person somewhere they cannot go (TR-1304). The plugin's session-start line for an old tares now also says to stop the crew and any other running tares command before upgrading, because an upgrade while one runs can leave the install broken. `tares factory doctor` no longer says to run `tares factory crew down` when no tares is on PATH, since there is no crew to stop; it keeps the note about stopping every tares process. Nothing changes on macOS or Linux.
- A failed browser sign-in left a copy of the output channel open (TR-1306). The copy is now closed when the browser output cannot be pointed at stderr.
- crew watch --restart --only-under no longer trusts a relative program name in the watch's command line, so it cannot match a watch of another install by the caller's folder (TR-1307).
- Closing the project icon picker, or an editor on the setup plan, left keyboard focus nowhere for one frame before it went back to the control that opened it (TR-1311). Focus now goes back in the same moment the picker or editor closes.
- On a factory project's Settings page, Edit plan opened "This page does not exist" (TR-1318): it linked to an address the console does not have. It is now Read the plan and opens the project's docs, where the spec and the plan are.
- `tares factory crew watch --restart --only-under <folder>` said the watch ran from another install and left it alone when the folder was a Windows venv whose python.exe is a launcher for the base interpreter (a uv venv on Python 3.12): the watch loop runs from the base python, so only the launcher is under the folder. It now also checks the launcher the watch was started by (TR-1319, TR-1323).
- `crew watch --restart --only-under` no longer takes an unrelated process for the watch's launcher when the system reused the launcher's pid: the parent counts only if it started no later than the watch (TR-1323).
- The schema check printed "no breaking change" when it compared no schema file, for example after the schemas folder moved (TR-1334). It now fails with "no schema files compared".
- Resolving an incident now marks its notes handled before it shows the incident as resolved. Before, a reader (a Slack button check, the console) could see "resolved" with notes still unhandled for a moment.
- The crew watch's Windows launcher is no longer reported as an orphan, and doctor over SSH no longer flags a correctly placed service when no watch is running.
- A lifecycle command run with --json no longer fails when the program has no stderr (pythonw); the live progress lines are then skipped and the notes are kept.
- The crew scan no longer reports the watch's Windows launcher as a stray when the watch's start time cannot be read; it leaves the launcher alone, as the orphan rule does. The launcher guards now have tests.
- A repo that is indexing again after Retry shows "cloning" until its new run starts, not the counts of the failed run before it.
- The crew engine P3 follow-ups from the M6 review (TR-1357). A failing `crew update --json` run now still lists `left_to_watch` in its error document, and the `--json` extras no longer live in module state. `crew down` and the low-disk check read the desk for this key's person, and a key with no person says so instead of showing an empty desk. A copy of stdout is no longer leaked when stderr cannot be duplicated. The `crew down` help and the smoke script read the desk correctly. More tests cover frozen crews, attached stations and equal start times.
- `tares factory heavy` waited for an hour behind a lock folder that held no pid file, which a release that stopped halfway could leave. A lock with no pid file for more than a few seconds is now taken over, and a release removes the pid file last.
- `tares factory heavy`: when two runs found the same stale lock at once, the slower one could delete the lock the faster one had just taken, so both ran together. A stale lock is now moved aside in one step, and a lock that turns out to be live is put back.
- The Your crew panel of setup no longer shows another credential's machine report to a key with no person, a long machine name at phone width no longer wraps the "Your machine" label, and the crew settings and the machine report share one rule for who speaks for the whole cell (TR-1379).
- Incidents filed from old findings could come out wrong on a cell in a time zone that sets its clocks back (TR-1382). In the hour that happens twice, notes sorted and joined by their wall-clock reading, so two notes could swap places or land in the wrong incident. Incident times are now compared as UTC.
- Choosing a factory default or a project's integration could succeed for a connection deleted a moment earlier. The check that the integration is connected now happens in the same step as the write. The first start after the upgrade now logs each secret, address or guardrail pair setting it drops because the factory already holds a different one, by integration, project or pair and key name, never the value (TR-1383).
- Adding or changing a guardrail pair could succeed for a guardrail whose connection was deleted a moment earlier. The check that the guardrail is connected now happens in the same step as the write (TR-1384).
- The incidents listed on a service page could come in the wrong order on a cell in a time zone that sets its clocks back (TR-1386). In the hour that happens twice, a later incident could be listed below an earlier one. The list now sorts by UTC.
- On Tares Cloud, Connect GitHub and Connect Linear in setup came back to a settings page instead of the step you started from, because the link carried no address to return to. They now return to the same step. The copy buttons of commands are an icon, and a command's box is only as wide as the command.
- On a team cell, every screen now agrees on what waits for whom. A project's page showed anyone who opened it the lead's milestones to check, calls to confirm, pull requests to merge and "Ready to close" as "Waiting for you", and marked the project "Waiting on you", while their home showed none of it. Projects, Tickets and Reviews had the same gap. The cause was two rules: the home kept these for the lead, the other screens asked nobody. Now one rule decides it, with or without a licence: a ticket's work is its assignee's, else the project lead's, else the admins'; a desk question is for the person it is addressed to, else the project's owner. The home, a project's page, Projects, Tickets and Reviews all read it. What waits for someone else is under "Waiting for others", named for them ("Milestone to check for Kiran"), with nothing to press, and Close and a milestone's checks are only offered to whoever they wait for. A project with no lead, or whose lead left the cell, gives its work to the admins and says "No lead, set one"; with no admin it waits for no one rather than for everyone. Removing a person clears them as lead and assignee. A passed pull request waits for its ticket's owner at the level the merge queue merges at (the hand-over's, under the cell's ceiling), never the viewer's own setting. A batch of calls on someone's desk is no longer asked again of the lead. On a team cell the Crew page names whose crew waits, and the standup and morning status name whom each open question is for instead of saying "you".
- Connecting Linear (or GitHub Issues) with the app on Tares Cloud showed "Linear is connected" but never added it to the Tickets step, because the form's last button only went back to the step without saving it as the step's integration; the app's page also opened in a new window whose return landed on a settings page. The app's page now opens in the same window and comes back to the step, the form opens again by itself with the app done, and Save and connect adds it to Tickets.
- On Windows the crew watch restarted every station again and again, saying it "came back on Claude Code 1.57.0-..., an older spare", and `tares factory crew update` ended with "not every station runs Claude Code". The station already ran the installed Claude Code: the watch read the plugin's install folder (`.tares/engine/versions/<tares version>`) in the station's command line as the Claude Code version. The version is now read only from the path of the binary, never from the value of an option or from a prompt, and on Windows, where the install keeps one `claude.exe` and no path names the version, that file is asked for it.

## [1.57.0] - 2026-11-12

Tares now merges ready pull requests itself through a merge queue, and the crew no longer merges.
The Software Factory gets an integrations framework: Linear and GitHub Issues tickets are written
through from Tares, and the review gate and CI checks plug in per project. The crew runs on the
Python engine only, projects can hold secrets for their builders, and "Show what it did" on the
Crew page reads as a conversation.

### Added
- The Tares guardrails are now readable rules (TR-862): `GET /api/factory/guardrails/rules` lists the defaults a crew station runs under (each with its role and a reason; nothing edits or removes one, a write that names one is 403) and your own rules. A deny rule is added, changed and removed by an admin, for the factory or for one project (`POST`, `PUT` and `DELETE` on the same address); an allow rule is a grant, read in place and written with the grants. `GET ...?station=<name>&project=<id>` returns the allow and soft_deny sentences that station runs under, with the crew's production pattern filled in. `/health` lists the `guardrail_rules` capability. The crew engine still writes its own sentences; it reads these in a later ticket.
- The crew reviewer's verdict is posted as the cell's GitHub App (TR-1244). When Settings, GitHub holds an App that can write to the pull request's repo, `record_review` also posts one comment (verdict, verified, not verified, findings with the blocking ones first, and the reviewer's station and crew in text, no @-mention) and a `factory/review` check run on the reviewed head, and says whether it did. With no such App it says it did not post and the reviewer posts with `gh` as before. The review gate and the pull request read accept the verdict as a commit status or a check run, and a new head still makes it stale.
- The merge queue (TR-856): Tares merges a ready pull request itself, with no agent and no tokens. Per repo and branch it merges one at a time, in milestone then ticket order, and a ticket whose dependency has not merged waits while later ones that do not need it go ahead. At L4-ship it merges once the review gate passes and CI is green, with the project's merge method (`PUT /api/projects/{uid}/merge-method`: squash by default, merge or rebase) and deletes the branch. At L3-review it asks the person on the desk first (or an admin or the project's lead approves with `POST /api/factory/merge-queue/{id}/approve`), and a new commit voids the approval. If no GitHub credential can write to the repo it asks once for one in Settings, GitHub. It stands down while GitHub's rate limit is low, and `/health` lists the `merge_queue` capability. The L4-ship text now says Tares merges, not a builder.
- The merge queue handles pull requests that conflict or are behind (TR-858). One that conflicts goes back to its builder (stage changes) with a reason naming the files it shares with the last pull request merged into that branch. On a project whose ci integration is GitHub Actions, one that is behind is brought up to date, waits for a check on the new commit, and then merges without a new review when its changes are the same as the commit that was reviewed; different changes go back for review. A reviewed pull request whose CI fails or a guardrail blocks goes back once per commit, and a merge GitHub refuses (405), or a pull request GitHub cannot tell is mergeable for ten ticks, puts one item on the desk.
- The bounce ladder (TR-1092): the merge queue counts how often a ticket in review or changes has come back, from a review that asked for changes or blocked and from the queue's own send backs (a branch update that needs a new review, a pull request waiting on CI or on a person, and Tares's same-changes review do not count). At three it writes a `ladder` row on the ticket, and `my_work` lists the ticket under `bouncing` with its count and last reason, for the orchestrator to give the builder a hint or reassign it. At five it adds one desk item for the hand-over's person ("<ticket> has come back 5 times: <reason>. What should happen?"), not blocking. Each step is written once.
- The crew watch now cleans up Docker on the crew machine once a day (TR-700). It removes stopped containers that ended a day ago, build cache older than a day, dangling images, and images no container has used for 7 days. It never removes volumes, an image a container uses, or one on the keep list. It never runs while a heavy command holds its lock, and it starts only after the watch has noted a day of image use. Before a heavy command, less than 10 GB free cleans up first; still low, the person gets one desk item. `tares factory doctor` shows Docker's disk use and what the next cleanup removes. The crew settings `docker_cleanup` (on unless false) and `docker_keep` (image names) control it. Python watch only.
- Plugin 0.6.10: projects can hold secrets for their builders (TR-775), such as another cell's read-only key. Any member of the project adds, replaces or removes them in the new Secrets section of the project page. Members see the names, who set each and when, and when one was last used, never a value. A builder runs `tares factory with-secrets -- <command>`, which fetches them from the cell with its person's key and sets them for that one command, so nothing is written to disk and a change applies at once. `tares factory secrets` and the MCP tool `list_secrets` give the names. Values are encrypted with `TARES_SECRETS_KEY` from the cell's environment (without it, secrets are off), and Claude Code sessions sent to the cell have every known value replaced with `[secret NAME]`.
- The local checks are now the built-in ci integration for a project with no CI of its own (TR-854). It reads the checks the builder recorded on the ticket and answers success, failure, pending or none for the pull request's head commit, naming the failing ones. A required command with no check on that commit is pending, and a check the session recording shows failing or with other counts is a failure; one the recording does not show yet is pending. A project's required checks are set with `PUT /api/projects/{uid}/required-checks` or the `set_required_checks` tool, by an admin or the project's lead; with none set, the project requires no checks.
- Settings, Crew, Morning status: the cell can send the standup each weekday morning to Slack, by email on Tares Cloud, or both, with a Send one now button (TR-825). Off until turned on.
- A software factory project can have an optional emoji icon, picked from a search-and-browse picker on the project page, shown beside its name on Projects, Home, Crew and its sub-pages. It saves at once and is never part of the project's name.
- The Software Factory now has an integrations framework (TR-838 to TR-843): seven slots (tickets, review, ci, memory, monitoring, guardrails, runtime), each filled by an integration. `GET /api/factory/integrations` lists them with their defaults, an admin can choose one for the factory, and a project can choose its own tickets, review and ci. Stored secrets never come back on a read. `/health` now lists the cell's capabilities, and `tares factory crew up` stops with an upgrade message when the cell does not have what it needs. Nothing changes for a running crew yet: the integrations themselves arrive in later milestones.
- Tares tickets are now the built-in tickets integration (TR-844): the integrations list shows Tares as installed, and creating, updating and changing the status of a Tares ticket or milestone can go through it. Tickets stay where they were; nothing changes for a project that keeps its tickets in Tares.
- Linear is now a tickets integration (TR-845): Tares syncs a Linear project's issues and can move an issue to to do, in progress, in review or done itself, for a team with or without an "In Review" state. A move Linear refuses is kept and tried again each time the integration syncs, the ticket's status in Tares is not overwritten meanwhile, a move you make in Linear yourself ends the wait, and the fifth failure puts an item on the desk. Nothing in Tares calls the integration yet.
- GitHub Issues is now a tickets integration (TR-846): Tares syncs a project's issues as before and can now write to GitHub itself with the cell's GitHub credential: it closes, reopens and labels an issue for the status you set, creates milestones and issues (with their "blocked by" links, once even when asked twice) and changes an issue's title, body or milestone. A move GitHub refuses is kept and tried again each time the integration syncs, the ticket's status in Tares is not overwritten meanwhile, and the fifth failure puts an item on the desk. A GitHub rate limit is waited out. Nothing in Tares calls the integration yet.
- Linear tickets integration (TR-847): Tares can now create and update issues, milestones and dependencies in a Linear project itself. It looks for the issue or milestone by its exact title or name first, so asking twice, or retrying after a failure, never makes a second copy and only adds what is missing (a "blocks" relation from each dependency, a milestone, a title or an order change). Every call to Linear now waits out Linear's rate limit (a 429 or a RATELIMITED answer, up to a minute each time, three tries) and long lists are read page by page. Nothing in Tares calls the integration yet.
- Moving a factory ticket to a stage now changes its status in whichever system holds the ticket (TR-848): to do, in progress, in review (a Tares ticket counts as in progress) or done, through the project's tickets integration, and the same when GitHub shows its pull request merged, including a pull request into a milestone branch. A write the system refuses or is slow to answer is kept and tried again by the integration's sync, never fails the stage change, and meanwhile a sync leaves the ticket's stage and status alone. A Tares ticket in a Tares project changes as before.
- Changing a ticket's title, order or milestone in a Linear or GitHub project now reaches Linear or GitHub (TR-849): the ticket and milestone routes call the project's tickets integration instead of refusing, and the change shows in Tares at once. A change Linear or GitHub is slow to answer is finished in the background and the route answers with `edit_pending`. Status, assignee and dependencies of such a ticket are still changed in Linear or GitHub, and the answer says so. GitHub has no order, so its tickets keep refusing a position change. A project that keeps its tickets in Tares is unchanged, except that an unknown field on an update is now refused instead of dropped, and a ticket or milestone made in a Linear or GitHub project is refused by the integration as the routes already did.
- The spec session now creates its tickets and milestones in Linear or GitHub Issues through `write_ticket` and `write_milestone` (TR-851): with a project linked, one set of calls makes the same milestones and tickets, with their dependencies, in either, instead of the session running `gh` or Linear's own tools. Writing the same title again finds the issue instead of making a second one, and the next sync leaves one Tares row for it. A new ticket's status, assignee and position are refused (they live in Linear or GitHub). `link_tickets` replaces `link_linear_project` and `link_github_issues`.
- The Tares reviewer is now the built-in review gate (TR-852): the review slot has a `tares` integration that answers `approved`, `changes`, `pending`, `stale` or `none` for a pull request's current commit from the ticket's review rounds, falling back to the `factory/review` status on the PR when the ticket has no round. A review of the current commit wins over a disagreeing status, and a review of an older commit says so. Nothing asks it yet: the merge decision comes in a later ticket.
- GitHub Actions is now the built-in ci integration for a project with a GitHub repository (TR-853). It reads the checks on the pull request's head commit and answers success, failure, pending or none, naming the failing checks. A commit with no checks yet is pending when the repository has workflow files and none when it has none, and a project can tick "This repository has no CI" to always answer none. The reviewer's own `factory/review` status is never counted as a check.
- One answer to "can this pull request merge?" (TR-855): `GET /api/factory/merge-ready?pr=<url or owner/repo#n>` and the `merge_ready` tool combine the review gate, the ci integration and the guardrails for the pull request's current commit, and list every reason it is not ready. A closed, merged or draft pull request, a pull request with no linked ticket, a review that is not an approval of this commit, a failing or pending check and a blocking guardrail each say so. CI that answers none counts as ready; a guardrail that only warns never blocks. No guardrail integration exists yet, so none is asked.
- A project whose review gate is an outside tool no longer waits for the crew reviewer (TR-857). Its pull requests are left out of the reviewer's queue (`review_queue`) and the Reviews and Home pages say "Waiting for <tool>" instead of "The reviewer reviews it". Tares reads the tool's verdict on the pull request's current commit each time it reads the pull request: an approval is kept as a pass for that commit and the ticket stays in review, a request for changes is kept with its findings and moves the ticket to changes, once for each commit. The builder skill sends the reviewer a `REVIEW` only when the project's gate is the Tares reviewer. Nothing changes for a project that uses the Tares reviewer.

### Changed
- The crew no longer merges pull requests (TR-860). A crew station's settings deny `gh pr merge`, the autonomy text says Tares merges (after the review gate and green CI at L4-ship, after the person's yes on the desk at L3-review), and starting the crew stops with the upgrade message when the cell does not list the `merge_queue` capability. A builder now reads its ticket's stage and reason each tick: it rebases on a conflict, fixes a failing CI run, asks the orchestrator when a guardrail blocks the merge, and runs its checks again when `merge_ready` says they are for an older commit. `crew status` and `crew down` work against any cell. A person's own session still merges as before.
- "Show what it did" on the Crew page reads as a conversation (TR-1177). Messages sent to the member are on the right, named by who sent them (another member, a person, or the factory itself, shown quietly); what it said is on the left. Each tool call is a card with its own result, matched by call: an icon for its kind, a one-line summary, how long it took and when, with the input and output folded away and a Copy button for each. A failed call is marked red. Three or more calls in a row fold into one card, and session events (started, memory compacted) are dividers. It opens at the newest and follows new lines unless you scroll up, Show earlier reads further back, and a long output loads whole when its card opens. `GET /api/crew/{name}?conversation=N` gives it, and `GET /api/crew/{name}/tool/{id}` one call's whole output.
- The builder and orchestrator station skills no longer tell a station to move a ticket's status in Linear or GitHub itself (TR-850): `set_stage` is the only way, and Tares writes it through to the ticket system. `tests/test_skills_ticket_moves.py` fails if a station skill says otherwise.
- Linking a project to Linear or GitHub Issues is now one endpoint, `PUT /api/projects/{uid}/tickets/link` with `{"integration": "linear" | "github", "link_fields": {...}}` (TR-849): `project` for Linear, `repo` and `issue_label` for GitHub. The body takes no key or token, the cell's own credential is used, and one that carries one is refused with a 400. `POST /api/projects/{uid}/linear` and `POST /api/projects/{uid}/github` are gone, with no alias; the console and the `link_tickets` tool use the new route. Unlinking (`DELETE`) and the sync routes are unchanged.
- `uv tool install git+https://github.com/glassflow/tares` and `pip install .` from a fresh clone now work without a built console (TR-1170). The wheel build no longer fails when `ui/dist` is missing: it says so in one line and builds a wheel without `tares/console`, and `tares up` on such an install says how to get the console. A release build still fails without it (`TARES_REQUIRE_CONSOLE=1`, set in release.yml). With `ui/dist` built the wheel is unchanged.
- The `--json` output of four crew commands is now one contract document with `schema_version` first, and each has a schema in `tares/crew/schemas/` (TR-973). `tares factory doctor --json` was a bare list of checks and is now `{"schema_version": 1, "ok": ..., "checks": [...]}`; it still exits 1 when a check is FIX, and the document is printed either way. `tares factory projects --json` was a bare list and is now `{"schema_version": 1, "projects": [...]}`. `tares factory crew setup --questions` was a bare list and is now `{"schema_version": 1, "questions": [...]}`. `tares factory crew setup --answers <file> --json` now carries `schema_version`, and with answers that do not validate it prints the error document (exit 1) instead of lines on stderr; a usage mistake with `--json` is the error document with code `usage` and exit 2. Scripts that read these outputs as a list need to read the `checks`, `projects` or `questions` key instead. Without `--json` nothing changes.
- `tares factory` now runs on the Python crew engine by default on Mac and Linux (TR-1069). The bash script went away in TR-975, see below. Stations are now allowed `tares factory ...`, and their settings carry the path of `tares` in `FACTORY_TARES`, which nothing reads yet. A station that started before you pulled this keeps its old permissions, so restart the stations right after the pull, or each asks for approval the first time it runs `tares factory ...`.
- The Claude Code plugin starts every hook, the status line and the MCP header helper with `tares hook <name>` (TR-1103), and `min_tares.txt` is now 1.56.0: the plugin needs tares 1.56.0 or newer on PATH. The shell helpers `hooks/guard.sh`, `hooks/session-start.sh` and `bin/mcp-headers` are gone (TR-1131 brought them back as shims). With an older tares the hooks do nothing (a usage error is not allowed to block a tool call or loop the Stop hook) and the session start says to upgrade.
- The bash crew is removed (TR-975). `tares factory` runs on the Python engine only, so `TARES_FACTORY_ENGINE`, `"engine"` in `~/factory/crew.json` and `TARES_FACTORY_WATCH` no longer do anything, and `claude-plugin/bin/factory` and `bin/factory-heavy` are gone. The heavy-command wrapper is now `tares factory heavy <timeout-seconds> <command...>`; the guard accepts that form and refuses a bare `factory-heavy`. The three hook shims from TR-1131 stay one more release. If you kept `TARES_FACTORY_ENGINE=bash` as a way back, use the previous release instead.

### Fixed
- The daemon starts on Windows again (TR-862): the Tares guardrails import pulled in the crew setup box, which loaded `termios` when the daemon started, and Windows has no `termios`. The box loads it only when it is shown.
- Settings, Linear no longer says Tares only reads from Linear (TR-1243). Tares also writes: it moves ticket statuses and creates and reorders tickets. When Linear refuses a write because the connection can only read ("Invalid scope"), Settings says so and what to do: connect Linear again on Tares Cloud, or use a key with write access. Saving a new connection clears it, and `GET /api/linear` carries it as `write_error`.
- On a team cell, a project page no longer shows a question or permission request addressed to someone else as "Question for you" with an Answer button the cell would refuse. It is under a new "Waiting for others" section, named for the person it is for ("Question for Adam"), with nothing to press, and it no longer makes the project "Waiting on you" (TR-1223 follow-up).
- A cell that gets its licence no longer splits each person's crew in two. Without a licence the cell keeps no machine for a crew; the first session after the licence names the machine and used to start a second crew, so the Crew page showed the same person twice, one of them ended. Now that session joins the crew it belongs to, the same holds the other way if the licence goes, and a cell that already split merges the two when it starts (only when the person has one crew of that name on a machine). On the Crew page a crew's own name shows next to its person, a crew without a machine says so, and crews whose members all ended over a day ago sit behind "Show N ended crews" (TR-1223).
- The Crew page shows every crew on the cell, each under the person it belongs to, yours first (TR-1223). Before, on a cell with several people it took the first orchestrator and reviewer of any crew, since crews repeat station names, so a running crew could show as "Not running" and builders of different crews were mixed. Each crew now has its own state line and members, the machine is named when a person has more than one crew, and "Show what it did" opens that crew's station. Each person's section folds, showing whether the crew runs and how many builders are on it; yours starts open, the others folded, and the page remembers your folds in this browser. A cell with one crew looks as before.
- A rule the crew proposes now says where accepting it writes it ("For shop", or "For every project" when it goes into the shared AGENTS.md) and the tickets it came back in. A rule belongs to no crew: the cell offers it when a review finding keeps coming back, whoever's reviewer found it (TR-1223).
- A builder that had finished never got a permission allowed for it. The watch now restarts a finished builder when a new grant names it, leaves the others asleep with one `regrant-skipped` line in the event log, and Allow on Home offers the grant in the station's name (TR-645).
- Plugin 0.6.11: allow in the console now works for a command only one role may run (TR-644). The guard honours a grant for that exact command for that station. Everything else stays with the role that owns it.
- On a team cell's Home, your builder shows what it is working on even when the ticket is assigned to a teammate (TR-890).
- Plugin 0.6.8: a challenger review that did not finish is never recorded as a pass (TR-530). Any Codex failure now shows as an error with its real reason, instead of "sandbox probably failed". When Codex is out of quota or signed out, the crew keeps working and one item on your desk says reviews have stopped.
- A Linear-linked project no longer has its tickets canceled when Linear answers with nothing, or when the project has more than 1000 issues. The sync now stops, shows the reason on the project's Linear link and leaves every ticket as it was (TR-1024).
- A plugin update no longer turns the guard off in Claude Code sessions that were already open (TR-1131). Those sessions kept running the old hook commands, `bash .../hooks/guard.sh` and the others, which TR-1103 had deleted, so every call failed with exit 127 and Claude Code let the tool call through unguarded. `hooks/guard.sh`, `hooks/session-start.sh` and `bin/mcp-headers` are back as one-line shims that run `tares hook`; they go one release later (TR-975).
- `tares factory doctor --json` gives every check its own id (TR-1130): a project with several repos got the same `project_<name>_trust`, `_github` and `lsp_<project>_<language>` ids for each repo, so the desktop app could not tell them apart. The ids now carry the repo's folder name, for example `project_shop_api_trust`. Spawning a builder for a project whose repo list holds plain strings now ends with the usual "is not one of the project's repos" refusal instead of a Python traceback.
- Allow on a crew permission request on a project page now opens the Allow dialog and keeps the grant there, instead of sending you to Home with nothing allowed. If the cell refuses, the dialog says why, and you stay on the list to allow the next one.
- On the factory Home page, a long answer option on a question in "Waiting for you" now wraps inside its card instead of running past the right edge.
- On a factory project page, a milestone check that is a shell command now shows in the console's monospace font and wraps inside its row, so long commands are easy to read. Checks written as sentences look the same as before (TR-1082).
- On the factory Crew page, each project now lists its running workers first, hides finished ones behind "Show N finished", and its count reads running and total, for example "2 running · 19".
- Plugin 0.6.7: a station restarted in the same conversation keeps showing up on the Crew page. The restart gives it a new transcript that starts with the whole conversation, and when the first request for that was too big to be accepted or answered in time, every later hook retried it and nothing else from the station was recorded, so it looked quiet for hours while it worked (TR-1102).
- On a team cell, the factory Home no longer shows someone else's Claude plan as "Your Claude plan": it now says whose plan it is, or just "Claude plan" when the cell cannot tell.

## [1.56.0] - 2026-11-10

A cell now opens as a Software Factory or an AI SRE, each with its own screens, and working as a team
on one cell moves to an enterprise licence (the code is in `ee/`). This release also brings a project
GitHub page, delete on the project page, a new factory Settings page, and one team list on Tares Cloud.

### Added
- Delete a project from its page: the last section of a factory project page deletes it, after you type its name. Its tickets, decisions, calls and docs are removed; pull requests and branches on GitHub stay.
- The crew engine ships inside the `tares` package everywhere it is installed (TR-1064): `psutil` and `keyring` are now installed with it, `tares/crew/schemas/` is in the wheel, and the Claude Code plugin names the oldest tares it works with (`min_tares.txt`). `tares hook compat` prints one line when the installed tares is older than that, and the plugin's session start shows it. A `tares` on PATH without the command, or any failure, shows nothing.
- `tares login` can keep its key in the OS keychain (the macOS Keychain, Windows Credential Manager or the Linux Secret Service) instead of `~/.tares/login.json`: set `TARES_KEYRING=on`. It is off by default, because the plugin's MCP header helper, `ship.py` and the bash doctor still read the key from the file. Tares now depends on `keyring`. Login says where the key went; with the keychain off it says "because the OS keychain is off (set TARES_KEYRING=on to use it)". When the key is in the keychain and cannot be read (the keychain is off, locked or denied), `tares logout` and a new `tares login` now stop and keep the saved login, instead of forgetting a key that is still live on the workspace. A login that cannot be saved removes the key it just made. The crew engine's own key file is restricted by the user's security identifier on Windows, and its calls to Tares stop after 15 seconds in total, as curl did.
- A GitHub page for each Software Factory project lists every pull request its tickets opened, grouped by repo: open, merged or closed without merging, with the ticket, CI, the crew's review, the size of the change and who merged it. The project page shows the three newest pull requests and links to it. Tares now keeps a pull request's size, author and who merged it from the GitHub read it already makes.
- The cell reads its enterprise licence from `TARES_LICENSE` or a key saved through `PUT /api/licence` (TR-912): `GET /api/licence` shows the organisation, seats, expiry and whether the key is valid, never the key itself. The desk warns 30 and 7 days before it ends, when it ends and when there are more people than seats. `/metrics`, which is public unless `TARES_METRICS_TOKEN` is set, now shows `tares_licence_valid`, `tares_licence_seats`, `tares_licence_used` and `tares_licence_days_left`, so seat counts and days left are visible there.
- A person's picture (TR-1016): where the console shows a person's initials (the account button, People rows) it shows their picture, round, and the initials when there is none or it does not load. On Tares Cloud it is the picture they sign in with, sent by the control plane when they open the cell. On a self-hosted cell a person sets their own on the People page by its address. People and whoami carry `picture`; only one https address of at most 2048 characters is taken, and the cell never fetches it.
- A People page in the Software Factory menu: who is on the cell, whether they have signed in, the projects they lead and the repos they own, and Add a person. On a self-hosted cell an admin adds them on the cell; on Tares Cloud the workspace owner invites them from the console, with the same invite the workspace page sends. Settings links to it.
- A first-run edition notice now also shows when the cell keeps its state in Postgres, and the edition chooser tells you when your choice was saved but the cell did not answer afterwards.
- The AI SRE home shows the demo service as soon as "Try the demo service" finishes, and a failed demo start links to the step-by-step setup.
- The AI SRE home shows a line after you hide the Start list, with a button to show it again.
- CI now runs the console's browser end-to-end suites on every pull request.
- On a team cell, the home's Your crew card shows your own crew's stations only.
- The team home's side column shows your crew's limit in words, the team's other crews, and the repos you own.
- The team home's Projects section has a Mine and Team switch, locks on private projects, who leads each one, and a line for private projects you are not on.
- The team home lists the projects you lead that wait on other people, and lets you hand one over anyway with a reason.
- On a team's console, Waiting for you now lets you answer, sign off, agree to, decline and allow from the page.
- The home of a team cell lists what is waiting for you with who each item is from.
- `GET /api/home` on a cell with more than one person adds the projects with lead, members, scope and a Mine and Team split (`?projects=mine|team`; a private project the person is not on shows only its lead's name), their crew then the team's crews (crews silent for a day named apart), and how far a crew may go. A project everyone has signed off on no longer counts as one the lead waits on, and a private project's desk items no longer count in the crew's open questions.
- `GET /api/home` on a cell with more than one person adds "you are waiting on" for the projects the person leads (which repo owner has not signed off, which repo has no owner, which question is open, with a "Hand over anyway" action) and the repos they own, and its `summary` says who they are waiting on. A private project the person is not on no longer counts in the home. A cell of one person returns what it did before.
- `GET /api/home` on a cell with more than one person answers for the person of the key: what is addressed to them (a spec question, a sign-off, a grant to agree to, a desk item, a milestone to check, pull requests to review), each with who it comes from, and a `summary` sentence. A cell of one person returns what it did before.
- People can answer a question, agree to or decline a grant and sign off from the console, and the console marks answers given there.
- Start checklist: if you hide it, the Home keeps a "Show it again" button so you can bring it back.
- Console: the AI SRE home shows a Get started checklist (model, connected logs or metrics, services, watching, Slack channel, and a test incident once the demo is running) with a Try the demo service button; Hide this list keeps it away for you.
- A fresh Tares now asks which one it is for, Software Factory or AI SRE: in the terminal on `tares up`, or in the console on first open (Docker, no terminal). `--edition` and `TARES_EDITION` skip the question, and an install that already has data stays Platform and is never asked.
- Console: a new factory cell shows a "Get your factory going" list on its home: sign in from Claude Code, connect GitHub, connect Linear or GitHub Issues, start the crew, write a first spec. Each row ticks from the cell's own state, and the list goes when all five are done or when you hide it, in any browser.
- Console: on an AI SRE cell, Settings (`/settings`) holds what is watched and how often, the model, budget and when it starts again, where notes go, the tools, the people and keys, and the edition. `GET /api/sre/settings` serves it in one call and `PUT /api/sre/settings/{group}` saves a group, and a failed save says which part was saved and which was not.
- Console: on an AI SRE cell, Runbooks and memory (`/runbooks`) shows for each service what the investigator should know: lines people add and remove from the page, each with who and when, and the agents' own notes below. The investigator reads a service's lines on its next check. `GET /api/sre/runbooks` serves it in one call; `POST /api/sre/runbooks/{service}/lines` and `DELETE /api/sre/runbooks/{service}/lines/{index}` change it.
- `GET /api/sre/timeline` answers the AI SRE Timeline page: what happened to the services, newest first, as windows of 10 minutes (an hour past two days) with the normal ones folded into one line, the deploys of linked repos, alerts, checks, incidents and metric steps, 200 items a page with `before` to page on. `GET /api/sre/timeline/raw` lists the events behind a window.
- Console: on an AI SRE cell, `GET /api/sre/services` lists every service as one row (state, what it is, how it is watched, where its data comes from, a 24 hour spark), with `?hidden=1` to include hidden ones; `GET /api/sre/services/{name}` answers a service page with a traffic and error chart (1h, 24h or 7d) marked with deploys from its linked repos, its incidents, what the watcher raised and its blind spots; `PUT /api/sre/services/{name}/settings` renames, hides, merges or links repos for a service. The console has a Services page and a page per service.
- Console: on an AI SRE cell, `GET /api/sre/incidents` lists the incidents by state (open, someone on it, the 20 latest resolved) with who has each and for how long, `GET /api/sre/incidents/{id}` answers one incident's whole page (the note, the evidence, the errors chart with deploys, the Slack message with its link, who was on it and "Was this worth raising?"), and `POST /api/sre/incidents/{id}/{action}` takes it, resolves it, reopens it or answers worth. `incidents` keeps the link Slack gives for its message.
- Console: `GET /api/factory/projects/{id}` answers a factory project's whole page in one read: where it stands, what waits for you on it, how many tickets are in each state, the few moving now, the next ticket, milestones as compact rows with their counts and a link to their tickets, decisions, open calls, documents, the command that opens a builder's session and where the tickets live. The page is at `/projects/<id>` on a factory cell.
- Console: on a factory cell, Settings (`/settings`) gathers the tools the crew uses, how far it may go, how many builders work at once, second check and pacing, what you allowed, model and budget, access keys, the edition and the way to every other setting, and changes each from the page. `GET /api/factory/settings` serves it in one call.
- Console: a factory cell has a Reviews page at `/reviews` with every pull request in review, across projects: the ones that wait for you to merge and, folded, the ones with the crew, oldest first, with the reviewer's verdict, CI and the milestone each holds up. `GET /api/factory/reviews` answers it, and Home's "PRs to review" and the page now share one rule for what waits for you.
- Console: on a factory cell, Crew (`/crew`) shows who is on the crew by role, what each is doing and when it was last heard, builders grouped by project, how far the crew may go, the Claude plan with each project's share, the rules the reviewer proposes (accept or reject) and the standup. `GET /api/factory/crew` serves it.
- Console: a factory cell has a home page at `/`: a greeting, what waits for you, your projects with their progress, the crew and its memory, your Claude plan use and the connected tools. It shows sample data behind the sample flag, and live data otherwise.
- Console: the factory home now shows live data, refreshed every 15 seconds, and its buttons work: Copy, Keep the crew's calls, Allow a permission, and Close a finished project. The factory Start address goes to the home page.
- MCP: on a Software Factory cell an agent sees outcome tools first (`what_needs_me`, `how_is_project`, `start_project`, `hand_project_to_crew`) and the building-block tools (connections, raw data, rules) only when an admin turns them on under Settings, Access and API keys, Agent tools; an AI SRE cell keeps `read`. `what_needs_me` and `how_is_project` show on a factory cell only. Crew stations see every tool. The list is a tidy view, not a permission boundary: the daemon checks every call with the caller's key. `GET/PUT /api/settings/mcp` and `mcp_object_tools` on `/api/whoami`.
- Console: on a factory cell, Docs and memory (`/docs`) shows the crew's shared rules and memory and each project's spec, plan, rules and memory, with who wrote each memory line and when, and adds a line from the page. `GET /api/factory/docs` and `GET /api/factory/docs/{id}` serve it; the daemon's own API reference moves from `/docs` to `/api/reference`.
- Console: on a factory cell, Projects (`/projects`) lists every project as one row with its goal, state, current milestone, tickets merged and blocked, repos and last activity, filtered by state, and closes a project whose tickets are all merged. `GET /api/factory/projects` serves it in one call.
- Console: on factory and AI SRE cells, Settings, Advanced (`/settings/advanced`) lists Connections, Explore, All resources, Connect and Reads, and one route table holds every page of both editions with a fallback until its page exists. Old addresses keep working; platform cells are unchanged.
- `GET /api/home` on an AI SRE cell returns the home page in one call: a headline, whether and why nobody is watching, the open incidents with their chart, the services with one state each, who is on watch and the blind spots. `POST /api/home/actions/raise_budget` adds to a spent watcher's budget and turns it back on.
- Console: the factory and AI SRE editions get their own frame, a header with search and an account menu, and a left menu that lists only the pages that exist. The platform edition keeps the console as it was.
- Incident notes belong to incidents that someone can take and resolve (`/api/incidents`).
- `GET /api/services` lists the services a cell receives data for.
- `PATCH /api/services/{name}` renames, hides or merges a service, and `PUT /api/settings/service-labels` says which label names the services in each source. The computed list is kept for 30 seconds and every change drops it.
- Each cell has an edition: factory, sre or platform. Existing cells stay as they are unless `TARES_EDITION` is set when they first start with no edition stored.
- Findings carry a title, what is failing, the likely cause and an outcome. `GET /api/findings` lists them across projects, and a project's results take `show=worth` to leave out the quiet checks.
- `GET /api/projects` and a project's summary say `ready_to_close` for a factory project that is still open with every ticket merged or otherwise finished.
- `GET /api/home` on a factory cell returns the home page in one call: a headline, what waits for you, the open projects, the crew, the Claude plan and the connected tools.
- On a factory cell, what waits for you on `GET /api/home` now lists the pull requests the reviewer passed, projects ready to hand to the crew, calls the crew made without you, questions and permission requests on the desk, and milestones whose tickets all merged, oldest first.
- Console: shadcn/ui primitives on Radix, themed with the Tares tokens, with a /dev/components gallery (part 1 of the component library).
- A service's event counts over time, all events or only errors, in buckets (`SreStore.event_series`, and `event_series_many` for every service in one query), and one rule for what counts as an error (`sre.is_error`). Nothing in the console reads them yet.
- `GET /api/blind-spots` lists silent services, services seen only through another, deploys not tied to a service and rules that match nothing. `PATCH /api/services/{name}` takes `repos`, and `GET /api/services/{name}/deploys` and `GET /api/repos` follow.
- AI SRE cells tell Slack when nobody is watching a service, and when watching starts again. `GET /api/watch` says who watches what and why a watcher stopped.
- "Was this worth raising?": a person answers yes or no on what a watcher raised (`PUT /api/findings/{id}/worth`, `DELETE` takes it back), results carry the counts and your own answer, `GET /api/watchers/hit-rate` gives each watcher's hit rate, `GET /api/feedback/export` exports the answers with the firing id, and an AI SRE watcher's prompt lists its recent "not worth it" cases.
- Console: `GET /api/factory/tickets` lists the tickets of every factory project, one page at a time: filter by project, milestone, state (or the three that move now) and text, sort by newest, oldest or ref, with the count of each state, the projects and milestones to choose from and how many canceled tickets are hidden.
- Console: on a factory cell, a project ready to close sorts after what holds the crew up and no longer sets the headline's age, its row says "Ready for 3 days", the Close dialog and the Keep and Close toasts use the Projects page's words, and an Allow for a crew station never offers to limit the grant to one project (`may_limit` on the Allow action).
- Console: a Tickets place in the factory sidebar lists every ticket of every project as a Board, a List or Swimlanes, with filters kept in the address.
- Console: the factory edition has a page for each ticket: its reviews and findings, checks, calls and decisions, history and working notes, what it depends on, its pull request, and the command that opens its builder's session.
- On AI SRE cells the Slack message of a finding has buttons: "I'm on it", Resolve and Reopen on the incident, and "Worth raising" / "Not worth it" on what a watcher raised. A click changes the incident or records the answer, replaces the message and says who did it in its thread, and a change made in the console shows in Slack too. Works on self-hosted cells now, and on Tares Cloud after TR-763.
- Console: on AI SRE cells the home page (`/`) is now the AI SRE home: why the watcher is off when it is, open incidents with their next step, what was raised and waits for a verdict, the services on watch and the blind spots, read from `GET /api/home` and refreshed every 30 seconds. Start now opens the home page.
- MCP: on an AI SRE cell an agent sees outcome tools first (`what_is_broken`, `how_is_service`, `what_happened`, `mark_incident`) and `read` keeps working. `mark_incident` follows the console's rules: a change the incident's state does not allow is refused in words, and `what_is_broken` shows the new state at once. A read-only key is refused with the daemon's own sentence. Crew stations see every tool.
- The spec skill brings in the area owners. It reads `list_areas`, asks the owner of a repo with
  `ask_area` instead of guessing, folds the answers into the spec, gives every ticket its `repo`
  (now also in a single-repo project, so the sign-off check sees it) and assigns it to that repo's
  owner in Tares, Linear or GitHub, and stops before the hand-over while sign-offs are missing.
  `tares factory spec --project <name>` run by someone who is not that project's lead opens a
  contributor session: it asks them the open questions, shows the tickets in their repos and signs
  off on their yes. It needs no git repo in the folder. That session is marked `TARES_SESSION_KIND=contributor`,
  so its start-up reminder points at the Contributor section and never says to build. The lead's
  `tares factory spec` is unchanged.
- The software-factory project page shows the questions put to area owners (open ones first, then
  the answered ones with who answered) and the sign-off of each repo with tickets: its area owner and
  "Signed off" or "Waiting", with a link to Settings, Areas for a repo nobody owns. A hand-over the
  lead pushed through without every sign-off is listed with its reason, who and when. Every override
  is kept, not only the latest: the project timeline carries each one once, on the page whose time
  window holds it, and an override stays attributed to the lead who made it when someone else hands
  over again later. The page is read-only; answering and signing off happen in Claude sessions.
- Questions to an area owner while a spec is written, and their sign-off before the hand-over.
  `ask_area(repo, text)` puts a question to the person who owns that repo, numbered Q1, Q2 in the
  project, and it shows in their `my_work` under `spec_questions` until they answer; a repo nobody owns
  is refused and says to set an owner. `answer_question(id, answer)` works only for the person the
  question is for. `list_questions(state)` lists them, and `sign_off(repo)` works only for that repo's
  area owner. The routes are under `/api/projects/{id}/questions` and `/signoffs`. `hand_over` now
  waits: every repo with tickets needs an area owner and, unless the owner is the project's lead, that
  owner's sign-off. If something is missing it is refused and lists "repo: area owner (not signed
  off)". The project's lead can hand over anyway with `override_reason`; the reason is kept on the
  hand-over and shown on the project timeline. A cell with only the person `local` is not held up.
- A desk item is for one person. It is addressed to the assignee of the ticket it names, else the
  project's lead, else whoever wrote it, or to the person you name in `to` (an id or an email), and it
  shows in that person's `my_work`; `desk_list` takes `mine`. Only words found in that person's own
  recorded sessions, at whichever station, answer it. If the words are not there yet you get a
  retryable error, because recordings can take a minute to arrive, and nothing is recorded until they
  are. A standing answer becomes a grant given by that person: a project grant, or with `grant_kind`
  personal one for their own machine. A project grant over repos other people own waits for them, as
  grants do elsewhere. Lines Claude Code writes that the person did not type (messages from other
  sessions, scheduled prompts, task notifications, compaction summaries) never answer an item. Items
  from before keep the older rule, but a standing answer on one is now checked like any grant, so a
  project key with no person is refused a project grant.
- My work for an orchestrator: `GET /api/crew/my-work` (and the `my_work` tool) returns what belongs to
  the person of the key: their ready tickets, the ones a station is working on, the milestones of
  projects they lead that are ready to check, and empty `desk` and `spec_questions` lists. A key with
  no person is refused; the root token and a cell of one person see the work of `local`. A hand-over
  now records who handed the project over and to which crew (`handed_by_person`, `crew`), and the
  `hand_over` tool names `<crew>-orchestrator` of your own crew, never someone else's running one.
  `hand_over` is refused for a key with no person, and `crew_projects` shows a person only the
  projects they lead or hold a ticket in, so the `factory` bash CLI, which reads that list, shows a person
  only their own projects too.
  A ticket with no assignee counts as its project lead's; only a project with no lead leaves it to
  `local`, so on a team cell the lead sees it in their `my_work` and `local` no longer does.
- Crews on the cell: it keeps a record of each crew (the person, the crew's name and the machine it
  runs on) and lists stations by crew, so two crews with the same station names never merge. The
  plugin stamps the machine (`hostname -s`, or `FACTORY_MACHINE`) next to the crew name, and the
  crew is made from the first station line of a recording with the person of the key that shipped
  it; a recording with no person joins the crew of `local`, and sessions from before crews join it
  when the cell starts. `GET /api/crew` returns `crews` (each with its person, machine, last seen
  and stations) next to the flat `stations` it had, and `?mine=1` keeps your own crews.
  `GET /api/crew/{name}` takes `crew` (an id or a name) and, when several crews have a station of
  that name, returns your own. `list_crew` names each station's crew, machine and person and takes
  `mine`.
- Ticket assignees: every ticket has an assignee person, or shows as unowned with the assignee as
  its source named them, wherever it lives. The Linear sync maps the assignee's email (a person's
  main or extra one), the GitHub Issues sync maps the first assignee's login, and a Tares ticket
  takes `assignee` (a person id or an email) on `POST` and `PUT /api/projects/{id}/tickets` and in
  `write_ticket`. Tickets show `assignee` ({id, name} or null) and `assignee_raw`; `list_tickets`
  and `get_ticket` add `assignee_note`. Assigning a person to a ticket of a private project adds
  them to it as a member (reason `ticket`); unassigning removes nobody. Changing the assignee does
  not touch who holds the ticket.
- A crew has a name (`crew_name` in crew.json, or `FACTORY_CREW_NAME`; default `crew`), and every
  station name and message target comes from it: `<name>-orchestrator`, `<name>-reviewer`,
  `<name>-releaser`, and `<name>-<project>-build-<slug>` for builders. That tells two crews on one
  machine, or one person's crews on two machines, apart. `factory crew up` asks for the name when
  the cell has more than one person. Stations get `FACTORY_CREW` and `FACTORY_ORCHESTRATOR`,
  transcripts carry a `crew` label, and `factory roster` prints the crew name and warns when the
  orchestrator session title does not match. The default crew keeps its names.
- Grants by kind: a grant is a row with a kind (`team`, `project` or `personal`), its words, who gave
  it and when. A team grant needs the admin scope; a project grant comes from the project's lead,
  from the owner of the repos it names, or from an admin; a personal grant is for the person on the
  key and only they see it. A project grant that names a repo (`areas`) owned by someone else waits
  until that owner consents, then takes effect. A project grant from someone who is neither the
  project's lead nor an admin also waits for the lead (for an admin when the project has no lead).
  Whoever a grant waits for can consent to it on a private project they are not in, and finds it in
  `waiting_for_me`. `POST /api/grants` adds one, `GET /api/grants`
  lists the team, project and your own personal grants with `state`, `consents`, `waiting_for` and
  `in_effect`, `POST /api/grants/{id}/consent` and `.../revoke` answer and end one. The MCP tools
  `add_grant` (new `kind` and `areas`), `read_grants` and `consent_grant` do the same. The first
  start turns each line of the old grants docs into a grant; the docs stay as they were, and a
  grant in effect is still written to them.
- Area owners: the cell records which person owns each GitHub repo (`owner/name`, matched in
  lowercase). `GET /api/areas` lists them, `PUT /api/areas` replaces the list and `DELETE
  /api/areas/{owner}/{name}` removes one (both need the admin scope), and `PUT
  /api/areas/{owner}/{name}` sets one: an admin may set or reassign any repo, anyone with a person
  on their key may claim a repo nobody owns yet, for themselves. The MCP tools `list_areas` and
  `set_area` do the same. Project keys cannot reach these routes.
- Crew settings, AGENTS.md and memory per person. `PUT /api/crew/settings` saves the caller's own
  settings (the system credential, the person `local` and a cell with auth off still change the
  cell-wide default), and `GET` returns theirs over that default; a key with no person is refused.
  Each person also has a personal AGENTS.md and memory, made with their first line
  (`POST /api/docs/personal/{agents|memory}/lines`, or `add_to_global` with `scope="personal"`),
  listed and readable only by them. `read_global_docs` returns the team AGENTS.md and memory, then
  "Your rules" (the team rule wins on a conflict) and "Your memory".
- The console shows a project's lead, scope and members. The project page has a "Lead" line with a
  Team/Private control and "Change lead" for the lead (an admin changes the lead only), a Members
  page under Settings to add and remove people, and the Projects list shows each lead, a lock on
  private projects, and a greyed "Private, led by <name>" row, with "Change lead" for an admin, for
  a private project you are not on.
- Project access: a project has a lead (the person who made it; none for a project made with the
  root token), a scope (`team` or `private`) and a member list, shown on `GET /api/projects` and
  `GET /api/projects/{id}` as `lead`, `scope` and `members`. `PATCH /api/projects/{id}` sets the
  lead and the scope, `POST /api/projects/{id}/members` and `DELETE .../members/{person}` change the
  members. These calls need a key with the admin scope; a read key is refused even when its person
  leads the project. The lead changes all of it; an admin or the root token may change only the lead
  (which is how a project with no lead is claimed), and the lead who handed it over stays on the
  members. A project set up with a person's key (not from a draft) is led by that person. A project
  from before reads as `team` with no lead.
- A private project is hidden from people who are not on it: every route under `/api/projects/{id}`
  and the MCP tools answer as if it did not exist (404, or `unknown project` when named), and the
  list shows it as a name and its lead, nothing else. The lead, the members, the system credential
  and the project's own keys see it whole. An admin who is not on it can only hand the lead over.
  Admin rights come from the key's admin scope alone, not from the person's `is_admin` flag.
  A create that names no project, on a cell whose default project is private, asks the caller
  who is not on it to name a project, without saying which one the default is.
- A private project's data stays out of what a non-member reads across the cell: the raw read
  (`/read`, MCP `read`), the event and field lists, entity counts, sources made for the project,
  triggers, agents and their runs, resources, the roster, subscriptions, skills, docs, the standup,
  the factory's stations, queue, desk and crew lists, and the tables that follow a project's work
  (sessions, queries, dispatches). An event belongs to a project by its `tares_project` label, by a
  Claude Code session recorded in the project, or by the `project` a shared finding or memory row
  was written in. Ask runs with the asker's own visibility and leaves a private project out of
  `list_projects`; with auth on, `/tares ask` in Slack treats every private project as hidden
  (Slack users are not mapped to people yet). An event with no project label stays visible to
  everyone. Two limits: an entity count over a source shared by several projects still counts the hidden rows, and a
  station's recent lines are not filtered by row.
- Ask transcripts belong to the person who had the chat: with auth on, a key lists and opens its
  own, the system credential sees all, and a key with no person can keep none (a save answers 403).
  The read log records which projects a read was about, including the ones that own the sources it
  read, so a private project's reads stay out of everyone else's activity feed. A label value or a
  bare key carried only by a private project's rows no longer lists, and a practice run starts from
  an event its caller can see. Ask transcripts saved before this upgrade have no person, so with auth
  on they are visible only to the system credential (the root token); with auth off nothing changes.
- MCP tools `list_people` and `set_project_access` (lead, scope, members added or removed, people
  by id or email).
- A recording carries the person who shipped it: the cell labels every Claude Code event pushed to
  `/ingest` with `tares_person=<person id>` from the key's person (`local` with auth off, no label
  for the root token), and drops any `tares_person` the sender wrote. Read by it like any label.
- People in the console: Settings, Access, people and API keys lists the people on the cell, lets an admin add and edit them, and shows a person on every key (pick one when you create a key, or bind an unbound one). The sidebar shows who you are.
- People on the cell: `GET/POST /api/people`, `PATCH /api/people/{id}`, and a person on every API
  key (`person_id` or `person: {email, name}` on `POST /api/keys`, `PUT /api/keys/{id}/person`).
  `/api/whoami` now says `kind` (system, key, project or open) and the `person` calling. A cell
  with auth off, or with only the person `local`, behaves as before.
- Every record of who did something (ticket history, assumptions, checks, reviews, the desk,
  decisions, the handover) keeps the person behind the key next to the station, and the API lists
  it as `person` and `by_label` ("Kiran (crew-builder-1)"). A line added to the global AGENTS.md or
  memory ends "(added by Kiran, 2026-10-21)".
- The crew's MCP tools `list_assumptions`, `desk_list` and `list_decisions` list the `person` (and
  `by_label`) too. A doc changed through a person's key records the person in `updated_by`, not the
  key's name, and so does a doc written with the Claude Code tools `write_doc` and `add_to_global`,
  a grant added with `add_grant` and a `hand_over` (they used to stamp "claude code session" over
  the person; that label now stays only for a key with no person and for the system credential).
  A `by` the caller sends still comes first.
- Settings, People: adding a person with an email that already belongs to someone says so instead of
  closing the form, changing a person's email keeps the old address as an extra email, and a key's
  person picker shows the person's name while people load.
- Console for areas, grants and personal settings: Settings has an Areas tab listing each repo with
  its owner and note. An admin adds, reassigns and removes owners; anyone else can take a repo
  nobody owns and edit the note on a repo they own. A project has a Grants view with each grant's
  words, kind, who gave it, who agreed and whether it is in effect or still waiting for someone,
  with Consent, Revoke and an Add a grant form. On the Crew page, "Your crew settings" can be
  changed (autonomy, builders at once, challenger, releaser), with "Use the team's" to drop a
  personal value. A project's docs show "Only you see these": your own rules and memory, with a
  field to add one line.

### Changed
- On a Tares Cloud factory cell, Settings, Team, People is the one team list: who is on the workspace, with invite and remove, plus each person's GitHub login and admin flag. General no longer lists the team and Add person is gone there. `DELETE /api/people/{id}` takes a person off the list and the seat count and revokes their keys; their work keeps their name. Self-hosted cells keep their own list.
- Final releases are open again: the team code now lives in ee/ under the enterprise licence.
- Settings on a factory cell is one page with a two-level sidebar: Crew, Tools, Models, Team, Workspace and Advanced, one group open at a time. The main menu gives way to a Back link. What you allowed has its own page, Advanced has its own place in the sidebar, and the long introductions are gone. The separate All settings page is gone on factory cells; old links land on the right item.
- Ticket assignees (naming an assignee on a ticket, and Linear and GitHub setting one on sync) are part of the enterprise code (TR-920). Without an enterprise licence, naming an assignee answers 402 `licence_required`, clearing one still works, and a sync never changes who a ticket is assigned to; an assignee already stored still shows. With a licence nothing changes.
- Repo owners (the `/api/areas` routes and the MCP tools `list_areas` and `set_area`) are part of the enterprise code (TR-919). Without an enterprise licence those routes answer 402 `licence_required` and the tools are not listed; with one nothing changes.
- A project's lead, privacy and members (`PATCH /api/projects/{uid}`, the `/members` routes and the MCP tool `set_project_access`) are part of the enterprise code (TR-921). Without an enterprise licence those routes answer 402 `licence_required` and the tool is not listed; the person who makes a project still leads it, and a private project stays private. With a licence nothing changes.
- Grants that wait for someone else to agree (a grant naming `areas` that an area owner or the project lead must consent to, `POST /api/grants/{gid}/consent` and `/decline`, and the MCP tool `consent_grant`) are part of the enterprise code (TR-922). Without an enterprise licence those routes answer 402 `licence_required`, `consent_grant` is not listed, and naming `areas` on a grant answers 402. Team, project and personal grants stay free and take effect at once for one person. A grant already waiting stays waiting: it never takes effect by itself, is not copied into the grants docs, and can only be revoked. With a licence nothing changes.
- Questions to repo owners (the `/api/projects/{id}/questions` routes, the `spec_questions` in `my_work` and the MCP tools `ask_area`, `answer_question` and `list_questions`) are part of the enterprise code (TR-923). Without an enterprise licence those routes answer 402 `licence_required`, the tools are not listed and `spec_questions` is empty; handing a project over works as before. With a licence nothing changes.
- Keeping crews apart by machine is part of the enterprise code (TR-924). Without an enterprise licence there is one crew per name on any machine, so a station that starts on a second machine joins the crew it already has instead of making another; a crew named `ana-laptop` keeps that name. With a licence crews are per person, name and machine as before. Crews already stored are not changed.
- The hand-over no longer waits for area owners to sign off: a project with a spec and tickets is ready to start once no question is open. "You are waiting on" lists only the projects with a question open for someone else, and a project is never both there and in Ready to start. Sign-off and Hand over anyway are gone from the home, the API (`/signoffs`, `override_reason`) and the crew tools (`sign_off`). Plugin 0.6.6: the spec lists the open questions and asks whether to start now or keep the project for later.
- A project can be kept for later: Later on its Ready to start card or its page takes it off the home, it shows under Later in All projects, and Start now or the hand-over brings it back (`POST /api/projects/{id}/later`, the `set_later` tool).
- Agents can have a monthly budget (`budget_period: month`, resets on the 1st UTC); existing budgets stay lifetime.
- An agent's page shows this month's spend next to a monthly budget (`budget_spent_usd` on `GET /api/agents/builtin`), and a test pins the month boundary in UTC.
- CI reads its test list from tests/ci_list.txt.
- Plugin 0.6.1: builders mock a screen first and wait for approval; the reviewer compares screenshots of every state with the approved mock.
- Plugin 0.6.2: the orchestrator keeps a builder's mock as the project doc "Mock: <screen>" and puts its desk item on the project, so the approval shows in `list_decisions`.
- Plugin 0.6.3: the Tares skill says an agent on a Software Factory cell sees outcome tools first and the building-block tools only when an admin turns them on.
- Plugin 0.6.4: the Tares skill names the four tools an agent sees first on an AI SRE cell (`what_is_broken`, `how_is_service`, `what_happened`, `mark_incident`) and says `read` keeps working there.
- The factory and AI SRE console editions have a words list (`ui/src/editions/words.json`) and checks
  that fail CI when an edition screen shows an internal name such as source, trigger or run, or an em
  dash. `tares.words` runs the same check over API JSON.
- The Crew page shows every person's crews, you first. Each crew lists its name, machine, when it was
  last seen and its stations with what each is on; crews not seen for a day fold under "Not seen
  recently". Stations are told apart by crew, so two crews with a `crew-reviewer` no longer open each
  other's detail. The desk groups its items "For you" and "For <name>", and other people's items are
  read-only. It reads the `crews` list from `/api/crew` and falls back to the flat `stations` only when
  a cell does not send it.
- A person's own crew settings can no longer be looser than the team's: `PUT /api/crew/settings`
  refuses an `autonomy` or `challenger` that is looser than the team's value with a 400 that names
  it, and a stricter one is kept. If the team tightens later, the person's older, looser value no
  longer applies. `clear: ["autonomy", ...]` in the body (and the MCP tool `set_crew_settings`)
  unsets a personal setting so the team's applies again. Ingest scope and the other settings have
  no ceiling.
- A personal AGENTS.md or memory holds at most 32 KB; a line that would pass it is refused with a
  400 saying the doc is full.
- The station skills work from your own work on a shared cell. The orchestrator ticks on `my_work`,
  checks milestones only for the projects you lead, and reads the crew's shared docs and settings at
  boot. A station sends a question for someone else as an `ASK` with `to:`, and the orchestrator files
  it on the desk. The reviewer and releaser answer only their own crew's tickets, and `/tares:ship`
  and the session skill hand over to the orchestrator of the crew that holds the project.
- `tares factory retire` now says which crew the station is in, so it ends the right one when another
  crew has a station of the same name.
- Without an enterprise licence, a desk item addressed to someone other than you (`to` in `POST /api/desk` and the MCP tool `desk_add`) answers 402 `licence_required` and writes nothing (TR-925); `to` yourself, `local` on a cell of one, or no `to` still works, and with a licence nothing changes. Binding a key to a person who is already on the cell (`PUT /api/keys/{kid}/person`, or `POST /api/keys` with `person_id`) never needs a licence.

### Fixed
- On a team cell's Home, a desk item holding a batch of calls can be answered (TR-1083). It was refused every time with a message nobody could act on. The row now keeps them all in one step or opens the project's calls page, and a call on your own desk no longer shows a second time under calls to confirm. A call already decided on the calls page no longer holds the item open.
- Dark mode on the factory screens: the command to open a builder's session, the project's steps, the Builders at once counter, the Autonomy choices and the search fields kept a white background under light text, so their text could not be read. They follow the theme now.
- Settings on a window under about 1100 px wide: a wide table (API keys, GitHub credentials) scrolls inside its card instead of pushing the page sideways. A focused dropdown shows one arrow, not a striped box, and the Agent tools and Edition choices keep a space between the box and its label.
- Waiting for you puts what the crew needs now first (TR-1021): items that hold crew work up (a blocking question, a refused command, a pull request waiting for your merge) show under "The crew is waiting on you". A crew question shows its options and the crew's recommendation, and an option can be clicked to answer. Items that waited more than a week and hold nothing up fold into "N older things". Each row says who it is from, with their picture. A desk item a builder of your crew put there is from your crew, not from you. A project with no lead no longer asks you to sign off your own repo, or says you are waiting on yourself. Long text is cut at a word. The team home uses the same milestone rule as the single-person home, and on a project a crew works on, its unrecorded checks stay the crew's instead of asking you to accept them.
- Plugin 0.6.5: the spec marks checks a person must try (`for: "person"`), and the orchestrator and your session record the crew's milestone checks with `record_milestone_check` (TR-1017).
- "Milestone to check" can be finished (TR-1017). The crew runs a milestone's checks when its last ticket merges and records each (`record_milestone_check`, `POST /api/projects/{id}/milestones/{m}/checks/{n}`); a failure is the crew's work. Only checks marked `for: "person"` reach you, each with Works or Something's wrong, which opens a fix ticket in the milestone. A milestone whose crew checks were never recorded can be accepted as it is. A project ready to close folds its milestone items into Ready to close, naming the ones not accepted.
- A decision reads as the answer chosen: "merge main into teams", not "D85 A". The person's words show under it ("You said: D85 A") only when they add something, on the project page, the ticket page and in the reviewer's brief. On a question with options every decision records the option: Tares maps a letter (alone, after the label, or before a separator) or the option's own words, and refuses free words that name no option, listing the options, so the orchestrator asks which one was meant.
- The Tickets board shows every open ticket when a cell has many merged ones: it reads open tickets first (`sort=open` on `GET /api/factory/tickets`), and a column that counts more tickets than the board read links to the List for that state instead of a Show more that did nothing.
- The Reviews page no longer lists pull requests nobody is on as "With the crew". A crew row now names who has it next (the reviewer, the builder, CI or the crew's merge), and a new "Nobody is on it" list shows the rest with the reason: the project is finished, it was never handed to the crew, you took it over, the pull request was closed without merging, or the reviewer blocked it. The home page counts from the same split.
- A ticket closed in its home (Linear, GitHub Issues or Tares) is closed everywhere: its stage in the crew's work is cleared in the same write, with a history line saying why, and a one-time repair at start closes the ones that kept their stage. Every screen now reads a ticket's state from one rule (`factory.state_of`).
- Pull request state stays current without anyone opening the project: the daemon reads open pull requests again in the background every 2 minutes (`TARES_PR_SYNC_SECONDS`, `off` turns it off). A project whose only open work is a pull request waiting for your merge now shows as waiting on you, not building.
- The Software Factory home's Connected chips say what each tool is connected to (GitHub · the account, Linear and Slack · the workspace, Claude Code · the last session), Slack is among them, a chip opens Settings, and Add a tool shows only while a tool is still unconnected. Settings says what each tool covers: the repos the open projects build in, the projects that keep tickets in Linear, the channels agents post to. Home and Settings read the same rows (`tares/factory_tools.py`), so Claude Code counts as connected on both only once a session has reported.
- New project on a Software Factory cell is a factory page: it gives the `tares factory spec` command to start the project's spec in Claude Code (with what you want to build filled in) and says what happens next. The template gallery and the generic setup pages land there instead; the Platform and AI SRE editions keep them.
- Private projects stay private on the settings pages and merge checks: the SRE Tools list leaves out their sources, a merge counts the links of services you cannot see, and the key count leaves out their keys.
- Admin routes no longer reveal or change what a private project holds: renaming or merging its services, the catalog export and import, and the key list and key changes answer a non-member as for a name that does not exist.
- A person outside a private project can no longer change, pause, delete, test or rerun its sources, triggers, agents, MCP servers or runs, or decide its rule proposals; those answer as if they were not there.
- Services, repos, watch rows, blind spots and deploys of a private project no longer show to people outside it.
- Private projects stay out of the raw incident, timeline, findings, feedback, usage, dependents and skill or doc use routes.
- Private projects no longer show on the factory and AI SRE screens to teammates who are not on them; their pages answer not found and their lists leave them out.
- A key that can only read is now refused the Settings pages of the factory and AI SRE editions, in the console and in the API.
- The AI SRE home no longer shows a read-only key buttons it cannot use, and says which key can only read when a click is refused.
- A person signed in with a login can now answer or allow a desk item that has no addressee, instead of getting "not addressed to you".
- A cell switched to the SRE edition no longer gets the AI SRE demo project added; only a brand-new cell is seeded with it.
- A factory cell no longer scans a day of events for every service each minute; the scan runs on AI SRE cells, and on platform cells that have a watcher.
- The incident backfill on first start is about 20 times faster, so a cell with many past findings starts without waiting on it.
- Factory settings you save as a signed-in person now show on the Settings, Crew and Reviews pages and the home card after a reload.
- The AI SRE home page's "Read the note" link opens the incident, the Timeline menu item opens the Timeline, a finding with no time no longer blanks the incident page, and Copy buttons no longer fail on a cell served over plain http.
- The AI SRE console no longer goes blank or prints "NaN" when a time is missing, and copy buttons no longer throw on a cell served over plain http.
- The console's e2e runner in CI no longer reports a passing suite as timed out when its timer fires just as the suite ends, and its own test fails instead of hanging if the timeout kill breaks.
- The AI SRE Start checklist stays hidden after you are renamed and is no longer shared by two people with the same name, and the factory home greets you by your own name.
- A grant that was declined stays declined when the project lead adds the same words, and two requests settling one grant no longer remove the line of a grant that is in effect.
- On a team cell, Your crew on the home counts only the questions addressed to you and the tickets that are yours, and another person's crew that waits shows "Waiting for an answer." instead of its reason.
- The team home records which option you clicked when you answer a question. A sign-off over more than 20 tickets says how many more there are. An option answer you let stand keeps the option's text as the grant, not its letter. A refused sign-off or decline shows its reason once, in the box. In a sample, the buttons that open a box open it and say nothing was sent.
- The Crew page standup line now counts only what happened in the last 24 hours: tickets done, started, sent to review, blocked and new questions, not what has been standing for days.
- An incident someone else is on can no longer be taken over by pressing "I'm on it"; the screen says who is on it.
- A crew's line on the team home no longer shows the reason of a station that also works on a project you cannot see.
- A grant that was declined stays declined: a consent that comes after the decline, or at the same moment, no longer puts it back in effect, and the grants list no longer shows a person who declined as having agreed.
- Incident notes no longer show No action when the note has a numbered next step. A finding with no verdict now counts as a note that waits for a person, on every edition.
- Console: the line chart and sparkline skip values that are not numbers instead of drawing them as zero, sort points by time, and draw one point as a short segment; y labels show the decimals small values need, marks can be listed as text, and disabled controls and focused fields now match the rest of the console.
- Plugin: the station and spec skills keep working against a Tares before 1.56. That Tares has no
  `my_work`, area or people tools, so the orchestrator there works from `crew_projects` and
  `list_tickets` as before, and the spec skips area owners and sign-offs. `tares factory crew up`
  no longer prints a shell error there when it counts the people.
- Settings, Areas: saving a repo pasted as its GitHub address (`https://github.com/glassflow/tares`)
  failed with "Method Not Allowed", because the address went into the request path. The console
  and the cell now take a GitHub address, with or without `.git`, or `git@github.com:`, as the
  repo it names; anything else that is not `owner/name` says so. `set_area` in Claude Code too.
- A cell used by one person before people existed keeps working for its owner once they become a
  person. The owner's old keys acted as nobody as soon as a second person existed, and the projects
  without a lead and the desk items addressed to no one stayed with `local`, out of the owner's
  `my_work`. When the control plane names the workspace owner (`owner: true` on a key's person, on
  `POST /api/people` or on `PUT /api/keys/{id}/person`, system credential only), `local` takes
  their name and email instead of a new person being made. A teammate never becomes `local`.
- Settings, Access on a cell with people: it said "You signed in with the root token" to everyone.
  It now names how you signed in (the root token, your API key and whose it is, or a project key).
  The People list marks your own row and shows the boot person as "Local (no login)" instead of
  "You"; on a cell without auth it is still "You".
- The sign-off section of the project page: a cell of one person no longer shows its own repos as
  "No area owner" or "Waiting" (they read "Lead's own repo, no sign-off needed" and count as ready).
  `hand_over` no longer says "handed over without every sign-off" after a later hand-over that needed
  no override. A hand-over and its entry in the override log are now saved together, and the one-time
  copy of an older override that has no time takes the hand-over's. `my_work` takes `project` (a name or an id) and then lists only that project's questions,
  because question numbers restart in each project; the contributor session uses it.
- Follow-ups to the sign-off work: an older override that has no time of its own now reads as
  overridden after the upgrade. A cell of one person still shows "Set one under Settings, Areas" next
  to an unowned repo. `my_work` with a `project` that does not exist, or that you cannot see, answers
  404 and names it, instead of an empty list. A `before` without a zone on the project timeline and
  results is read as UTC, not as the host's time. A timeline filtered by `trigger`, `agent`, `outcome`
  or `entity` leaves out the hand-over override events, which match none of them.
- Opening another person's station on the Crew page no longer asks their machine for a context reading
  (the page says only its owner can), and the server refuses that ask with a 403. When the page
  cannot tell who you are, it says so and offers Retry instead of treating you as `local`.
- Ending a station by its name alone no longer ends another person's station of that name: only its
  owner (or the system credential) ends it, and anyone else gets a 403.
- A desk item can only be addressed to someone who can see the project: for a private project, `to` must
  be its lead or a member, else the item is refused (403) instead of reaching a person who cannot open it.
- An answer typed within a minute before the cell stamped the question still answers it, so a machine
  whose clock runs a little behind no longer gets the retry error every time.
- The factory watch read the `/context` ask of every crew on the cell each round, and failed on the
  ones that were not its own. It now answers only an ask for the session its own station plays.
- Area owners: reassigning a repo's owner without a note no longer clears the note it had (send
  `note` to replace it, or `null` to clear it), and the MCP `set_area` refuses a repo name with a
  `#`, `?` or space instead of saving a shortened repo.
- Reading grants settled every waiting grant on the cell, as the reader, including ones on a
  private project the reader cannot see. It now settles only grants the reader may see, recorded as
  Tares, and a grant that cannot be settled stays waiting instead of failing the read.
- Answering a spec question and signing off a repo now work with an ordinary key bound to a
  person (read and ingest scopes, no admin). They were refused with "this credential
  lacks the 'admin' scope", so an area owner needed an admin key. The same checks apply as before:
  a question is answered by the person it is addressed to, and a repo is signed off by its current
  area owner; anyone else gets 403, and a repo with no area owner gets 409. On a cell with one
  person, a key bound to nobody acts as that person (`local`), so it can do both for them.
  The contributor part of the spec skill now says what to do when an ordinary key is refused a
  ticket edit (pass it to the lead), and quotes the real 404 text of `my_work`.
- The idle-wake scans of the reviewer and the orchestrator now look only at the caller's own tickets
  (`/api/review-queue?mine=1`, `/api/desk?...&mine=1`), so a crew no longer wakes for another person's work.
  The Claude Code retry marks are kept per person and crew, so two people's crews with the same
  station names no longer overwrite each other's marks. A personal PUT of `{"kit": null}` is no change.

## [1.55.1] - 2026-11-02

A long Claude Code session keeps reaching Tares however large its unsent backlog grows. Claude
Code plugin 0.6.2.

### Fixed
- Plugin: a long session no longer stops reaching Tares. The hook sent the session's whole unsent
  backlog in one request, and once that took longer than the 5 s timeout every hook failed the
  same way, so the person's replies in that session were never recorded. Each hook now sends at
  most 2 MB and moves on from there, so a backlog catches up over the next few hooks, and the
  session's last hook sends whatever is left (TR-809).

## [1.55.0] - 2026-11-01

The MCP server is listed in the official MCP registry, `tares mcp --transport stdio` works, and
two fixes for cloud cells with their state in Postgres: a scrape of `/metrics` no longer opens a
database connection, and a cell that could not reach its database at start restarts itself.

### Added
- The Tares MCP server is published to the official MCP registry as `ai.glassflow/tares`, so
  MCP clients and directories can find it. Each release tag publishes the new version.

### Fixed
- `tares mcp --transport stdio` works: its startup line went to stdout, which is the MCP channel
  over stdio, so the client could not read the server's first reply. It now goes to stderr.
- A tool call while Tares isn't running says so and names the address it tried, instead of "All
  connection attempts failed".
- `/metrics` no longer reads the state database. On a cell with its state in Postgres
  (`TARES_STATE_DSN`), every scrape opened a connection to it, so idle cells held about a fifth of
  the database's connections between them. `/api/usage` still reports the run and delivery counts.
- A cell whose state database could not be reached at start (no DNS, a connect timeout, no free
  connection slots) answers `/health` with 503 while degraded, so the liveness probe restarts it
  and it tries again. Before, it stayed degraded until someone restarted it. Every other degraded
  cause still answers 200 and waits for a person.

## [1.54.1] - 2026-10-29

Grok is named as the challenger when it reviews, xAI agents default to grok-4.6 with grok-4.3
for Triage, and DuckDB's memory is capped on machines without a container limit. Claude Code
plugin 0.6.1.

### Changed
- xAI: an agent without a model runs on grok-4.6 (xAI lists a non-reasoning model first), and the
  Triage (on a schedule) preset picks grok-4.3 for the frequent look. Each applies only while the
  key lists that model.

### Fixed
- Challenger with Grok: the console, the summary and Claude no longer say Codex when Grok
  reviews. `/tares:challenger` and the tares skill name the challenger as Codex or Grok, each
  challenge on the timeline names its reviewer ("Challenger (Grok) reviewed commit ..."), the
  summarizer calls the reviewer by that name and no longer reports a plan challenge that did not
  happen, and the template's setup says how to pick Grok.
- Template setup: the "model provider" step counts any configured provider, not only an
  Anthropic key, and the Model picker lists the default provider's models instead of Anthropic's.
- DuckDB's memory is capped at 1 GB when `tares up` runs on a machine with no container memory
  limit. Before, DuckDB took its own default of 80% of the machine's RAM and its cache grew to about
  4 GB on a Mac after two days of crew use. Set `TARES_DUCKDB_MEMORY_LIMIT` (for example `2GB`) to
  change it; a container limit still sets it to 60% of that limit. The cap also sets
  `TARES_DUCKDB_THREADS` to 2 unless you set it.
## [1.54.0] - 2026-10-28

Grok can review a challenger session instead of Codex, and xAI is one of the model provider
presets. Claude Code plugin 0.6.0.

### Added
- Console: xAI (Grok) is a preset in Settings, Model providers, next to the others.
- Plugin: Grok can be the challenger's second model instead of Codex. Set `challenger_backend` to
  `grok` and give the plugin an xAI key (`xai_api_key`, or `XAI_API_KEY`). Each plan or commit is
  reviewed in one xAI API call over the patch and the changed files, with the same priority tags,
  fix loop, waivers and Tares record as Codex. A reply passes only when it ends on an explicit
  `VERDICT: CLEAN`; one that names nothing and gives no verdict is inconclusive, and a
  `VERDICT: FINDINGS` whose findings cannot be read as `[P1]`/`[P2]` lines blocks. The challenge line
  shipped to Tares now names the `reviewer` (codex or grok) and, for Grok, the `model`. Codex stays
  the default. Running Codex itself against xAI does not work: xAI rejects Codex's second turn
  ("Could not decode the compaction blob").

## [1.53.0] - 2026-10-28

A cell serves a page's requests side by side instead of one after another, and a cloud cell keeps
its state (projects, tickets, agents, settings) in Postgres while its events stay in DuckDB. On
factory the project page went from 1.2 s to about 0.7 s, and the Crew page from 0.43 s to about
0.3 s (P-TR-268).

### Added
- State in Postgres, opt-in with `TARES_STATE_DSN` (install the `postgres-state` extra). A cell
  started with it the first time copies its 44 state tables out of `tares.duckdb` in one
  transaction, checks the row counts and records the move. From then on it reads and writes state
  only there. Without the variable nothing changes: local installs keep everything in DuckDB.
- A cell that moved refuses to start without `TARES_STATE_DSN`, or with one that points at a
  database without its state, instead of serving the stale DuckDB copy. At startup it waits up to
  120 s for its state database (`TARES_STATE_CONNECT_WAIT`), and reconnects once after a dropped
  connection.

### Changed
- Request handlers run in worker threads, so the requests a page fires together are served
  together. Catalog reloads, ingest and poll writes, ticket stage moves and desk answers no longer
  run their database work on the event loop, so one slow query no longer stalls the rest of the
  cell.
- The Crew page's projects, repos, assumptions and pull request refresh are read once per request
  instead of once per item.
- Codex challenger results are kept in their own table when the event arrives, so the review
  queue, the ticket view and the merge gate no longer scan every event. The "Codex open" chip stays.

## [1.52.0] - 2026-10-27

The Projects list reads as a work queue, the software factory crew paces itself and moves onto
new Claude Code versions on its own, and cells time every request and can close `/metrics`.

### Changed
- Console: the Projects list no longer marks the Challenger workflow and Default projects as AI SRE
  (TR-698). The chip went to every project that was not a software factory, but a project of no
  special kind is an ordinary one. Now only a project made from an AI SRE template (`ai_sre_demo`
  or `rius_rca`) shows the AI SRE chip, a software factory project keeps the factory chip, and
  every other project shows no kind chip. The legend under the list names a kind only when a row
  of that kind is on show.
- Console: on the Projects list at 720px and below, a done project now says "done" after its ring
  (TR-696). The count folds away there, so a project with every ticket closed but still active and
  a done project looked alike apart from a shade of colour. A wide screen is unchanged.
- Factory: cmux says "Waiting for you" about a minute after every crew station's turn ends, even
  though a station between ticks needs nothing (TR-694). The notice is cmux's own, not Claude
  Code's, and Claude Code has no setting that drops it while keeping permission prompts. The fix
  is a cmux option on your side: set `"notifications": {"agentIdleReminder": false}` in
  `~/.config/cmux/cmux.json`. `tares factory doctor` now reads that file and says when the option
  is still on, with the exact line to add. It never edits your cmux file, and the stations' own
  settings leave cmux's permission alerts alone.
- Console: the Projects list marks each project's kind with an icon (TR-692). A software factory
  project shows the factory emoji instead of the bolt, and an AI SRE project shows an AI SRE chip
  with a heartbeat icon (TR-698 narrowed that to projects made from an AI SRE template). Both fold to the icon alone at 720px and below, keeping the name in
  the tooltip and screen reader label, and the legend under the list explains each kind on show.
  The status cell is now the progress ring and its count alone: the dot beside it is gone for a
  project with tickets. A project with no tickets keeps the dot, and a paused, error or draft
  project adds a short word ("paused", "error", "draft").
- Console: the project page says a build station "is waiting for you" only when it needs you
  (TR-693, TR-616). Before, every turn end showed the banner, even when the builder was waiting on
  its own Monitor or on the reviewer and would resume by itself. The plugin now reports a new
  session state, `idle`, for a turn that ended (the Stop hook and Claude Code's idle notice), and
  keeps `waiting` for a permission request or prompt, an elicitation dialog or an AskUserQuestion
  (new PermissionRequest and PreToolUse hooks). An idle session shows no banner, and still turns
  quiet after 90 minutes. The crew list labels the two states "Idle" and "Waiting for you". Rows
  from the old plugin keep working. Restart `tares up` first, then reload the plugin on a running crew: an old daemon
  drops the `idle` lines, so a session would stay `working`.
- Console: the Projects list reads as a work queue (TR-689). It hides done projects by default, with
  Active, Done and All tabs that show their counts, and a line saying how many are hidden. A
  filter box matches names and goals, and the sort offers recently active, most work pending, least
  work pending and name. The name is the link, so the Open button is gone, and so is the Objects
  column. Goals are a tooltip on the name, or on every row with the Goals toggle. A project's
  status is the progress ring and its count; the Active and Done words are gone. A Pending column shows the work left, with what is in progress
  and blocked. Updated now follows a project's newest ticket update, not only edits to the project
  itself (`/api/projects` gains `activity_at`). The software factory tag is a small icon chip with
  a legend, and the table keeps its layout at narrow widths. The view lives in the address
  (`?show=`, `?sort=`, `?q=`, `?goals=1`), so a link or a reload keeps it.
- A factory project's tickets list reads every working doc's title and every ticket's last review
  in one query each, instead of a few queries per ticket (TR-657).
- The review queue reads the tickets in review across every project, when each went to review and
  its last review in one query each, instead of every project's whole ticket list and a few
  queries per ticket (TR-648).
- The projects list reads the catalog once and every software factory project's tickets in one
  query, instead of both once per project (TR-659).

### Added
- Factory: `factory doctor` reads each handed-over repo's languages from its manifests (Python,
  TypeScript, JavaScript, Go, Rust, at the repo root or one folder down) and says whether Claude Code has a
  language server for each: the plugin enabled and its binary on your PATH. For a missing one it
  prints the install command and the plugin that wires it in (`npm install -g pyright`, then
  `claude plugin install pyright-lsp@claude-plugins-official`). `factory crew up` offers the same
  install and runs it only on a yes; `--yes` and a closed input install nothing. The builder and
  reviewer manuals say to prefer the LSP tool over grep for definitions, references and callers
  when it answers. Without a server the tool has nothing to answer with (TR-682).
- Factory: the crew setting `idle_wake` (off by default) wakes stations on events instead of fixed
  ticks. The watch asks Tares what each station has to do and restarts an idle station with one
  message only when there is work (a PR without a verdict, changes asked for, a `RELEASE`, a desk
  answer, a new project), and otherwise sends a liveness wake after 15 to 30 minutes, then 30, then
  every 45, so an empty crew costs a few model turns an hour instead of one every 15 to 30 minutes
  per station (TR-673).
- Factory: the crew setting `pace` (off by default) slows the crew before the plan's limit instead
  of hitting it and restarting every station at once. From the plan usage Tares already keeps: at
  85% of the 5-hour window or 90% of the week no new builder starts (`factory spawn builder`
  says why) and each station is told once to tick every 45 minutes; at 95% of the window a builder
  also finishes its step, pushes, writes down where it is and waits. A station that stopped while
  a limit was full is not revived into it; after the reset the watch revives it in its own
  conversation, and starts a new one only when that fails. The Crew page and
  `factory crew status` say when the crew is pacing and until when (TR-674).
- The tares-factory crew moves onto a new Claude Code by itself. Claude Code installs by swapping a
  symlink, so a station kept the version it started on until someone restarted it. `tares factory
  crew watch` now sees a newer `claude --version` that has held for a whole round and restarts
  one idle station a round, in the same conversation, the orchestrator last; a busy or attached
  station waits. `tares factory crew update` (or `crew restart`) does every station right away,
  waits for each to be idle, and checks there is one session per station name. A restarted station
  is checked to run the new version: Claude Code's daemon keeps pre-warmed spares of the old
  version that a restart could claim, so those are stopped and the restart is tried again.
  `factory roster`, `factory doctor` and the Crew page show each station's Claude Code version and
  flag older ones. The crew setting `claude_update` (on by default) switches the watch's part off
  for people who pin a version. A frozen crew (`crew freeze`) moves no station onto a newer Claude
  Code either; the watch says there is one, once for each version, and `crew update` still works
  (TR-669).
- Factory: the Claude Code move now covers the crew's helpers and no longer gives up. A helper moves
  onto a new Claude Code with the watch, like its station, after the other stations and before the
  orchestrator; a busy helper does not hold the orchestrator back. A station whose restart keeps
  landing on an old pre-warmed spare is no longer parked on the old version: after three tries in
  its conversation it is started again in a new conversation on the new version (a helper is not,
  since its task lives only in its conversation), and if that fails too the watch tries again after
  a pause that doubles (10 minutes, up to 4 hours; `crew update` tries at once). `factory roster`, `crew status`, `factory doctor` and the Crew page show it as
  retrying. `crew update --wait 00` waits without a deadline, and the message for a caller that is
  not behind, or whose watch will not move it (`claude_update` off, a frozen crew), no longer
  promises a move by the watch (TR-687).
- `/metrics` times every HTTP request: `tares_http_request_duration_seconds{route, method, status}`,
  labelled by route template (such as `/api/projects/{uid}/tickets`), never by a raw path or ID
  (TR-652).
- `TARES_METRICS_TOKEN` closes `/metrics`: when set, a scrape needs `Authorization: Bearer <token>`
  and anything else gets 401. Unset, `/metrics` stays public as before (TR-653).

### Fixed
- Factory: `desk_answer` finds every line of a long pasted reply (TR-697). A reply of more than
  about 500 characters was stored cut off, so its later lines were "not found" and only the first
  lines matched. A person's turn is now kept up to 20000 characters, and the `<pasted_content>`
  tags Claude Code wraps a paste in are ignored, so a whole paste matches with or without them.
  Replies already stored stay cut; paste or resend the missing lines once.
- "Raise the limit" on an agent that stopped at the daily run cap opened the agent's own page,
  which has no cap field. It now opens Settings, Limits, with the daily cap in view and marked
  for that agent; an agent with its own cap in its project still opens its own page (TR-670).
- The daily run cap message now says when the next run is allowed ("next run allowed in
  1h 12m"), counted from the run in the 24 hour window that has to age out first, on the project
  banner and in the capped run's error. An agent's run list folds the runs the cap refused into
  one line ("50 runs skipped, capped") that opens to list them. The Limits help line names who the
  number applies to: every agent without its own limit (TR-671).
- A firing that arrives while its agent is already running for the same key now gets the
  `skipped` write-back with reason `in_flight`. Since 1.51.0 it was sent only when the duplicate
  slipped past the first dedupe; the common case, caught when the firing is delivered, sent
  nothing, and Rius kept the firing queued (RIUS-1103).
- A link to a refused run inside a folded group of capped runs opened the group, but clicking
  the group's line could not close it again. The link now opens the group once, and the line
  toggles it like any other (TR-678).
- Factory: a `crew watch` started from the frozen kit copy and left running past an unfreeze
  restarted itself, on a kit change, from the copy it came from. By then the copy was removed, so
  the watch died. It now restarts from the checkout's `factory` (TR-686).
- Factory: the guard let a builder or reviewer run `/path/to/bin/factory crew freeze`, `up`,
  `down`, `halt` and the other orchestrator-only commands, and `factory finish`, by spelling the
  CLI with a path. It now matches the command by its name, whatever the path (TR-686).
- Factory: `factory doctor` and `crew up` counted a language server plugin enabled only for the
  repo's own folder as ready. Stations run in the crew home and in worktrees, never in that
  folder, so they had no server. Only a plugin enabled for you (user scope) counts now (TR-686).
- Factory: the guard still let a builder run an orchestrator-only command when the verb was
  followed by a backtick, `>`, `}` or `#` (`` x=`factory crew down` ``, `factory crew down>log`). A
  verb now ends at anything but a letter, digit, `_` or `-`. `ls ~/factory down`, a folder named
  factory handed to `ls`, `cat` and the other readers, was refused as if it were the CLI; it now
  runs (TR-688).
- Factory: the folder-named-factory rule above could be turned into a bypass. A reader written
  inside a quote (`` X='; ls a' factory down `` or a quote opened on the line before) made the guard
  blank the word `factory`, although the quote ends before it and the CLI runs. The rule now skips
  quoted strings as a unit, so a reader only owns the word when it starts a command outside any
  quote; quoted arguments to a real reader (`ls "my dir" ~/factory down`) still work (TR-690).

## [1.51.1] - 2026-10-21

The crew standup writes its summary again, and its Slack channel list fits on screen.

### Fixed
- Crew standup: the summary was never shown, always "the model call failed (KeyError)", whatever
  the model provider. The model answered; recording what the call cost then failed and the
  summary was dropped.
- The Slack channel picker opens upward when there is no room below it, so at the foot of the
  standup sheet the list is no longer cut off by the bottom of the window.

## [1.51.0] - 2026-10-21

The write-back webhook hears about runs that were refused, and the software factory crew keeps
the person's answers and tidies up after `crew down`.

### Added
- The write-back webhook gets a `skipped` event when a run a trigger dispatched is refused
  before it starts: `reason` is `daily_cap` (the agent's runs in 24 hours), `budget_exhausted`
  (its `budget_usd`) or `in_flight` (a run of the agent for the same key is already going, named
  in `in_flight_run_id`), with the `message` the run records. It carries the `key` and `labels` a
  finding would, so a receiver that files findings by the firing, as Rius does, stops waiting
  for one that never comes (RIUS-1103). A rerun, a bootstrap run or a hand-off sends none.

### Fixed
- Software factory: a reply the person types while the orchestrator's turn is still running now
  reaches Tares as their turn, so the desk records it as their answer. Before, Claude Code kept it
  only as a queued message and `desk_answer` never found it (TR-522). Messages from other sessions
  and task notifications still stay out.
- `tares factory crew down` withdraws the "stop the crew?" question, whether or not it was asked
  with a project, so it no longer sits on the desk after the crew has stopped (TR-599).

### Changed
- Project page: the crew's own stations in the crew list link to the Crew page, where their
  sessions live.

## [1.50.0] - 2026-10-20

A standup report for the crew: what moved in each project, summed up by the cell's model, ready to
copy or send to Slack. Software factory project pages stay current while they are open.

### Added
- Crew page: a Standup button opens a report of what moved in each project handed to the crew,
  since yesterday (since Friday on a Monday), over the last 7 days, or between custom dates. It
  lists what is done, started, in review, blocked (with the reason) and waiting on you, with PR
  links, and puts projects that did not move on one line. The cell's model adds an overall line
  and a line or two per project; without a provider the report still shows, with the reason.
  Copy takes the markdown, Send to Slack posts it to a channel you pick as the cell's bot.
- `GET /api/crew/standup?since&until` returns the report and its markdown (the last day by
  default, at most 92 days). `POST /api/crew/standup/summary` adds the model's summary (admin,
  counted as Ask usage). `POST /api/slack/post {channel, text}` posts as the cell's bot (admin,
  refused from crew stations).

### Fixed
- A software factory project page left open no longer shows stale tickets, docs and sessions. The
  factory pages refresh every 5 seconds, and every console page re-reads as soon as its tab is
  visible again. A doc being edited keeps your changes through a refresh.

## [1.49.0] - 2026-10-20

OpenAI agents default to a chat model, a retired or gone crew station stops showing as Quiet, and
the model provider table fits its card.

### Added
- Tares can mark a crew station ended (`POST /api/crew/<station>/end`), so a station whose session is gone stops showing as Quiet.
- `factory retire` marks the station ended on Tares, so a retired builder no longer shows as Quiet.
- Crew page: Mark ended on a Quiet station clears a session that is gone.

### Fixed
- An agent on OpenAI that names no model no longer defaults to `babbage-002`. The OpenAI model list drops models an agent cannot run on (legacy completions, embeddings, audio, images, moderation), and the default is the first of `gpt-5`, `gpt-5-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o` the key can use.
- Crew page: a finished project lists its builders under it, instead of under Other builders.
- Settings, Model providers: the provider table stays inside its card. The action buttons wrap instead of pushing the table past the right edge, and the models column no longer squeezes to one word per line.

## [1.48.0] - 2026-10-19

The factory crew follows your `tares login`: a new login reaches every station, a logout stops the
crew, and an old key can no longer break it. `crew status` reads cleanly once the crew is stopped.

### Fixed
- Crew stations no longer get 401s after a new `tares login` because an old key stuck around
  (TR-560). The crew follows the saved login: `tares login` gives the crew's key file the new key
  and `tares logout` empties it, so the crew stops with you. `tares factory` no longer passes the
  key to any process it starts, where Claude Code's background service could keep a revoked one
  for every station. Stations and the watch read only the crew's key file, a station never writes
  it, and a key Tares refuses never replaces a working one.
- `tares factory doctor` shows which key the stations use and which one this terminal uses, by key
  id, and flags a `TARES_AUTH_TOKEN` that overrides the saved login. `tares status` says so too.
- `tares factory crew status` no longer shows a question left over from a finished project once the crew is stopped, and says "no stations running" instead of printing an empty station table. Finishing a project withdraws its open desk questions, and `crew down` withdraws any left on finished projects (TR-559).

## [1.47.0] - 2026-10-16

Connect Linear on Tares Cloud with one click, and a self-hosted install connects Linear with a
personal API key. The crew's progress at a glance (progress rings, a fold for each project's
builders), allowing a refused permission from the console, what the crew spends, and a sidebar
that folds to icons.

### Added
- The Projects list and a software-factory project's page show a small ring of the project's tickets:
  done, in progress, blocked and to do, with the counts in its tooltip, so a stuck project shows at
  a glance. The project page's counts and ticket bar use the same buckets and name blocked tickets.
  `GET /api/projects` carries `progress` for every factory project.
- A crew station's attach command (`tares factory crew attach <name>`) has a Copy button wherever
  the console shows the station: a session's page, the project's Crew rows, the stopped-build
  banner, Next up and the Crew page, so you can open a stuck or waiting builder in one paste. A
  station that has ended offers none.
- A permission a crew station was refused comes to the desk with the station and the exact
  command, and you allow it with one click on the project page or the Crew page: you check the
  sentence that becomes the grant, choose this project or every project, and click **Allow for
  <station>**. Tares keeps it as a grant and as your decision made in the console, and the crew's
  watch restarts that station with it in its settings, in the same conversation, once it is
  between turns (never while you have it open with `crew attach`). `desk_add(station=, command=)`,
  `POST /api/desk/{ref}/allow` (the person only) and `via` on decisions.
- What the crew spends, on the Crew page: how much of your Claude plan is left (the 5-hour and
  weekly limits and when each resets, on a Pro or Max login) and which projects used the current
  5-hour window, in a pane below Projects that is folded by default with the most used limit on its
  line; and for each station how near it is to compacting. A station's detail reads its context
  window by category, as `/context` shows it, when you ask. Settings, Usage has tokens and an
  estimated API cost per project for the last week. Stations report through a status line the
  factory sets; the watch reads `/usage` every 10 minutes and `/context` when asked, without a
  model call. `tares factory plan` and `tares factory context <station>` read them by hand.
- Token usage per reply of every Claude Code session the plugin ships, counted once per reply and
  filled in from the replies already stored.
- The console's left sidebar folds to a rail of icons and back with its Collapse row; the choice
  is remembered in this browser. Start, Connect and Reads have icons of their own, Settings is a
  gear and the Workspace link an outside-link icon, so no two look alike when only icons show.
- On Tares Cloud, Settings > Linear has **Connect Linear**: you approve the GlassFlow Linear app in
  Linear and come back connected, with no OAuth application of your own to set up. The control
  plane hands the token to the workspace and renews it; Disconnect goes through it and revokes the
  token. `TARES_LINEAR_CONNECT_URL`, `PUT`/`DELETE /api/settings/linear-connection` (admin).

### Changed
- Linear on a self-hosted install connects with a personal API key only. Sign-in with your own
  Linear OAuth application is gone (`/api/linear/oauth-app`, `/api/linear/oauth/start`, the
  callback, `TARES_LINEAR_CLIENT_ID`); a workspace connected that way asks to connect again.
- Crew page: each project's builders fold, shut by default, behind a line that counts them and
  names any waiting or quiet one, so the Waiting for review list stays in view. The fold opens for
  the member you pick or link to, and the whole Projects pane folds to its header. Both are
  remembered in this browser.
- Crew page: each project shows its progress ring beside the merged count, which now leaves canceled
  tickets out as the ring does, and its usage line gives only its share of the 5-hour window and of
  the week, with no API prices. A station's detail says less: one context line and its bar, the
  window by category in a fold that reads it when opened, the newest 8 lines with the rest a click
  away, and no project or ticket section for the crew's own stations. `/api/crew/projects` rows
  carry `progress`.

### Fixed
- The attach command for a helper of a crew station (`crew-reviewer-lint`) names the helper, not
  a station that doesn't exist.

## [1.46.0] - 2026-10-14

Work on a factory project in your own Claude Code session with `tares factory session`, taking it
over from the crew when the crew holds it. Skills for building a software factory and an AI SRE,
a tidier Settings page, and Tares on Homebrew follows every release.

### Added
- A software-factory skill (`npx skills add glassflow/tares`) so coding harnesses suggest Tares for building a software factory with Claude Code.
- An ai-sre skill so coding harnesses suggest Tares for building an AI SRE and AI monitoring.
- `tares factory session`: work on a project in your own Claude Code session, bound to it on Tares,
  with the Tares plugin loaded (a plain `claude` has no Tares tools). `session <project>` works on
  a planned project and picks up a ticket left in progress; when the crew holds the project, it
  asks whether to take it over, and on yes the orchestrator stops its builders (they push what
  they have and write their progress) and releases it to your session. `session "<idea>"` plans a
  small project lightly (goal, short spec, tickets, checks) and builds it in the same session,
  offering the full spec and the hand-over to the crew when it turns out big. No argument asks
  which project. The spec's single-session hand-over now names this command (TR-520).
- `POST /api/projects/{id}/takeover` (the person only) and `/release` (the orchestrator, or the
  person's session), `releasing` in `/api/crew/projects`, the `release_project` MCP tool, and
  `finish_project(words=...)` for a person's own session, which also finishes a project the crew
  never held.

### Changed
- Plugin, marketplace and PyPI descriptions and keywords name both uses: a software factory for Claude Code and an AI SRE. Plugin 0.5.1.
- Settings: the tabs are a sidebar on the left, grouped into the workspace, Agents (model
  providers, limits, observability) and Integrations (GitHub, Linear, Slack). `?tab=` links are
  unchanged; the "Agents" tab is now "Limits". The API keys explanation says in plain words what
  each kind of key can do, and plain notices show in a box instead of as floating text (TR-521).
- Factory projects whose tickets live in Linear say so under the project name, with a link to the
  Linear project and the Linear logo, also on the Tickets section. A ticket's page links the
  issue from its first row instead of a subheading (TR-495).
- Crew appears in the console's sidebar as soon as `tares factory crew up` has an orchestrator
  running, before any project is handed over (TR-494).
- After a release, the Homebrew tap is pointed at the new PyPI version and the install is checked
  (TR-498, TR-499). The README shows `brew install` as the alternative to uv (TR-500).

## [1.45.0] - 2026-10-14

Sign in to a Tares Cloud workspace with `tares login`: the factory, `tares status` and the Claude
Code plugin then use it with no environment variables to set. Needs control plane 1.25.0, which
serves the page `tares login` opens; workspaces on 1.44.0 or later can be picked.

### Added
- `tares login` connects this machine to one of your Tares Cloud workspaces without environment
  variables (TR-490). It opens the console in your browser; you sign in and pick a workspace on
  Tares 1.44.0 or later, and this machine gets its own admin key on it, handed back to the CLI
  over localhost with a one-time code. The workspace and key are saved in `~/.tares/login.json`,
  readable only by you. Logging in again replaces the key. `--cloud` points at another console.
  `tares logout` removes the key from the workspace and deletes the file.
- `tares status` and `tares factory` use the saved login when no flag or `TARESD_URL` says
  otherwise (TR-491). The order everywhere: a flag, then `TARESD_URL` / `TARES_AUTH_TOKEN`, then the
  saved login, then the local daemon. `tares factory doctor` says which one it used, its messages
  suggest `tares login`, and `crew status` says when the crew still runs against the workspace it
  was started with.
- Claude Code plugin 0.5.0 (TR-492): outside the factory, session shipping uses the saved
  workspace and key while `tares_url` and `access_token` are left as they came, and the MCP
  connection presents the key when `mcp_url` is that workspace's `/mcp`. `tares login` prints the
  `mcp_url` to set.

## [1.44.0] - 2026-10-13

The software factory: a crew of Claude Code sessions on your machine builds every project you
hand it, from tickets to pull requests merged on main, and Tares keeps the plan, the ledger, the
reviews and your decisions. The crew CLI ships inside Tares as `tares factory`. Every session
now reaches Tares through one shared MCP endpoint instead of starting its own `tares-mcp`.
1.43.0 stays with the decision-model watcher (released as 1.43.0-rc.1 to rc.3); this release
does not contain it.

### Added
- `/tares:spec` turns an idea into a Tares project: it interviews you one question at a time,
  has a second model challenge the plan, and writes the spec, plan, milestones with acceptance
  checks, tickets with their dependencies and repos, working docs and an AGENTS.md. Docs and
  tickets live on the cell; tickets can live in Linear instead. `tares factory spec` opens it in
  the repo the project builds on.
- `tares factory crew up` starts one crew for every project: an orchestrator and a reviewer, a
  releaser only when asked for. The first run asks its settings in a box like Claude Code's and
  keeps them on Tares, offers to trust the crew's own folder, and starts a watch that revives
  stopped stations. The orchestrator starts builders per milestone and repo with
  `tares factory spawn`. `tares factory doctor` checks what the crew needs.
- The ledger: tickets carry a holder and a stage, the orchestrator assigns them and each holder
  moves its own. Pull requests are read from GitHub; a project can span several repos, and a
  cross-repo dependency holds a builder back.
- The build, recorded: assumptions, checks matched against what the session actually ran,
  project memory, subagent transcripts, review rounds with findings and verdicts tied to the
  commit, a review queue and brief, and recurring findings offered as rules. The Codex
  challenger can review each builder's commits.
- The desk: one list of questions for the person across every project, answered in the
  orchestrator's session. Answers are kept as typed and checked against what the person wrote;
  a reply of one option's letter ("B") is recorded as that option. A standing answer becomes a
  grant. When a project's work is done the orchestrator asks, and the person's yes marks it
  finished (`tares factory finish` does it from the terminal).
- Console: a Crew page (members, projects, a member's details, notices), and on a factory
  project page the build's steps, tickets by milestone, a GitHub section with each repo's pull
  requests, assumptions, decisions and the sessions that worked on it.
- The project page's results have a Show filter: everything, only findings (leaves out what had
  nothing to report), or one agent's conclusions, so a root-cause agent's notes are not buried
  under a watcher's quiet checks. Remembered per project in the browser.
  `GET /api/projects/{id}/results` takes `show` (`findings` or `agent:<name>`); today's counts
  stay the whole project's.
- An agent's budget can be set on the agent form (Advanced): what it may spend in all, with what
  it spent so far. Blank means no budget. Before, the form kept the value but had no field for it.

### Changed
- The Claude Code plugin (0.4.0) connects to Tares's MCP endpoint over HTTP instead of starting
  `tares-mcp` in every session. Locally, run `tares mcp --transport streamable-http` next to
  `tares up` (new plugin option `mcp_url`, default `http://127.0.0.1:8788/mcp`); on Tares Cloud,
  set `mcp_url` to `https://<cell>.tares-glassflow.com/mcp` and nothing needs installing. The
  session's station, role and project travel as `X-Tares-*` headers, so many sessions share one
  server. `tares factory` refuses to start sessions when the endpoint does not answer, and
  `doctor` reports it. `tares-mcp` over stdio still works for other MCP clients.

### Fixed
- `write_milestone` keeps the goal or checks an update leaves out.
- A session that exits shows as ended: the plugin reports the end before shipping the last
  lines, and a session silent past its window shows quiet instead of waiting forever.
- A build session started from Tares's own prompt joins its project, and the project page names
  an in-progress ticket only for the session working on it.
- The desk waits a few seconds for a reply Tares has not read yet before refusing it.
- The session picker in "Summarize a session now" shows each session's repo, branch and state in
  full: they sat to the right of the id and were cut off at the edge of the dialog, and the list
  was cut off under it. The label now sits under the id and wraps, and dialogs no longer clip a
  menu that opens inside them.

## [1.42.0] - 2026-10-06

A new cell opens on Start instead of Overview: a short list of what is ready, and the first
project's goal right under it. Slack picks a channel from every public channel, posts to any of
them without an invite, and one Slack workspace can serve several Tares workspaces (with the
Tares Cloud control plane 1.24.0). Released as 1.42.0-rc.1 to rc.4 first; this is rc.4 plus a
copy fix in Settings > Slack.

### Added
- Start (`/start`): the demo, a model provider (on Tares Cloud the included credit and what is
  left of it), GitHub and Slack, each ticking itself from the cell's state and every one
  optional; then the goal step for the first project. Connect GitHub and Connect Slack on Tares
  Cloud come back to Start. The demo starts from here, and shows Open and Remove once running;
  the demo's page points back to creating your own project. A row whose state is not known yet
  says "Checking…" in its place. The goal step's fields tell browsers and password managers not
  to fill them.
- The goal step asks for an optional name and description. `POST /api/setup/drafts` and
  `POST /api/projects/{id}/setup/plan` take `name` (kept as the project's name, over the
  planner's) and `description` (given to the planner with the goal);
  `GET /api/projects/{id}/setup` returns both for a draft.
- Projects shows the same list (the demo, model provider, GitHub, Slack, done ones ticked) until
  model provider, GitHub and Slack are done or it is hidden.
- Settings > Usage (self-hosted) holds model spend and storage; on Tares Cloud model spend
  joins Settings > Workspace.

- The plan screen offers "Ask Tares to fix it" when the plan still has a mistake of its own after
  Tares's own fix rounds; it hands what the check found back to the planner. Problems carry
  `who` (`tares` or `you`) on `POST /api/setup/check`.
- One Slack channel picker wherever Tares asks for a channel: the guided setup, the agent form,
  a project's subscribers and template forms. It is a searchable list; a private channel shows a
  lock. Under it, one line says how to add Tares to a private channel (`/invite @Tares`).
- `POST /api/slack/channels/changed` (admin; body `{"event", "channel"}`, both optional): drops
  the cell's cached channel list, so the next read asks Slack again. Tares Cloud calls it when
  Slack reports a channel joined, created, renamed or archived.
- `/tares ask <workspace> <question>`: on Tares Cloud, when the first word after `ask` is this
  workspace's name (the `workspace` parameter of `TARES_SLACK_CONNECT_URL`), it is dropped from
  the question. This is how Tares Cloud asks which workspace when one Slack is linked to several.
  Without `TARES_SLACK_CONNECT_URL` (self-hosted) nothing is dropped.

### Changed
- `GET /api/slack/channels` lists every public channel of the connected Slack, not only the ones
  Tares was added to, plus the private channels Tares is in. Each channel carries `is_member`
  next to `is_private`. The cell keeps the list for a minute, a failed read for a few seconds or
  as long as Slack's Retry-After asks. When a re-read fails, the last good list is served with
  `stale: true` instead of an error, and the picker keeps showing it. The picker reads the list again every few seconds while it is on screen, when the window regains
  focus and when the dropdown opens, so a channel Tares was just added to appears without a
  reload.
- No screen asks for a Slack channel ID any more. When there is nothing to pick from, the picker
  says why: Slack not connected (with a link to Settings, Slack), a missing permission (Reconnect
  Slack), Slack did not answer (the reason, and Try again), or no channel Tares can see. A channel
  already saved as an ID or a name keeps working and shows by name.
- Settings > Slack lists the bot scopes a pasted token needs: `chat:write`,
  `chat:write.public`, `channels:read`, `groups:read` and `commands`.
- The planner gets up to three rounds to fix what the check finds (was one), and is never asked
  about what only the person can answer, like which Slack channel.
- Labels the planner writes as an object (`{"location": "berlin"}`) become one label per key; the
  label error now shows the shape it expects.
- `/` opens Start while the cell has no project of your own (the demo, the default project and
  drafts do not count), and Projects after that. Start leaves the sidebar with the first project.
- The default project is made the first time something is created outside a project, not when
  the cell starts, so a cell where everything starts in a project never has one. Projects no
  longer lists it; when it holds something, a line under the list points to All resources.

### Removed
- Overview and the landing screen. Its "What do you want to build?" box handed the text to the
  goal step, which asked the same question again; the goal step is now the one place.

## [1.41.0] - 2026-10-02

Switch workspaces from the console, and connect GitHub and Slack from the workspace's own
Settings. Pairs with the Tares Cloud control-plane release 1.21.0 and ships after it; a
self-hosted instance looks and behaves as before.

### Added
- Workspace switcher: with `TARES_WORKSPACES_URL` set, the top left of the console shows the
  workspace's name and opens a menu of your workspaces (the default one marked, ones still being
  set up or suspended greyed out), Workspace settings, All workspaces, New workspace and Account.
  When the Tares Cloud session has expired it offers "Sign in to switch workspaces"; the console
  never waits for the list. The bottom "Workspace" link moves into this menu.
- Connect Slack: with `TARES_SLACK_CONNECT_URL` set, Settings > Slack offers Connect Slack, then
  shows which Slack workspace it is connected to and Disconnect, in place of the two paste boxes.
  `PUT` and `DELETE /api/settings/slack-team` store the Slack team, and
  `GET /api/settings/slack-bot-token` returns it as `team`.
- GitHub App on Tares Cloud: an `app_broker` credential carries the repositories this workspace
  follows (`repositories`, `owner/repo`; replaced when a create or update sends it, kept when it
  does not). Settings > GitHub lists them, with Pick repositories or Change repositories.
- Settings shows what happened when Tares Cloud sends you back from connecting or disconnecting
  (`cloud`, `cloud_detail`), once.
- `/health` reports `slack_connect_url` and `workspaces_url` when set, next to `workspace_url`
  and `github_connect_url`.
- Settings > Workspace: with `TARES_WORKSPACE_API_URL` set, a first tab manages the workspace
  without leaving it. It shows the plan, Tares version and state; the team, where the owner
  invites by email and removes members; storage used with the warning at 80%, which the owner
  grows within the plan; the included credit while a trial applies; "Open this workspace when I
  sign in"; and, for the owner, Delete workspace, which asks you to type the workspace name and
  then lands you in your next workspace. Members see the same, read-only. Signed out of Tares
  Cloud, the tab offers "Sign in to manage this workspace". Needs the matching Tares Cloud
  control-plane release first.
- `/health` reports `workspace_api_url` and `logout_url` when `TARES_WORKSPACE_API_URL` and
  `TARES_LOGOUT_URL` are set.

### Changed
- Members who are not the workspace owner see who can connect GitHub and Slack instead of
  buttons that would be refused. Personal tokens are unaffected.
- Connect GitHub, Change repositories and Disconnect bring you back to Settings > GitHub.
- The note that users, the Slack app, plan and storage are managed in the workspace is gone from
  Settings.
- Sign out on Tares Cloud: with `TARES_LOGOUT_URL` set, Sign out also signs you out of Tares
  Cloud and lands on its sign-in page, so the next visit asks you to sign in again. The sidebar
  shows the email you are signed in with. Self-hosted, Sign out works as before.
- With `TARES_WORKSPACE_API_URL` set, the switcher's Workspace settings opens Settings >
  Workspace, and All workspaces and Account leave the menu. New workspace stays.

### Fixed
- Disconnecting the Tares Cloud GitHub App from a workspace now goes through Tares Cloud, which
  stops its deliveries to the workspace. Delete used to remove only the workspace's copy, so
  events kept arriving.

## [1.40.0] - 2026-10-02

### Changed
- Agent traces carry `user.id` (the instance, so one user per cell in Rius's Users view) on every
  span, `rius.main_agent.name` beside `gen_ai.agent.name`, and `service.version` on the resource.

## [1.39.0] - 2026-10-02

The GitHub connector: a personal token or the GitHub App, and agents that act on your
repositories. Ships with the Tares Cloud release that adds Connect GitHub.

### Added
- GitHub App: **Settings > GitHub > Create GitHub App** (GitHub's manifest flow), then install it
  on an organization. Every event of every repository it is installed on arrives as it happens in
  one `github` source: pull requests, pushes, reviews, comments, issues, releases, CI runs. The App
  credential mints and refreshes installation tokens; on Tares Cloud an `app_broker` credential
  gets them from the control plane and the cell holds no key (`TARES_GITHUB_CONNECT_URL` shows
  "Connect GitHub").
- Personal token sources report pull requests opened, merged or closed next to commits (`prs`).
  Both ways produce the same event types, keys (`owner/repo#number` for pull requests) and labels
  (`repo`, `action`, `number`, `author`, `branch`, ...).
- Webhook signatures: GitHub, Linear and HMAC-SHA256, checked on the raw body. A source that
  checks signatures needs no Tares key on its ingest URL; the generic webhook connector opts in
  with `signature_scheme`.
- Agents on GitHub: a credential per agent and read or read-and-write access through GitHub's MCP
  server, set up by Tares; with the App, a `github_create_check_run` tool posts the verdict as a
  check run with line notes.
- The guided setup opens with a goal from your GitHub repositories, and the planner knows GitHub's
  event names (a merged pull request is `event_type pull_request`, `action merged`).
- Trigger filters take `in` (a list, case-insensitive).

### Changed
- The `github` connector is labelled "GitHub". Upgrading adds `event_type = commit` to every
  trigger on a token source that had no event filter, so it keeps meaning what it meant.
- Shared code context: with the App it uses the App's source instead of a source per repository,
  and offers "every merged pull request".

## [1.38.0] - 2026-10-02

Projects are the unit, parts are shared between them, a guided setup starts a project from a goal,
and an agent's run ends the way you set. Released as 1.38.0-rc.1 to rc.8 (entries below) and run
on glassflow-web before this release.

### Added
- Projects: a Default project on every cell, a goal and a one-sentence outline per project, a
  health line, today's totals, What the agents found (one result per chain, with a headline and a
  next step, markable as handled) and Activity, one thread per firing with what it led to.
- Parts are shared: sources, triggers, agents, MCP servers and skills belong to the cell and any
  number of projects use the same one. A project holds its wiring (which trigger wakes which agent,
  on or off, and the handoffs); a run belongs to the project whose wiring started it. All
  resources lists every part with the projects that use it (`GET /api/resources`); the catalog
  export carries each project's `wiring:`.
- Guided setup (Projects, Create new): goal, plan, connect, try it. The plan is written in the
  background as a draft on the cell, reuses parts already there, and runs once as practice with
  "Run it once now". Drafts can be finished later or deleted.
- Skills: instructions agents load when a task matches, shared across projects.
- Handoffs: an agent names, per verdict, the agent that takes over on the same entity. An agent
  can have no trigger of its own, so only a handoff starts it.
- How a run ends: "Ends every run with a conclusion" (`concludes`) and the verdicts an agent may
  give (`verdicts`), enforced in the run; the handoff verdict is a dropdown of them, and the runs
  table has an outcome column.
- Project keys and outside agents that join a project, read it and record findings in it.

### Changed
- Views are gone: a trigger names its own `sources`, `filters` and `key_field`. Upgrading moves
  each view into the triggers that used it and places every object in a project.
- Deleting a part removes it from every project; the agents a deleted trigger woke stay, without
  a trigger. Pausing or deleting a project keeps the parts another project uses.
- The console names sources and tools by what they watch or call, with the internal name
  underneath.

### Fixed
- Repairing a template's deleted trigger gives it back to the template's agents.

## [1.38.0-rc.8] - 2026-10-01

Eighth release candidate: fixes from a run through every template.

### Changed
- Guided setup, Try it: the button is "Run it once now" (was "Run a practice spike"), and while
  the run works its line shows a spinner and the seconds so far.
- A template's Slack channel (the Challenger workflow's) is picked from the channels the Tares bot
  is in, like an agent's, instead of typed as an ID.
- From existing objects: each source in the picker shows what it watches (its repo) and can be
  found by it; tools show their service.
- The AI SRE demo and Challenger templates ask for a model provider, not an Anthropic key, and
  point to What the agents found instead of a Runs list that is no longer on the page.
- Two sources of a project on the same host (Prometheus metrics and its alerts) are told apart by
  their kind in Setup and All resources.
- All resources shows an agent that only a handoff starts as "on handoff", not "off".

## [1.38.0-rc.7] - 2026-10-01

Seventh release candidate: drafts can be deleted.

### Added
- "Delete draft" on a draft's row in Projects and at the foot of its setup page. Only the draft
  goes; nothing of it was set up yet.

### Fixed
- The agent form's trigger menu opened behind the prompt box; it opens over it now.

## [1.38.0-rc.6] - 2026-10-01

Sixth release candidate: how an agent's run ends is a setting, and deleting a part is final.

### Added
- How it ends, on the agent form: "Ends every run with a conclusion" gives the agent the
  conclude tool whatever its prompt says, and Tares adds the instructions for it. A run that
  stops without concluding is asked once more, with conclude the only choice. An agent whose
  prompt mentions conclude keeps working as before.
- Verdicts it can give: each a word of your own with when to give it. The conclude tool accepts
  only these, and a finding must carry one; any other word goes back to the agent to fix within
  the run. The triage and root-cause presets come with theirs.
- A handoff's verdict is a dropdown of the agent's verdicts that still takes free text, and the
  form says when a handoff names a verdict the agent never gives.
- The runs table has an outcome column for agents that conclude: the verdict, no verdict, or no
  finding, with the headline beside it; an open run shows the same above its finding.
- `concludes` and `verdicts` on agents in the API and in catalog export and import.

### Changed
- An agent can have no trigger of its own: only a handoff starts it. The agent form offers "No
  trigger", and the trigger can be changed after the agent is made.
- Deleting a trigger, agent, MCP server or source drops it from every project's list (no
  "deleted by hand" banner). The agents a deleted trigger woke stay, without a trigger; a deleted
  MCP server leaves the agents that listed it. Cells where a part was deleted before are mended
  when Tares starts.

## [1.38.0-rc.5] - 2026-10-01

Fifth release candidate: plain names and copy, from a UI review of rc.4.

### Changed
- Sources show what they watch (glassflow/argus-core) with the internal name small underneath, on
  Sources, a source's page, All resources and the setup plan; the planning steps name them the
  same way. A pasted GitHub URL is shown as owner/name. Tools show the service (GitHub).
- A wake-up on another agent's findings reads in plain words ("watcher flags a service for a
  closer look"), and Setup lists that agent after the one whose findings wake it.
- In the setup plan, a wake-up on every event no longer shows a "0 events" count and window; a
  tool already on Tares is on (Remove drops it) instead of carrying an off switch; the plan's
  notes are a plain panel, not a warning.
- Projects and Overview show each project's goal instead of the template it started from.
- Settings, API keys hides revoked keys behind "Show N revoked"; workspace sign-in keys
  (`user:<id>`) are labelled as such there and on All resources.
- A paused project says its agents look once it is resumed; page subtitles are full sentences;
  a template's panel (the demo stack) takes the full row so its URLs no longer break mid-word.

## [1.38.0-rc.4] - 2026-10-01

Fourth release candidate: parts are shared between projects, and the guided setup keeps a draft.

### Changed
- Sources, triggers, agents, MCP servers and skills belong to the cell, and any number of
  projects use the same one. A project holds its own wiring: which trigger wakes which agent, on
  or off, and which agent digs in on whose verdict. The same agent can serve two projects, each
  waking it its own way. A part's `owned_by` now means the project that made it, never a lock.
- A run belongs to the project whose wiring started it: each project's timeline, results, today
  totals, spend and an agent's runs and stats on its page show its own runs only. Two projects
  wiring the same trigger to the same agent run it once, shown in both. A second project's firing
  or handoff for an agent already running on the same entity joins that run.
- Pausing a project turns its wiring off; a trigger another running project uses keeps running.
  Deleting a project keeps every part another project uses (`kept_shared` in the response) and
  deletes the rest it made. `delete_sources=all` is unchanged.
- Skills are shared: one skill per cell, used by the projects that pick it. Editing it changes it
  everywhere; removing it from a project deletes it only when no project uses it. A new skill
  cannot take the name of one already on the cell.
- The guided setup starts a draft project on "Plan it": the plan is written in the background
  and shown step by step as it happens, edits are kept on the draft, and the draft waits on the
  Projects list ("Finish setting up") until it is set up, under the same id.
- The setup plan picks a source by kind first (your sources of that kind, or a new one), shows
  sources and tools by what a person calls them (glassflow/argus-core, GitHub), offers agents,
  tools and skills already on the cell from a list, and posts findings to Slack itself (a channel
  picker) instead of suggesting a Slack MCP server.

### Added
- All resources: a page listing every source, trigger, agent, MCP server, skill and key with the
  projects that use it (`GET /api/resources`).
- `wiring:` in the catalog export (each project's wake-ups and handoffs); import restores it.
- `POST /api/setup/drafts`, `POST /api/projects/{id}/setup/plan`, `PUT /api/projects/{id}/setup`
  with a plan, `POST /api/projects/{id}/skills/{name}/use`, `GET /api/skills`, and `?project=`
  on the agent list, its runs and enable/disable.

### Upgrade
- Once, at the first start: each agent's trigger, handoffs and on/off (its `tares://agent/`
  subscription) become the wiring of the project that made it, in one transaction; each
  project's skills become shared skills (the same name with other text keeps both, the second
  renamed name-2). Runs and firings keep their project.

### Fixed
- Page header buttons stay beside the title; the setup plan wraps long names inside their card;
  switching who does the work no longer flashes the other side's problems; the on/off switch
  looks like one; a plan label given as a bare word reads the field of that name.

## [1.38.0-rc.3] - 2026-09-30

Third release candidate: fixes from testing rc.2 on a live cell.

### Fixed
- The first start after an upgrade that fills template trigger descriptions no longer crashes
  (`RuntimeError: no running event loop`): the fill re-reads the catalog without starting sources.
- A project whose own agent connects with its project key and checks in (never subscribing a
  webhook) counts as having an agent: health no longer says "No agent is set up", the Setup page
  lists the agent, and the project sentence says it sees what happened when it checks in. Until the
  key is used, health warns that the agent has not connected yet.
- Agents the guided setup plans are told to finish with `conclude`, so their results carry a
  headline and a next step. A headline or summary derived from a note skips the narration it opens
  with ("I have a complete picture.").
- A practice run on a source Tares already had starts from the newest event the wake-up's filters
  let through, not the newest event of any entity, and its timeline reaches back to that event.
- "Looked at today" leaves out firings no agent was on.
- The own-agent check in the guided setup accepts a key made in place of the first one, and no
  longer mentions subscribing for an agent that checks in; that key's finding answers a practice
  firing too.
- Console: unknown addresses show a not-found page and `/views` goes to the projects; the pause
  dialog warns when a source is shared with another project; the delete dialog lists skills;
  "Marked as handled" no longer shows a machine id; the paused banner no longer repeats "Paused";
  the redirect for a missing agent, trigger or firing reads correctly; going from one source page
  straight to another shows the new source.

## [1.38.0-rc.2] - 2026-09-30

Second release candidate: plain descriptions on triggers, so the project sentence says what wakes it.

### Added
- A trigger can say what wakes it in plain words: an optional `description`, one line of at most
  160 characters phrased to follow "When" ("an alert fires for the checkout service"). The project
  sentence, the "Wakes when" card and the guided setup plan use it instead of the rule's own
  wording. `POST` and `PUT /api/triggers`, the trigger list, catalog export and import, and the MCP
  `create_trigger` and `update_trigger` tools take it; an update that leaves it out keeps it, an
  empty one clears it. The console's trigger editor and the setup wake-up editor have a field for it.
- The guided setup asks the model for a short plain description of each wake-up and stores it on
  the trigger.
- The AI SRE demo's trigger reads "Prometheus fires an alert for the demo service", the Rius RCA
  trigger "Rius sends an alert for a service", the challenger workflow trigger "a challenger
  session in Claude Code ends". Projects made from these templates before get the description once
  at start, when their trigger has none.

### Changed
- A trigger over a number reads more naturally without a description: "the total alert_active
  across any of its 3 sources goes above 0 in 1 minute for one service" instead of "the total
  alert_active of any of its 3 sources events goes above 0 ...".

## [1.38.0-rc.1] - 2026-09-30

Release candidate: projects are the unit, the goal-first project page and the guided setup, for testing on a cell before 1.38.0.

### Added
- Every cell has a default project. It is created at start when missing and holds everything no
  other project does: a source, trigger, agent or MCP server made without naming a project, and
  anything a project lets go of. It cannot be deleted. `GET /api/projects` lists it first, with
  `default: true`.
- A trigger, agent or MCP server belongs to exactly one project. `POST /api/triggers`,
  `/api/agents/builtin`, `/api/mcp-servers` and `/api/sources` take `project` (the default project
  when it is left out), and the list endpoints report it. An agent runs on a trigger of its own
  project and uses that project's MCP servers; anything else is refused with a 400 naming it.
  Moving a trigger to another project (`PUT /api/triggers/{name}` with `project`) takes its agents
  along.
- Sources are shared: a source can be in any number of projects. `GET /api/sources` lists them
  under `projects`. `POST /api/projects/{uid}/sources` adds an existing source to a project and
  `DELETE /api/projects/{uid}/sources/{name}` takes it out, refused while one of the project's
  triggers reads it. A trigger's sources always join its project.
- `POST /read` takes `project` and `sources` to read only those sources. A Tares agent's `read`
  and `stats` tools cover its project's sources unless it names others.
- A custom project may start empty and be filled as its objects are made. Saving its object list
  moves the triggers, agents and MCP servers it names out of the default project (with what they
  need: an agent's trigger, a trigger's agents and sources) and refuses one that belongs to another
  project, naming that project.
- Every agent run records what woke it (`woken_by`: trigger, schedule, manual, rerun, bootstrap
  or handoff), the run it repeats (`parent_run_id`) and its project. A firing records its
  trigger's project and, when a finding tripped it, the run that wrote that finding. A finding
  event carries `run_id` and `dispatch_id` in its payload and labels.
- `GET /api/projects/{uid}/timeline` lists what happened in a project, newest first, one thread
  per firing or per run that no firing woke. Each thread holds the firing (trigger, entity, an
  excerpt of what it carried, where it was delivered) and its runs, and each run holds what it led
  to: its reruns, and the firings its finding tripped with the runs they woke. A watcher's alert,
  its finding and the root-cause run that finding started read as one thread. Filters: `trigger`,
  `agent`, `outcome`, `entity`; page with `before` set to the previous page's `next_before`.
- Skills: a project holds named instructions (a playbook, a runbook, a house style) its agents load
  when a task matches. A skill is a name (lowercase letters, digits and dashes), a one-paragraph
  description of up to 1024 characters and a markdown body of up to 64 KB. When an agent's project
  has skills, its system prompt ends with a "Skills available in this project" list of names and
  descriptions, and it gets a `skill` tool that returns a body; a project with none leaves the
  prompt and tools as they were. A run records the skills it loaded (`skills` on the run), and the
  runs table shows them. Routes: `GET`/`POST /api/projects/{uid}/skills`,
  `GET`/`PUT`/`DELETE /api/projects/{uid}/skills/{name}`, and
  `POST /api/projects/{uid}/skills/upload` with a SKILL.md as the body (front matter with `name`
  and `description`, then the body). Writing a skill needs the admin scope, reading one the read
  scope. Skills go with their project when it is deleted, are exported and imported under a
  top-level `skills:` section (each with its `project` by name), and a template may plan one.
  The project page has a Skills tab: list with the agents that loaded each skill in the last 7
  days, new skill with a markdown preview, upload a SKILL.md, edit, delete. Ask never gets skills.
- Handoffs: an agent names, per verdict, the agent that takes over when it concludes. `handoffs`
  on an agent is a list of up to ten `{verdict, agent, cooldown}` (the verdict one lowercase word,
  the agent another agent of the same project, the cooldown a duration, 30m when left out). When
  a run ends with a finding whose verdict matches one (case does not matter), that agent is
  started on the concluded entity, handed the finding with a line naming who handed it off, the
  verdict and the entity. The run is `woken_by: handoff` with the finishing run as its parent, and
  the project timeline shows it under that run. It runs whether or not the agent is on for its
  own trigger. A chain stops after three handoffs in a row: the next is a capped run, "handoff
  chain stopped at depth 3". Within the cooldown for the same agents and entity no run starts and
  the finishing run's results say "handoff to X skipped: cooldown". An agent already running for
  the entity, the daily cap and the budget each leave a capped run with the reason. The API and
  the catalog take and return `handoffs` (an update without the field keeps them, an empty list
  clears them); a template may plan them. Deleting an agent removes the handoffs to it, and the
  delete returns `handoffs_removed_from`. The agent form has a "When it concludes" section
  (verdict, an agent of the project, cooldown), and the project's Setup tab lists each agent's
  handoffs. A trigger over the findings source still wakes an agent as before; handoffs are the
  simpler way to chain two agents. The AI-guided builder proposes them: for "one agent looks
  first, another digs in on its verdict" it proposes the second agent left off for its trigger,
  then the first with a handoff to it. An agent that only runs on a handoff says so on its page.
- Project keys: a key that belongs to one project. It reads that project and records findings in
  it, and nothing else. `POST /api/projects/{uid}/keys {name}` makes one, as does
  `POST /api/keys {name, project}`; its scopes are `read` and `findings` (a read-only one takes
  `scopes: ["read"]`). With a project key, `/read` and stats cover the project's sources, and the
  shared findings and memory sources only with the project's own rows; `/catalog`,
  `GET /api/projects` and `GET /api/projects/{uid}` show that project only (without its template
  parameters); its skills, timeline and findings are readable, another project's are not. Every
  other route answers 403 "this key only reads project <name> and records findings in it",
  including a route added later, since the routes a project key may use are an explicit list.
  `GET /api/whoami` names the key's project. Revoking a key removes its subscriptions; deleting
  the project revokes its keys. Keys are enforced when the instance has `TARES_AUTH_TOKEN`, like
  every other key.
- Joining a project: `POST /api/projects/{uid}/subscribe {url}` (a project key or admin) POSTs
  every firing of every trigger of the project to the URL, the triggers added later included;
  `DELETE /api/projects/{uid}/subscribe/{sid}` removes it (a project key only its own). A
  project key cannot point the URL at an internal address (loopback, private ranges, link-local
  such as cloud metadata); `TARES_WEBHOOK_ALLOW_PRIVATE=1` allows it for a cell whose agents
  share its network.
  `POST /api/projects/{uid}/findings {entity, finding, verdict?, label?, headline?, next_step?}` records an external
  agent's finding under the key's name: it lands on the entity's timeline like a Tares agent's,
  and shows in the project timeline as a thread of kind `run` with `external: true`.
  `GET /api/projects/{uid}/findings?entity=&agent=&limit=` lists the project's findings, Tares
  agents' and external ones, and `POST /api/projects/{uid}/stats {by, window, where?}` counts
  per label value over the project's sources. `GET /api/projects/{uid}/external-agents` (admin)
  lists who joined: the webhook (masked), the key, the last delivery.
- MCP tools for an agent working inside one project: `list_projects`, `join_project(url)`,
  `stats`, `list_skills`, `get_skill`, `list_findings`, `record_finding` and `project_timeline`.
  Each takes an optional `project`; with a project key it is implied, otherwise it names the
  project (an id or a name) and may be left out while there is only one.
- The project page has a Keys section on Setup (create, the secret shown once, revoke) and an
  External agents list on the Agents tab (where it delivers, the key, the last delivery, revoke).
  The Activity tab marks an external agent's finding as recorded from outside Tares.
- A project has a goal: one line of at most 200 characters saying what it is for.
  `POST /api/projects` takes `goal`, and `PUT /api/projects/{uid}` with only `goal` sets or clears
  it on any project, the default one included. The project endpoints and the summary return it,
  and the catalog export and import carry it. A project made from a template without a goal gets
  the template's own (every built-in template has one); the AI-guided builder's project card can
  propose one.
- `GET /api/projects/{uid}/outline` says how a project works in plain sentences built from its
  configuration, with no model call: "When checkout-errors gets more than 5 events in 5 minutes
  for one service, checkout-triage looks first. If it concludes investigate, checkout-rca digs
  in." It lists what the project watches (with each source's state), when it wakes (with the
  cooldown), its agents (first look or handed off to) and its skills.
- An agent's `conclude` takes `headline` (one line, what was found) and `next_step` (what a person
  should do, empty when nothing needs doing), and agents are told to give both. Runs and finding
  events carry them. For a run without them they are taken from the note: its heading, bold lead
  or first sentence, and the text after "Next step:", "Next:" or "Recommendation:".
- `GET /api/projects/{uid}/results` lists what the agents found, newest first: one result per
  chain of runs (a triage that handed off is not a result, the run it handed to is), marked
  `action` or `no_action`, with headline, summary, next step, the agents in the chain, its cost
  and time, and today's totals (since midnight UTC). External agents' findings are results too.
  Page with `before`. `GET /api/projects/{uid}/results/{run_id}` adds the full note and the steps
  that led to it, in plain words. `POST /api/projects/{uid}/results/{run_id}/handled` marks a
  result handled or clears the mark (admin).
- `GET /api/projects/{uid}/health` says whether a project is working, needs attention, is paused
  or is still setting up, in one message, with what needs attention first: sources that fail
  (several with the same error are one issue), stay silent far longer than usual or never
  received anything, no agent that can run, no model provider, an agent at its daily cap or
  budget.
- A project key may read its project's results, outline and health.
- Guided project setup. `POST /api/setup/plan {goal, who?, existing_sources?}` answers a whole
  plan in plain words, written by the cell's model provider through a forced `propose_plan` tool
  (it may read the connectors, sources, source fields and templates first): what the project
  watches (new sources, or existing ones reused), when it wakes, who does the work (Tares agents,
  or the person's own agent joining with a project key), outside tools (MCP servers, off until
  turned on) and know-how (skills). The plan is checked with the catalog's own validators; a
  wrong plan gets one retry with the errors, then a 422 with a plain message. No model provider:
  409. `POST /api/setup/adjust {plan, instruction}` revises it. The numbers a person tunes are
  knobs (threshold, window, cooldown): the condition, cooldown, sentences and summary are derived
  from them without the model, so an edited plan applies exactly.
- `POST /api/setup/apply {plan}` creates the project with its goal and name, then its sources,
  MCP servers (enabled tools only), skills, triggers and agents (a handoff-only agent left off for
  its trigger), all owned by the project; a failing step undoes everything and says which step.
  For the person's own agent it makes a project key (read and findings) instead of agents. The
  answer says what only the person can do: each source's ingest URL and example event, tools that
  need a token, and the agent's key (shown once), MCP URL and Claude Code command.
  `TARES_PUBLIC_URL` sets the address these URLs use (default: the request's), `TARES_MCP_URL`
  the MCP address (default: that address plus `/mcp`).
- `GET /api/projects/{uid}/setup` answers the setup step, the plan and live checks: each source
  waiting, receiving (with the fields seen) or in error, each tool untested, ok or in error (from
  its last test), and whether the own agent has used its key and subscribed. `PUT` with `{step}`
  moves the step (connect, try, done). A project lists `setup: {step, practice_run}` when it was
  set up this way. `POST .../setup/test-event {source}` stores the plan's example event in a push
  source, labelled `practice=true`, without waking any trigger. All setup routes need admin; a
  project key cannot reach them.
- Practice runs. `POST /api/projects/{uid}/setup/practice` runs the first agent that looks once
  on the example event's entity (woken by `practice`), or, for the person's own agent, sends a
  practice firing (`practice: true` in the body) to the project's subscriptions; the next finding
  its key records within 10 minutes is practice. Runs (and firings) carry `practice`; a practice
  run's handoffs are practice too. Results show them with `practice: true`; today's totals and the
  daily cap check in health leave them out.
- `POST /api/setup/check {plan}` (admin, no model call) answers `{plan, problems}`: the plan
  normalized exactly as apply does it, with the wake-up, cooldown and agent sentences and the
  summary derived from the plan as it now is, and what stops it from applying per item
  (`{where: "watches.w1" | "wakes.k1" | "agents.a1" | "tools.t1" | "skills.s1" | "own_agent" |
  a section | "plan", message}`) in plain words. Apply accepts any plan check accepts. A plan
  tool may name an MCP server already on the cell (`existing: true`), and a plan agent a
  `provider`. The apply answer carries the plan as the project keeps it: a secret typed into a
  new source's settings is left out of the stored plan.
- The guided setup's Plan screen is editable in place, one part at a time. Watches: change a
  source for one already on Tares (with its kind and when it last received an event) or a new one
  (its connector and settings, the source form), remove one or add another; a swapped source
  follows into the wake-ups that counted it, and a wake-up left with no source is flagged. Wakes
  when: what to count (events, or the average, highest, lowest or total of a number), the
  threshold and window, "only when" conditions built from the sources' real fields, counted
  separately for each of a label, or every so many minutes instead; add or remove a wake-up.
  Agents: switch between Tares agents and your own agent; each Tares agent's name, when it runs
  (a wake-up, or only on a handoff), instructions, model, tools and handoffs; add one from the
  agent presets, or remove one. Your own agent: its name and whether Tares calls its webhook or
  it checks in. Know-how: copy a skill from another project, or remove one. Tools: attach an MCP
  server already on Tares. The plan is checked after every edit; problems show next to the part
  they concern, and "Looks right, set it up" waits until there are none.

### Changed
- The guided setup's summary is derived from the plan as edited, never the model's own text, and
  names the person's own agent once: "your agent is told" for a name like "your own agent", "your
  agent claude-code is told" otherwise (it read "your agent your own agent is told"). A new
  polled source whose connector takes a secret and has none yet needs a credential; one with its
  secret filled in needs nothing.
- The store writes its startup migrations into the database file before serving (a checkpoint).
  Upgrading a cell from 1.37.0 and then stopping it hard (a pod killed) could otherwise replay the
  new `agent_runs` columns into a corrupted index: every request then failed with "database has
  been invalidated" until a restart.
- Every write needs the admin scope, except `/read`, `/subscribe`, `/unsubscribe`, a project's
  `stats`, the label preview (a read key), recording a finding (the findings scope) and ingest.
  A read key could create and delete projects, add MCP servers and GitHub credentials, and pause
  or repair projects; those now answer 403. A route added later needs admin unless listed.
- A firing's webhook body carries `project` (the trigger's project id) and `run_ids` (the Tares
  agent runs the same firing started).
- `POST /read` with `project` also reads the shared findings and memory sources, only the rows
  recorded in that project. A finding records its project in its payload.
- Views are gone. A trigger names the sources it watches, the filters that narrow them and the
  label its entity is keyed by (`sources`, `filters`, `key_field`; the key defaults to the first
  source's primary label). `/api/views`, `POST /query` and `POST /derive` answer 404, and a
  trigger that names a `view` is refused with 400 "views were removed: give the trigger `sources`
  (and `filters`)". The MCP `query`, `derive` and `update_view` tools are gone: read with `read`,
  and `create_trigger` / `update_trigger` take `sources`, `filters`, `key_field` and `project`. A
  Tares agent's `stats` tool counts over sources instead of a view and its `query` tool is gone.
  In Ask and the project builder, a trigger card names its sources; there is no view card.
- Deleting a project deletes its triggers, agents and MCP servers, a custom project's too. Its
  sources stay, since other projects may read them, unless `delete_sources=a,b` names them: each
  is deleted when no other project uses it, and kept and listed under `kept` when one does. A
  source left in no project joins the default project. `delete_sources=all` names every source of
  the project. Callers that relied on a template project taking its sources with it (Rius, for
  one) now pass `delete_sources=all`.
- The catalog export names each trigger's `project`, `sources`, `filters` and `key_field`, each
  agent's and MCP server's `project` and each source's `projects`, and never a `views:` section.
  The default project is listed under `projects:` only when it has a goal, and only its goal is
  imported; objects that name it go to the importing cell's own.
- The project page's Firings tab is now Activity: one row per thread with the agents that ran and
  how each ended, opening in place to the firing, its deliveries, each run's finding and what the
  run led to, with filters and "Load older". `?tab=firings` links still open it.
- The `triage` preset says a finding with verdict investigate hands the entity off. The
  `rca-from-triage` preset is labelled "Root cause after a handoff" and asks for the key and label
  of the entity it was handed.
- `tares status` shows projects instead of views. The activity log of reads reports `scope`
  (what was read) instead of `view`.

### Upgrade
- The first start after upgrading folds every view into the triggers that used it: each trigger
  takes its view's sources, filters and key field, then the views table is dropped. A trigger
  whose view was already gone is paused with no sources, so it shows up to be fixed or deleted. A
  schedule trigger keeps its last tick, so it does not fire early.
- The same start places everything in a project: a trigger with none goes to the default project,
  an agent with none follows its trigger, an MCP server with none goes to the project whose agents
  use it (else the default one), a source in no project joins the default one, and a trigger's
  sources become members of its project. Objects a template or custom project owned stay there;
  a custom project's list drops its views. The pass runs at every start and changes nothing the
  second time.
- Runs and firings from before lineage get their project from their agent's or trigger's owner,
  when it still exists. What woke them stays unknown.
- The first start gives each project made from a template its template's goal. Custom projects
  and the default project start without one. This runs once, so a goal cleared later stays
  cleared.
- New columns, added at start: `usecases.setup`, `agent_runs.practice`, `dispatch_log.practice`.
  Every earlier run and firing reads as not practice.
- A catalog exported while views existed still imports: each trigger that names a view gets that
  view's sources, filters and key field, a custom project's view entries are dropped, and objects
  without a project go to the default project.

## [1.37.0] - 2026-09-29

### Added
- The `rius_rca` template takes a `daily_cap`: runs per rolling 24 hours for its agent, so the Rius
  control plane can size each workspace's RCA by plan. It is set on create and changed with
  `PUT /api/projects/{uid}`; without it the agent uses the cell-wide cap (Settings, Agents), as
  before. An agent's own cap is kept when its settings are edited in the console, and a run it
  caps says to raise it in the project.

### Changed
- A Tares finding in Slack is posted whole when it fits in one message block; Slack folds it
  behind "Show more". Only a note longer than that gets an excerpt in the channel and the full
  note in the thread. In 1.36.0 any note longer than its excerpt was threaded, so a one-paragraph
  root cause appeared twice.

### Fixed
- Clicking a button on a Tares message in Slack no longer shows "This app is not configured to
  handle interactive responses". `/api/slack/events` acknowledges the click, so a Slack app
  pointed at a cell can enable Interactivity with that same URL as its Request URL.

## [1.36.0] - 2026-09-29

### Changed
- Slack messages from Tares read like an alert, not a wall of text. An agent's finding shows the
  entity as a header, the agent, trigger, verdict, model and time as fields, a short excerpt (the
  note's own summary when it has one) and View in Tares / Open timeline buttons; the full note goes
  in the message's thread. A short note is posted whole. A trigger firing shows the trigger as a
  header, the entity and time as fields, the payload, and a View firing in Tares button. Buttons
  appear when the cell has a public address (`TARES_PUBLIC_URL`).
- Wide markdown tables in Slack become one line per row instead of a code block that wrapped
  badly; narrow ones stay aligned. Long notes no longer split inside a code block, which showed a
  stray ``` in Slack.
- The `rca-from-triage` preset opens its note with a one-sentence conclusion, which is what a chat
  notification shows.

## [1.35.0] - 2026-09-28

### Added
- Runs record what they produced: a pull request opened, a commit pushed, a Slack message posted,
  a write-back delivered. Each is read off the run's own tool calls and deliveries, so an agent
  cannot claim work it did not do, and shows as a chip in the runs table (on agent and project
  pages), linked where there is a URL. An agent that ends with `conclude` can add `produced` lines
  for anything Tares cannot see.
- Watch, then escalate: two prompt presets for a two-agent chain. `triage` runs on a schedule
  trigger, reads the window's counts and ends with `no_op` or a finding marked `investigate` on the
  entity that looks off (only values up at least 3x and by at least 50 events are considered).
  `rca-from-triage` is woken by those findings, fetches its own evidence and writes a root-cause
  note on the same entity.
- An agent can now be woken by findings through a view filtered to another agent's findings
  (`agent eq <name>`), which is how one agent hands off to the next. A view over all findings, or
  over the agent's own, is still refused, since the agent would wake itself forever.
- Schedule triggers: a trigger can fire every N minutes on a view instead of on a condition
  (`condition: {every: "10m", summary_by: [service, status_code]}`, or "on a schedule" in the
  console). It fires once per interval for the whole view, even when nothing happened, and hands
  the agent it wakes a summary of the window: per label, the counts against the window before,
  plus a few recent lines. Pausing works as for any trigger, and a restart does not fire a tick
  twice. Condition triggers are unchanged.
- Tares agents have a `stats` tool: counts per value of one label through a view, for the last
  window against the window of the same length before it, largest change first, capped to the top
  rows. An agent can see which services, status codes or paths moved on a busy stream without
  reading lines, where `read` and `query` return only the most recent events per source.
- A Tares agent can end a run on purpose with the `conclude` tool: `no_op` when there is nothing to
  hand on (the run is a success with no finding, no Slack post and no write-back), or `finding`
  with a `verdict` and the entity the finding is about. The verdict is a label on the finding, so a
  view can select one agent's findings with one verdict, and another agent's trigger can wake on
  them. The tool is offered only to an agent whose prompt names it; every other agent behaves as
  before. The runs table shows a no-op run as "no finding" and a finding's verdict next to its
  status.
- Runs per agent per day is a console setting (Settings, Agents). A value saved there replaces
  `TARES_AGENT_DAILY_CAP`; clearing it goes back to the environment value, then to 50. It applies
  from the next run, with no restart, and a capped run's message says where to raise it.

## [1.34.0] - 2026-09-28

### Added
- On-demand firings on the ingest path. A delivery to `POST /ingest/{token}` carrying
  `X-Tares-Bypass-Cooldown: true` (or `1`) wakes every trigger over that source for the keys the
  delivery carries, even for a key inside its cooldown, so a person can ask for one analysis now
  instead of waiting the interval out. It is not a switch that turns the cooldown off: the firing
  records its time as usual, so the next unmarked event for that key waits the full cooldown
  again. The header carries no authority of its own, ingest still needs the `ingest` scope, and it
  works for any push source with no change to a project or its template (RIUS-687).

### Changed
- Agent traces follow the GenAI conventions more closely. Every span of a run carries
  `gen_ai.agent.name` and `gen_ai.agent.version`, so one agent reads as one agent across instances.
  `session.id` is now one run: the firing's delivery id when the agent reports one, else the
  trigger dispatch, else the run. Before, it was the entity key, which made one session of every
  run an agent ever made on that entity.
- LLM spans carry `gen_ai.tool.definitions`. Tool spans carry `gen_ai.tool.call.id`,
  `gen_ai.tool.call.arguments` and, when the call failed, `error.type`. The root span carries the
  waking firing's labels as `tares.label.<name>`.
- The AI-guided project builder traces as its own agent, `project-builder`, instead of `ask`: its
  own `service.name`, root span and `gen_ai.agent.name`, with the step in `tares.build_step`. All
  turns of one build share a `session.id`, sent by the console.
- A run that stops before calling the model (no provider, no model, daily cap, budget) says why on
  its root span in `tares.run.skipped_reason`.

## [1.33.0] - 2026-09-15

### Fixed
- A provider the deployment hands the cell (`TARES_PLATFORM_PROVIDER_URL`) and an OpenAI key
  from the environment now list their models at daemon start. Before, the picker stayed empty
  until someone pressed refresh under Settings, since an environment entry has no save moment.

## [1.32.0] - 2026-09-15

### Added
- A deployment can hand a cell a model provider: `TARES_PLATFORM_PROVIDER_URL`, `_KEY` and `_NAME`
  seed an OpenAI-compatible entry that is the default until the user picks another or saves a key
  of their own. It lists the models its key may use, prices runs from the endpoint's own cost
  figure, and cannot be edited or removed from the console. Tares Cloud uses it to run a trial cell
  through its LiteLLM with a per-cell budget that covers Claude and GPT models alike.

## [1.31.0] - 2026-09-14

### Added
- `GET /metrics`: the daemon's own counters in the Prometheus text format, public like `/health`.
  Events ingested and poll outcomes per source, source state, agent runs per outcome with their
  duration, model calls, tokens and spend per provider, database size against its limit and
  whether ingest is paused, trigger evaluation passes and their duration, and the event loop's
  lag with a count of stalls. Counts only: no entity key or payload ever appears.

## [1.30.0] - 2026-09-14

### Added
- Model providers. A cell holds a list of providers under Settings, Model providers: Anthropic,
  OpenAI, or any OpenAI-compatible endpoint (LiteLLM, OpenRouter, Ollama, vLLM). Each agent picks
  a provider and a model; Ask and the builder run on the cell default. An endpoint lists its own
  models on save, with a refresh, and an agent can name a model by hand when it lists none.
  `OPENAI_API_KEY`, `OPENAI_BASE_URL` seed an OpenAI entry; `TARES_MODEL_PROVIDER` names the
  default. The Anthropic key and gateway settings and their endpoints are unchanged, and a
  provider named by a template or an import that the cell lacks runs on the default instead of
  failing.
- The spend meter prices OpenAI models, takes a router's own cost figure when it reports one
  (OpenRouter, LiteLLM), splits spend by provider, and records tokens without a dollar figure for
  a model it cannot price. Traces name the provider on every model span.
- A run that ends with no conclusion and no tool call says to check that the model supports tool
  calling. A tool name a model mis-cases still reaches the tool; an unknown one answers with the
  list of real tools.

### Changed
- Console, CLI and README say "model provider" where they said "Anthropic key". A failed run's
  error reads `ModelError: ...` where it read `RuntimeError: ...`.

## [1.29.1] - 2026-09-10

### Fixed
- The write-back reported the wrong delivery id when more alerts for the same service arrived
  while the agent was running. The `key` is now resolved when the run starts, so it names the
  firing that woke the agent, and the body carries that firing's labels as `labels` so a receiver
  can check the attribution instead of filing the report by `key` alone (TR-294).

## [1.29.0] - 2026-09-08

### Added
- Storage is measured against the volume the database sits on when no `TARES_MAX_DB_SIZE` is
  set, so a grown volume shows on the next mount with nothing to refresh; `/api/usage` says
  which (`max_bytes_source`). At 95% of the limit (`TARES_INGEST_PAUSE_PCT`) ingest is refused
  with 507 and poll connectors wait, with the reason on the source's health, `/health` and the
  Overview, instead of the database filling its disk and dying. Reads, agents and the console
  keep working; ingest resumes on its own once there is room.
- A run records whether its finding reached the write-back webhook: delivered, an HTTP status,
  or failed with the last error after the retries. The agent page shows it beside the run.
- Agents can report a label's value as the write-back's `key` (`webhook_key_label`, "report as
  key" on the form) instead of the entity, for a system that files reports by its own id.
- Project page: the Firings tab filters by outcome, trigger and entity.

### Changed
- The `rius_rca` template keys by service: `service` is the primary label, `delivery_id` and
  the new `rule` are labels, the trigger cools down for 5 minutes per service, and the callback
  reports the delivery id as `key` through `webhook_key_label`.
- Project page: an agent's configuration is one folded row, so its runs are in view.

## [1.28.0] - 2026-09-08

### Added
- Model access through an LLM gateway. Tares agents and Ask can talk to any server that speaks
  the Anthropic Messages format (LiteLLM, Portkey, a proxy in front of Bedrock or Vertex).
  From the environment with `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`, the names Claude
  Code uses, with `TARES_ANTHROPIC_BASE` still read as the old name; or from Settings, Model
  access, stored on the instance and winning over the environment, so a cloud customer with a
  mandated proxy configures it on their own cell. The gateway token is sent as a bearer header
  and never returned; a stored key is sent as the Anthropic key header. The Settings tab is now
  called Model access and leads with where model calls go. Thanks to KurochkaR for the
  environment half.

## [1.27.0] - 2026-09-08

### Added
- `http_poll` connector: polls any JSON endpoint on a schedule and stores one event per item, for
  the APIs no other connector covers (a weather service, a status page, a SaaS export, your own
  API). GET or POST with a body, extra headers, one credential header stored as a secret, a
  payload key into the reply, the webhook connector's mapping (event type, summary, event time)
  and a deduplication key with a bounded cursor. Nested values are reachable by dotted name in
  labels, the fields profile, view filters and trigger fields. A poll under 10 seconds is refused;
  a 429 or 5xx backs the source off up to an hour, honouring Retry-After, and the source's health
  says so. Discover fetches once and proposes the payload key, fields, labels, time and
  deduplication fields and a summary line.
- The source form for it reads in order: method, URL, credentials yes/no, additional headers as
  key-value rows, body for POST. The mapping appears only after Discover, as decisions already
  made with the sample value beside each, under a "What Tares fetched" panel showing the reply.
- Connector schema features any connector can use: `choices` on a string field (a dropdown,
  validated on save), a `map` type (key-value rows), `format: "url"` (checked on save; now on
  Prometheus, Prometheus alerts and Loki too) and `label` for the words the form shows.
- The builder proposes `http_poll` for goals that read a public or third-party API instead of a
  webhook plus a script; the landing lights it up for api, endpoint and weather.

### Changed
- Test and save results on the source form sit above the buttons, not at the top of the page. A
  required field the click found empty turns red and scrolls into view.

## [1.26.0] - 2026-09-07

### Added
- The running builder is two columns that fill the window: the conversation on the left with
  the answer box pinned under it, and a "To do" column on the right holding every card the
  assistant proposed, in order. A decided card folds to one line; the pointer the chat leaves
  for each card opens it. A one-line head carries the steps, the project name and the goal.
- Overview shows the Projects panel with a Create new button also on an instance with no
  project yet.

### Changed
- The builder no longer invents a trigger for the agent. The daemon refuses an agent proposal
  whose trigger does not exist and hands the model the real list; Continue on the views and
  triggers step waits until a trigger exists; the step names sources that have no events yet,
  since views and triggers are grounded in real events; the agent card never prefills an
  unknown trigger.
- Landing: the starter sentences keep a fixed order instead of reordering while you type, the
  picked one is marked, and the call to action reads "Build it".
- The assistant shows a readable line for a non-JSON error reply (a proxy's 502 page) instead of
  its HTML.

### Fixed
- Behind an nginx ingress, a builder step sent about five seconds after the previous one could
  fail with 502 "upstream prematurely closed connection": the proxy reused a pooled connection
  uvicorn was closing at that moment, and does not retry a POST. The daemon now keeps idle
  connections for 75 seconds, longer than the proxy's pool.

## [1.25.0] - 2026-09-04

### Added
- AI-guided project builder. Describe what you need; the assistant proposes sources from the
  installed connectors, then views and triggers, then an agent, one step at a time, asking
  before it guesses. Each proposal is the object's own form, prefilled, with the fields only you
  can fill (tokens, URLs, the Slack channel) marked. Every Apply creates a real object through
  its normal API and adds it to an ordinary project, so leaving part way keeps what was created.
  `POST /api/agent/chat` takes `mode: "build"` and `step`; each step gets only its own proposal
  tools. A goal that matches a template is proposed as that template, as its own form.
- A new landing screen. A cell with no project of its own opens on one question, "What do you
  want to build?", with starter sentences from the templates, a paste-your-last-incident door,
  and the demo as a one-click offer instead of a seeded project. The same screen is Create new
  under Projects; the template gallery sits behind a "Use a guided template" button. Each
  template declares its sentence (`sentence` in `describe()`); `GET /api/projects/templates/{key}`
  serves one template, hidden ones included.
- Project page: source rows fold open to the ingest endpoint or the polled target, with
  Configure. Framed empty states for runs, firings and events.

### Changed
- Deleting a source or a view no longer refuses with "remove it from those views first". The
  dialog lists what depends on it (views, triggers, agents) and, on yes, deletes them too, in
  order. `DELETE /api/sources/{name}` and `/api/views/{name}` take `cascade=true`;
  `GET /api/catalog/dependents?kind=&name=` lists what would go.
- Deleting a hand-assembled project (including one the builder made) lists its objects with
  Select all or individual picks; the chosen ones are deleted with the project, the rest stay.
  Picking a source pulls in what depends on it. `DELETE /api/projects/{uid}` takes
  `delete=kind:name,...`.

## [1.24.0] - 2026-09-04

### Added
- Runs on an agent's page and on a project's Agents tab can be filtered to successful or failed
  runs only, and "Show more" pages past the first fifty. `GET /api/agents/builtin/{name}/runs`
  takes `status` and `offset`.

### Fixed
- Edit on a project built from a template without its own wizard (Rius RCA among them) opened the
  create form and said "this instance has no project named rius_rca". The form now loads the
  template by key, hidden templates included, prefills the project's params (secrets blank,
  blank keeps the stored value) and saves in place.

### Changed
- The root span of a traced run carries `tares.instance` and `tares.agent` as span attributes,
  not only on the resource, so a backend's span view shows which instance sent it.

## [1.23.0] - 2026-09-03

### Added
- **Agent tracing.** Every Tares agent run and every Ask turn can be exported as an
  OpenTelemetry trace: a root span for the run, one span per model call (model, messages,
  tokens, stop reason) and one per tool call, in the OpenInference and gen_ai conventions the
  GenAI observability backends read. Rius is a preset (an API key is enough); any OTLP/HTTP
  endpoint works with the `otlp` provider (Langfuse, Phoenix, a collector). Each agent is its
  own service (`<instance>/<agent>`), so a backend groups per agent out of the box. Settings,
  Observability holds the switch and the provider; `TARES_TRACING_*` sets the same from the
  environment, and a console value wins. Off unless switched on.

## [1.22.0] - 2026-09-03

### Added
- **Pause a project with its sources.** Pause asks whether to pause the project's sources too,
  so nothing accumulates while it is paused (the AI SRE demo's traffic, for one). Resume brings
  back exactly the sources pause stopped; one paused by hand before stays paused. The API takes
  `{"sources": true}` on the pause endpoint.
- **The GitHub source form uses the tokens the cell already holds.** A stored token is picked
  from a dropdown, the repo from the list that token can see, and a new token can be saved in
  the cell from the same form. Field defaults are shown on a fresh source (limit starts at 20).

### Changed
- **The challenger project's sessions open in place.** The open session is marked and its
  detail sits under its row: the summary, that session's memory proposals with Accept and
  Reject, then the exchange. Commits are one line per session instead of a chip per commit,
  and the open session is in the URL. The separate proposals panel is gone; a row whose
  session still has proposals waiting says so.
- The project page shows the status badge beside the project name.

## [1.21.0] - 2026-09-02

### Added
- **The rius_rca project template.** The Tares half of the Rius RCA integration: one API call
  plans the alert source (keyed by a primary delivery_id label), the view, the per-delivery
  trigger, the Rius MCP server and the RCA agent with its callback, budget and prompt from
  params. Hidden from the console gallery; created by the Rius control plane.

### Fixed
- Unknown /api and /ingest paths answered with the console's HTML and a 200; they are a JSON
  404 now.
- PUT on a view or agent no longer requires the name in the body (the path already names it);
  a rename attempt is still refused.
- GET /api/sources/discover explains that discover is POST-only instead of failing as an
  unknown source.

## [1.20.0] - 2026-09-01

### Changed
- **The side nav has two groups.** Projects sits at the top with Overview and Ask; everything a
  project is made of is one Catalog group in pipeline order (sources, views, triggers, agents,
  firings, MCP servers, Explore). The Data and Automate sections are gone.
- The project page drops the "How it works" box, and Change history aligns with the other
  sections.
- The demo's logs source drops routine 2xx access lines and polls every 10s in hosted mode:
  errors and incident evidence still land, the always-on traffic noise does not.

### Fixed
- Deleting a template project with purge also deletes its triggers' firings (the dispatch log,
  the per-recipient deliveries, the cooldown state). They used to linger on Firings pointing at
  a trigger that no longer existed.

## [1.19.0] - 2026-09-01

### Changed
- **Every project renders on one page.** The AI SRE demo, the challenger workflow, shared code
  context and hand-assembled projects all use the page built for hand-assembled ones: Setup
  (sources, views and triggers editable in place, subscribers), Events, Firings, Agents, and a
  Sessions tab where the template reports sessions. Template actions (cause an incident,
  summarize a session), Repair for hand-deleted objects, how it works and the change history all
  moved with it.
- **Templates describe themselves as data.** The generic summary (runs, triggers, totals) is
  computed for every template; extras arrive as declared panels and cards. A new template needs
  no console work.

## [1.18.0] - 2026-09-01

### Added
- **The project page shows and manages subscribers.** A Subscribers box on the Setup tab lists
  everyone subscribed to the project's triggers (Tares agents, Slack channels, webhooks) with
  what each received, and adds or removes them right there. A project's own agent links to the
  Agents tab of the page.
- The project Firings tab replaces delivery counts with a "delivered to" column: the actual
  recipients, colored by outcome.
- /projects is now just the list of projects, with Create new; the four ways to start one moved
  to their own page at /projects/new.

### Fixed
- Deleting a trigger also removes its subscriptions. They could never fire again, but they
  showed up on Firings as subscribers waking on triggers that no longer exist. Rows left
  behind by earlier releases: remove them on the Tares agents page.

## [1.17.0] - 2026-09-01

### Changed
- **Deliveries is now Firings.** One word per thing across the console: a firing is the event, a
  delivery is what happens to it per recipient, and "dispatch" no longer appears in copy. The
  page lives at /firings (/deliveries redirects); external subscribers moved to the agents page.
- **The entity pages became clean lists with filters.** Sources gains connector and project
  filters; Views gets source and trigger filters, a capped sources column and no author column;
  Triggers gets a view filter, last-fired and subscribers columns (an active trigger nobody
  listens to says nobody); Firings names who each firing was delivered to. Doctrine subtexts
  removed across pages, and project membership is a table row on detail pages.
- **The firing page is one facts box** (trigger, project, entity, time, outcome, each recipient
  with its result and a link to the run), with the payload beneath.
- The hand-assembled project page grew operations: delete views and triggers in place, firing
  rows link to the firing page and the trigger card, Slack channels named, firings that reached
  nobody say so once with a count, events uncollapse to the whole stored line.

### Fixed
- A delivery whose Tares agent is still running showed as failed on the firing page; running,
  delivered and failed are now three distinct states.

## [1.16.0] - 2026-08-31

### Added
- **Hand-assembled projects have a new page.** Four tabs: Setup (the sources with their health,
  and every view and trigger as a card, editable in place; the watched view and trigger links
  stay on the page), Events (the newest rows across the project's sources, merged), Firings
  (every dispatch across the project's triggers, with the agent and Slack subscribers), and
  Agents (each agent inline with its runs, every delivery target, and its configuration).
  Template projects keep the existing page.

### Changed
- Trigger page: the watched view and the project badge moved from the subtitle into the first
  box, above condition, context window and cooldown.

## [1.15.1] - 2026-08-31

### Changed
- Rerun is offered on every finished agent run, not only ones that failed. An agent that uses
  all its rounds can still conclude with a partial "how far I got" report, which counts as ok;
  the rerun opens with that report and continues from it.

## [1.15.0] - 2026-08-31

### Added
- **A per-agent budget.** `budget_usd` on a Tares agent caps its lifetime spend, counted from
  its run log; once reached, runs are capped before any model call and the run says where to
  raise or clear it (Configuration, Advanced). In the catalog YAML and the API alike.
- On a hosted demo stack, the AI SRE demo's agent is born with a $2 budget: the shared stack
  fires around the clock, and an idle trial cell would otherwise burn its whole credit over a
  weekend.

## [1.14.0] - 2026-08-31

### Added
- **Projects from existing objects.** On the Projects page, next to the templates, "From existing
  objects" assembles a project from the sources, views, triggers, agents and MCP servers you
  already have (any that are not part of another project). Nothing is created: the project
  adopts them, its page shows their runs and firings, Edit changes the list, and removing an
  object or deleting the project leaves the object in place. The API is
  `POST /api/projects {"template": "custom", "name": ..., "objects": [{"kind", "name"}, ...]}`;
  catalog YAML takes the same shape.

### Added
- An agent run now opens with the agent's previous finding for the same entity, so it builds on
  its earlier conclusion instead of rediscovering it. Most useful for context-maintaining
  agents, where the last summary is a head start.
- A run that did not work (failed, out of rounds, capped, or empty) has a **Rerun** button on the
  agent page: the agent runs again with the same inputs, same trigger, same entity, and the
  firing's data when a firing woke it. `POST /api/agents/builtin/{name}/runs/{run_id}/rerun`.

### Fixed
- The challenger session thread showed only the first three findings of a review, cut mid-word
  at 500 characters. The thread now lists every finding in full (from the event payload, so
  sessions already recorded get it too), and challenge events keep their whole text.

### Changed
- **Use cases are now projects; recipes are templates.** A project is a named set of sources,
  views, triggers, agents and MCP servers with one page; the shipped recipes are Tares
  templates. The API moves to `/api/projects` and `/api/projects/templates`, catalog YAML uses
  `projects:` with `template:`, and the seed variable is `TARES_SEED_PROJECT`. The old routes
  (`/api/usecases*`, with their old response shape), the old YAML keys (`usecases:`, `recipe:`)
  and `TARES_SEED_USECASE` keep working for two releases. Ids (`uc_...`) and the database tables
  are unchanged.
- `claude_code` events label the repository the session ran in as `repo` (was `project`); the
  challenger session table, the memory proposals and the plugin's session-start memory lookup
  use the same word. On upgrade the saved `claude_code` sources and the views that read only
  them are rewritten to `repo`; events stored earlier keep their old label, like any label
  change. Memory already accepted keeps reaching Claude: it is keyed by the repo name, which
  did not change.

## [1.13.0] - 2026-08-27

### Added
- **Challenger workflow in the console**: the use case page lists every challenger session with
  the plan and per-commit review outcomes, the Claude/Codex exchange as one thread, a Summarize
  button per session, and the summarizer's memory proposals with Accept / Reject. Decisions are
  kept on the memory source (reject writes a `rejected_proposal` the plugin never hands out).
- The first session marked as a challenger session creates the `challenger_workflow` use case
  by itself; `/tares:challenger` in Claude Code is the whole setup.

### Fixed
- Plugin: the `set_session_flow` call is recognised under Claude Code's plugin-namespaced tool
  name, so marking a session works from a marketplace install.
- Plugin: commits made with options between `git` and `commit` (`git -c ... commit`) are reviewed.
- Plugin: Codex 0.150 dropped `--full-auto`; reviews run with `--sandbox read-only`.
- Plugin: only `[P1]` findings count as blocking on a plan critique.
- Use case actions that start an agent run (`summarize`) failed with "no running event loop"
  under `tares up` and left a run stuck at running.
- Challenge outcomes read "no findings" / "N findings (M blocking)" instead of Codex's PASS/FAIL,
  which elsewhere in the console means a broken run.
- Agent page: the opened run no longer scrolls back into view on every refresh.

## [1.12.0] - 2026-08-27

### Added
- **Challenger workflow**: a second model challenges Claude Code's plan and every commit,
  locally, while Tares keeps the record. The `challenger_workflow` use case adds a per-session
  view, a trigger on session end and a summarizer agent that writes a session summary with
  memory proposals. `set_session_flow` MCP tool marks a session; the `claude_code` connector
  gains the `flow` label and `challenge_plan` / `challenge_commit` / `challenge_waived` events
  with verdict, sha, round and finding counts.
- **Claude Code plugin 0.2.0**: `/tares:challenger` turns a session into a challenger session;
  the plugin runs Codex on plan exit and after each commit (blocking on P1/P2 findings by
  default, with a fix loop and `/tares:challenger-waive`), ships every review to Tares, and
  hands accepted project memory to Claude at session start. Review mechanics adapted from
  andreidavid/codex-review (MIT).

## [1.11.0] - 2026-08-26

### Added
- **Install skill for coding agents**: `skills/tares/SKILL.md`, installed with
  `npx skills add glassflow/tares --skill tares`. From one prompt, an agent installs Tares,
  starts it, adds a first source over the HTTP API, connects itself over MCP and shows one read.
  `AGENTS.md` at the repo root for contributors.
- **`tares status`**: a readiness checklist for a running instance (daemon, auth, sources
  receiving, views, triggers, Tares agents and whether a key is set, MCP endpoint, which agent
  clients on this machine have Tares registered, Slack) that ends with the one next step.
  `--json` for scripts; exit 1 when nothing answers. `tares up` prints the same "Next:" line
  right under the console URL once the daemon is up. `/health` now carries `version` and
  `uptime_seconds`.

### Changed
- An agent run without a key, and the banners on the Agents pages, now say what to do: set
  `ANTHROPIC_API_KEY` before `tares up`, or add a key under Settings (linked).
- README rewritten around the always-on agents positioning: who Tares is for, how it compares,
  common questions. Docs got a new introduction and a page for coding assistants
  (docs.glassflow.ai/tares/agents/ai-resources) with `/tares/llms.txt`.

## [1.10.0] - 2026-08-25

### Changed
- **A key stored in the console now takes precedence over `ANTHROPIC_API_KEY` in the
  environment** (it was the other way around). Saving your own key takes over immediately, and
  removing it falls back to the environment key. If you relied on the env var overriding a
  stored key, clear the stored key instead. The Slack bot token keeps its env-first order.

### Added
- **Grafana Loki connector.** Polls a LogQL stream selector via `query_range` (timestamp
  cursor, bearer/basic/tenant auth, match/drop filters); one event per log line with the
  stream's labels, and the same derived fields (level, HTTP status, JSON scalars) as
  docker_logs. Works against any reachable Loki, including Grafana Cloud.
- **The AI SRE demo works against a hosted demo stack.** When `TARES_DEMO_PROMETHEUS_URL`,
  `TARES_DEMO_LOKI_URL` and `TARES_DEMO_API_SERVER_URL` are set (a hosted deployment sets them;
  a self-hoster can point them at their own stack), the demo use case adapts: nothing to
  install, URL defaults from the environment, logs from Loki instead of a local Docker
  container, and the actions say plainly the stack is shared. With them unset, the local docker
  compose flow is unchanged.
- **The spend meter records which key paid.** Every `model_usage` row carries a `key_source`
  (environment vs console), and `GET /api/usage/model` reports a `by_key_source` split, so an
  operator-provided key's spend is separable from your own.
- **`TARES_SEED_USECASE`**: seed a named use case once on first boot, so an instance can start
  with a working setup instead of an empty catalog. One-shot by a settings marker; deleting the
  seeded use case never brings it back.

## [1.9.0] - 2026-08-24

### Added
- **Model usage and cost per agent run.** Every run records the model it used, its token usage
  (input, output, cache write/read) and a USD cost priced at write time; Ask turns (console and
  Slack `/tares ask`) are metered the same way. Runs from before this release show no cost
  (unknown, never guessed), and models without a known price record tokens with a null cost.
- **`GET /api/usage/model`**: the instance's Anthropic spend as all-time totals plus a per-day
  tail, split by surface (agent runs vs Ask). Generic metering data only, for a hosted control
  plane or your own budgeting.
- **The Tares agents page is an operational surface.** Stat cards (total cost, runs, success
  rate, average duration), Overview / Runs / Configuration tabs, and runs as a table showing
  model, rounds, tokens and cost per run, expandable to the finding. The agents list gains runs
  and cost columns, and the Overview page a Model spend panel.
- The write-back webhook body now carries the run's `usage` and `cost_usd`.

## [1.8.2] - 2026-08-19

### Added
- **Shared code context links its guide** from the Use cases card and the setup page.

### Fixed
- **A trigger no longer misses a key whose source ingested right after another.** Trigger
  evaluation is debounced to once per 10 seconds per trigger; a second source of the same view
  that ingested inside that interval was left unevaluated until the next ingest, by which time a
  short detection window could have slid past its events (two repos polled a second apart: the
  first fired, the second never did). A skipped evaluation is now re-run once the interval ends.
- **Shared code context**: the `every_commit` trigger's detection window is 5 minutes (was 2),
  matching the 60-second source poll.

## [1.8.1] - 2026-08-19

### Fixed
- **Auto-discover (Docker) works again.** The environment scan still read the Prometheus
  connector's old one-shot discover shape and failed with an internal error whenever a
  Prometheus container was running. It now proposes a reachable starter metrics source (`up`,
  keyed by `service` when the server has that label) and says how many metrics wait to be picked
  on the source page; an unreachable or unexpected server never fails the scan.

### Added
- **AI SRE demo as a use case.** Use cases > AI SRE demo (tagged demo) creates the same three sources,
  `service_timeline` view, `incident` trigger and `incident-first-look` agent that
  `demo/catalog.demo.yaml` seeds, with one click and no catalog import; an existing unowned demo
  catalog is adopted rather than duplicated. The wizard lists what to do first (start the stack,
  set an Anthropic key) with copyable commands, and the use case page has Cause an incident
  (error_spike, latency, dependency_outage) and Clear the fault buttons that flip the api-server's
  fault switch. Recipes can now declare `tags`, `SETUP` steps and `ACTIONS` (served at
  `POST /api/usecases/{id}/actions/{name}`).

## [1.8.0] - 2026-08-18

### Added
- **Use cases.** A new top-level console section and a framework behind it: a use case is a
  ready-made setup (a recipe with parameters) that creates ordinary Tares objects and owns them.
  Instances live in `usecases`; the sources, views, triggers, agents and MCP servers they create
  carry `owned_by` and show a "part of use case" badge on their normal pages, stay fully editable
  and deletable there (an edit marks the object customized and the engine keeps that version; a
  hand delete shows as missing on the use case page with a Repair action). Engine operations:
  create (all or nothing), update (re-plan and diff), pause and resume (trigger and agent off,
  sources keep ingesting), delete (optionally purging events), repair. API under `/api/usecases`;
  a `usecases:` section in catalog YAML seeds instances on first boot and export includes them.
- **Console for use cases.** The Use cases page (recipe cards, existing instances), a four-step
  wizard for the shared code context recipe (GitHub access with a token permissions guide, source
  repos picked from the token's repositories or pasted, the context repo with a look inside it and
  the page layout, trigger and agent with a preview of what Start creates), and the instance page
  with Runs and Configuration tabs, first-look runs labelled, runs deep-linked to the agent run,
  and the underlying objects with their state. The breadcrumb shows the use case name.
- **GitHub credential stored once.** Settings > GitHub holds a token by name; `github` sources
  take `credential: <name>` instead of a token per source, MCP servers take
  `credential:github/<name>` as their auth value, and rotating the token in one place rotates
  every user of it. Test shows the login and scopes; the credential lists what uses it. New API
  under `/api/integrations/github`, including the repositories the token can see and a look into a
  repository's tree.
- **MCP servers take extra headers.** A non-secret `headers` map alongside the auth header, merged
  into every request; used to send `X-MCP-Toolsets` and `X-MCP-Readonly` to GitHub's hosted MCP.
  Console form has it under Advanced; catalog YAML round-trips it.
- **Settings has tabs.** Access and API keys, Anthropic, GitHub, Slack; `?tab=` deep-links.
- **Per-agent round cap.** A Tares agent has `max_rounds` (1 to 24; blank means the default). The
  default is 6, or 12 once the agent uses external MCP servers, since a run that reads a diff or a
  file and writes back needs more than the read-and-conclude budget. Every run records the cap it
  was held to and shows `rounds/max`. When the budget runs out mid-investigation the model gets one
  final call with tools disabled to conclude from what it has; if it still cannot, the run ends
  `exhausted` (a new status, not a failure), keeps the last text as a partial note, and says to raise
  max rounds. Catalog YAML imports and exports the field; the agent form has it under Advanced.
- **Use case: shared code context.** The first recipe on the use-case framework. Given a stored
  GitHub credential, a list of source repositories and a context repository, it creates one commits
  source per repo, a `repo_activity` view keyed by repo, a trigger that fires on any new commit
  (batched per repo, 5m cooldown), GitHub's hosted MCP server registered with the same credential
  (toolsets `repos,pull_requests`), and a Tares agent that reads each diff and keeps the context repo
  current, as a pull request (default) or direct commits. Two page layouts: keep the repo's existing
  pages and update them in place (default; the wizard shows what is at the chosen path, root
  allowed), or one page per source repository plus an index. On Start it can run once per repo over
  the last 7 days (the "first look", on by default) so the context repo starts current without
  waiting for a commit. The agent writes plain sentences with no em dashes and finishes with a
  finding that says what it changed and links the pull request. `POST /api/usecases` with `recipe: shared_code_context`, or a `usecases:` entry in
  the catalog.
- **GitHub commits carry their changed files.** With a token or credential, the `github` connector
  fetches each new commit's file list (paths, status, additions, deletions, patch) into the payload,
  capped at 20 files and 4k characters per patch with a `files_truncated` flag; one extra API call
  per commit, paused for 10 minutes when the rate limit runs low. Off with `files: false`.

## [1.7.1] — 2026-08-17

No code changes; a version bump so managed cells can be re-provisioned through the upgrade path
(a same-version upgrade is a no-op there), which is how a cell picks up `TARES_WORKSPACE_URL` from a
control plane that started setting it after the cell was last applied.

## [1.7.0] — 2026-08-17

### Added
- **A cloud cell links to its workspace.** Users, the Slack app install, plan and storage are
  managed in the control plane, and a user in the data-plane console had to know that to find
  them. When the instance is given `TARES_WORKSPACE_URL` (a generic, env-gated hook the cloud
  sets per cell, surfaced on `/health` as `workspace_url` next to `login_url`), the console shows
  a **Workspace** link in the sidebar footer and a banner at the top of Settings naming what lives
  there, including the Slack split: the bot token is here, the app install is in the workspace.
  Self-host never sets it and sees nothing. (TR-142)

## [1.6.0] — 2026-08-17

### Changed
- **The console wears the GlassFlow design system.** Palette and typography now come from the
  agency's HeroUI Kit V3 variables, the same source Rius uses, so the two products share one
  palette by construction: cream canvas and surface tiers, eclipse ink, one Space Sand accent,
  both themes. Geist Mono carries labels, badges, table headers and code; the pixel voice is kept
  for the Overview's headline numerals. Square corners, hairlines and no shadows stay, now
  consistent everywhere. (TR-140)
- **The sidebar is one layer, rearranged.** Overview and Ask on top, then **Data** (Sources,
  Explore, Views), **Automate** (Triggers, Tares agents, MCP servers, Deliveries) and **Agent
  access** (Connect, Reads), with Settings in the footer. Agents is now Tares agents only; the new
  **Deliveries** page holds every subscriber with its delivery health and every trigger firing (a
  Slack channel is a destination, not an agent). MCP servers has its own entry. Security is
  Settings, which is what it always held. Old routes redirect. (TR-137)

### Fixed
- **Agent findings render properly in Slack.** They were posted as raw markdown, so headers, bold
  and pipe tables arrived as literal `##`, `**` and pipes. Both posting paths now send Block Kit
  through the same converter `/tares ask` uses; tables become aligned monospace blocks and long
  findings split under Slack's block cap. (TR-141)
- Em dashes removed from every string the daemon serves (connector descriptions, field help,
  errors, prompts, CLI output). (TR-143)

## [1.5.1] — 2026-08-17

### Fixed
- **The trigger page keeps setup out of the way.** It mixed the running system (subscribers,
  delivery status, recent firings) with always-open forms for wiring a webhook or a Slack channel.
  Those forms now sit behind **Add webhook** / **Add Slack channel** buttons beside **Add a Tares
  agent**, opening in a small panel with Cancel; on success it closes and the subscriber table
  reloads. (TR-138)

## [1.5.0] — 2026-08-14

### Added
- **Tares agents can use external MCP servers.** Register a server once under **Agents → MCP
  servers** (streamable-HTTP URL plus an optional auth header, stored as a secret; **Test** lists
  its tools), then opt individual agents in on their form. At run time the server's tools are
  offered alongside the built-in reads, prefixed by server name; every run records which external
  tools it called. A down server is skipped, a verbose one truncated, and a failed call is
  evidence for the model rather than a failed run. This moves the read-only boundary for the
  agents you opt in: tools from an external server can act on whatever that server exposes.
- **Per-agent model.** Pick a model per agent from a curated dropdown, or leave it following the
  instance default (`TARES_AGENT_MODEL`). Catalog YAML accepts any `claude-*` id.
- **Findings deliver where you already look.** The agent form's delivery options are toggled
  rows: post to a **Slack channel** via the workspace bot (picked from the bot's own channel
  list), or POST each finding to an **authenticated write-back webhook** as JSON with its run
  metadata (trigger, entity, model, rounds, tool calls, timing; never a credential), with an
  optional bearer token. The legacy per-agent Slack incoming webhook remains and can now actually
  be turned off.
- **Paste a catalog snippet.** The Add source page is a connector card grid with **Add via
  YAML**: paste a snippet from the docs or a teammate and it merges into the live catalog,
  validated first, upsert-only, nothing removed, no restart. One paste can carry sources, views,
  triggers, agents and MCP servers; the result links to everything it created.

### Fixed
- **Delivery links on a dispatch page go where the story continues.** They all pointed at a page
  that no longer shows agents. A Tares agent now links to its run for that exact firing (opened,
  highlighted and scrolled to), an external agent to its roster row; a Slack delivery is plain
  text, because the message's story continues in Slack.
- Em dashes removed from every rendered string, and redundant help lines under form fields
  trimmed.

## [1.4.0] — 2026-08-14

### Added
- **Ask keeps your conversations.** Every conversation is saved on the instance and the Ask page
  is now the familiar AI-chat shell: a sidebar of conversations next to the chat, newest resumed
  automatically when you come back — navigating away, refreshing, or closing the browser loses
  nothing. Start another with **+ New conversation**; delete one from the sidebar. Resumed
  conversations carry their full transcript back into the agent's context. The instance keeps the
  newest 50; the ⌘K palette stays a single column and picks up the same latest conversation.
  Opening an old conversation just reads it — only a sent message or an applied proposal moves it
  to the top.

## [1.3.1] — 2026-08-14

### Fixed
- **Explore no longer sticks at "reading timeline…" in a background tab.** The read is skipped
  while the tab is hidden (deliberately, to save load), but nothing re-fired it when the tab
  became visible — you stared at the spinner until the next 10-second tick. The read now fires
  the moment the tab becomes visible.
- **The Connect page no longer shows the access token in clear text.** The Claude Code snippet
  rendered the real token unmasked the moment the page opened; on a secured instance that is the
  root token on screen. Every credentialed snippet now displays the masked token while the copy
  button carries the real one — which also fixes the other tabs, where copying without hitting
  reveal first copied the mask.

## [1.3.0] — 2026-08-10

### Added
- **Connect → MCP is a client picker.** The tab showed three generic snippets (JSON, Claude Code,
  stdio) and left the translation to the reader. It now asks which client you're connecting —
  Claude Code, Codex CLI, Cursor, Claude Desktop, Other (JSON), stdio — and shows that client's
  exact command with this instance's endpoint and token filled in, mirroring the docs page
  (docs.glassflow.ai/tares/agents). A verify line says what to ask and which tool call to expect.
- **A fresh instance lands on Sources.** Opening the console with zero sources showed an Overview
  of zeros. The first landing of a page load now goes to Sources, where the next step actually is;
  clicking Overview afterwards still works, and a failed source-list load never counts as empty.
- **The trigger page links the webhook contract.** Wiring an external agent to a trigger now points
  at Connect → Webhook (push) — which is deep-linkable as `/connect?tab=push` — for what the
  endpoint receives and how to acknowledge. The webhook tab itself was tightened: a minimal
  receiver example, and a note that built-in Tares agents need no endpoint at all.

### Fixed
- **The webhook tab stated the wrong acknowledge rule.** It said any response below 500 counts as
  delivered; the dispatcher only counts 2xx. It now matches the code: 2xx acknowledges, 5xx and
  transport errors are retried with backoff, a 4xx is recorded as failed and not retried.
- The webhook tab's delivery-history link said "Agents" but pointed at Activity.

## [1.2.1] — 2026-08-07

### Fixed
- **A subscribed Slack channel is no longer presented as an agent.** It sat under "Agents woken by
  this trigger" in a column headed `agent`, but a channel is a destination — it doesn't wake up and
  do anything. The section is now "Where this trigger delivers" and the column is `subscriber`,
  which covers all three kinds honestly: a Tares agent, a connected webhook, and a channel. The
  delete-trigger warning and the trigger's summary line had inherited the same wrong noun.
- **A subscription can be removed from the console.** Subscribing was a one-way door — the
  `/unsubscribe` endpoint and the per-trigger subscription id both existed, and nothing used them.
  Removing affects only that trigger; a subscriber wired to several keeps the rest, and its delivery
  history is kept.
- A Slack row showed the raw channel ID (`#C0BNV121CRX`). It now resolves to the channel name, with
  a lock for a private one, falling back to the id — which is the honest answer when the bot has
  been removed from the channel and the name is no longer knowable.

## [1.2.0] — 2026-08-07

### Added
- **Pick a Slack channel from a list instead of pasting an ID.** Subscribing a trigger to Slack meant
  going to Slack, right-clicking the channel, Copy link, and pulling the ID out of the URL. The
  console now offers the channels the bot can actually post to.

  It lists via `users.conversations`, **not** `conversations.list`, and that distinction is the whole
  design: `conversations.list` returns every public channel including ones the bot was never invited
  to, and posting to one of those fails at the first firing with `not_in_channel` — a subscription
  that can never deliver, which is what `validate_slack_channel` exists to prevent. On a real
  workspace this was the difference between offering 32 channels and offering the 2 that work.

  Because a missing channel now means "the bot isn't in it", the console says so: a line under the
  picker and a Refresh beside it that re-fetches only the channel list, so someone half-way through
  wiring up a trigger doesn't lose it to a page reload. Private channels are included and marked with
  a lock; the value submitted is the channel ID, which survives a rename.

  Needs the `channels:read` and `groups:read` scopes. **Scopes are baked into the token at install**,
  so a token issued before them keeps posting fine but cannot list — the console detects exactly that
  and falls back to the free-text box it always had, with a prompt to reinstall. Nobody loses the
  ability to subscribe.

### Changed
- **Slack alerts no longer unfurl links.** The alert body carries every label on the event, so a
  source with a `host` label — web traffic, CDN logs and deploys nearly always have one — made Slack
  fetch that site and staple a preview card onto the alert. It roughly doubled the height with
  nothing about the incident, read as though Tares were linking somewhere relevant when it was
  echoing a label value, and meant alerting had the side effect of Slack fetching a customer's URLs.
- **A Slack alert now links the firing** (`/dispatches/<id>`) rather than the entity — "what fired
  and what did it carry" is the question the reader has. An agent's *finding* still links the
  entity's timeline, which is what a finding is about. Both require `TARES_PUBLIC_URL`; a link to
  127.0.0.1 is worse than no link.
- **Slack timestamps are readable.** The footer was a raw ISO string with microseconds and a UTC
  offset; it now uses Slack's date token, which renders in each reader's timezone. Event ages read
  `[29m ago]` instead of `[T-1734s]` — in the Slack copy only, since the payload is the agent-facing
  contract and goes out over MCP verbatim.
- **`/tares ask` answers render properly.** Markdown tables arrived as raw pipes plus a `|---|---|`
  row, and `---` rules as three literal dashes. Tables are not an edge case — the assistant is told
  to use small tables where they help — and Slack renders no tables at all, so one becomes an aligned
  monospace block, the only place Slack keeps columns lined up.
- **The last native dropdowns in the console are gone.** A native `<select>`'s open menu is drawn by
  the OS and cannot be themed, so it arrived as a light system popup in the middle of the console.
  The view filter operator, the trigger aggregate, the Explore lens, the poll interval unit, the
  normalize rule kind and the sources status filter all use the app's own picker now.

### Fixed
- The Slack channel field's placeholder promised only "the channel ID", while a lowercase channel
  name has always been accepted too.

## [1.1.1] — 2026-08-07

### Added
- **Vercel sources expose the request (`proxy`) block**: `status_code`, `url`, `method`, `referer`,
  `path_type`, `region` and `cache` join the fields you can build labels and keys from. `url` is
  what the client actually requested — the existing `path` is the *rewritten* value, a Next.js
  segment on prerendered routes (`/tares?_rsc=19hu7` logs as `tares.segments/_head.segment`), so it
  cannot answer which page failed. Label the code as `{"field": "status_code", "type": "number"}` to
  filter `>= 400` and aggregate it in a trigger. Fields are resolved in `label_context()`, so ingest
  and backfill agree. Entries without a `proxy` block (builds) get no request labels rather than
  empty ones.

### Fixed
- **The Vercel connector discarded the HTTP status code.** It was computed in `_map_one` and never
  passed to the envelope — dead since it was written. With no status field to label on, the only
  value in reach containing digits was `path`, so a status label had to be built as a regex over it:
  that fires only when the request path is *literally* `404` (a hit on the 404 page asset, not a
  request that returned 404), names the 404 page instead of the URL that failed, and is empty on
  live traffic.
- **The assistant had to guess the shape of a view filter, and got it wrong.** `propose_view` typed
  `filters` as an array of unconstrained objects, so the shape was stated nowhere the model could
  see it — producing a flat `{"service": "x"}` pair, then `"=="` for the operator, both rejected on
  Apply. The schema now requires `field`, `op` and `value` with `op` as an enum of the operators
  the code validates against (`eq`, `neq`, `contains`, `gt`, `gte`, `lt`, `lte`), which is enforced
  when the tool is called rather than asked for in prose. The prompt gains the matching rule,
  including that a filter `field` may also be one of the built-in columns `event_type`, `source`,
  `text` and `key_value`.
- **A view proposal showed `2 filter(s)` instead of the filters.** That was the one place a bad
  filter could be caught before applying it, and it was showing a count. Cards now render each
  filter, and a malformed one as its raw JSON in a marked chip.

## [1.1.0] — 2026-08-07

### Changed
- **Breaking: `/api/agent/chat` no longer accepts an `X-Anthropic-Key` header.** There is one
  Anthropic key per instance — the environment's `ANTHROPIC_API_KEY`, else the one stored under
  Security — and it is the key the console assistant, `/tares ask` in Slack and trigger-woken
  agents all resolve. The console used to keep a copy in `localStorage` and send it per request, so
  a key added on the Ask page made Ask work while Slack and Tares agents still reported none
  configured, having no browser to read it from. Callers passing the header now get the instance's
  key; a request with no key configured anywhere gets a 400. Stale browser copies are cleared on
  load.
- **Ask and Organize are one assistant.** Organize was Ask with a different system prompt — both
  surfaces posted the same tools to the same endpoint — so the judgement it carried (what makes a
  good entity key, labels come only from real fields, watch for value variants, views key on labels
  rather than raw fields) now governs every proposal Ask makes. Asking Ask to add a label
  previously got an agent with none of it. Organize's full-source sweep survives as a starter
  prompt; `/organize` redirects to `/ask`.
- **The Ask page is readable at length.** The transcript scrolls with the page instead of inside a
  64vh slot; autoscroll follows the tail only while you are at the tail and detaches on a
  deliberate upward scroll; turns are labelled and separated; tool calls draw as a pipeline of
  steps showing what ran, how long it took and what came back; the composer is pinned, takes
  multiple lines, and offers Stop, New and copy-on-hover.
- **The source page says what it already knows.** The labels table gains a `rules` column naming
  which rule rewrites a label's values (`≈ regex`, `≈ 2 renames`, `≈ regex +5`, `≈ none`), from one
  shared summary so the editor and the read-only table cannot describe the same rule differently.
  The entity key is chosen from one dropdown above the table rather than a radio per row.
  `Labels & key` collapses, defaulting to closed. `Fields` lists only what is not already a label.
- Dropdowns are ours. A native `<select>`'s open menu is drawn by the OS and cannot be themed, so
  one macOS menu appeared mid-form beside our own styled inputs. `Picker` replaces it, keeping
  keyboard control, `disabled`, and listbox semantics.

### Fixed
- **A failed tool call in the assistant reported itself as successful.** The `ok` flag was derived
  from the response body starting with `{"error"`, which only ever matched the local unknown-tool
  case — every daemon error path raises `HTTPException` and serializes as `{"detail": …}`, so a 404
  read as a success and the console drew a failed read as a completed step. It now comes from the
  status code.
- **Pressing Stop could break the next question.** A turn stopped before it produced text
  serializes to an empty string, which the Messages API rejects, so the following question failed
  with a warning pointing at nothing.
- **A label rewriting every value looked identical to a pass-through one.** The table computed the
  distinction and styled it with a bare `button.active`, which no CSS rule matched — every
  `.active` rule is scoped to another component. On one instance a rule silently collapsing 237
  distinct values to 2 was invisible from the table.
- The merge panel never said which label it was editing, and a rule that blanks a value rendered as
  a green `ok` badge followed by nothing — indistinguishable from a value that failed to render.
  Blanking is a legitimate rule and is now stated as one.
- Normalize panels no longer expand on load, a long label name no longer pushes its badge onto a
  second line, and three paragraphs describing the columns directly beneath them are gone.

## [1.0.2] — 2026-08-06

### Changed
- **Documentation moved to <https://docs.glassflow.ai/tares>.** Every link in the README, the
  security policy, the packaging metadata, the CLI's exposure warning and the console now points
  there. Two of them were already wrong: the security policy and the issue template still linked
  `navflow.ai/docs`, and the console's Agents page linked `www.tares.ai`, which does not resolve.
- Repository URLs follow the rename to `glassflow/tares`.

## [1.0.1] — 2026-08-05

### Fixed
- **The 1.0.0 container image was never published** — its Dockerfile still copied `navflow/`, so
  the build failed after the tag was cut. 1.0.0 exists on PyPI but has no image; **1.0.1 is the
  first usable image tag**.
- Everything the rename missed outside `tares/` and `ui/`: the Dockerfile set `NAVFLOW_HOME` (which
  the new guard refuses to start on), both compose files set `NAVFLOW_CATALOG` / `NAVFLOW_AUTH_TOKEN`
  (so self-host compose could not boot at all), and the Claude Code plugin invoked the removed
  `navflow-mcp` entry point.

## [1.0.0] — 2026-08-05

**Breaking. There is no compatibility layer, deliberately.**

### Changed
- **`navflow` is now `tares`** — the Python package and module, the `navflow` / `navflowd` /
  `navflow-mcp` commands (now `tares` / `taresd` / `tares-mcp`), the PyPI project, and the image
  (`ghcr.io/glassflow/tares`). Existing `navflow` releases and image tags stay resolvable; nothing
  new is published under them.
- **Every `NAVFLOW_*` environment variable is now `TARES_*`** — the prefix is the only change, e.g.
  `NAVFLOW_DB` → `TARES_DB`, `NAVFLOW_AUTH_TOKEN` → `TARES_AUTH_TOKEN`, `NAVFLOW_PORT` →
  `TARES_PORT`. The MCP proxy's target is `TARESD_URL`.
- **The daemon refuses to start if any `NAVFLOW_*` variable is set**, printing the mapping. This is
  deliberate and is the one place a breaking rename must be loud: silently ignoring
  `NAVFLOW_DB` would open a database somewhere else and come up healthy and empty.
- **The data file is `tares.duckdb`** (was `navflow.duckdb`), and the default home is `~/.tares`.
  Starting with a `navflow.duckdb` beside a missing `tares.duckdb` is refused, with the `mv` to run
  — indistinguishable from data loss otherwise.

### Upgrading from 0.3.x
Rename your environment variables, `mv navflow.duckdb tares.duckdb`, and use `tares` in place of
`navflow`. The daemon will tell you if you miss one.

## [0.1.4] — 2026-07-03

### Removed
- **`changelog` and `config` connectors** — dummy, demo-only connectors that had outlived their
  use. Also drops the demo api-server's unused `/admin/changelog` and `/admin/config` endpoints and
  renames its fault switch `/admin/inject` → `/demo/inject`.

## [0.1.3] — 2026-07-03

### Added
- **`include_payload` on `read`/`query`** — opt-in flag that returns the full lossless stored
  record as `raw` on each row, alongside the summary `text`. Exposed over HTTP and the MCP
  `read`/`query` tools; covers all connectors.

### Fixed
- **Claude Code plugin install** — publish a root `marketplace.json` (`/plugin marketplace add
  glassflow/navflow`) and fix `plugin.json` load errors on Claude Code 2.1 (duplicate hooks ref,
  missing `ingest_token` default, `navflow_url` required flag hiding its default).
- **Double-ingest for `claude_code`** — the first pushed event flips a poll-mode source to push
  mode, so a source fed by the plugin no longer also tails files and ingests every event twice.

## [0.1.2] — 2026-07-02

### Added
- `navflow --version`.

## [0.1.1] — 2026-07-02

First public (soft-launch) release.

### Added
- **`read(selector, window)` primitive** — a correlated, time-ordered read across *all* sources
  matching a strict-AND conjunction of `label=value` constraints, with no view required. Exposed
  over HTTP (`POST /read`) and MCP (the `read` tool). Views become an optional narrowing lens;
  triggers still attach to a view.
- **Console redesign** — a selector-first **Explore** (pick an entity, add filters, read across
  every source, with a human/agent view toggle), a **⌘K Ask** command palette, a three-act
  navigation, and separate Views / Triggers pages.
- **Agents → Reads** — a client filter (defaults to `mcp`) over the read activity log.

### Changed
- Package and CLI distribution renamed from `navflow-mvp` to **`navflow`**.

Earlier `0.0.x` history is in the git log.
