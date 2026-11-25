# Codex probes (TR-1576)

By-hand checks of what a Factory station on Codex would rest on. They are not in CI: most need a signed-in
`codex` CLI and spend a little of that account's quota (the whole set, with reruns, moved a Plus plan's 5-hour
window from 6% to 8%). Results and what they mean are in `RESULTS.md` here, with the numbers of each run in `results/`.

Nothing here touches a real repository or service. Each scenario builds a throwaway crew home, a git
repository with a local bare remote, and fake `gh` and `trivy` commands that only write a line to a file
(`fixture.py`). A denied action is one that left no line and no commit.

```bash
uv venv && uv pip install -e .
export TARES_PROBE_CODEX=/path/to/codex        # the version under test; 0.171.0 when this was written
export PATH="$PWD/.venv/bin:$PATH"             # so `tares factory heavy` is this checkout's
python tests/factory/codex_probe/probe_guard.py     --out /tmp/codex-probe/guard
python tests/factory/codex_probe/probe_transport.py --out /tmp/codex-probe/transport
python tests/factory/codex_probe/probe_hooks.py     --out /tmp/codex-probe/hooks
python tests/factory/codex_probe/probe_pace.py      --out /tmp/codex-probe/pace
python tests/factory/codex_probe/probe_mcp.py       --out /tmp/codex-probe/mcp
python tests/factory/codex_probe/probe_evidence.py  --out /tmp/codex-probe/evidence   # offline, no codex
python tests/factory/codex_probe/probe_launch.py    --out /tmp/codex-probe/launch     # TR-1592: the product's own start
python tests/factory/codex_probe/probe_station.py   --out /tmp/codex-probe/station    # TR-1593: turn state, the next prompt, the stop
python tests/factory/codex_probe/probe_guard_on.py  --out /tmp/codex-probe/guard-on   # TR-1594: the guard, proven on
python tests/factory/codex_probe/probe_mcp_station.py --out /tmp/codex-probe/mcp-station  # TR-1596: Tares over MCP, against a real cell it starts and throws away
```

Run them with the checkout's own interpreter (`.venv/bin/python`): a station's guard is `tares hook codex-guard`,
and the probes that start a station through the product put that interpreter's folder first on the station's PATH.

`--only NAME ...` runs some scenarios of the first two, of `probe_launch.py`, of `probe_station.py` and of `probe_guard_on.py`. Each run writes `results.json` (or `hooks.json`,
`pace.json`, `mcp.json`, `evidence.json`, `launch.json`, `station.json`, `guard_on.json`, `mcp_station.json`) and one wire transcript per scenario: every JSON-RPC message in both
directions, so the evidence is the wire itself. The transcripts hold local paths and are not committed;
`fixtures/commands.wire.jsonl` is a small one with the paths replaced.

| File | What it is |
|---|---|
| `appserver.py` | a client for `codex app-server` over stdio or a Unix socket (WebSocket frames), standard library only |
| `fixture.py` | the throwaway world, and the hook tables: the guard as a station runs it (`tares hook codex-guard`, the product's `tares/crew/codex_guard.py` since TR-1594), and the plain `guard.py` for the controls |
| `slack_probe_server.py` | a local MCP server with a Slack-like write tool and read tool |
| `probe_guard.py` | halt in the middle of a turn, wrong role, heavy commands, MCP tools, helpers, typed input, broken hooks |
| `probe_transport.py` | Unix socket against stdio: controller loss, pending approvals, two controllers, crashes, `crew down` |
| `probe_hooks.py` | which hook events fire, their payloads, and the challenger's two answers |
| `probe_pace.py` | a Codex quota reading through the crew's own `pace_now` |
| `probe_mcp.py` | a station's token and identity headers on an HTTP MCP endpoint |
| `probe_evidence.py` | Codex command records through the crew's own `match_check` |
| `probe_launch.py` | TR-1592: the crew's own launch code against the real Codex: the socket, a stale one, the person's own setup kept out, an older Codex |
| `probe_guard_on.py` | TR-1594: the crew's own check that a station's guard runs, against the real Codex: the record at the start of a turn and for each call, a station without trust for its hooks stopped before it ran anything, a repository's own Codex setup kept out |
| `probe_mcp_station.py` | TR-1596: a Codex station on a real Tares cell that the probe starts and throws away: its ticket read and started over MCP as itself, the crew's key looked for everywhere it must not be, a wrong key, no key, and the endpoint down |
| `cell.py` | that cell: `tares up` and `tares mcp` in a folder of their own, a login required, set to the Codex runtime, with a project, a ticket and a key for the crew |
| `standin_mcp.py` | a stand-in for the crew's MCP endpoint, for the probes that start a station and are not about Tares: a Codex station does not start without one |
| `probe_station.py` | TR-1593: the crew's own station code against the real Codex: turns with nobody connected, a prompt refused in a turn, a conversation carried on after a stop, a station blocked on an approval |

The model and effort default to `gpt-5.6-luna` and `low` (`TARES_PROBE_MODEL`, `TARES_PROBE_EFFORT`). The
scenarios depend on the model doing the steps it is asked for; a scenario that fails because the model did
something else says so in its `commands` and `final_answer`, and is worth one more run before it is believed.
