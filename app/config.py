import logging
import os
import sqlite3

logger = logging.getLogger("app.config")


class ConfigError(RuntimeError):
    """Raised at startup when a required Runtime variable is missing.

    Deliberately fails loudly and names the variable: atlasflow splits env
    vars into Build vs Runtime scopes, and a secret placed only in Build
    variables is silently absent at request time — this turns that mistake
    into an immediate, legible crash instead of a mysterious 500 later.
    """


def _bool(env, name, default):
    val = env.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, env=None):
        # Bugs to avoid reintroducing: every setting below must read from
        # this resolved `env`, never bare `os.environ`. `env` is how tests
        # (and anything else constructing a Config explicitly) get a fully
        # isolated, deterministic configuration — falling through to the
        # real process environment for even one field breaks that isolation.
        # This bit in practice: importing the `litellm` package auto-loads
        # a local .env file via python-dotenv as an import side effect,
        # silently mutating the real os.environ; a `_bool` that ignored
        # `env` and read os.environ directly picked that up mid test run.
        env = env if env is not None else os.environ

        self.testing = _bool(env, "TESTING", False)
        self.llm_provider = env.get("LLM_PROVIDER", "mistral")

        self.secret_key = env.get("SECRET_KEY")
        if not self.secret_key:
            if self.testing:
                self.secret_key = "test-secret-key"
            else:
                raise ConfigError("SECRET_KEY is required (set it as a Runtime variable)")

        self.mistral_api_key = env.get("MISTRAL_API_KEY")
        if self.llm_provider == "mistral" and not self.mistral_api_key and not self.testing:
            raise ConfigError(
                "MISTRAL_API_KEY is required when LLM_PROVIDER=mistral "
                "(set it as a Runtime variable, or set LLM_PROVIDER=fake for local dev)"
            )

        self.mistral_model = env.get("MISTRAL_MODEL", "mistral-medium-latest")

        # LiteLLM ("LLM_PROVIDER=litellm") routes to whichever backend
        # LITELLM_MODEL names, via LiteLLM's own `provider/model` convention
        # (e.g. "mistral/mistral-medium-latest", "openai/gpt-4o-mini") — it
        # resolves that backend's own API key from the environment itself.
        # LITELLM_API_KEY is only needed for a backend that doesn't map to
        # one of LiteLLM's well-known env var names.
        self.litellm_model = env.get("LITELLM_MODEL", "mistral/mistral-medium-latest")
        self.litellm_api_key = env.get("LITELLM_API_KEY")
        # A self-hosted or custom-endpoint backend (hosted_vllm/, ollama/,
        # lm_studio/, a proxy, ...) needs a base URL LiteLLM can't guess.
        # LiteLLM already reads a provider-specific env var directly
        # (HOSTED_VLLM_API_BASE, OLLAMA_API_BASE, ...) with no app code
        # involved — this is only a generic override for when that's more
        # convenient than looking up the exact per-provider name.
        self.litellm_api_base = env.get("LITELLM_API_BASE")

        # A fast commercial API answers well within this; a self-hosted
        # model (especially a "thinking"/reasoning one, which can spend
        # hundreds of completion tokens before ever reaching the final JSON)
        # may genuinely need longer. Applies to both providers; each retry
        # (2 by default) waits up to this long again, so a generous value
        # here multiplies into a much longer worst-case job duration —
        # jobs run off-request in a background thread (docs §3), so that's
        # safe, just slower to fail visibly.
        self.llm_timeout_seconds = int(env.get("LLM_TIMEOUT_SECONDS", "45"))

        # Unset by default — each provider/model's own default temperature is
        # used unless this is explicitly set. Lower values (e.g. 0.6) trade
        # creativity for consistency, which matters more for a task like
        # "follow this exact JSON field structure" than for open-ended prose;
        # a smaller self-hosted model in particular may need this turned down
        # to reliably hit the requested schema rather than drifting to
        # plausible-but-wrong field names under higher-temperature sampling.
        llm_temperature = env.get("LLM_TEMPERATURE")
        self.llm_temperature = float(llm_temperature) if llm_temperature not in (None, "") else None

        # How long FakeProvider pauses between the pieces it streams. Zero
        # under TESTING (a suite has nobody watching and shouldn't sleep);
        # otherwise a small pause, so `LLM_PROVIDER=fake` local dev shows the
        # reply actually arriving token by token (§5.7) rather than appearing
        # complete in one frame, which is the one thing a fake model would
        # otherwise fail to demonstrate about the real one.
        self.fake_stream_delay_seconds = float(
            env.get("FAKE_STREAM_DELAY_SECONDS", "0" if self.testing else "0.015")
        )

        self.database_url = env.get("DATABASE_URL")
        self.sqlite_path = env.get("SQLITE_PATH", "/data/app.db")

        self.resource_index_path = env.get("RESOURCE_INDEX_PATH", "app/data/resources.sqlite")
        self.resource_shortlist_max = int(env.get("RESOURCE_SHORTLIST_MAX", "8"))

        # How many student messages a single scenario conversation allows
        # before it must close (§5.5). Every turn is one LLM call, so this is
        # directly a waiting-time budget: on a slow self-hosted model at
        # ~60-120s per turn, 3 turns is roughly 3-6 minutes per scenario.
        # Raise it for a faster backend and a longer Socratic back-and-forth.
        self.max_conversation_turns = int(env.get("MAX_CONVERSATION_TURNS", "3"))

        # The side chat beside a scenario (§5.6) — how many terminology
        # questions a student may ask per scenario. Every question is its own
        # LLM call and also counts against MAX_LLM_CALLS_PER_DAY, so this cap
        # exists to stop the glossary eating the whole daily allowance and
        # leaving nothing for actual training; it is not there to ration
        # curiosity, which is why it is well above what a normal scenario
        # needs.
        self.max_help_questions_per_scenario = int(env.get("MAX_HELP_QUESTIONS_PER_SCENARIO", "5"))

        self.miss_threshold = int(env.get("MISS_THRESHOLD", "3"))
        self.max_llm_calls_per_day = int(env.get("MAX_LLM_CALLS_PER_DAY", "60"))

        instructor_emails = env.get("INSTRUCTOR_EMAILS", "")
        self.instructor_emails = {
            e.strip().lower() for e in instructor_emails.split(",") if e.strip()
        }

        self.log_level = env.get("LOG_LEVEL", "INFO")
        self.port = int(env.get("PORT", "3000"))

        # atlasflow always terminates TLS upstream (docs §2), so Secure
        # cookies are correct and required in any real deployment — but a
        # bare `flask run` for local dev has no TLS, and a spec-compliant
        # HTTP client (unlike some curl configurations) will silently refuse
        # to store or send a Secure cookie over plain http://, breaking login
        # entirely. Default secure; only local dev should ever flip this.
        self.session_cookie_secure = _bool(env, "SESSION_COOKIE_SECURE", not self.testing)

        # Off by default so any `flask <command>` (db upgrade, seed-curriculum,
        # export-user, ...) can import the app factory against a fresh or
        # mid-migration database without also reaping jobs from a jobs table
        # that might not exist yet, or spinning up a background worker thread
        # for a one-shot CLI process. Only docker-entrypoint.sh's final
        # `exec gunicorn` (and local dev, if you want the training loop to
        # actually run end-to-end) should set this.
        self.run_background_worker = _bool(env, "RUN_BACKGROUND_WORKER", False)

    @property
    def database_uri(self):
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self._writable_sqlite_path()}"

    def _writable_sqlite_path(self):
        """Falls back to /tmp if the configured path isn't actually usable —
        checked with a real sqlite3 connection in WAL mode (matching exactly
        what app/db.py configures for the real engine), not just a plain
        file write.

        That distinction is load-bearing, found the hard way: a plain
        `open(path, "w")` can succeed on a directory where SQLite itself
        still fails with "unable to open database file" — SQLite needs
        proper file-locking support, and WAL mode specifically also needs
        mmap/shared-memory support for its `-shm` file, neither of which a
        basic write test exercises. That gap let a bad SQLITE_PATH reach
        production as an uncaught crash-loop instead of triggering this
        fallback.

        Also resolves to an absolute path first: a relative SQLITE_PATH
        (e.g. the `./instance/dev.db` this template's own .env.example
        suggests for local dev) is only meaningful relative to whatever the
        *current* process's cwd happens to be — exactly the kind of
        environment-specific behavior a Runtime Variable shouldn't depend on.
        """
        path = os.path.abspath(self.sqlite_path)
        directory = os.path.dirname(path)
        probe_path = os.path.join(directory, ".write-test.sqlite")
        try:
            os.makedirs(directory, exist_ok=True)
            conn = sqlite3.connect(probe_path)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE t (id INTEGER)")
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
            finally:
                conn.close()
            return path
        except (OSError, sqlite3.OperationalError) as exc:
            logger.warning(
                "sqlite_path_unusable_falling_back_to_tmp",
                extra={"extra_fields": {"configured_path": self.sqlite_path, "resolved_path": path, "error": str(exc)}},
            )
            fallback_dir = "/tmp/app-data"
            os.makedirs(fallback_dir, exist_ok=True)
            return os.path.join(fallback_dir, os.path.basename(path))
        finally:
            for candidate in (probe_path, f"{probe_path}-wal", f"{probe_path}-shm", f"{probe_path}-journal"):
                if os.path.exists(candidate):
                    os.remove(candidate)
