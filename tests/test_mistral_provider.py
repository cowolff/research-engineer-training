from app.llm.mistral import MistralProvider


def _fake_response(content, prompt_tokens=10, completion_tokens=20):
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def test_temperature_is_unset_by_default_but_overridable(monkeypatch):
    captured = {}
    provider = MistralProvider(api_key="x", model_name="mistral-medium-latest")
    monkeypatch.setattr(
        provider._client.chat, "complete", lambda **kwargs: captured.update(kwargs) or _fake_response("{}")
    )
    provider.raw_complete("system", "user")
    assert "temperature" not in captured

    captured.clear()
    provider = MistralProvider(api_key="x", model_name="mistral-medium-latest", temperature=0.6)
    monkeypatch.setattr(
        provider._client.chat, "complete", lambda **kwargs: captured.update(kwargs) or _fake_response("{}")
    )
    provider.raw_complete("system", "user")
    assert captured["temperature"] == 0.6
