"""Real provider, backed by Mistral's `mistral-medium` family.

Verified directly against the installed `mistralai` SDK (2.9.3) rather than
against possibly-stale docs, per docs/IMPLEMENTATION_PLAN.md §5.1's "verify at
implementation time" note:

- the client class is `mistralai.client.Mistral` — the top-level `mistralai`
  package re-exports nothing in this version, so `from mistralai import
  Mistral` does NOT work; `from mistralai.client import Mistral` does.
- `client.chat.complete(model=..., messages=[...], response_format=..., timeout_ms=...)`
  returns a `ChatCompletionResponse` with `.choices[0].message.content` (str)
  and `.usage.prompt_tokens` / `.usage.completion_tokens`.
- transient HTTP failures raise `mistralai.client.errors.SDKError`, which
  carries `.raw_response.status_code` — that's what the retry loop below
  checks against 429/5xx.

Re-verify this against Mistral's current docs if the SDK major version changes.
"""

import random
import time

from mistralai.client import Mistral
from mistralai.client.errors import SDKError

from app.llm.base import LLMUsage

_DEFAULT_TIMEOUT_SECONDS = 45
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2


class MistralProvider:
    def __init__(self, api_key, model_name, timeout_seconds=_DEFAULT_TIMEOUT_SECONDS, temperature=None):
        self.model_name = model_name
        self._client = Mistral(api_key=api_key)
        self._timeout_ms = int(timeout_seconds * 1000)
        self._temperature = temperature

    def raw_complete(self, system, user, schema=None):
        # `schema` is accepted for protocol compatibility with
        # app/llm/client.py but not yet used here — Mistral's own API
        # supports the same json_schema response-format convention
        # LiteLLMProvider uses (docs §5.1), but nothing has shown a need for
        # it on this provider's fast, commercial-grade model. Wire it through
        # the same way if that changes.
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        kwargs = {}
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        last_exc = None
        for attempt in range(_MAX_RETRIES + 1):
            started = time.monotonic()
            try:
                response = self._client.chat.complete(
                    model=self.model_name,
                    messages=messages,
                    response_format={"type": "json_object"},
                    timeout_ms=self._timeout_ms,
                    **kwargs,
                )
                latency_ms = int((time.monotonic() - started) * 1000)
                text = response.choices[0].message.content or ""
                usage = LLMUsage(
                    prompt_tokens=response.usage.prompt_tokens or 0,
                    completion_tokens=response.usage.completion_tokens or 0,
                    latency_ms=latency_ms,
                    cost_estimate_cents=_estimate_cost_cents(
                        self.model_name, response.usage.prompt_tokens or 0, response.usage.completion_tokens or 0
                    ),
                )
                return text, usage
            except SDKError as exc:
                last_exc = exc
                status = getattr(exc.raw_response, "status_code", None)
                if status not in _RETRYABLE_STATUS or attempt == _MAX_RETRIES:
                    raise
                time.sleep((2**attempt) * 0.5 + random.uniform(0, 0.5))

        raise last_exc  # pragma: no cover - loop always returns or raises above


# Rough, deliberately conservative estimates for the /admin/usage teaching
# artefact — not a billing-grade calculation. Update alongside MISTRAL_MODEL.
_PRICE_PER_MILLION_TOKENS_USD = {"input": 0.40, "output": 2.00}


def _estimate_cost_cents(model_name, prompt_tokens, completion_tokens):
    input_cost = prompt_tokens / 1_000_000 * _PRICE_PER_MILLION_TOKENS_USD["input"]
    output_cost = completion_tokens / 1_000_000 * _PRICE_PER_MILLION_TOKENS_USD["output"]
    return round((input_cost + output_cost) * 100, 4)
