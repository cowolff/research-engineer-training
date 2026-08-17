"""Shared reliability contract for every LLM call, regardless of provider:
JSON parse + schema validation, one retry with the validation error fed back
to the model, a concurrency cap, and an llm_calls row per attempt. See
docs/IMPLEMENTATION_PLAN.md §5.1.
"""

import json
import logging
import threading

from pydantic import ValidationError

from app.db import db
from app.models import LLMCall

logger = logging.getLogger("app.llm")

# atlasflow gives one small/medium VM, not a fleet — two concurrent LLM calls
# is enough to keep the job worker's few background threads busy without
# risking the provider's own rate limits.
_semaphore = threading.Semaphore(2)


class LLMGenerationError(RuntimeError):
    pass


def _log_call(user_id, purpose, model_name, ok, usage, error=""):
    db.session.add(
        LLMCall(
            user_id=user_id,
            purpose=purpose,
            model=model_name,
            ok=ok,
            latency_ms=usage.latency_ms if usage else 0,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cost_estimate_cents=usage.cost_estimate_cents if usage else 0.0,
            error=error[:2000],
        )
    )
    db.session.commit()


def _try_once(provider, system, user, schema):
    text, usage = provider.raw_complete(system, user, schema=schema)
    return json.loads(text), usage


def generate_structured(provider, purpose, system, user, schema, user_id):
    """Returns a validated instance of `schema`. Raises LLMGenerationError if
    both the original attempt and the one retry fail to validate.

    `schema` is passed down to the provider too, not just used for
    validation here — a provider that supports schema-constrained decoding
    (docs §5.1) uses it to make the requested shape the *only* one the model
    can produce, rather than relying purely on this function's after-the-fact
    check and one retry to catch a wrong shape."""
    with _semaphore:
        try:
            data, usage = _try_once(provider, system, user, schema)
            result = schema.model_validate(data)
            _log_call(user_id, purpose, provider.model_name, True, usage)
            return result
        except (json.JSONDecodeError, ValidationError) as first_error:
            _log_call(user_id, purpose, provider.model_name, False, None, str(first_error))
            logger.warning("llm_validation_retry", extra={"extra_fields": {"purpose": purpose, "error": str(first_error)}})

            retry_user = (
                f"{user}\n\n"
                f"Your previous response failed schema validation with this error:\n{first_error}\n"
                f"Return ONLY a single valid JSON object matching the schema. No markdown fences, no commentary."
            )
            try:
                data, usage = _try_once(provider, system, retry_user, schema)
                result = schema.model_validate(data)
                _log_call(user_id, purpose, provider.model_name, True, usage)
                return result
            except (json.JSONDecodeError, ValidationError) as second_error:
                _log_call(user_id, purpose, provider.model_name, False, None, str(second_error))
                raise LLMGenerationError(
                    f"{purpose} generation failed validation twice: {second_error}"
                ) from second_error
