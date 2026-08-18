import os

from app.config import Config


def test_config_ignores_the_real_process_environment_for_every_bool_field(monkeypatch):
    """Regression: `_bool()` used to read os.environ directly regardless of
    the `env` mapping passed to Config(), so anything that mutated the real
    process environment as a side effect — importing `litellm` auto-loads a
    local .env via python-dotenv — silently flipped RUN_BACKGROUND_WORKER /
    TESTING / SESSION_COOKIE_SECURE for the rest of the test process. A
    Config built from an explicit `env` dict must be fully isolated from
    whatever is actually set in os.environ."""
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("RUN_BACKGROUND_WORKER", "1")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    cfg = Config({"SECRET_KEY": "x", "LLM_PROVIDER": "fake"})

    assert cfg.testing is False
    assert cfg.run_background_worker is False
    assert cfg.session_cookie_secure is True  # default for testing=False


def test_config_respects_explicit_env_dict_values():
    cfg = Config({"SECRET_KEY": "x", "LLM_PROVIDER": "fake", "TESTING": "true", "RUN_BACKGROUND_WORKER": "1"})

    assert cfg.testing is True
    assert cfg.run_background_worker is True


def test_llm_temperature_unset_by_default():
    cfg = Config({"SECRET_KEY": "x", "LLM_PROVIDER": "fake"})
    assert cfg.llm_temperature is None


def test_llm_temperature_parses_to_float_when_set():
    cfg = Config({"SECRET_KEY": "x", "LLM_PROVIDER": "fake", "LLM_TEMPERATURE": "0.6"})
    assert cfg.llm_temperature == 0.6


def test_writable_sqlite_path_resolves_relative_paths_to_absolute(tmp_path, monkeypatch):
    """A relative SQLITE_PATH is only meaningful relative to whatever cwd a
    given process happens to have — a Runtime Variable shouldn't depend on
    that. Regression: this is what a local-dev value like './instance/dev.db'
    copied verbatim into a Runtime Variable actually hits in production."""
    monkeypatch.chdir(tmp_path)
    cfg = Config({"SECRET_KEY": "x", "LLM_PROVIDER": "fake", "SQLITE_PATH": "./instance/dev.db"})

    resolved = cfg._writable_sqlite_path()

    assert os.path.isabs(resolved)
    assert resolved == str(tmp_path / "instance" / "dev.db")


def test_writable_sqlite_path_falls_back_to_tmp_when_sqlite_cannot_actually_open_it(monkeypatch):
    """Regression: the old probe was a plain file write, which can succeed on
    a directory where SQLite itself still fails with 'unable to open
    database file' (SQLite needs real file-locking support, which a plain
    write doesn't exercise) — so a bad path reached production as an
    uncaught crash-loop instead of triggering this fallback. Simulated here
    by making sqlite3.connect itself fail, regardless of why."""
    import sqlite3

    cfg = Config({"SECRET_KEY": "x", "LLM_PROVIDER": "fake", "SQLITE_PATH": "/some/unusable/path/app.db"})

    def _broken_connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", _broken_connect)
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)  # directory creation itself isn't what's under test

    resolved = cfg._writable_sqlite_path()

    assert resolved.startswith("/tmp/app-data/")
    assert resolved.endswith("app.db")


def test_writable_sqlite_path_succeeds_on_a_real_writable_directory(tmp_path):
    cfg = Config({"SECRET_KEY": "x", "LLM_PROVIDER": "fake", "SQLITE_PATH": str(tmp_path / "app.db")})

    resolved = cfg._writable_sqlite_path()

    assert resolved == str(tmp_path / "app.db")
    # The probe file and its WAL/journal siblings must not be left behind.
    assert os.listdir(tmp_path) == []
