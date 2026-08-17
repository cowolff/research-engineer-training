# repo_template

A minimal Flask + Docker starting point for new projects deployed on
[atlasflow](https://atlasflow.com). `GET /` returns `Hello, World!` — that's
the entire app. Copy this folder out as the root of a new repo and build
up from here.

## Why it's shaped this way

Every choice below came from actually deploying a real app to atlasflow
and hitting the failure mode it's guarding against — not speculative
best practice. Keep these even after you've replaced the Hello World
route with real functionality.

### Deploying to atlasflow

atlasflow's [container requirements](https://atlasflow.com/docs/guides/container-requirements)
are strict and unconfigurable:

- **Your container must listen on port 3000 and bind to `0.0.0.0`** —
  not `127.0.0.1`. Binding to localhost only prevents atlasflow from
  reaching the app over its internal network. **Custom port configuration
  isn't supported at all** — atlasflow always connects to 3000, so
  `docker-entrypoint.sh` defaults to it without needing any env var set.
- **The health check is `GET /`**, not `/health` or anything else. It's
  probed every 15 seconds with a 5-second timeout, expects a 2xx, and
  atlasflow stops routing traffic to the deployment after 3 consecutive
  failures. Their own checklist specifically calls out the most common
  way to fail this: **`/` redirecting to a login page** for anonymous
  visitors. If you add authentication later, make sure `/` (or whatever
  route you point the health check at) stays reachable and fast for an
  unauthenticated request — see `tests/test_app.py` for a regression
  test that pins this down.
- **The `CMD` in the Dockerfile is JSON-array form calling a script that
  `exec`s gunicorn**, not a bare shell-form `CMD gunicorn ...`. Shell
  form runs under `/bin/sh -c`, which becomes PID 1 and does *not*
  forward `SIGTERM`/`SIGINT` to gunicorn — `docker stop` (or atlasflow
  redeploying/stopping the container) then has to wait out the full
  stop timeout and `SIGKILL` it instead of a clean shutdown. `exec` inside
  the entrypoint script replaces the shell process with gunicorn so it
  receives the signal directly. (This is also what a Dockerfile linter's
  `JSONArgsRecommended` / `DL3025` warning on `CMD` is telling you to fix.)

### Environment variables on atlasflow

atlasflow splits env vars into **Build** and **Runtime** scopes in the
project settings. A Build-scoped variable is available while the image
is being built but **is not present in the running container** unless
you also add it as a Runtime variable. Any secret or config value your
app reads at request/startup time (a `SECRET_KEY`, a `DATABASE_URL`, an
API key) needs to go in **Runtime Variables** — putting it in Build
Variables only will crash the app at startup with a "missing config"
error that looks unrelated to this distinction.

### Other defensive choices worth keeping

- **`ENV HOME=/tmp`** — gunicorn creates a control socket under
  `$HOME/.gunicorn/` by default. An arbitrary non-root UID with no
  matching `/etc/passwd` entry (something some hosting platforms impose
  regardless of what the Dockerfile specifies) has no `HOME`, which
  defaults to `/` — and gunicorn then fails with `Permission denied`
  trying to create a directory there. `/tmp` is writable by any UID.
  Costs nothing even if your current platform doesn't need it.
- **Two-stage Dockerfile** (`deps` → runtime) — keeps the final image
  free of pip's build-time cruft and cleanly separates "what changes
  when dependencies change" from "what changes when app code changes"
  for Docker's layer cache.
- **If you add a database or anything that writes to disk**: some
  hosting platforms run the container as an arbitrary non-root UID.
  `VOLUME`-declared directories are root-owned by default and won't be
  writable under those UIDs — create the directory explicitly and
  `chmod` it permissively (it holds only this app's own data, not shared
  with other tenants, so that's a reasonable tradeoff for working under
  an unknown runtime UID). Also confirm with atlasflow's docs whether
  local disk on their microVMs actually persists across
  restarts/redeploys before relying on it for anything that needs to
  survive one — this template has no persistence story at all yet.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug
```

Or with Docker, matching production:

```bash
docker build -t repo-template .
docker run --rm -p 3000:3000 repo-template
curl http://localhost:3000/
```

## Running tests

```bash
source .venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

## License
MIT
