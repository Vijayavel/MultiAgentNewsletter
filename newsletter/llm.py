"""
llm.py - One JSON chat call to any OpenAI-compatible provider, with timeouts and retries.

Two layers of retry:
- the openai SDK retries 429/5xx/timeouts a few times within seconds (LLM_MAX_RETRIES);
- if the provider is still busy or unreachable, we wait longer and try again
  (LLM_RETRY_WAITS, default 10s then 30s), since demand spikes usually pass in a minute.
An empty or non-JSON reply is retried once.
"""

from __future__ import annotations

import json
import logging
import re
import time

from openai import APIConnectionError, InternalServerError, OpenAI, OpenAIError, RateLimitError

from .config import Provider, Settings

log = logging.getLogger("newsletter.llm")

# Busy, rate-limited, timed out or unreachable: worth waiting for. (APITimeoutError is an APIConnectionError.)
TRANSIENT_ERRORS = (APIConnectionError, InternalServerError, RateLimitError)


def _sleep(seconds: float) -> None:  # separate so tests can skip the waiting
    time.sleep(seconds)


class LLMError(Exception):
    """Raised when a provider call fails or returns no usable JSON."""


def parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating ``` fences or a little text around it."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


def chat_json(provider: Provider, s: Settings, system: str, user: str, max_tokens: int) -> dict:
    if not provider.api_key:
        raise LLMError(f"{provider.key_env} is not set.")
    client = OpenAI(
        api_key=provider.api_key,
        base_url=provider.base_url,
        timeout=s.llm_timeout,
        max_retries=s.llm_max_retries,
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    waits = list(s.retry_waits)
    for attempt in (1, 2):
        started = time.monotonic()
        while True:
            try:
                resp = client.chat.completions.create(
                    model=provider.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    **{provider.token_param: max_tokens},
                )
                break
            except TRANSIENT_ERRORS as e:
                if not waits:
                    raise LLMError(f"{provider.name} ({provider.model}) request failed: {e.__class__.__name__}: {e}") from e
                wait = waits.pop(0)
                log.warning("%s busy or unreachable (%s); trying again in %.0fs", provider.name, e.__class__.__name__, wait)
                _sleep(wait)
            except OpenAIError as e:  # bad key, bad model name, bad request: retrying won't help
                raise LLMError(f"{provider.name} ({provider.model}) request failed: {e.__class__.__name__}: {e}") from e
        usage = getattr(resp, "usage", None)
        log.info(
            "%s/%s replied in %.1fs (tokens in=%s out=%s)",
            provider.name, provider.model, time.monotonic() - started,
            getattr(usage, "prompt_tokens", "?"), getattr(usage, "completion_tokens", "?"),
        )
        try:
            return parse_json(resp.choices[0].message.content)
        except (ValueError, IndexError, AttributeError) as e:
            log.warning("%s returned unusable JSON (attempt %d): %s", provider.name, attempt, e)
    raise LLMError(f"{provider.name} ({provider.model}) returned no valid JSON.")
