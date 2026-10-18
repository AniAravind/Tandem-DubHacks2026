import logging
import hashlib
import os
import time

from openai import OpenAI, RateLimitError, BadRequestError

log = logging.getLogger(__name__)

# xAI and Groq are OpenAI-compatible. The key comes from the environment only (never commit it).
# A placeholder keeps this module importable when XAI_API_KEY is not set; real calls will fail with an auth error.
client = OpenAI(api_key=os.getenv("XAI_API_KEY") or "missing-xai-api-key",
                base_url=os.getenv("XAI_BASE_URL", "https://api.x.ai/v1"),
                timeout=float(os.getenv("XAI_TIMEOUT_S", "8")), max_retries=0)   # the orchestrator does its one retry
MODEL = os.getenv("XAI_MODEL", "grok-4.7").split()[0]   # VERIFY exact id in the provider console
# Groq rate limits are per model, so a smaller model is an independent quota when the primary is exhausted.
_GROQ = "groq.com" in os.getenv("XAI_BASE_URL", "")
FALLBACK_MODELS = [m for m in os.getenv("XAI_FALLBACK_MODELS", "openai/gpt-oss-20b" if _GROQ else "").replace(",", " ").split()
                   if m != MODEL]
MAX_OUTPUT_TOKENS = int(os.getenv("XAI_MAX_OUTPUT_TOKENS", "1500"))
LONGEST_INLINE_WAIT_S = 15


class ProviderUnavailable(Exception):
    """Every configured model is rate limited right now. retry_after is seconds until the soonest one frees up."""

    def __init__(self, retry_after: float):
        super().__init__(f"AI provider unavailable for about {round(retry_after)}s")
        self.retry_after = retry_after


def models() -> list[str]:
    return [MODEL, *FALLBACK_MODELS]


def retry_after_s(exc: RateLimitError) -> float:
    try:
        return float(exc.response.headers.get("retry-after", "2"))
    except (TypeError, ValueError, AttributeError):
        return 2.0


def _cooldown_key(model: str) -> str:
    identity = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1") + "|" + os.getenv("XAI_API_KEY", "")
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return "llm:cooldown:" + fingerprint + ":" + model


def cooling_until(model: str) -> float:
    from django.core.cache import cache
    try:
        return float(cache.get(_cooldown_key(model)) or 0)
    except Exception:                                           # noqa: BLE001  cache outage must not stop the chat
        return 0.0


def cool_down(model: str, seconds: float) -> None:
    from django.core.cache import cache
    try:
        cache.set(_cooldown_key(model), time.time() + seconds, timeout=max(1, int(seconds) + 1))
    except Exception:                                           # noqa: BLE001
        pass


def _extra(model: str) -> dict:
    # gpt-oss reasons before answering; low effort keeps each turn inside free-tier token budgets.
    if model.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low", "max_completion_tokens": MAX_OUTPUT_TOKENS}
    return {}


def chat(messages: list[dict], tools: list[dict], tool_choice: str = "auto"):
    """One completion from the first model that is not rate limited.

    A short wait (retry-after <= 15s) is surfaced to the orchestrator's single retry. A long wait, such as a daily
    token cap, cools that model down for every worker and moves on to the next model immediately.
    """
    now = time.time()
    soonest = None
    for model in models():
        until = cooling_until(model)
        if until > now:
            soonest = min(soonest or until, until)
            continue
        try:
            return client.chat.completions.create(model=model, messages=messages, tools=tools, tool_choice=tool_choice,
                                                  temperature=0.2, parallel_tool_calls=False, **_extra(model))
        except BadRequestError as exc:
            # Rejected generations never executed. Let an independent model regenerate
            # from the original context; never parse or execute failed_generation.
            if exc.code != "tool_use_failed" or model == models()[-1]:
                raise
            log.warning("AI model %s generated an invalid tool call; trying fallback", model)
        except RateLimitError as exc:
            wait = retry_after_s(exc)
            log.warning("AI model %s rate limited for %.0fs", model, wait)
            if wait <= LONGEST_INLINE_WAIT_S and model == models()[-1]:
                raise
            cool_down(model, wait)
            soonest = min(soonest or now + wait, now + wait)
    raise ProviderUnavailable(max(1.0, (soonest or now + 60) - now))
