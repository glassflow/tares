# Working on Tares

For contributors and coding agents working in this repository. The product docs live at
https://docs.glassflow.ai/tares; this file is about the repo itself.

## Layout

- `tares/` the package: `daemon.py` (HTTP API + console), `cli.py` (`tares up`, `tares mcp`),
  `store.py` (DuckDB), `connectors/`, `agent*.py` (Tares agents), `mcp*.py` (the MCP proxy),
  `projects/` (projects and the Tares templates they start from).
- `ui/` the console, a Vite + React app. The daemon serves `ui/dist` and the wheel bundles it.
- `tests/` plain scripts. `demo/` the demo stack. `skills/` the install skill agents use.

## Tests

Tests are plain Python scripts, not pytest. Run one with `python tests/test_x.py`; it exits
non-zero on failure. CI runs the list in `.github/workflows/ci.yml`; add a new test file to that
list or it never runs.

```bash
uv venv && uv pip install -e ".[otlp-grpc]"
python tests/test_cli.py
```

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
| `TARES_CATALOG` | a catalog file to import on first boot (only while the catalog is empty) |
| `ANTHROPIC_API_KEY` | model key for Tares agents and Ask; a key saved in the console wins |
| `ANTHROPIC_AUTH_TOKEN` | a bearer token instead of the key, for a gateway that wants `Authorization: Bearer`; beats the key when both are set |
| `ANTHROPIC_BASE_URL` | an Anthropic-compatible gateway (LiteLLM, Portkey, a Bedrock or Vertex proxy) instead of api.anthropic.com; `TARES_ANTHROPIC_BASE` is the old name, still read |
| `TARES_AGENT_MODEL` | model for Tares agents |
| `TARES_MAX_DB_SIZE` | cap on the DuckDB file; `/api/usage` reports against it |
| `TARES_SLACK_BOT_TOKEN`, `TARES_SLACK_SIGNING_SECRET` | the Slack surface |
| `TARES_SEED_PROJECT` | template of the project to seed on first boot (`TARES_SEED_USECASE` still read) |
| `TARES_TRACING_ENABLED` | agent tracing on/off (a console setting wins over it) |
| `TARES_TRACING_PROVIDER`, `TARES_TRACING_ENDPOINT` | `rius` (endpoint preset) or `otlp` (any OTLP/HTTP endpoint) |
| `TARES_TRACING_API_KEY`, `TARES_TRACING_HEADERS` | bearer key, or `k=v,k2=v2` headers for the exporter |
| `TARES_INSTANCE_NAME` | prefix of the trace `service.name` (`<instance>/<agent>`); default the hostname |

`grep -rhoE "TARES_[A-Z_]+" tares/*.py | sort -u` is the authoritative list.

## Release

`scripts/release.sh <version>` bumps `pyproject.toml`, commits and tags. Pushing the tag runs
`release.yml` (PyPI) and `docker-publish.yml` (`ghcr.io/glassflow/tares:<version>`). Add a
`CHANGELOG.md` entry first.

## Writing rules

- No em dashes in any rendered string: console copy, CLI output, docs, README. Use a comma, a
  colon or a new sentence.
- Plain language. Name the observable symptom, then the cause.
- Keep commits short, no attribution trailers.
