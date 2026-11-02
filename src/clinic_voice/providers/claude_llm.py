"""Chat model port over Claude on Microsoft Foundry (ADR 0013), through the Anthropic SDK.

The chat engine speaks OpenAI-format messages and tools (chat.py); this adapter converts them
to the Messages API shape on each call: system text goes to the top-level `system`, assistant
tool calls become `tool_use` blocks, and the tool results that follow one assistant message
are a single user message of `tool_result` blocks. Facts verified on 2 November 2026 against
claude-haiku-5-5 on Foundry (Hosted on Azure): tool use works with thinking disabled at effort
"low" (about 1-2 s a call); a conversation may open with an assistant turn (the greeting) but
may not end with one (prefill is a 400); `temperature` is not sent (non-default sampling is a
400 on this model).

Thinking is disabled, so no thinking block ever has to be passed back between tool rounds.
Errors are ChatLLMError with the same short codes as the OpenAI adapter; no request or
response body, header or provider message is ever placed in an exception or a log line.
"""

import json
import re

import anthropic
from anthropic import Anthropic, AnthropicFoundry

from clinic_voice.providers.chat_llm import MAX_TOOL_CALLS, ChatLLMError, LLMReply, ToolCall

TOOL_ID = re.compile(r"[^A-Za-z0-9_-]")


def _tool_id(value) -> str:
    """A tool_use id the Messages API accepts (letters, digits, _ and -). Ids from another
    model (a fallback round) are kept recognisable but made safe."""
    return TOOL_ID.sub("_", str(value or ""))[:64] or "call"


def _arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def convert_tools(tools: list[dict]) -> list[dict]:
    """OpenAI function tools as Messages API tools."""
    result = []
    for tool in tools:
        function = tool.get("function") or {}
        result.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object"},
            }
        )
    return result


def convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-format messages as (system text, Messages API messages). Consecutive turns of
    one role are merged, and a trailing assistant turn is dropped (it would be a prefill)."""
    system: list[str] = []
    result: list[dict] = []

    def add(role, blocks):
        if not blocks:
            return
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": list(blocks)})

    for message in messages:
        role, content = message.get("role"), message.get("content")
        text = content if isinstance(content, str) else ""
        if role == "system":
            if text.strip():
                system.append(text)
        elif role == "user":
            if text.strip():
                add("user", [{"type": "text", "text": text}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": text}] if text.strip() else []
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": _tool_id(call.get("id")),
                        "name": function.get("name", ""),
                        "input": _arguments(function.get("arguments")),
                    }
                )
            add("assistant", blocks)
        elif role == "tool":
            add(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": _tool_id(message.get("tool_call_id")),
                        "content": text,
                    }
                ],
            )
    while result and result[-1]["role"] == "assistant":
        result.pop()
    return "\n\n".join(system), result


def parse_message(message) -> LLMReply:
    """The visible text and tool calls of a Messages API response. A refusal raises
    ChatLLMError('refusal'); max_tokens before any text or tool call raises 'length'."""
    if message.stop_reason == "refusal":
        raise ChatLLMError("refusal")
    texts, calls = [], []
    for block in message.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use" and len(calls) < MAX_TOOL_CALLS:
            arguments = block.input if isinstance(block.input, dict) else None
            calls.append(ToolCall(block.id, block.name[:64], arguments))
    text = "\n".join(t for t in texts if t).strip() or None
    if message.stop_reason == "max_tokens" and text is None and not calls:
        raise ChatLLMError("length")
    return LLMReply(text, calls)


class ClaudeChatLLM:
    """ChatLLMPort over the Messages API (an AnthropicFoundry client)."""

    def __init__(self, client, model: str, max_tokens: int = 700, effort: str | None = "low"):
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort

    def __repr__(self):
        return f"ClaudeChatLLM(model={self._model!r})"  # Never the key.

    def complete(self, messages, tools):
        system, converted = convert_messages(messages)
        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": converted,
            "thinking": {"type": "disabled"},
        }
        if system:
            body["system"] = system
        if self._effort:
            body["output_config"] = {"effort": self._effort}
        if tools:
            body["tools"] = convert_tools(tools)
        try:
            message = self._client.messages.create(**body)
        except anthropic.APITimeoutError:
            raise ChatLLMError("timeout") from None
        except anthropic.APIConnectionError:
            raise ChatLLMError("transport") from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise ChatLLMError("unauthorized") from None
        except anthropic.APIStatusError as error:
            raise ChatLLMError(f"http_{error.status_code}") from None
        except anthropic.AnthropicError:
            raise ChatLLMError("malformed") from None
        return parse_message(message)


def build(settings) -> ClaudeChatLLM:
    # No SDK retries: a failed call goes to the fallback model at once (chat_llm.build).
    options = {"timeout": settings.chat_llm_timeout_seconds, "max_retries": 0}
    if settings.claude_direct():
        client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value(), **options)
    else:
        client = AnthropicFoundry(
            resource=settings.claude_foundry_resource,
            api_key=settings.claude_foundry_api_key.get_secret_value(),
            **options,
        )
    return ClaudeChatLLM(
        client,
        settings.claude_model,
        settings.chat_llm_max_tokens,
        settings.chat_llm_reasoning_effort,
    )
