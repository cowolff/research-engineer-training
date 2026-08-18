"""LiteLLM provider — a unified gateway to 100+ backends (OpenAI, Anthropic,
Azure, Bedrock, Mistral's own API, local models via Ollama, ...) behind one
call shape, selected with `LLM_PROVIDER=litellm`. Where MistralProvider
(app/llm/mistral.py) is a direct, hand-rolled integration against one vendor's
SDK, this one exists for the opposite reason: swapping the backing model
without touching this app's code, by changing `LITELLM_MODEL` alone.

LiteLLM resolves each backend's own API key from the environment using its
own convention (`MISTRAL_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...
— see https://docs.litellm.ai/docs/providers) based on the `provider/model`
prefix in `LITELLM_MODEL` (e.g. `mistral/mistral-medium-latest`,
`openai/gpt-4o-mini`, `anthropic/claude-3-5-sonnet-latest`). `LITELLM_API_KEY`
is an escape hatch for backends that don't map to one of those well-known
names — leave it unset and LiteLLM's own auto-detection handles the rest. A
self-hosted or custom-endpoint backend (`hosted_vllm/`, `ollama/`,
`lm_studio/`, a proxy, ...) also needs a base URL, which LiteLLM likewise
resolves from its own per-provider env var (`HOSTED_VLLM_API_BASE`,
`OLLAMA_API_BASE`, ...) with no app code involved; `LITELLM_API_BASE` is the
same kind of generic override as `LITELLM_API_KEY`, for when that's more
convenient than looking up the exact per-provider name.

Verified against the installed `litellm` SDK (1.97.0):
- `litellm.completion(model=..., messages=[...], response_format={"type":
  "json_object"}, num_retries=..., timeout=...)` returns a `ModelResponse`
  with `.choices[0].message.content` (str) and `.usage.prompt_tokens` /
  `.usage.completion_tokens` — an OpenAI-compatible shape regardless of the
  actual backend.
- Every backend's errors are normalised into openai-python exception types
  (`litellm.exceptions.RateLimitError` etc., all subclassing
  `openai.APIStatusError`), so a single `except` on the common base plus
  `num_retries` (passed straight through to LiteLLM's own retry/backoff,
  rather than reimplementing it here as app/llm/mistral.py does) covers every
  backend uniformly.
- `litellm.completion_cost(completion_response=response)` returns a USD float
  from LiteLLM's own maintained pricing tables across providers — used
  instead of a hand-rolled per-model price table.
- `num_retries` needs the `tenacity` package at runtime, but `litellm` does
  NOT declare it as a dependency — it's imported lazily only when a retry is
  actually attempted, so this passes silently until the first real failure,
  where it surfaces as "tenacity import failed" and masks whatever error
  actually triggered the retry. `tenacity` is in requirements.txt for exactly
  this reason; don't drop it as "unused" without checking here first.

**Why this streams (`stream=True`) instead of a plain blocking call** — this
was added after tracing a real, reproducible failure against a self-hosted
`hosted_vllm/` reasoning model:

- A non-streaming request receives *zero* response bytes until the server has
  finished the *entire* completion. `timeout=` maps to httpx's `read`
  timeout, which is a per-chunk gap timeout, not a hard deadline — but with
  nothing streaming, "waiting for the first chunk" and "waiting for the whole
  response" are the same wait, so it behaves exactly like a hard deadline in
  practice: a slow model reliably loses the race.
- A "thinking" model can spend the large majority of that time on hidden
  reasoning before ever producing visible output — verified directly against
  this app's configured backend: a trivial two-word answer took 300+
  `reasoning_content` tokens and tens of seconds before the first visible
  character; a real tutorial-generation prompt took 67s end-to-end. Both
  comfortably exceed a 45s default, and there's no fixed ceiling that's
  "long enough" in general — the model can legitimately take minutes.
- With `stream=True`, verified chunks arrive roughly every 0.02s for the
  *entire* duration, reasoning included — so the same `timeout=` value now
  means "the connection went silent for this long", which essentially never
  false-triggers on a model that's merely slow but still generating,
  regardless of total wall-clock time. This is what actually fixes the
  timeout failures; raising `LLM_TIMEOUT_SECONDS` alone only ever bought
  more margin against a hardcoded ceiling that stays wrong for some model.
- `litellm.stream_chunk_builder(chunks, messages=...)` reconstructs the same
  `ModelResponse` shape a non-streaming call would have returned — same
  `.choices[0].message.content` (reasoning tokens correctly excluded; they
  live in a separate `reasoning_content` field this app has no use for) and
  `.usage` — so nothing downstream of `raw_complete()` needed to change.

**Why `response_format` is the schema itself, not `{"type": "json_object"}`**
— json_object mode only guarantees syntactically valid JSON, not any
particular shape. Verified directly against this app's self-hosted backend:
at `temperature=0.6` with plain json_object mode, two consecutive first
attempts at the real `ScenarioSpec` schema both used different, wrong field
names (`scenario_type`/`context`/`question` instead of `type`/`prompt_md`)
— lower temperature narrows *how* it samples, not *whether* it tracks the
requested field names. Passing the Pydantic class as `response_format`
instead makes LiteLLM build an OpenAI-style strict JSON schema
(`litellm.utils.type_to_response_format_param`, via
`openai.lib._pydantic.to_strict_json_schema`: every field required,
`additionalProperties: false`, nested `$defs` for `list[SomeModel]` fields)
and this is what actually reached the point of constraining generation,
not just describing it in prose — verified with a live call using the real
`ScenarioSpec` (nested `Artifact`/`RubricItem` lists, a `Literal` enum for
`type`): correct field names, correct nesting, valid on the **first**
attempt, no retry needed. `hosted_vllm/`'s serving stack (vLLM's own guided
decoding) is what enforces this — LiteLLM does no schema-specific
transformation for that provider beyond the conversion above, it passes
`response_format` straight through to vLLM's OpenAI-compatible endpoint.
Falls back to plain `{"type": "json_object"}` when no schema is given, so
this only ever adds a constraint, never removes the existing safety net —
app/llm/client.py's post-hoc validation and one retry still run regardless,
for any provider or backend where structured decoding isn't available or
doesn't hold.

Re-verify if LiteLLM's major version changes.
"""

import time

from app.llm.base import LLMUsage

_DEFAULT_TIMEOUT_SECONDS = 45
_MAX_RETRIES = 2


class LiteLLMProvider:
    def __init__(
        self,
        model_name,
        api_key=None,
        api_base=None,
        timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        temperature=None,
    ):
        self.model_name = model_name
        self._api_key = api_key
        self._api_base = api_base
        self._timeout_seconds = timeout_seconds
        self._temperature = temperature

    def raw_complete(self, system, user, schema=None, on_delta=None):
        import litellm

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        kwargs = {}
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            # LiteLLM's completion() parameter is named `base_url`; it feeds
            # into the same internal `api_base` resolution as each
            # provider's own env var (e.g. HOSTED_VLLM_API_BASE) and takes
            # priority over it when both are set.
            kwargs["base_url"] = self._api_base

        # A Pydantic class here (rather than the bare json_object dict) is
        # what actually constrains generation to the requested shape — see
        # module docstring. LiteLLM converts it to an OpenAI-style strict
        # JSON schema itself; nothing to build by hand here.
        response_format = schema if schema is not None else {"type": "json_object"}

        started = time.monotonic()
        stream = litellm.completion(
            model=self.model_name,
            messages=messages,
            response_format=response_format,
            timeout=self._timeout_seconds,
            num_retries=_MAX_RETRIES,
            stream=True,
            **kwargs,
        )
        # Each chunk read is bound by `timeout` individually (see module
        # docstring) — this is what turns a hard overall deadline into a
        # per-chunk idle timeout. The chunks are still collected in full for
        # `stream_chunk_builder` below, which is what everything downstream
        # parses; `on_delta` is a display-only side channel taken off the same
        # pass, so the student watches the reply appear at the rate the model
        # actually produces it (§5.7).
        chunks = []
        for chunk in stream:
            chunks.append(chunk)
            if on_delta is not None:
                delta = _visible_text(chunk)
                if delta:
                    on_delta(delta)
        latency_ms = int((time.monotonic() - started) * 1000)

        response = litellm.stream_chunk_builder(chunks, messages=messages)
        text = response.choices[0].message.content or ""
        usage = response.usage
        return text, LLMUsage(
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
            cost_estimate_cents=_estimate_cost_cents(response),
        )


def _visible_text(chunk):
    """The user-visible slice of one streamed chunk. A reasoning model also
    streams `reasoning_content` (which is exactly why this provider streams at
    all — see the module docstring); that is hidden scratch work and is
    deliberately not forwarded, the same way `stream_chunk_builder` excludes
    it from the reconstructed message."""
    try:
        return chunk.choices[0].delta.content or ""
    except (AttributeError, IndexError, TypeError):
        # A chunk carrying only usage or a finish_reason has no delta content.
        return ""


def _estimate_cost_cents(response):
    import litellm

    try:
        return round(litellm.completion_cost(completion_response=response) * 100, 4)
    except Exception:  # noqa: BLE001 - an unpriced/unknown model must not break generation
        return 0.0
