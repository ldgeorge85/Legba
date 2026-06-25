# syntax=docker/dockerfile:1.7
#
# legba-ui-v3 — multi-stage build of the Dockview workstation SPA.
#
# Pattern (b) from the containerization brief: this image is a
# build-only job. It produces the static `dist/` and copies it into
# a shared docker volume (`legba_ui_dist`) under /dist. The substrate's
# `legba-caddy` container mounts the same volume read-only and serves
# the files at the configured vhost (LEGBA_PUBLIC_DOMAIN; default localhost).
#
# Pattern rationale: avoids running a second long-lived UI container
# whose sole job is to `npm run preview` or rerun `vite preview`. The
# build-job runs once on `up -d`, exits 0, and the caddy container
# carries the runtime topology. Simpler restart semantics; the SPA
# tree is reproducible from the build job and easy to inspect via
# `docker run --rm -v legba_ui_dist:/dist alpine ls /dist`.
#
# Re-running the build job (e.g. after a TypeScript change) re-emits
# the volume contents. Caddy serves the new files on next request
# without restart.
#
# Build: docker compose --profile ui build legba-ui-build
# Run:   docker compose --profile ui up legba-ui-build  (one-shot)
# Verify: docker run --rm -v legba_ui_dist:/dist alpine sh -c 'ls /dist/assets | head'

# --------------------------------------------------------------- build
FROM node:20-slim AS build

WORKDIR /app

# Cache layer: only the lockfile + package.json bust npm install re-runs
# when deps change, not when source changes. We use `npm install`
# (not `npm ci`) because the existing legba-ui-v3 lockfile drifted
# from package.json during the 2026-05-21 typecheck-fix pass
# (@types/node + a few testing libs added without a lockfile
# regen, per RUNBOOK §12.2). The brief explicitly says "no npm on
# the host" — so the lockfile regen runs inside this container
# the first time a build happens after a package.json edit.
# `npm install` will materialize the missing entries and update
# /app/package-lock.json inside the build layer (the host copy
# stays stale until someone copies the regen out, which is fine
# for the container path).
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# Copy the rest of the UI source. .dockerignore excludes node_modules
# + dist + tsconfig.tsbuildinfo (so a partial host build doesn't
# pollute the container build).
COPY . .

# Strict typecheck + build. The vite build script in package.json
# runs `tsc -b && vite build` — both must pass.
RUN npm run build && ls -la dist

# --------------------------------------------------------------- publish
# Tiny final stage carrying just the static tree. The compose
# orchestration mounts the volume into /out and the entrypoint
# rsyncs (or cp -a) the dist into it. Using alpine because rsync
# is already there and we keep the image under 10 MB.
FROM alpine:3.20 AS publish

WORKDIR /dist-src

# Bring in the built tree.
COPY --from=build /app/dist /dist-src/

# Default entrypoint: copy /dist-src into /out (the mounted shared
# volume) and exit. `rm -rf /out/* /out/.[!.]*` first so re-runs
# pick up file deletions, not just additions. The `find ... -delete`
# form keeps the mount point alive (no `rm -rf /out` which would
# unlink the bind target itself).
ENTRYPOINT ["sh", "-c", "find /out -mindepth 1 -delete && cp -a /dist-src/. /out/ && echo 'legba-ui-v3 dist published to /out (volume legba_ui_dist)'"]
