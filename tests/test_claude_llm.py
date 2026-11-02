"""Claude on Foundry adapter (providers/claude_llm.py) and the fallback composite."""

import json

import httpx2
import pytest
from anthropic import AnthropicFoundry

from clinic_voice.chat import TOOLS
from clinic_voice.config import Settings
from clinic_voice.providers import chat_llm, claude_llm
from clinic_voice.providers.chat_llm import ChatLLMError, FakeChatLLM, LLMReply


def _adapter(handler, effort="low"):
    client = AnthropicFoundry(
        resource="test-resource",
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return claude_llm.ClaudeChatLLM(client, "claude-haiku-5-5", 700, effort)


def _message(content, stop_reason="end_turn"):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


CONVERSATION = [
    {"role": "system", "content": "You are the receptionist."},
    {"role": "assistant", "content": "Hello, how can I help?"},
    {"role": "user", "content": "Any cleaning slots tomorrow?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call.1",
                "type": "function",
                "function": {"name": "check_slots", "arguments": '{"service": "cleaning"}'},
            },
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "list_services", "arguments": ""},
            },
        ],
    },
    {"role": "tool", "tool_call_id": "call.1", "content": '{"slots": []}'},
    {"role": "tool", "tool_call_id": "c2", "content": '{"services": []}'},
]


def test_request_shape_and_reply():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json=_message(
                [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "check_slots",
                        "input": {"service": "cleaning"},
                    },
                ],
                "tool_use",
            ),
        )

    reply = _adapter(handler).complete(CONVERSATION, TOOLS)
    assert seen["url"] == "https://test-resource.services.ai.azure.com/anthropic/v1/messages"
    body = seen["body"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 700
    assert body["system"] == "You are the receptionist."
    assert body["thinking"] == {"type": "disabled"}
    assert body["output_config"] == {"effort": "low"}
    assert "temperature" not in body and "tool_choice" not in body
    assert {t["name"] for t in body["tools"]} == {t["function"]["name"] for t in TOOLS}
    assert all("input_schema" in t for t in body["tools"])
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["assistant", "user", "assistant", "user"]
    calls = body["messages"][2]["content"]
    assert [c["id"] for c in calls] == ["call_1", "c2"]
    assert calls[0]["input"] == {"service": "cleaning"} and calls[1]["input"] == {}
    results = body["messages"][3]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    assert [r["tool_use_id"] for r in results] == ["call_1", "c2"]
    assert reply.text == "Checking."
    assert reply.tool_calls[0].name == "check_slots"
    assert reply.tool_calls[0].arguments == {"service": "cleaning"}


def test_merges_turns_and_drops_trailing_assistant():
    system, messages = claude_llm.convert_messages(
        [
            {"role": "system", "content": "A"},
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "user", "content": "  "},
            {"role": "assistant", "content": "a note"},
        ]
    )
    assert system == "A"
    assert messages == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
        }
    ]


def test_no_effort_omits_output_config():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_message([{"type": "text", "text": "Hi"}]))

    assert _adapter(handler, effort=None).complete(CONVERSATION[:3], []).text == "Hi"
    assert "output_config" not in seen["body"] and "tools" not in seen["body"]


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "unauthorized"), (403, "unauthorized"), (429, "http_429"), (529, "http_529")],
)
def test_errors_are_short_codes(status, code):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"message": "secret"}})

    with pytest.raises(ChatLLMError) as raised:
        _adapter(handler).complete(CONVERSATION[:3], [])
    assert raised.value.code == code
    assert "secret" not in str(raised.value)


def test_transport_failure():
    def handler(request):
        raise httpx2.ConnectError("down")

    with pytest.raises(ChatLLMError) as raised:
        _adapter(handler).complete(CONVERSATION[:3], [])
    assert raised.value.code == "transport"


@pytest.mark.parametrize(("stop", "code"), [("refusal", "refusal"), ("max_tokens", "length")])
def test_refusal_and_empty_length(stop, code):
    def handler(request):
        return httpx2.Response(200, json=_message([], stop))

    with pytest.raises(ChatLLMError) as raised:
        _adapter(handler).complete(CONVERSATION[:3], [])
    assert raised.value.code == code


def test_fallback_answers_when_primary_fails():
    primary = FakeChatLLM([ChatLLMError("http_429")])
    secondary = FakeChatLLM([LLMReply("From the fallback.")])
    reply = chat_llm.FallbackChatLLM(primary, secondary).complete(CONVERSATION[:3], [])
    assert reply.text == "From the fallback."
    assert len(primary.calls) == 1 and len(secondary.calls) == 1


def test_fallback_not_used_when_primary_answers():
    secondary = FakeChatLLM()
    composite = chat_llm.FallbackChatLLM(FakeChatLLM([LLMReply("Claude.")]), secondary)
    assert composite.complete(CONVERSATION[:3], []).text == "Claude."
    assert secondary.calls == []


def _settings(**values):
    return Settings(_env_file=None, database_url="postgresql://x@localhost/x_test", **values)


def test_build_selects_provider():
    claude = {
        "claude_foundry_resource": "Test-Resource",
        "claude_foundry_api_key": "foundry-test-key",
    }
    assert chat_llm.build(_settings()) is None
    assert isinstance(chat_llm.build(_settings(**claude)), claude_llm.ClaudeChatLLM)
    both = chat_llm.build(_settings(**claude, chat_llm_api_key="g"))
    assert isinstance(both, chat_llm.FallbackChatLLM)
    assert isinstance(chat_llm.build(_settings(chat_llm_api_key="g")), chat_llm.OpenAIChatLLM)
    assert _settings(**claude).voice_configured() is False  # Speech key still needed.
    assert "foundry-test-key" not in repr(both)


def test_resource_must_be_a_name():
    with pytest.raises(ValueError):
        _settings(claude_foundry_resource="evil.example.com/x")
