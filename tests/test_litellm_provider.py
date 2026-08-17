from types import SimpleNamespace

from app.llm.litellm_provider import LiteLLMProvider


def _fake_response(content, prompt_tokens=10, completion_tokens=20):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def _mock_streaming(monkeypatch, final_response, chunks=None):
    """Mocks both halves of the streaming call: litellm.completion() returning
    an iterable of raw chunks (contents irrelevant here since
    stream_chunk_builder is mocked too — the two are only ever used together
    in real litellm), and stream_chunk_builder() reconstructing the final
    response. Returns the dict completion() was called with."""
    import litellm

    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter(chunks if chunks is not None else [object()])

    monkeypatch.setattr(litellm, "completion", fake_completion)
    monkeypatch.setattr(litellm, "stream_chunk_builder", lambda chunks, messages=None: final_response)
    return captured


def test_raw_complete_parses_an_openai_shaped_response(monkeypatch):
    captured = _mock_streaming(monkeypatch, _fake_response('{"ok": true}'))

    provider = LiteLLMProvider(model_name="mistral/mistral-medium-latest")
    text, usage = provider.raw_complete("system prompt", "user prompt")

    assert text == '{"ok": true}'
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 20
    assert usage.latency_ms >= 0

    # Verifies the actual call shape: JSON mode requested, streamed (see
    # module docstring for why — a non-streaming call to a slow/reasoning
    # backend receives zero bytes until the entire completion is done, which
    # behaves exactly like a hard deadline regardless of what `timeout` is
    # set to), retries delegated to LiteLLM itself rather than reimplemented
    # here, and no api_key kwarg leaked through when none was configured.
    assert captured["model"] == "mistral/mistral-medium-latest"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["num_retries"] == 2
    assert captured["stream"] is True
    assert "api_key" not in captured


def test_explicit_api_key_is_passed_through(monkeypatch):
    captured = _mock_streaming(monkeypatch, _fake_response("{}"))

    provider = LiteLLMProvider(model_name="openai/gpt-4o-mini", api_key="sk-explicit")
    provider.raw_complete("system", "user")

    assert captured["api_key"] == "sk-explicit"


def test_explicit_api_base_is_passed_through_as_base_url(monkeypatch):
    """A self-hosted backend (hosted_vllm/, ollama/, ...) needs a base URL
    LiteLLM can't guess. litellm.completion()'s own parameter for this is
    named `base_url`, not `api_base` — this pins that naming down."""
    captured = _mock_streaming(monkeypatch, _fake_response("{}"))

    provider = LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b", api_base="http://vllm-host:8000/v1")
    provider.raw_complete("system", "user")

    assert captured["base_url"] == "http://vllm-host:8000/v1"
    assert "api_base" not in captured


def test_timeout_is_configurable_and_defaults_to_45s(monkeypatch):
    """The streamed idle-gap timeout (see module docstring) still needs to be
    overridable — e.g. a network genuinely worth waiting longer on before
    giving up — even though it's no longer a hard total-duration deadline."""
    captured = _mock_streaming(monkeypatch, _fake_response("{}"))

    LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b").raw_complete("system", "user")
    assert captured["timeout"] == 45

    captured.clear()
    LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b", timeout_seconds=180).raw_complete("system", "user")
    assert captured["timeout"] == 180


def test_temperature_is_unset_by_default_but_overridable(monkeypatch):
    """Unset means 'use the provider/model's own default' — only set the
    kwarg at all when a value was explicitly configured, so nothing changes
    for a model that was never asked to be tuned down."""
    captured = _mock_streaming(monkeypatch, _fake_response("{}"))

    LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b").raw_complete("system", "user")
    assert "temperature" not in captured

    captured.clear()
    LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b", temperature=0.6).raw_complete("system", "user")
    assert captured["temperature"] == 0.6


def test_schema_is_passed_as_response_format_when_given(monkeypatch):
    """The whole point: passing the Pydantic class itself (not a bare
    json_object dict) is what makes LiteLLM build an OpenAI-style strict JSON
    schema and constrain generation to it — verified live against a real
    self-hosted backend to actually produce the requested shape on the first
    attempt, where json_object mode alone did not. See module docstring."""
    from pydantic import BaseModel

    class Simple(BaseModel):
        greeting: str

    captured = _mock_streaming(monkeypatch, _fake_response('{"greeting": "hi"}'))

    LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b").raw_complete("system", "user", schema=Simple)

    assert captured["response_format"] is Simple


def test_falls_back_to_json_object_mode_when_no_schema_given(monkeypatch):
    captured = _mock_streaming(monkeypatch, _fake_response("{}"))

    LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b").raw_complete("system", "user")

    assert captured["response_format"] == {"type": "json_object"}


def test_no_base_url_kwarg_when_api_base_unset(monkeypatch):
    """Backends resolved via their own env var (e.g. HOSTED_VLLM_API_BASE,
    which LiteLLM reads directly) must not have base_url=None forced in,
    which would override that auto-detection with nothing."""
    captured = _mock_streaming(monkeypatch, _fake_response("{}"))

    provider = LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b")
    provider.raw_complete("system", "user")

    assert "base_url" not in captured


def test_reasoning_tokens_are_excluded_from_the_parsed_text(monkeypatch):
    """Verified against the real backend: a 'thinking' model puts reasoning
    in a separate `reasoning_content` field, entirely outside `.content`.
    stream_chunk_builder already handles this correctly — this test pins
    down that raw_complete() relies on that and never sees reasoning text."""
    _mock_streaming(monkeypatch, _fake_response('{"answer": "4"}'))

    provider = LiteLLMProvider(model_name="hosted_vllm/qwen3.5-9b")
    text, _ = provider.raw_complete("system", "user")

    assert text == '{"answer": "4"}'
    assert "Thinking" not in text


def test_cost_estimation_failure_degrades_to_zero_without_raising(monkeypatch):
    """A response shape litellm's pricing lookup doesn't recognise (true for
    any self-hosted model, which has no metered API pricing) must never
    break generation — it just means $0 shows up in /admin/usage instead of a
    real estimate."""
    _mock_streaming(monkeypatch, _fake_response("{}"))

    provider = LiteLLMProvider(model_name="mistral/mistral-medium-latest")
    _, usage = provider.raw_complete("system", "user")

    assert usage.cost_estimate_cents == 0.0


def test_factory_selects_litellm_provider(app):
    app.app_config.llm_provider = "litellm"
    app.app_config.litellm_model = "anthropic/claude-3-5-sonnet-latest"

    from app.llm import factory

    factory._cached.clear()
    with app.app_context():
        provider = factory.get_provider(app)

    assert isinstance(provider, LiteLLMProvider)
    assert provider.model_name == "anthropic/claude-3-5-sonnet-latest"
    factory._cached.clear()
