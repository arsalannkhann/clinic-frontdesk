"""Real-time call agent (ADR 0014): a LiveKit Agents worker for the web call.

This module is the synchronous half, CallState; call_worker.py is the LiveKit half (`python -m
clinic_voice.call_worker dev` locally, `start` in production), which needs the `call` extra and
the API's database and keyring settings plus LIVEKIT_* and the chat model. Each job is one
call: the room was created by a participant token from calls.start, whose dispatch metadata
names the chat session (bearer id) and the caller's address.

CallState reuses the chat engine: the caller's words and the
agent's replies go into the session's encrypted transcript, every user turn counts against
CHAT_MAX_TURNS and the clinic's daily model cap, and the tools are chat.run_tool with a voice
Turn, so the booking guard and chat.confirm are unchanged. The LiveKit half streams speech to
text, Claude and text to speech, lets the caller interrupt, asks "are you still there?" after a
silence and hangs up after a second one, hangs up when the caller is done (end_call), and ends
every call at CALL_MAX_MINUTES. The agent publishes a held time and a made booking as its
participant attributes ("call.held", "call.booking") for the call screen.

Audio and transcripts never leave this process except to the configured speech and model
providers: session recording to LiveKit Cloud is off. Logs carry session ids, tool names and
error codes only.
"""

import json
import logging
import re

from clinic_voice import chat, public
from clinic_voice.db import connect
from clinic_voice.errors import PublicError

log = logging.getLogger("clinic_voice.call_agent")

ATTRIBUTE_HELD = "call.held"
ATTRIBUTE_BOOKING = "call.booking"
TOKEN = re.compile(r"^[0-9a-f]{32}$")

CALL_RULES = """
- Talk like an experienced front-desk receptionist on the phone: warm, brisk and natural. Vary \
short acknowledgements ("Sure", "Got it", "One moment"); never say "Certainly!" or "Great \
question".
- If you did not catch what the caller said, or it is incomplete, ask one short question to \
clarify. Never guess a name, a number, a day or a time.
- When the caller says they are done, says goodbye, or has nothing else, say one short goodbye \
and call end_call. Do not end the call while a booking is half done unless they ask."""

SILENCE_PROMPT = (
    "The caller has gone quiet. Ask briefly, in their language, whether they are still there."
)
GOODBYE_SILENT = "It seems we got disconnected. Please call again any time. Goodbye."
GOODBYE_TIMEOUT = "We have reached the time limit for this call. Please call again. Goodbye."
GOODBYE_LIMIT = "This call has reached its limit. Please call the clinic directly. Goodbye."


class CallError(Exception):
    """The job cannot become a call (bad metadata, unknown or closed session)."""


def parse_metadata(raw: str | None) -> tuple[str, str | None]:
    """(session bearer id, caller address) from the dispatch metadata written by calls.start."""
    try:
        data = json.loads(raw or "")
    except ValueError:
        raise CallError("metadata") from None
    token = data.get("session") if isinstance(data, dict) else None
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise CallError("metadata")
    address = data.get("address")
    return token, address if isinstance(address, str) else None


def tool_schemas() -> list[dict]:
    """The voice tools as raw function schemas ({name, description, parameters})."""
    return [dict(tool["function"]) for tool in chat.VOICE_TOOLS]


class CallState:
    """The synchronous side of one call: the chat session, its Turn and the transcript. Not
    thread safe; the LiveKit side calls it from one worker thread at a time."""

    def __init__(self, url, settings, token: str, address):
        keyring, hash_key = public.crypto(settings)
        conn = connect(url)
        try:
            try:
                session = chat.find_session(conn, token)
            except PublicError:
                raise CallError("session") from None
            clinic = chat._clinic(conn, session["clinic_id"])
            self.prompt = chat.system_prompt(conn, clinic, voice=True) + CALL_RULES
            conn.commit()
        except BaseException:
            conn.close()
            raise
        self.settings = settings
        self.clinic = clinic
        self.turn = chat.Turn(url, settings, clinic, session, address, keyring, hash_key, conn)
        self.turn.token, self.turn.voice = token, True
        self.greeting = chat.voice_greeting(clinic)
        # Set by user_said for the turns after it: the reply must not wait on the database.
        self._ended = False
        self._capped = False
        self._closed = False

    @property
    def session_id(self):
        return self.turn.session["id"]

    def precheck(self, text: str) -> str:
        """The outcome of a caller utterance decided without the database, so the reply can
        start at once while user_said stores it: "emergency" by the text, "limit" or "capped"
        when an earlier user_said found the call over its turn limit or the clinic over its
        daily model cap. A turn that crosses a limit is therefore stopped one turn late."""
        if self._ended:
            return "limit"
        if chat.EMERGENCY.search(text):
            return "emergency"
        if self._capped:
            return "capped"
        return "ok"

    def user_said(self, text: str) -> str:
        """Store one caller utterance as a user turn. Returns "ok", "emergency" (the caller
        is to hear the emergency script), "limit" (CHAT_MAX_TURNS reached: end the call) or
        "capped" (the clinic's daily model cap: no model reply)."""
        turn = self.turn
        with turn.db() as conn:
            row = conn.execute(
                "SELECT status, turn_count FROM chat_sessions WHERE clinic_id=%s AND id=%s"
                " FOR UPDATE",
                (turn.session["clinic_id"], turn.session["id"]),
            ).fetchone()
            if row is None or row["status"] != "active":
                self._ended = True
                return "limit"
            if row["turn_count"] >= self.settings.chat_max_turns:
                self._ended = True
                return "limit"
            conn.execute(
                "UPDATE chat_sessions SET turn_count=turn_count + 1 WHERE clinic_id=%s AND id=%s",
                (turn.session["clinic_id"], turn.session["id"]),
            )
            chat.append(conn, turn.keyring, turn.session, "user", {"text": text})
        if chat.EMERGENCY.search(text):
            log.info("call emergency script session=%s", self.session_id)
            return "emergency"
        if not chat._model_call_allowed(turn):
            log.warning("call model daily cap reached session=%s", self.session_id)
            self._capped = True
            return "capped"
        return "ok"

    def emergency_text(self) -> str:
        return chat.emergency_script(self.clinic)

    def capped_text(self) -> str:
        return chat.fallback_text(self.clinic)

    def agent_said(self, text: str) -> None:
        # The goodbye can arrive after the call closed; it is not stored then.
        if text.strip() and not self._closed:
            self.turn.store("assistant", {"text": text})

    def run_tool(self, call_id: str, name: str, arguments: dict | None) -> dict:
        """Run one tool call and store it with its result, as a chat turn would."""
        turn = self.turn
        turn.store(
            "assistant",
            {"text": None, "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}]},
        )
        result = chat.run_tool(turn, name, arguments)
        tool_name = name if name in chat.VOICE_TOOL_NAMES else None
        turn.store("tool", {"tool_call_id": call_id, "result": result}, tool_name=tool_name)
        log.info(
            "call tool session=%s tool=%s outcome=%s",
            self.session_id,
            tool_name or "unknown",
            result.get("error", "ok"),
        )
        return result

    def held(self) -> dict | None:
        """The time the call holds for the caller (chat.proposal_out), if any."""
        if self.turn.proposal is None:
            return None
        held = chat.proposal_out(self.turn.proposal, self.turn.zone)
        return json.loads(json.dumps(held, default=str))

    def booking(self) -> dict | None:
        """The booking this call made, for the call screen."""
        booking = self.turn.booking
        if booking is None:
            return None
        return {
            "reference": booking["reference"],
            "status": booking["status"],
            "starts_at": booking["starts_at"].astimezone(self.turn.zone).isoformat(),
            "doctor_name": booking["doctor_name"],
            "service_name": booking["service_name"],
            "text": chat.booked_text(booking, self.turn.zone),
        }

    def close(self) -> None:
        self._closed = True
        self.turn.close()
