"""Claude API client for Yogi.

Every LLM call in the app goes through this module, so switching model or
tuning effort happens in one place.

Config (from .env or environment):
    ANTHROPIC_API_KEY  - Claude API key (required)
    CLAUDE_MODEL       - model id, default claude-sonnet-5-5
"""
import json
import os
from typing import AsyncIterator

import anthropic

DEFAULT_MODEL = "claude-sonnet-5-5"

# Effort per task. Sonnet 5.5 levels: low | medium | high | xhigh | max.
EFFORT_CHAT = "low"        # general chat: fast first token
EFFORT_SQL = "medium"      # text-to-SQL: correctness matters most
EFFORT_SUMMARY = "low"     # summarising query results

# Server-side refusal fallback (re-runs a declined request on another model).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

REFUSAL_MESSAGE = "I can't help with that request. Please ask about your telecom network KPIs."

SQL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "One read-only PostgreSQL SELECT query"},
    },
    "required": ["sql"],
    "additionalProperties": False,
}

_client: anthropic.AsyncAnthropic | None = None


class LLMRefusal(Exception):
    """Claude declined the request (stop_reason == 'refusal')."""


def model_name() -> str:
    return os.getenv("CLAUDE_MODEL") or DEFAULT_MODEL


def get_client() -> anthropic.AsyncAnthropic:
    """Create the client lazily so the app still starts without a key."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=2)
    return _client


def _system(text: str) -> list:
    # Cache the stable system prompt; only the user message changes per request.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _common(effort: str, max_tokens: int) -> dict:
    return {
        "model": model_name(),
        "max_tokens": max_tokens,
        "output_config": {"effort": effort},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }


def _text_of(message) -> str:
    # Read blocks by type: with adaptive thinking a response can start with a thinking block.
    return "".join(block.text for block in message.content if block.type == "text").strip()


async def complete(system: str, user: str, effort: str = EFFORT_SUMMARY,
                   max_tokens: int = 8000, timeout: float = 60.0) -> str:
    """Single non-streaming call; returns the text answer."""
    response = await get_client().with_options(timeout=timeout).beta.messages.create(
        **_common(effort, max_tokens),
        system=_system(system),
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        raise LLMRefusal(REFUSAL_MESSAGE)
    return _text_of(response)


async def generate_sql(system: str, user: str, timeout: float = 90.0) -> str:
    """Text-to-SQL with structured output: the reply is guaranteed JSON {"sql": "..."}."""
    output_config = {"effort": EFFORT_SQL, "format": {"type": "json_schema", "schema": SQL_OUTPUT_SCHEMA}}
    response = await get_client().with_options(timeout=timeout).beta.messages.create(
        **{**_common(EFFORT_SQL, 8000), "output_config": output_config},
        system=_system(system),
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        raise LLMRefusal(REFUSAL_MESSAGE)
    text = _text_of(response)
    try:
        return json.loads(text)["sql"].strip()
    except (json.JSONDecodeError, KeyError, TypeError):
        return text  # caller's extract_sql/validate_sql will reject it if it is not SQL


async def stream_chat(system: str, user: str, effort: str = EFFORT_CHAT,
                      max_tokens: int = 8000, timeout: float = 120.0) -> AsyncIterator[str]:
    """Stream text deltas for the chat UI."""
    async with get_client().with_options(timeout=timeout).beta.messages.stream(
        **_common(effort, max_tokens),
        system=_system(system),
        messages=[{"role": "user", "content": user}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
        final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            raise LLMRefusal(REFUSAL_MESSAGE)


async def ping(timeout: float = 5.0) -> dict:
    """Cheap health check: verifies key + model access without generating tokens."""
    try:
        model = await get_client().with_options(timeout=timeout, max_retries=0).models.retrieve(model_name())
        return {"llm": "connected", "model": model.id}
    except anthropic.AuthenticationError:
        return {"llm": "error", "model": model_name(), "message": "Invalid or missing ANTHROPIC_API_KEY"}
    except anthropic.NotFoundError:
        return {"llm": "error", "model": model_name(), "message": f"Model {model_name()} not available for this key"}
    except anthropic.APIConnectionError:
        return {"llm": "disconnected", "model": model_name(), "message": "Cannot reach Claude API"}
    except Exception as e:  # missing credentials raise before any HTTP call
        return {"llm": "error", "model": model_name(), "message": friendly_error(e)}


MISSING_KEY_MESSAGE = "Claude API key is invalid or missing. Set ANTHROPIC_API_KEY in .env."


def friendly_error(error: Exception) -> str:
    """Map SDK errors to short messages for the chat UI."""
    if isinstance(error, LLMRefusal):
        return str(error)
    if isinstance(error, anthropic.AuthenticationError) or "authentication method" in str(error):
        return MISSING_KEY_MESSAGE
    if isinstance(error, anthropic.RateLimitError):
        return "Claude API rate limit reached. Please retry in a moment."
    if isinstance(error, anthropic.APITimeoutError):
        return "Request timeout"
    if isinstance(error, anthropic.APIConnectionError):
        return "Cannot reach Claude API. Check internet connection."
    if isinstance(error, anthropic.APIStatusError):
        return f"Claude API error ({error.status_code}): {error.message}"
    return str(error)
