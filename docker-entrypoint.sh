#!/bin/sh
set -e

# Defaults to 3000 to match what atlasflow hardcodes and doesn't allow
# overriding (see README). Stays overridable via $PORT for local dev or
# other hosting platforms that use the Heroku/Railway/Render-style
# convention of injecting it instead.
export PORT="${PORT:-3000}"

# Explicit, not relying on Flask CLI auto-discovery: this project has both a
# wsgi.py file and an app/ package, and auto-discovery's own file-vs-package
# preference is the same kind of ambiguity gunicorn's app:app would hit below.
export FLASK_APP=wsgi:app

# Idempotent by design (docs/IMPLEMENTATION_PLAN.md §11 build order / §16
# curriculum ownership): safe to run on every boot, including redeploys where
# nothing changed. `flask db upgrade` applies any new migrations;
# seed-curriculum re-syncs topics/concepts from curriculum/topics.yaml so a
# curriculum edit ships on the next redeploy with no separate manual step.
#
# RUN_BACKGROUND_WORKER=0 is forced here explicitly, not just left unset —
# if the container was started with it already set to 1 (e.g. `docker run
# --env-file .env` where .env has RUN_BACKGROUND_WORKER=1 for local `flask
# run` convenience, or a Runtime Variable set by mistake), these two
# one-shot commands would otherwise inherit that and try to reap jobs from a
# table that doesn't exist yet on a first-ever deploy. Don't rely on "nobody
# sets this from outside" — force it.
RUN_BACKGROUND_WORKER=0 flask db upgrade
RUN_BACKGROUND_WORKER=0 flask seed-curriculum

# Only the actual server process reaps stale jobs and starts the background
# worker thread (docs §11) — set unconditionally here, overriding whatever
# it was before.
export RUN_BACKGROUND_WORKER=1

# exec replaces this shell process with gunicorn, so gunicorn becomes
# PID 1 and receives SIGTERM/SIGINT directly from `docker stop` for a
# clean shutdown. Without it (or with a shell-form CMD in the Dockerfile
# instead of the JSON-array form used here), Docker signals the shell
# instead, which doesn't forward it, and has to SIGKILL after the
# stop timeout — this is also what a "JSONArgsRecommended"
# Dockerfile-linter warning on CMD is telling you to fix.
#
# The entry point is wsgi.py, not app.py: this project's own top-level
# package is named `app/`, and `import app` resolves to that package (Python
# prefers a same-named package over a same-named .py file in the same
# directory) — `gunicorn app:app` would fail with AttributeError since
# app/__init__.py exposes `create_app`, not a bare `app` instance.
exec gunicorn -b "0.0.0.0:${PORT}" --workers 1 --threads 4 --worker-class gthread --timeout 120 wsgi:app
