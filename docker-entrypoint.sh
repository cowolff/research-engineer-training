#!/bin/sh
set -e

# Defaults to 3000 to match what atlasflow hardcodes and doesn't allow
# overriding (see README). Stays overridable via $PORT for local dev or
# other hosting platforms that use the Heroku/Railway/Render-style
# convention of injecting it instead.
export PORT="${PORT:-3000}"

# exec replaces this shell process with gunicorn, so gunicorn becomes
# PID 1 and receives SIGTERM/SIGINT directly from `docker stop` for a
# clean shutdown. Without it (or with a shell-form CMD in the Dockerfile
# instead of the JSON-array form used here), Docker signals the shell
# instead, which doesn't forward it, and has to SIGKILL after the
# stop timeout — this is also what a "JSONArgsRecommended"
# Dockerfile-linter warning on CMD is telling you to fix.
exec gunicorn -b "0.0.0.0:${PORT}" --workers 1 --threads 4 --worker-class gthread --timeout 120 app:app
