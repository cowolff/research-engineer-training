"""The provider boundary. A provider's only job is: given a system prompt and
a user prompt, return raw text and how much that cost. Parsing, schema
validation, and retry-on-validation-failure are shared logic in
app/llm/client.py, not duplicated per provider — see docs §5.1."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_estimate_cents: float = 0.0


class LLMProvider(Protocol):
    model_name: str

    def raw_complete(self, system: str, user: str, schema: type | None = None) -> tuple[str, LLMUsage]:
        """Return (raw_json_text, usage). Must not raise on a model refusal or
        malformed output — return the text as-is and let the caller's schema
        validation surface the problem, so it's logged and retried uniformly.

        `schema`, when given, is the Pydantic model the response will be
        validated against. A provider whose backend supports schema-
        constrained decoding should use it to constrain generation directly
        (see app/llm/litellm_provider.py) rather than only checked after the
        fact — a smaller/self-hosted model in particular may otherwise
        substitute plausible-but-wrong field names fairly consistently, which
        neither lower temperature nor post-hoc validation alone fixes on the
        first attempt. A provider that doesn't support this may ignore
        `schema` and fall back to unconstrained JSON mode; app/llm/client.py's
        post-hoc validation (and one retry) still applies either way.
        """
        ...
