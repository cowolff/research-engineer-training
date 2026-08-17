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
