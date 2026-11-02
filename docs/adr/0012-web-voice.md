# ADR 0012 Web voice receptionist

Status: accepted, 1 November 2026. Tested against local PostgreSQL with a scripted model (`FakeChatLLM`) and a scripted speech port (`FakeSpeech`); the Sarvam adapter was smoke-tested once against the live speech API. Nothing is deployed and no telephony is involved.

A patient on the clinic website taps "Talk to the AI receptionist" and speaks. We want that without a telephony platform, a voice-agent vendor (Bolna) or a second conversation engine: the spoken conversation should be the chat conversation of ADR 0011, with the same tools, rules, limits and booking path.

Decisions:

- Turn-based pipeline: speech-to-text, then the existing chat turn, then text-to-speech. The browser records one utterance at a time (16 kHz mono 16-bit PCM WAV) and posts it as base64 JSON to `POST /v1/chat/sessions/{id}/voice`; the API transcribes it, runs `chat.send(..., voice=True)` and returns the reply text and base64 MP3 audio. The only new dependency is a speech API key (`SPEECH_API_KEY`, Sarvam saarika for STT and bulbul for TTS) next to the chat model key. No streaming, WebRTC or WebSocket.
- One engine. A voice turn is a chat turn: the same session, encrypted transcript, tools, proposal flow, emergency script, per-address and per-clinic limits, busy lease and fallbacks. `voice=True` only appends spoken-reply rules to the system prompt (one or two short sentences, no symbols, never read ids, natural times, and after `propose_booking` point the patient to the card on screen). Typed chat is unchanged.
- The model still only proposes. Booking completes through the existing proposal card: the patient types name and phone, ticks consent and taps confirm on screen. Name and phone are never collected by voice.
- Speech before the turn. The session is checked first (same 404, 409 and a separate per-address bucket under `CHAT_MESSAGE_IP_PER_HOUR`), then STT runs, so an empty transcript is 422 `no_speech` and consumes no turn, and an unknown session never costs a speech call. STT failure is 503 `voice_unavailable`; TTS failure still returns the text with `audio` null.
- Privacy. Audio is never stored or logged. The transcript is stored only as the session's encrypted user message. The adapter (`providers/speech.py`) raises fixed codes (`timeout`, `transport`, `unauthorized`, `http_<status>`, `malformed`), never provider text, does not follow redirects, and its `repr` never shows the key.
- Greeting audio is synthesized once per adapter, clinic, text and language and cached in process memory (bounded), since every session of a clinic has the same greeting.
- `voice_enabled` on the public profile is `chat_enabled` with both keys configured, so the website shows the voice button only where it can work.

Consequences: each turn costs one STT and one TTS call besides the model calls (about 0.5 s and 1.6 s observed), so a spoken reply takes a few seconds; barge-in and partial transcripts are not supported. Phone lines come later through a telephony adapter that streams audio into the same chat engine (the same tools and booking rules), rather than a second conversation logic hosted by a voice-agent vendor.
