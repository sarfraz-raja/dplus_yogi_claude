"""Claude API client for Yogi.

Every LLM call in the app goes through this module, so switching model or
tuning effort happens in one place.

Config (from .env or environment):
    ANTHROPIC_API_KEY  - Claude API key (required)
    CLAUDE_MODEL       - model id, default claude-sonnet-5-5
"""
import json
import os
import time
from typing import AsyncIterator

import anthropic

DEFAULT_MODEL = "claude-sonnet-5-5"

# Effort per task. Sonnet 5.5 levels: low | medium | high | xhigh | max.
EFFORT_CHAT = "low"        # general chat: fast first token
EFFORT_PLAN = "medium"     # question -> query plan: correctness matters most
EFFORT_SQL = EFFORT_PLAN   # kept for backward compatibility
EFFORT_SUMMARY = "low"     # summarising query results

# Server-side refusal fallback (re-runs a declined request on another model).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

REFUSAL_MESSAGE = "I can't help with that request. Please ask about your telecom network KPIs."
MISSING_KEY_MESSAGE = "Claude API key is invalid or missing. Set ANTHROPIC_API_KEY in .env."

# After an authentication failure, skip Claude for a while instead of failing every request slowly.
AUTH_BACKOFF_SECONDS = 60

_client: anthropic.AsyncAnthropic | None = None
_auth_failed_at = 0.0


class LLMRefusal(Exception):
    """Claude declined the request (stop_reason == 'refusal')."""


class LLMUnavailable(Exception):
    """Claude cannot be used right now (missing/invalid key)."""


def model_name() -> str:
    return os.getenv("CLAUDE_MODEL") or DEFAULT_MODEL


def get_client() -> anthropic.AsyncAnthropic:
    """Create the client lazily so the app still starts without a key."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=2)
    return _client


def available() -> bool:
    """False while a recent authentication failure is still fresh."""
    return time.time() - _auth_failed_at > AUTH_BACKOFF_SECONDS


def _note_error(error: Exception):
    global _auth_failed_at
    if isinstance(error, anthropic.AuthenticationError) or "authentication method" in str(error) \
            or "api_key" in str(error).lower() and "none" in str(error).lower():
        _auth_failed_at = time.time()


def _system(text: str) -> list:
    # Cache the stable system prompt; only the messages change per request.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _common(effort: str, max_tokens: int) -> dict:
    return {
        "model": model_name(),
        "max_tokens": max_tokens,
        "output_config": {"effort": effort},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }


def _messages(user_or_messages) -> list:
    if isinstance(user_or_messages, str):
        return [{"role": "user", "content": user_or_messages}]
    return list(user_or_messages)


def _text_of(message) -> str:
    # Read blocks by type: with adaptive thinking a response can start with a thinking block.
    return "".join(block.text for block in message.content if block.type == "text").strip()


async def complete(system: str, user, effort: str = EFFORT_SUMMARY,
                   max_tokens: int = 8000, timeout: float = 60.0) -> str:
    """Single non-streaming call; returns the text answer. `user` is a string or a messages list."""
    if not available():
        raise LLMUnavailable(MISSING_KEY_MESSAGE)
    try:
        response = await get_client().with_options(timeout=timeout).beta.messages.create(
            **_common(effort, max_tokens),
            system=_system(system),
            messages=_messages(user),
        )
    except Exception as e:
        _note_error(e)
        raise
    if response.stop_reason == "refusal":
        raise LLMRefusal(REFUSAL_MESSAGE)
    return _text_of(response)


async def structured(system: str, user, schema: dict, effort: str = EFFORT_PLAN,
                     max_tokens: int = 8000, timeout: float = 90.0) -> dict:
    """Structured output: the reply is guaranteed to be JSON matching `schema`."""
    if not available():
        raise LLMUnavailable(MISSING_KEY_MESSAGE)
    output_config = {"effort": effort, "format": {"type": "json_schema", "schema": schema}}
    try:
        response = await get_client().with_options(timeout=timeout).beta.messages.create(
            **{**_common(effort, max_tokens), "output_config": output_config},
            system=_system(system),
            messages=_messages(user),
        )
    except Exception as e:
        _note_error(e)
        raise
    if response.stop_reason == "refusal":
        raise LLMRefusal(REFUSAL_MESSAGE)
    if response.stop_reason == "max_tokens":
        raise ValueError("Claude plan was cut off (max_tokens)")
    return json.loads(_text_of(response))


async def stream_chat(system: str, user, effort: str = EFFORT_CHAT,
                      max_tokens: int = 8000, timeout: float = 120.0) -> AsyncIterator[str]:
    """Stream text deltas for the chat UI. `user` is a string or a messages list."""
    if not available():
        raise LLMUnavailable(MISSING_KEY_MESSAGE)
    try:
        async with get_client().with_options(timeout=timeout).beta.messages.stream(
            **_common(effort, max_tokens),
            system=_system(system),
            messages=_messages(user),
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
    except Exception as e:
        _note_error(e)
        raise
    if final.stop_reason == "refusal":
        raise LLMRefusal(REFUSAL_MESSAGE)


async def stream_chat_with_tools(system: str, messages: list, tools: list, effort: str = EFFORT_CHAT,
                                 max_tokens: int = 8000, timeout: float = 120.0) -> AsyncIterator[tuple]:
    """Stream a chat turn that may call a tool.

    Yields ("text", chunk) while Claude writes, then ("tool", name, input_dict) for each
    tool call in the final message.
    """
    if not available():
        raise LLMUnavailable(MISSING_KEY_MESSAGE)
    try:
        async with get_client().with_options(timeout=timeout).beta.messages.stream(
            **_common(effort, max_tokens),
            system=_system(system),
            messages=_messages(messages),
            tools=tools,
        ) as stream:
            async for text in stream.text_stream:
                yield ("text", text)
            final = await stream.get_final_message()
    except Exception as e:
        _note_error(e)
        raise
    if final.stop_reason == "refusal":
        raise LLMRefusal(REFUSAL_MESSAGE)
    if final.stop_reason in ("max_tokens", "refusal"):
        return
    for block in final.content:
        if block.type == "tool_use":
            data = block.input
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    continue
            if isinstance(data, dict):
                yield ("tool", block.name, data)


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


def friendly_error(error: Exception) -> str:
    """Map SDK errors to short messages for the chat UI."""
    if isinstance(error, (LLMRefusal, LLMUnavailable)):
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
