"""Chat model port (P2, ADR 0011): OpenAI-compatible chat completions with tools.

The default deployment is Sarvam (`POST {CHAT_LLM_BASE_URL}/chat/completions`, model
sarvam-105b). Facts verified on 31 October 2026: the endpoint accepts the OpenAI `tools`
format and returns `choices[0].message.tool_calls` with JSON-string `arguments`; sarvam-105b
spends reasoning tokens (usage.completion_tokens_details.reasoning_tokens, about 80-200) out
of max_tokens, so max_tokens is about 1500; the message may carry `reasoning_content`, which
is never shown to a patient, stored or logged.

The adapter is a thin, stateless HTTP client. Errors are ChatLLMError with a short code
(timeout, unauthorized, http_<status>, malformed, transport); no request or response body,
header or provider message is ever placed in an exception or a log line.
"""

import json
import re
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol

import httpx

THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
MAX_TOOL_CALLS = 8


class ChatLLMError(Exception):
    """The model call failed. `code` is a fixed short string (never provider text)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    # Parsed JSON arguments, or None when the model sent something that is not a JSON object.
    arguments: dict | None


@dataclass(frozen=True)
class LLMReply:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class ChatLLMPort(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        """One chat completion over OpenAI-format messages and tool definitions."""


def _clean_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = THINK.sub("", value).strip()
    return text or None


def parse_reply(data) -> LLMReply:
    """The visible text and tool calls of a chat completion response. reasoning_content
    and any <think> block are dropped. Raises ChatLLMError('malformed')."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ChatLLMError("malformed") from None
    if not isinstance(message, dict):
        raise ChatLLMError("malformed")
    calls = []
    for index, raw in enumerate((message.get("tool_calls") or [])[:MAX_TOOL_CALLS]):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                arguments = None
        if not isinstance(arguments, dict):
            arguments = None
        call_id = raw.get("id") if isinstance(raw.get("id"), str) and raw.get("id") else None
        calls.append(ToolCall(call_id or f"call_{index}", name[:64], arguments))
    return LLMReply(_clean_text(message.get("content")), calls)


class OpenAIChatLLM:
    """ChatLLMPort over an OpenAI-compatible /chat/completions endpoint."""

    def __init__(
        self,
        client: httpx.Client,
        base_url: str,
        model: str,
        api_key: str,
        auth_header: str = "api-subscription-key",
        max_tokens: int = 1500,
    ):
        self._client = client
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._key = api_key
        self._auth_header = auth_header
        self._max_tokens = max_tokens

    def __repr__(self):
        return f"OpenAIChatLLM(model={self._model!r})"  # Never the key.

    def _headers(self) -> dict:
        if self._auth_header == "authorization":
            return {"Authorization": f"Bearer {self._key}"}
        return {"api-subscription-key": self._key}

    def complete(self, messages, tools):
        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": 0.2,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            response = self._client.post(self._url, json=body, headers=self._headers())
        except httpx.TimeoutException:
            raise ChatLLMError("timeout") from None
        except httpx.HTTPError:
            raise ChatLLMError("transport") from None
        if response.status_code in (401, 403):
            raise ChatLLMError("unauthorized")
        if not response.is_success:
            raise ChatLLMError(f"http_{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise ChatLLMError("malformed") from None
        return parse_reply(data)


class FakeChatLLM:
    """Scripted ChatLLMPort for tests. `script` items are LLMReply objects (returned in
    order) or exception instances (raised). When the script runs out, `default` is returned
    (a plain text reply). `calls` records the messages and tool names of every request."""

    def __init__(self, script=None, default: LLMReply | None = None):
        self._lock = Lock()
        self.script = list(script or [])
        self.default = default or LLMReply("How else can I help?")
        self.calls: list[dict] = []

    def complete(self, messages, tools):
        with self._lock:
            self.calls.append(
                {
                    "messages": json.loads(json.dumps(messages)),
                    "tools": [t["function"]["name"] for t in tools],
                }
            )
            item = self.script.pop(0) if self.script else self.default
        if isinstance(item, BaseException):
            raise item
        return item


def build(settings) -> ChatLLMPort | None:
    """The configured adapter, or None when CHAT_LLM_API_KEY is unset."""
    if not settings.chat_configured():
        return None
    return OpenAIChatLLM(
        httpx.Client(timeout=settings.chat_llm_timeout_seconds, follow_redirects=False),
        settings.chat_llm_base_url,
        settings.chat_llm_model,
        settings.chat_llm_api_key.get_secret_value(),
        settings.chat_llm_auth_header,
        settings.chat_llm_max_tokens,
    )
