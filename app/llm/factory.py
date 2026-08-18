_cached = {}


def get_provider(app):
    """One provider instance per process, chosen by LLM_PROVIDER:
    - fake: deterministic, no API key, no network call — see docs §5.1 on why
      that's not optional.
    - mistral: direct integration against Mistral's own SDK.
    - litellm: routes to whichever backend LITELLM_MODEL names (Mistral,
      OpenAI, Anthropic, Azure, Bedrock, local models, ...) through one
      unified call shape — see app/llm/litellm_provider.py.
    """
    cfg = app.app_config
    if "provider" not in _cached:
        if cfg.llm_provider == "fake":
            from app.llm.fake import FakeProvider

            _cached["provider"] = FakeProvider(chunk_delay_seconds=cfg.fake_stream_delay_seconds)
        elif cfg.llm_provider == "litellm":
            from app.llm.litellm_provider import LiteLLMProvider

            _cached["provider"] = LiteLLMProvider(
                model_name=cfg.litellm_model,
                api_key=cfg.litellm_api_key,
                api_base=cfg.litellm_api_base,
                timeout_seconds=cfg.llm_timeout_seconds,
                temperature=cfg.llm_temperature,
            )
        else:
            from app.llm.mistral import MistralProvider

            _cached["provider"] = MistralProvider(
                api_key=cfg.mistral_api_key,
                model_name=cfg.mistral_model,
                timeout_seconds=cfg.llm_timeout_seconds,
                temperature=cfg.llm_temperature,
            )
    return _cached["provider"]
