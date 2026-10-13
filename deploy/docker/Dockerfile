# Tares — one image for the daemon and the remote MCP server (same image, different command).
# Build from the repo root:  docker build -f deploy/docker/Dockerfile -t tares .
# syntax=docker/dockerfile:1

# 1) build the console SPA so it can be embedded in the wheel (hatch force-include ui/dist)
FROM node:20-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# 2) build + install the package (console baked in) with all connector extras
FROM python:3.12-slim AS app
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY tares/ ./tares/
# the wheel bundles the Claude Code plugin (hatch force-include), for `tares factory`
COPY claude-plugin/ ./claude-plugin/
COPY --from=ui /ui/dist ./ui/dist
RUN pip install --no-cache-dir ".[otlp-grpc]"

# Run as a non-root user. Own /data before declaring the VOLUME so a first-mount Docker named
# volume inherits the ownership; on k8s an empty PVC needs fsGroup: 1000 (set in the cell chart).
RUN groupadd -r -g 1000 tares \
 && useradd -r -u 1000 -g tares -d /data -s /usr/sbin/nologin tares \
 && mkdir -p /data && chown tares:tares /data

ENV TARES_HOME=/data
VOLUME /data
EXPOSE 8787 8788
USER tares
# the daemon by default; the compose mcp service overrides this with `tares mcp ...`
CMD ["tares", "up", "--host", "0.0.0.0", "--data-dir", "/data"]
