# ADR 0013: Claude on Microsoft Foundry as the chat and voice model

Status: accepted, 2 November 2026

## Context

The chat and web voice receptionist ran on `openai/gpt-oss-120b` through Groq's free tier
(ADR 0011, 0012). Each model call is about 1,700 prompt tokens and a booking takes several
calls, so the 8,000 tokens-per-minute cap returned 429s within one conversation and patients
heard the fallback ("we cannot book right now"). The owner deployed `claude-haiku-5-5` on a
Microsoft Foundry resource (Global Standard, hosted on Azure).

## Decision

- `providers/claude_llm.py` implements the existing `ChatLLMPort` with the official Anthropic
  SDK (`AnthropicFoundry`, resource name only, never a free-form URL). chat.py keeps its
  OpenAI-format messages; the adapter converts them per call (top-level `system`, `tool_use`
  blocks, one user message of `tool_result` blocks per assistant message, consecutive turns
  merged, a trailing assistant turn dropped because prefill is a 400).
- Thinking is disabled and `output_config.effort` is `CHAT_LLM_REASONING_EFFORT` (low): a
  receptionist turn is short, and no thinking block has to be replayed between tool rounds.
  No `temperature` is sent (non-default sampling is a 400 on Haiku 5.5).
- `chat_llm.build` returns Claude when `CLAUDE_FOUNDRY_RESOURCE` and `CLAUDE_FOUNDRY_API_KEY`
  are set, wrapped in `FallbackChatLLM` with the `CHAT_LLM_*` model when that is also set:
  any Claude failure (429, 5xx, timeout, refusal) is answered by the fallback model in the same
  turn. SDK retries are off so a failure falls through at once.
- Errors stay fixed short codes; no provider text, body or key is logged.

## Consequences

About $0.10 / $0.50 per million input / output tokens: roughly $0.004 per booking call.
Measured locally: a spoken booking call completes end to end with Claude answering every
turn; model calls take about 1-2 s.

Amendment (2 November 2026): ANTHROPIC_API_KEY, a Claude Console key, runs the same model on
the Claude API (api.anthropic.com) and takes precedence over Foundry when set; the request
shape, thinking and effort settings are unchanged. `scripts/set-claude-key.sh` stores the key
in the API's Vercel project and the call agent's LiveKit secrets without echoing it.
