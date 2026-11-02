# ADR 0014: Real-time call agent on LiveKit

Status: accepted, 2 November 2026

## Context

The web voice receptionist (ADR 0012) is half duplex: the browser records until 600 ms of
silence, uploads the utterance, and one request runs Sarvam STT, the chat engine (Claude Haiku
5.5, ADR 0013) and Sarvam TTS in sequence. Production logs on 2 November 2026 (API in dub1):
STT 0.6-1.5 s, model plus database 1.0-2.6 s, TTS 1.1-3.5 s, so a caller waits 3.5-7 s for the
first sound. A call cannot be interrupted, does not notice silence, and does not hang up.
Moving the host saves a few hundred milliseconds; the sequence is the cost.

## Decision

Calls run as a streaming pipeline on LiveKit (open-source Agents framework, LiveKit Cloud):

- The browser joins a LiveKit room over WebRTC. `POST /v1/chat/sessions/{id}/call` (public,
  behind the web proxy) mints a short-lived participant token for a fresh room whose token
  carries an explicit dispatch of the `clinic-receptionist` agent. The dispatch metadata holds
  the chat session's bearer id and the caller's address; the token is signed by the API, so the
  browser can read but not change it, and the bearer id is the browser's own.
- `clinic_voice.call_agent` is a LiveKit agent worker: a second process of this package, like
  the tick worker, with the same database and keyring settings. Each call is one existing
  chat session: the caller's words and the agent's replies are stored in the encrypted
  transcript, tools are chat.run_tool with a voice Turn, so the booking guard (name and number
  must come from the caller, number in an earlier turn) and confirm() are unchanged.
- Streaming everywhere: speech to text while the caller speaks (Deepgram Nova-3 multilingual
  through LiveKit Inference, or Sarvam streaming), an end-of-turn model rather than a silence
  timer, Claude streamed token by token (thinking disabled, effort low), and text to speech
  started on the first phrase (Cartesia Sonic-3 through LiveKit Inference, or Sarvam Bulbul
  streaming). Providers are settings, so a local run needs only the Sarvam key.
- Real call behaviour: the caller can interrupt (barge-in); after a silence the agent asks
  whether they are still there and hangs up after a second silence; an end_call tool lets the
  agent say goodbye and hang up when the caller is done; an unclear utterance gets a short
  clarification question; the emergency script and the per-clinic daily model cap still apply;
  a call ends at CALL_MAX_MINUTES.
- The worker is deployed to LiveKit Cloud agent hosting in Mumbai (ap-south), next to the
  India-hosted speech models, from Dockerfile.call on its own lock (requirements-call.lock,
  because livekit-plugins-anthropic needs anthropic<2). The half-duplex /voice endpoint stays
  as the fallback when LiveKit is not configured.
- The website's call screen (web/components/public/chat/RealtimeCall.tsx, livekit-client)
  is chosen when the profile reports realtime_call. It joins with echo cancellation and noise
  suppression, plays the agent's audio, shows the agent's state ("lk.agent.state"), live
  captions from the "lk.transcription" text stream, an orb driven by both voices' levels, and
  the held time and booking from the agent's attributes. The CSP allows wss and https to
  *.livekit.cloud.

## Consequences

The first sound after the caller stops is expected at about 0.8-1.2 s (end-of-turn
decision, first Claude tokens, first TTS audio), measured once deployed. Tool calls still cross
to the database in eu-west-1; moving the database to ap-south-1 is a later option. The same
worker can later answer phone calls through LiveKit SIP. LiveKit Cloud usage (WebRTC minutes,
agent hosting, inference) is billed per minute and needs the owner's account.
