# The Software Factory on Codex

A crew can run on Codex instead of Claude Code: every station is a Codex session, planning (`tares factory spec`,
`tares factory session`) runs in your own `codex`, and nothing needs Claude Code, an Anthropic account or the
Claude plugin. This page covers setup, what a Codex station may do, the guard and its limits, recovery and
troubleshooting.

## What is supported

| | |
| -- | -- |
| Codex CLI | 0.171.0, the version the crew was tested on. An older one is refused at a station's start; a newer one starts with a note. |
| Codex login | The login file `codex login` writes (`~/.codex/auth.json`, or `$CODEX_HOME/auth.json`). A login kept in the system keyring and an API key in the environment are not supported yet. |
| Platforms | Measured on macOS (arm64). Linux has not been run end to end. Windows cannot run a Codex station: a station listens on a Unix socket. |
| Runtime per crew | A crew keeps the runtime it was started on. A crew whose stations recorded no runtime is on Claude Code. |

## Setup

1. Install Codex 0.171.0 and sign in: `npm install -g @openai/codex@0.171.0`, then `codex login`. If `codex` is not
   on your `PATH`, set `CODEX_BIN` to it.
2. Choose Codex as the factory's runtime, in the console (Setup, Runtime, Codex) or from the command line:

   ```bash
   tares factory runtime codex
   ```

   `tares factory runtime` alone says what new crews start on, what the crew of this crew home runs on, and which
   Codex it found. Setting the runtime needs the admin scope, which your own crew key carries.
3. Start a crew in a crew home that has none yet: `tares factory crew up`. An existing crew stays on its runtime;
   to run one on Codex next to it, give the new one its own crew home (`FACTORY_CREW_HOME`).
4. Check it: `tares factory doctor` checks `codex`, its login, the plugin, each role's model, and for each running
   station its Codex folder, its guard and its connection to the Tares MCP endpoint.

Each role's model is `stations.<role>.model` in `crew.json`. A station with no model named runs on the model the
Codex account gives by default; a Claude model's name is not handed to Codex.

## What a Codex station is

Each station is one `codex app-server` of its own, started detached on a private Unix socket in the crew home.
Nobody stays connected to it: the crew's watch visits it briefly, once a round, to hand it a prompt, record what it
did and look at what it asks.

A station has a Codex folder of its own (`<crew home>/stations/<station>.codex`) that holds a link to your login
and nothing else of yours: none of your plugins, MCP servers, skills, notify programs or trusted folders. The
account's connectors and remote plugins are switched off, and the station's working folder is marked not trusted,
so a repository's own `.codex/config.toml` adds nothing to it. Its one MCP server is the crew's Tares endpoint,
reached with the crew's key through the plugin's headers helper; the key is in no environment or argument list.

## What a station may do

- **Its sandbox is its workspace** (`workspace-write`), plus the crew's reports folder. It has no network, and Codex
  never lets a sandboxed command write a `.git` folder, so a fetch, a commit or a push asks for more.
- **What it asks beyond the sandbox is decided by its rules.** Codex's own approval reviewer judges each request
  against a policy the station is started with: who the station is, and its rule sentences, your grants among
  them. These are the sentences a Claude Code station's auto mode reads. Every message in a station's conversation
  comes from the factory or from another station, so none of them counts as your consent, whoever it claims to
  quote: what no rule covers is refused.
- **A refusal comes to you.** The station writes the command in its report and blocks the ticket with it, so it
  reaches your desk. Allow it in the console with a grant; the crew then restarts the station in the same
  conversation with the new policy, and it runs the command again.
- **A grant never widens the sandbox.** Each request it allows is one decision, recorded as a `codex-review` event
  (allowed or refused, the command, the risk and the reason) and said by the watch: `tares factory events` shows
  them.
- **The guard comes first.** It runs before the reviewer is asked, so a halted crew (`tares factory crew halt`) and
  the guard's own refusals (merges, production, tags) stop a request whatever a grant allows.
- **A question in words** goes to your desk, and your answer goes back to the station.

## The guard and its limits

The guard is the plugin's `guard.py`, run by Codex before every tool call and at the start of every turn
(`tares hook codex-guard`). It also closes a command's standard input, so nothing typed into a shell that is
already running gets past it. The crew proves the guard is on: a turn whose start the guard did not record is
interrupted before the model answers, and every command of a turn is compared with the guard's record. A station
that ran something the guard has no record of is stopped and shows as stopped by the crew.

What it does not stop, on either runtime: a command hidden in a script file, or one that opens a terminal of its
own. A hook that times out lets the command through, as on Claude Code. The comparison is after the fact: Codex lists
a command once it has ended. The guard is a check on a cooperating station, not a sandbox; the sandbox and the
reviewer are.

## Messages between stations

A Codex station sends with the Tares tool `send_to_station`. The crew reads the call from the sender's own
conversation, so the sender is who it says it is, and delivers the message between the recipient's turns, once. A
message to a stopped station waits and is delivered when it runs again. A message is never your answer or consent.

## Planning and review

`tares factory spec` and `tares factory session` start your own `codex` with the spec or session manual and the
crew's Tares MCP endpoint. The reviewer's second look (`tares factory second-look`) is a separate, fresh,
read-only Codex session on the same model unless `stations.reviewer.second_look_model` names another; the answer
says which. The optional challenger does not run on Codex stations yet.

## Seeing the crew

- `tares factory status` says `runtime: Codex <version>`, each station's model, and any station the crew stopped,
  with why and how to start it again. `--json` carries `runtime` for the crew and for each station.
- The console's Crew page shows each member's runtime and model, a wait for an approval in words, a member the crew
  stopped with why, and a Codex member's usage.
- **Usage**: tokens are counted when Codex writes them down. The cost of a Codex reply is unknown (the cell has no
  price for a Codex model) and is shown as unknown, never as $0. Pacing reads the account's own limits.

## Recovery

- **A station's app-server died**: the watch revives it in the same conversation. A command that was running has an
  unknown outcome; the station is told so before anything else, and `tares factory outcome` settles one by its call
  id.
- **Stopped by the crew** (its guard was off, or its last turn could not be compared with the guard's record): look
  at what it ran, then `tares factory revive <station>`; `--fresh` starts it in a new conversation.
- **`tares factory crew down`** interrupts each station's turn, waits for it to end and stops its app-server. A station
  with undelivered messages is not retired until they are settled; `--drop-mail` drops them.

## Troubleshooting

| What you see | What to do |
| -- | -- |
| `codex` not found | Install it, or set `CODEX_BIN`. |
| An older Codex is refused | Install 0.171.0 or newer. |
| No Codex login | Run `codex login`. A keyring login or an API key is not supported yet. |
| "this crew runs on Claude Code, and Tares now gives ... a launch for Codex" | A crew never moves to another runtime. Set the runtime back, or start a Codex crew in a crew home of its own. |
| A station "cannot reach an MCP server it is started with" | `tares factory doctor`: the Tares address, the crew's key, the endpoint. |
| A station says Tares does not answer | Tares is up; the station's sandbox has no network. It asks for more, and its rules decide. |
| A request is refused that should not be | Give a grant in the console; the station is restarted with it. `tares factory events` shows each decision. |
| A station shows as stopped by the crew | See Recovery. |

## Tests

The automated tests run against a fake app-server (`tests/factory/fake_codex.py`) and fixtures; they need no Codex
and no account. The real, authenticated probes are run by hand: `tests/factory/codex_probe/` (see its README), with
a signed-in `codex` and `TARES_PROBE_CODEX` naming it.
