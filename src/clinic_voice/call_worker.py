"""The real-time call agent's LiveKit half (ADR 0014): `python -m clinic_voice.call_worker dev`
locally, `start` in production, `download-files` once to fetch the VAD and turn detector models.

Needs the `call` extra. Each call streams speech to text, Claude (thinking disabled, ADR 0013)
and text to speech; the caller can interrupt; a silence of CALL_SILENCE_SECONDS gets "are you
still there?" and a second one a goodbye and hang-up; end_call hangs up when the caller is
done; every call ends at CALL_MAX_MINUTES. Preemptive generation is off so a tool such as
book_appointment never runs on words the caller had not finished. Session recording is off.
The synchronous half (session, transcript, limits, tools) is call_agent.CallState.
"""

import asyncio
import json
import logging
from typing import Any

import anthropic
from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    StopResponse,
    function_tool,
    inference,
)
from livekit.agents.beta.tools.end_call import EndCallTool
from livekit.plugins import anthropic as lk_anthropic
from livekit.plugins import sarvam, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from clinic_voice.call_agent import (
    ATTRIBUTE_BOOKING,
    ATTRIBUTE_HELD,
    GOODBYE_LIMIT,
    GOODBYE_SILENT,
    GOODBYE_TIMEOUT,
    SILENCE_PROMPT,
    CallError,
    CallState,
    parse_metadata,
    tool_schemas,
)
from clinic_voice.config import get_settings

log = logging.getLogger("clinic_voice.call_worker")
settings = get_settings()


class ClaudeLLM(lk_anthropic.LLM):
    """Claude on Foundry with thinking disabled and the configured effort (ADR 0013)."""

    def chat(self, **kwargs):
        extra = dict(kwargs.pop("extra_kwargs", None) or {})
        body = dict(extra.get("extra_body") or {})
        body["thinking"] = {"type": "disabled"}
        if settings.chat_llm_reasoning_effort:
            body["output_config"] = {"effort": settings.chat_llm_reasoning_effort}
        extra["extra_body"] = body
        return super().chat(extra_kwargs=extra, **kwargs)


def make_llm():
    key = settings.claude_foundry_api_key.get_secret_value()
    client = anthropic.AsyncAnthropicFoundry(
        resource=settings.claude_foundry_resource, api_key=key, max_retries=1
    )
    return ClaudeLLM(
        model=settings.claude_model,
        client=client,
        api_key=key,  # Required by the plugin even with a client; the client is what is used.
        max_tokens=settings.chat_llm_max_tokens,
        # Caches the system prompt and tools (and the conversation so far) between turns.
        caching="ephemeral",
        _strict_tool_schema=False,
    ), client


async def warm(client):
    """Open the model connection while the greeting plays: the first request of a fresh
    connection pays about a second of connection setup that the caller would otherwise hear."""
    try:
        await client.messages.create(
            model=settings.claude_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except anthropic.AnthropicError as error:
        log.warning("call model warm-up failed error=%s", type(error).__name__)


def make_stt():
    if settings.call_stt == "sarvam":
        return sarvam.STT(
            language="unknown",
            model="saaras:v3",
            mode="transcribe",
            api_key=settings.speech_api_key.get_secret_value(),
        )
    return inference.STT(model=settings.call_stt, language=settings.call_stt_language)


def make_tts():
    if settings.call_tts == "sarvam":
        return sarvam.TTS(
            target_language_code="en-IN",
            model=settings.speech_tts_model,
            speaker=settings.call_tts_voice or settings.speech_tts_speaker,
            api_key=settings.speech_api_key.get_secret_value(),
        )
    kwargs = {"language": settings.call_tts_language}
    if settings.call_tts_voice:
        kwargs["voice"] = settings.call_tts_voice
    return inference.TTS(model=settings.call_tts, **kwargs)


class Call:
    """One call: serializes CallState (one worker thread at a time) and owns hang-ups."""

    def __init__(self, ctx: JobContext, state: CallState):
        self.ctx = ctx
        self.state = state
        self.session: AgentSession | None = None
        self._lock = asyncio.Lock()
        self._silences = 0
        self._published: dict[str, str] = {}
        self._ending = False

    async def run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def publish(self):
        """The held time and the booking as agent attributes, for the call screen."""
        attributes = {
            key: json.dumps(value) if value else ""
            for key, value in (
                (ATTRIBUTE_HELD, self.state.held()),
                (ATTRIBUTE_BOOKING, self.state.booking()),
            )
        }
        changed = {k: v for k, v in attributes.items() if self._published.get(k) != v}
        if changed:
            self._published.update(changed)
            await self.ctx.room.local_participant.set_attributes(changed)

    async def hang_up(self, goodbye: str):
        if self.session is None or self._ending:
            return
        self._ending = True
        await self.session.say(goodbye, allow_interruptions=False)
        self.ctx.add_shutdown_callback(self.ctx.delete_room)
        self.session.shutdown()

    def on_user_state(self, event):
        if event.new_state == "speaking":
            self._silences = 0
        elif event.new_state == "away" and self.session is not None and not self._ending:
            self._silences += 1
            if self._silences == 1:
                self.session.generate_reply(instructions=SILENCE_PROMPT)
            else:
                asyncio.create_task(self.hang_up(GOODBYE_SILENT))

    def on_metrics(self, event):
        """Per-stage latency (no content): end of turn, model first token, speech first byte."""
        m = event.metrics
        kind = type(m).__name__
        if kind == "EOUMetrics":
            log.info(
                "call latency session=%s eou_ms=%d stt_ms=%d hook_ms=%d",
                self.state.session_id,
                m.end_of_utterance_delay * 1000,
                m.transcription_delay * 1000,
                m.on_user_turn_completed_delay * 1000,
            )
        elif kind == "LLMMetrics":
            log.info(
                "call latency session=%s llm_ttft_ms=%d llm_ms=%d prompt_tokens=%d cached=%d",
                self.state.session_id,
                m.ttft * 1000,
                m.duration * 1000,
                m.prompt_tokens,
                m.prompt_cached_tokens,
            )
        elif kind == "TTSMetrics":
            log.info("call latency session=%s tts_ttfb_ms=%d", self.state.session_id, m.ttfb * 1000)

    def on_item(self, event):
        item = event.item
        if getattr(item, "role", None) == "assistant":
            text = (item.text_content or "").strip()
            if text:
                asyncio.create_task(self.run(self.state.agent_said, text))


class Receptionist(Agent):
    def __init__(self, call: Call):
        tools: list = [self._clinic_tool(schema) for schema in tool_schemas()]
        tools.append(
            EndCallTool(
                extra_description="Use it once the caller is done or says goodbye.",
                end_instructions="Say one short, warm goodbye in the caller's language.",
                delete_room=True,
                ignore_on_enter=True,
            )
        )
        super().__init__(instructions=call.state.prompt, tools=tools)
        self._call = call

    def _clinic_tool(self, schema: dict):
        name = schema["name"]

        async def handler(raw_arguments: dict[str, Any], context: RunContext):
            call_id = context.function_call.call_id
            async with context.with_filler("One moment.", delay=1.2):
                result = await self._call.run(
                    self._call.state.run_tool, call_id, name, raw_arguments
                )
            await self._call.publish()
            return json.dumps(result, ensure_ascii=False, default=str)

        return function_tool(handler, raw_schema=schema)

    async def on_user_turn_completed(self, turn_ctx, new_message):
        text = (new_message.text_content or "").strip()
        if not text:
            raise StopResponse()
        outcome = await self._call.run(self._call.state.user_said, text)
        if outcome == "ok":
            return
        if outcome == "emergency":
            self.session.say(self._call.state.emergency_text(), allow_interruptions=False)
        elif outcome == "capped":
            self.session.say(self._call.state.capped_text())
        else:
            asyncio.create_task(self._call.hang_up(GOODBYE_LIMIT))
        raise StopResponse()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


# Each idle process imports the chat engine and loads the VAD; a cold start can exceed the
# default 10 s on a small machine.
server = AgentServer(setup_fnc=prewarm, initialize_process_timeout=45.0)


@server.rtc_session(agent_name=settings.call_agent_name)
async def entrypoint(ctx: JobContext):
    try:
        token, address = parse_metadata(ctx.job.metadata)
        url = settings.database_url.get_secret_value()
        state = await asyncio.to_thread(CallState, url, settings, token, address)
    except CallError as error:
        log.warning("call refused reason=%s", error)
        await ctx.delete_room()
        return
    log.info("call started session=%s", state.session_id)
    call = Call(ctx, state)

    async def cleanup():
        await asyncio.to_thread(state.close)

    ctx.add_shutdown_callback(cleanup)
    if settings.call_llm == "claude":
        llm, client = make_llm()
        warming = asyncio.create_task(warm(client))
    else:
        llm, warming = inference.LLM(model=settings.call_llm), asyncio.create_task(asyncio.sleep(0))
    session = AgentSession(
        stt=make_stt(),
        llm=llm,
        tts=make_tts(),
        vad=ctx.proc.userdata["vad"],
        turn_handling={
            "turn_detection": MultilingualModel(),
            "endpointing": {"min_delay": 0.3, "max_delay": 2.5},
            "interruption": {"enabled": True},
            "preemptive_generation": {"enabled": False},
        },
        user_away_timeout=settings.call_silence_seconds,
        max_tool_steps=4,
    )
    call.session = session
    session.on("user_state_changed", call.on_user_state)
    session.on("conversation_item_added", call.on_item)
    session.on("metrics_collected", call.on_metrics)
    await session.start(room=ctx.room, agent=Receptionist(call), record=False, session_host=False)
    session.say(state.greeting)

    async def time_limit():
        await asyncio.sleep(settings.call_max_minutes * 60)
        await call.hang_up(GOODBYE_TIMEOUT)

    limit = asyncio.create_task(time_limit())

    async def stop_limit():
        limit.cancel()
        warming.cancel()

    ctx.add_shutdown_callback(stop_limit)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agents.cli.run_app(server)
