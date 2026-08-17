# ── Stage 1: install Python dependencies ──────────────────────────────────────
FROM python:3.12-slim AS deps
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: compile the resource index ────────────────────────────────────────
# Hermetic — parses and validates resources/resources.yaml against
# curriculum/topics.yaml and writes a read-only SQLite file. No network calls,
# so this stage (and the app image) never depends on the sites it links to
# being reachable at build time. See docs/IMPLEMENTATION_PLAN.md §7.2.
FROM python:3.12-slim AS resources
WORKDIR /build
RUN pip install --no-cache-dir pyyaml
COPY curriculum/ ./curriculum/
COPY resources/ ./resources/
COPY tools/build_resource_index.py ./tools/build_resource_index.py
RUN python tools/build_resource_index.py \
      --in resources/resources.yaml \
      --curriculum curriculum/topics.yaml \
      --out /out/resources.sqlite

# ── Stage 3: runtime ───────────────────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app
COPY --from=deps /install /usr/local
COPY wsgi.py ./
COPY app/ ./app/
COPY curriculum/ ./curriculum/
COPY migrations/ ./migrations/
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Overwrites anything under app/data/ from the build context with the
# freshly-built, validated index — the source of truth is always this stage's
# output, never a stray local file.
COPY --from=resources /out/resources.sqlite ./app/data/resources.sqlite

# SQLite lives here; some hosting platforms run the container as an arbitrary
# non-root UID, so this is created explicitly and made writable by any UID —
# see README → "Other defensive choices worth keeping".
RUN mkdir -p /data && chmod 777 /data

# An arbitrary non-root UID with no matching /etc/passwd entry has no
# HOME, which defaults to "/" — gunicorn's control socket then tries to
# create /.gunicorn there and fails with Permission denied. Several
# hosting platforms run containers as a non-root UID regardless of what
# this Dockerfile specifies; /tmp is writable by any UID, unlike "/".
# Cheap, no downside — keep this even if your current platform doesn't
# need it.
ENV HOME=/tmp

# atlasflow requires the container to listen on port 3000 and bind
# 0.0.0.0 (not 127.0.0.1), with no override supported — see README →
# "Deploying to atlasflow". 3000 is the default here for exactly that
# reason; $PORT stays overridable for local dev or other platforms.
EXPOSE 3000

CMD ["/app/docker-entrypoint.sh"]
