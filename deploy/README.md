# Deploying Tares

Tares is one engine (taresd) with config **profiles** — the same image serves local, demo, and
self-hosted single-tenant.

```
deploy/
  docker/Dockerfile             one image: daemon + remote MCP (same image, different command)
  compose/docker-compose.yml    one open host, no auth (pulls the prebuilt image)
  compose/docker-compose.selfhost.yml   self-host, auth (pulls the prebuilt image)
  compose/docker-compose.build.yml      override to build the image locally instead of pulling
.github/workflows/docker-publish.yml    CI: build + push the image to GHCR
```

## The image

One multi-stage image (compiles the console SPA, bakes it into the wheel, installs the
extras). **CI builds and publishes it to GHCR** on every push to `main` and on `v*` tags, so servers
**pull** a prebuilt image rather than building on the box. The image ref is
`${TARES_REGISTRY:-ghcr.io/glassflow/tares}:${TARES_VERSION:-…}`.

- **Version:** both compose files default to **`:latest`** — CI publishes only on release tags, so
  `:latest` tracks the newest **release** (not `main`). For a reproducible deploy, pin a specific
  release without editing files via `TARES_VERSION=0.1.4`.
- **Registry:** `TARES_REGISTRY=registry.example.com/tares` points at another registry (DOCR,
  Docker Hub).
- **GHCR access:** make the package public (GitHub → package settings) or `docker login ghcr.io` on
  the server with a PAT, so `docker compose pull` can fetch it.
- **Build locally** instead of pulling (dev, or no CI) — layer the build override and `build`, which
  inherits the base file's tag so a plain `up` uses it:
  ```sh
  docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.build.yml build
  ```

### Cutting a release

```sh
scripts/release.sh 0.0.2          # bumps pyproject version, commits, tags v0.0.2
git push && git push origin v0.0.2
```

Pushing the tag triggers CI to publish `ghcr.io/glassflow/tares:<version>` (plus `:<major>.<minor>`
and `:latest`). Self-host deploys on `:latest` upgrade with
`docker compose -f …selfhost.yml pull && up -d`; pinned deploys bump `TARES_VERSION=<version>`.

## Scenario 2: one open host

`docker compose` brings up the daemon, the remote MCP server, and Caddy fronting both on one
hostname, with no auth. Use it on a private network or to try Tares; anything public takes
Scenario 3. Crew machines and the Claude Code plugin reach it at the host's address.

```sh
# local (plain HTTP on :80) — pulls the prebuilt image
docker compose -f deploy/compose/docker-compose.yml up -d
#  → console http://localhost  ·  MCP http://localhost/mcp  (streamable-http)

# a non-privileged host port instead of 80
TARES_HTTP_PORT=8820 docker compose -f deploy/compose/docker-compose.yml up -d

# a domain turns on automatic HTTPS (point its DNS A record at the host first)
TARES_DOMAIN=tares.example.com docker compose -f deploy/compose/docker-compose.yml up -d
#  → console https://tares.example.com  ·  MCP https://tares.example.com/mcp
```

An agent connects by adding the `/mcp` URL as a streamable-http MCP server. Routing: `/mcp` and
`/sse` → the MCP service; everything else → taresd (console + API).

## Scenario 3 — self-hosted single-tenant

A writable instance for one team, behind a shared token. `docker-compose.selfhost.yml` runs taresd
+ the MCP server + Caddy, all requiring `TARES_AUTH_TOKEN`.

```sh
export TARES_AUTH_TOKEN=$(openssl rand -hex 24)   # the access token (humans + agents)
export TARES_DOMAIN=tares.acme.com
docker compose -f deploy/compose/docker-compose.selfhost.yml pull   # fetch the prebuilt image
docker compose -f deploy/compose/docker-compose.selfhost.yml up -d
# update later:  docker compose -f …selfhost.yml pull && up -d   (:latest → newest release)
```

- **Console:** browse to the host; the login screen takes the token.
- **MCP (agents):** add `https://<host>/mcp` as a streamable-http MCP server with
  `Authorization: Bearer $TARES_AUTH_TOKEN`. The token is required to connect and is forwarded to
  taresd.
- **Crew machines:** each person runs `tares login` against the host; the Claude Code plugin then
  sends the session lines with that person's key. Ingest is gated by auth like every other route.
- **Auth model:** one shared bearer token. For per-user SSO, front the console with oauth2-proxy /
  Tailscale / Caddy basic-auth (machine paths — MCP + ingest — keep their tokens).

**Single-writer.** The SQLite store lives on the volume, so this is exactly one `taresd` + one volume:
**do not scale to replicas.** Back up the data by snapshotting the `tares-data` volume (or copying
`/data/tares.sqlite` with its `-wal` file, and `/data/archive/`); restore by putting them back before boot.
