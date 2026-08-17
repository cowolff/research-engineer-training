# Maluna Engineer Training

A scenario-based training tool for Cognitive Science students becoming AI
research engineers. Mistral (`mistral-medium`) generates engineering scenarios
— including fabricated logs and broken output to debug — grades free-text
answers against a rubric fixed before the student answers, and writes a
tutorial the moment a student repeatedly misses an essential concept.
Tutorials are canonical per concept (generated once, shared across every
student who later misses the same thing) and cite only a build-time-compiled,
curated resource index — never a URL the model made up.

Full design rationale lives in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).
This README covers running it.

## Prototype status

This runs as a **single container on [atlasflow](https://atlasflow.com)**,
storing all application data in SQLite on the container's local disk — which
atlasflow resets on every redeploy (see IMPLEMENTATION_PLAN.md §9). Use
`GET /export.json` while logged in to keep a copy of your attempts and
tutorials. The curriculum ships with a seed of 10 topics / 57 concepts and 46
curated resources — real breadth to demo, not yet the full target depth
(IMPLEMENTATION_PLAN.md §11's Phase C).

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then edit as needed; LLM_PROVIDER=fake needs no API key
set -a && source .env && set +a

flask db upgrade
flask seed-curriculum
python tools/build_resource_index.py --out app/data/resources.sqlite

RUN_BACKGROUND_WORKER=1 flask run --debug
```

`RUN_BACKGROUND_WORKER=1` is set inline on the `flask run` command itself, not
in `.env` — Flask's CLI auto-loads `.env` on **every** `flask` command,
including `flask db upgrade` and `flask seed-curriculum` just above, so baking
it into the file would make those crash against a database they haven't
finished setting up yet. This mirrors what `docker-entrypoint.sh` does for the
real deployment, for the same reason.

Then open <http://127.0.0.1:5000>, register an account, and click **Start
training**. With `LLM_PROVIDER=fake` (the default in `.env.example`) every
generation is instant and deterministic — no API key, no network call. Switch
to `LLM_PROVIDER=mistral` and set `MISTRAL_API_KEY` to use the real model.

To see an instructor's cohort view, set `INSTRUCTOR_EMAILS=you@example.com`
before registering with that address (or run
`flask promote-instructor you@example.com` after the fact).

### Local dev over plain HTTP

atlasflow always terminates TLS upstream, so session cookies are `Secure` by
default — correct in any real deployment, but a bare `flask run` has no TLS,
and a spec-compliant HTTP client will refuse to send a `Secure` cookie over
plain `http://`, which silently breaks login. `.env.example` sets
`SESSION_COOKIE_SECURE=false` for exactly this reason. Don't set it in
atlasflow's Runtime Variables.

### Matching production, via Docker

The quickest check that the built image itself works:

```bash
docker build -t maluna .
docker run --rm -p 3000:3000 \
  -e SECRET_KEY=dev-only \
  -e LLM_PROVIDER=fake \
  maluna
curl http://localhost:3000/
```

To run the container against your real `.env` config instead of retyping each
`-e` flag, `--env-file` works — with two overrides, because a few settings in
`.env` are specific to running `flask run` from the repo root and don't carry
over to the container as-is:

```bash
docker run --rm -p 3000:3000 \
  --env-file .env \
  -e SECRET_KEY=dev-only \
  -e SQLITE_PATH=/data/app.db \
  maluna
```

- **`SECRET_KEY`** — `.env.example` ships this commented out, so `--env-file`
  alone won't set it; either uncomment a real value in `.env` or pass it here.
- **`SQLITE_PATH`** — `.env`'s `./instance/dev.db` is a path relative to the
  repo root for local `flask run`. Inside the container the working directory
  is `/app`, and the writable location the image actually sets up is `/data`
  (see "Other defensive choices" below) — so this needs the container path,
  not the local one.
- **`RUN_BACKGROUND_WORKER`**, if it's in your `.env`, does **not** need
  overriding here — `docker-entrypoint.sh` forces it to the correct value at
  the correct point in boot regardless of what's already set when the
  container starts (see that file for why: this exact class of bug bit local
  `flask db upgrade` too, from `.env` alone, before command-specific overrides
  fixed it there — same root cause, same fix, two places it had to be applied).

The container runs migrations and re-syncs the curriculum on every boot
(`docker-entrypoint.sh`), so either form above is a real end-to-end check of
the deployed artifact, not just the Flask app in isolation.

## Running tests

```bash
source .venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

Tests never call a real LLM API or need `MISTRAL_API_KEY` — `tests/conftest.py`
always runs against `FakeProvider`, which parses the same structured prompts
the real provider builds and returns deterministic responses. This is also
what CI should run against.

## Why the deployment plumbing is shaped this way

Every choice below came from atlasflow's actual constraints, not speculative
best practice — see IMPLEMENTATION_PLAN.md §2 for the full mapping. Keep these
even as the app grows further.

### Deploying to atlasflow

atlasflow's [container requirements](https://atlasflow.com/docs/deployments)
are strict and unconfigurable:

- **Your container must listen on port 3000 and bind to `0.0.0.0`** —
  not `127.0.0.1`. Binding to localhost only prevents atlasflow from
  reaching the app over its internal network. **Custom port configuration
  isn't supported at all** — atlasflow always connects to 3000, so
  `docker-entrypoint.sh` defaults to it without needing any env var set.
- **The health check is `GET /`**, not `/health` or anything else. It's
  probed every 15 seconds with a 5-second timeout, expects a 2xx, and
  atlasflow stops routing traffic to the deployment after 3 consecutive
  failures. The most common way to fail this: **`/` redirecting to a login
  page** for anonymous visitors. `/` stays a static, DB-free page that never
  redirects — see `tests/test_app.py` for the regression tests that pin this
  down, including one that trips a monkeypatch if `/` ever touches the
  database. `GET /healthz` is a *separate*, DB-touching endpoint for humans —
  atlasflow never probes it.
- **The `CMD` in the Dockerfile is JSON-array form calling a script that
  `exec`s gunicorn**, not a bare shell-form `CMD gunicorn ...`. Shell
  form runs under `/bin/sh -c`, which becomes PID 1 and does *not*
  forward `SIGTERM`/`SIGINT` to gunicorn — `docker stop` (or atlasflow
  redeploying/stopping the container) then has to wait out the full
  stop timeout and `SIGKILL` it instead of a clean shutdown. `exec` inside
  the entrypoint script replaces the shell process with gunicorn so it
  receives the signal directly.
- **The entry point is `wsgi.py`, not `app.py`.** This project's own package
  is named `app/`, and `import app` resolves to that package — Python prefers
  a same-named package over a same-named `.py` file in the same directory.
  `gunicorn app:app` would fail with `AttributeError` since `app/__init__.py`
  exposes `create_app`, not a bare `app` instance.
- **`RUN_BACKGROUND_WORKER=1` is set only right before the final `exec
  gunicorn`** in `docker-entrypoint.sh` — not during the `flask db upgrade` /
  `flask seed-curriculum` calls just before it. Those import the app factory
  too, and starting the job-reaper / background worker against a database
  that might not have its tables yet would crash the deploy.

### Environment variables on atlasflow

atlasflow splits env vars into **Build** and **Runtime** scopes in the
project settings. A Build-scoped variable is available while the image
is being built but **is not present in the running container** unless
you also add it as a Runtime variable. Every secret or config value this app
reads at request/startup time (`SECRET_KEY`, `MISTRAL_API_KEY`,
`INSTRUCTOR_EMAILS`, ...) needs to go in **Runtime Variables** — putting it in
Build Variables only will crash the app at startup with a config error that
looks unrelated to this distinction. Full list: IMPLEMENTATION_PLAN.md §13.

`SECRET_KEY` specifically must be a **stable** Runtime variable, not generated
at boot — otherwise every redeploy logs everyone out on top of the data reset
described above.

### Other defensive choices worth keeping

- **`ENV HOME=/tmp`** — gunicorn creates a control socket under
  `$HOME/.gunicorn/` by default. An arbitrary non-root UID with no
  matching `/etc/passwd` entry (something some hosting platforms impose
  regardless of what the Dockerfile specifies) has no `HOME`, which
  defaults to `/` — and gunicorn then fails with `Permission denied`
  trying to create a directory there. `/tmp` is writable by any UID.
- **Three-stage Dockerfile** (`deps` → `resources` → runtime) — the
  `resources` stage compiles `resources/resources.yaml` into a read-only
  SQLite index, hermetically (no network calls during the build — see
  IMPLEMENTATION_PLAN.md §7.2). Both non-runtime stages are cached
  independently, so an app-code change doesn't force a dependency reinstall
  or a resource-index rebuild.
- **`/data` is created explicitly and `chmod 777`'d** — some hosting
  platforms run the container as an arbitrary non-root UID, under which a
  `VOLUME`-declared or implicitly-created directory would be root-owned and
  unwritable. It holds only this app's own SQLite file, not shared with other
  tenants, so that's a reasonable tradeoff for working under an unknown
  runtime UID. Whether atlasflow's local disk actually persists across
  restarts is moot here — this app treats it as ephemeral on purpose (§9).

## License
MIT
