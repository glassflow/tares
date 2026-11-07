# Working on Tares

For contributors and coding agents working in this repository. The product docs live at
https://docs.glassflow.ai/tares; this file is about the repo itself.

## Layout

- `tares/` the package: `daemon.py` (HTTP API + console), `cli.py` (`tares up`, `tares mcp`),
  `store.py` (DuckDB), `connectors/`, `agent*.py` (Tares agents), `models.py` (the model
  provider adapter both agent loops call), `mcp*.py` (the MCP proxy),
  `projects/` (projects and the Tares templates they start from).
- `ui/` the console, a Vite + React app. The daemon serves `ui/dist` and the wheel bundles it.
- `ee/` the enterprise code, the package `tares_ee`, under `ee/LICENSE`. It is loaded through the
  `tares.ee` entry point group and never imported by `tares/`.
- `tests/` plain scripts. `demo/` the demo stack. `skills/` the install skill agents use.

## Tests

Tests are plain Python scripts, not pytest. Run one with `python tests/test_x.py`; it exits
non-zero on failure. CI runs the tests named in `tests/ci_list.txt` (Python) and
`tests/factory/ci_list.txt` (bash), in parallel shards (`tests/ci_run.py`). A new test adds its line
there, in its sorted place (byte order, `LC_ALL=C sort`); never list tests in ci.yml.
The console's browser suites (`ui/tests/*.e2e.cjs`) are named in `ui/tests/e2e_suites.txt`, same rule (sorted,
one name per line); the `e2e` job runs them in shards (`tests/ci_e2e.py`, one port per suite). Run one by hand with
`cd ui && npm run build && TARES_E2E_PORT=<free port> node tests/<name>.e2e.cjs`.
Browser suites locally: `cd ui && npm ci && npm run build && npx playwright install chromium`, then from the repo root
`python tests/ci_e2e.py` (all) or `python tests/ci_e2e.py --list` (names).
ee tests: `python tests/ci_run.py --suite ee` (Python in `ee/tests/ci_list.txt`, bash in `ee/tests/factory/ci_list.txt`).
A licensed scratch daemon: `python ee/tests/licensed.py serve --port 8799 --data-dir $(mktemp -d) [--edition factory] [--auth] [--licence expired]`.
A Python test CI should not run goes in `tests/ci_skip.txt` as `name: reason`; `test_ci_list` fails for a
`tests/test_*.py` in neither file.

```bash
uv venv && uv pip install -e ".[otlp-grpc]"
python tests/test_cli.py
```

After you change entry points in `pyproject.toml`, re-run `pip install -e .` (or
`uv pip install -e .`) so the installed metadata updates.

## Console

```bash
cd ui && npm ci && npx tsc --noEmit && npm run build   # produces ui/dist
```

Rebuild after any change under `ui/src`; the daemon serves whatever is in `ui/dist`.
`TARES_UI_DIST` points the daemon at another build directory.

## Environment contract

Every knob is a `TARES_*` variable read at start. The ones you will meet most:

| Variable | Meaning |
|---|---|
| `TARES_HOME` | data directory (default `~/.tares`); `--data-dir` overrides |
| `TARES_HOST`, `TARES_PORT` | daemon bind address (127.0.0.1:8787) |
| `TARES_MCP_HOST`, `TARES_MCP_PORT`, `TARES_MCP_TRANSPORT` | the MCP endpoint (127.0.0.1:8788, streamable-http) |
| `TARESD_URL` | where `tares-mcp` finds the daemon |
| `TARES_AUTH_TOKEN` | turns auth on; also the token `tares-mcp` presents |
| `TARES_KEYRING` | `on` lets `tares login` keep its key in the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) instead of `login.json`; off by default, because the plugin's MCP header helper, `ship.py` and the bash doctor still read the file |
| `TARES_CATALOG` | a catalog file to import on first boot (only while the catalog is empty) |
| `ANTHROPIC_API_KEY` | model key for Tares agents and Ask; a key saved in the console wins |
| `ANTHROPIC_AUTH_TOKEN` | a bearer token instead of the key, for a gateway that wants `Authorization: Bearer`; beats the key when both are set |
| `ANTHROPIC_BASE_URL` | an Anthropic-compatible gateway (LiteLLM, Portkey, a Bedrock or Vertex proxy) instead of api.anthropic.com; `TARES_ANTHROPIC_BASE` is the old name, still read |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | seed an OpenAI provider entry; a provider saved in the console wins |
| `TARES_PLATFORM_PROVIDER_URL`, `TARES_PLATFORM_PROVIDER_KEY`, `TARES_PLATFORM_PROVIDER_NAME` | seed an OpenAI-compatible provider entry the deployment hands the cell (Tares Cloud's LiteLLM with a per-cell key); the default unless a console choice or `TARES_MODEL_PROVIDER` says otherwise; not editable in the console |
| `TARES_MODEL_PROVIDER` | id of the default provider (`anthropic`, `openai`, or a saved endpoint's id) when none is chosen in the console |
| `TARES_AGENT_MODEL` | model for Tares agents on the default provider |
| `TARES_MAX_DB_SIZE` | storage limit `/api/usage` and `/health` measure against; unset = the volume the database sits on |
| `TARES_INGEST_PAUSE_PCT` | share of the limit at which ingest is refused (507) and polls pause; default 95 |
| `TARES_SLACK_BOT_TOKEN`, `TARES_SLACK_SIGNING_SECRET` | the Slack surface |
| `TARES_EDITION` | the cell's edition on its first start: factory, sre or platform; after that the value saved in the cell wins and Settings, Edition changes it |
| `TARES_SEED_PROJECT` | template of the project to seed on a brand-new cell, one with no projects yet; otherwise skipped (`TARES_SEED_USECASE` still read) |
| `TARES_WEBHOOK_ALLOW_PRIVATE` | let a project key subscribe a webhook on an internal address (loopback, private, link-local); off by default |
| `TARES_AGENT_DAILY_CAP` | runs per Tares agent per day (default 50); a value saved under Settings, Agents wins over it |
| `TARES_TRACING_ENABLED` | agent tracing on/off (a console setting wins over it) |
| `TARES_TRACING_PROVIDER`, `TARES_TRACING_ENDPOINT` | `rius` (endpoint preset) or `otlp` (any OTLP/HTTP endpoint) |
| `TARES_TRACING_API_KEY`, `TARES_TRACING_HEADERS` | bearer key, or `k=v,k2=v2` headers for the exporter |
| `TARES_PUBLIC_URL` | the address people reach the cell at, for the ingest URLs the guided setup shows; default the request's own |
| `TARES_MCP_URL` | the MCP address the guided setup gives an outside agent; default `TARES_PUBLIC_URL` plus `/mcp` |
| `TARES_FACTORY_MCP_URL` | the crew's shared MCP endpoint, set by `tares factory` for stations and the spec session; the plugin connects there (default: local Tares gives 127.0.0.1:8788/mcp, a remote one its own `/mcp`) |
| `TARES_FACTORY_CRED_FILE` | path of the crew's token file, read by the plugin's MCP header helper (`claude-plugin/bin/mcp-headers`); same file as `TARES_FACTORY_TOKEN_FILE` |
| `TARES_METRICS_TOKEN` | when set, `/metrics` wants `Authorization: Bearer <token>` and answers 401 without it; unset keeps it public (the cell's auth token is not accepted there) |
| `TARES_INSTANCE_NAME` | prefix of the trace `service.name` (`<instance>/<agent>`); default the hostname |

`grep -rhoE "TARES_[A-Z_]+" tares/*.py | sort -u` is the authoritative list.

## Release

Final releases are held while the file `RELEASE_BLOCK` exists; `scripts/check_release_block.sh`
enforces it in `release.sh`, `release.yml` and `docker-publish.yml`. Release candidates (`-rc.N`) pass.

`scripts/release.sh <version>` bumps `pyproject.toml`, commits and tags. Pushing the tag runs
`release.yml` (PyPI) and `docker-publish.yml` (`ghcr.io/glassflow/tares:<version>`). Add a
`CHANGELOG.md` entry first.

## Writing rules

- No em dashes in any rendered string: console copy, CLI output, docs, README. Use a comma, a
  colon or a new sentence.
- Plain language. Name the observable symptom, then the cause.
- Keep commits short, no attribution trailers.
