# The crew's machine interface

`tares factory <command> --json` prints JSON for a program to read. This folder holds the schemas
(JSON Schema, draft 2020-12). `tests/test_crew_contract.py` validates what the real commands print
against them, and `tests/fixtures/crew_contract/` holds a sample of each.

| Command | Prints | Schema |
|---|---|---|
| `status --json` (also `crew status`) | one document | `status.json` |
| `roster --json` | one document | `roster.json` |
| `desk --json` (also `crew desk`) | one document | `desk.json` |
| `events --json [--follow] [--since <cursor>]` | one document per line | `events.json` |
| `tidy --json` | nothing on success; the error shape on a refusal | `error.json` |
| any of them, when it fails with `--json` | one document | `error.json` |

Only the Python engine prints JSON. With `TARES_FACTORY_ENGINE=bash`, `status`, `roster` and `desk` print their
text, as before, and `events` has no bash version: it prints its usage on stderr and exits 1. A command run
without `--json` is unchanged. `tidy --json` shapes failures only: on success it prints nothing on stdout, as
`tidy` does without it.

## Rules

- stdout holds the JSON and nothing else: compact, UTF-8, one document per line, LF line ends, on every
  platform. Notes for a person (a station named twice, the crew on another Tares) stay on stderr.
- Every document has `schema_version` as its first key. It is the number in `schema_version.json`. A change
  that keeps old readers working (a new optional field) keeps the number; any other change raises it.
- Callers ignore keys they do not know, and branch on `error.code`, never on `error.message`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The command ran. `status --json` and `roster --json` say 0 for a crew with no stations too. |
| 1 | The command ran and failed. With `--json` stdout holds `{"schema_version": 1, "error": {"code": "...", "message": "..."}}`; without it the same message is a line on stderr. |
| 2 | A usage error or a refusal before the command ran: an unknown argument, a cursor that is not one, `events` without `--json`, an unknown `TARES_FACTORY_ENGINE`, a command the Python engine does not have yet. The message is text on stderr; with `--json`, a usage error of a command that has JSON is the error shape. |

`error.code` is one of: `error` (a refusal with no more specific name), `usage`, `bad_cursor`,
`station_locked`, `claude_not_found`, `sessions_unreadable`, `job_state_unreadable`, `tares_error`
(a call to Tares failed). A bug with `--json` is `error` with the exception named.

## events

`events.jsonl` in the crew home is the log every crew command appends to. `events --json` sends it as a
stream: `{"schema_version": 1, "cursor": "<file id>:<offset>", "event": {"at", "station", "event", ...}}`
per line. An event has `at`, `station` and `event`; the rest is optional: `detail` for most events, `tool` and
`reason` for a `guard-deny` line. Callers ignore keys they do not know.

- Without `--follow` it sends what the file holds (from `--since`) and stops. With `--follow` it keeps
  reading as the file grows, until the reader goes away.
- `cursor` is opaque: keep the last one and give it back as `--since` after a reconnect, and nothing older is
  sent again. It is the file's id (`os.stat().st_ino`, on every platform) and the byte offset after the line.
- Without `--since` it starts at the beginning of the file as it is now.
- A file that was truncated is read again from its start; a file that was rotated (another id) is read from
  its start, after the old file is read to its end (the old file is kept open between polls, so a line
  written just before the rename is still sent). A `--since` cursor of another file, or past the end of this
  one, starts at the beginning too: a line may then arrive twice.
- Three cases lose or miss lines, and a caller that cannot accept that reads the file itself:
  - A file truncated and written past the old offset between two polls (0.25 s) looks like a file that
    only grew: the lines before the old offset are not seen.
  - After a reconnect, a file removed and made again that got the same id and is not smaller than the
    cursor's offset is read from the cursor, not from its start.
  - On Windows an open file cannot be renamed, so the file is closed between polls and the last line
    written just before a rotation can be missed.
- Only complete lines are sent: a line still being written is waited for. A line that is not a JSON object
  is skipped.
