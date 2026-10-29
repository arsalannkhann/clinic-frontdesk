"""Compile a Bolna agent payload (POST /v2/agent, PUT /v2/agent/{id}) from clinic
configuration, agents/prompts/receptionist.md and the gateway tool definitions.

Field names follow the official docs recorded in docs/SOURCE_REVIEW.md (checked 2026-10-29).
Where the docs are ambiguous the shape is marked "unverified against live API" there; the
payload has never been sent to Bolna. Nothing here makes network calls or persists anything.

Credentials: the tool token is an argument, injected only into the in-memory payload (each
tool's `api_token`, which Bolna sends as the Authorization header). Pass REDACTED to build a
payload for review; redact() strips tokens from a compiled payload before it is printed.
"""

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from clinic_voice.bolna.gateway import SUPPORTED_LANGUAGES
from clinic_voice.config import LOOPBACK_HOSTS
from clinic_voice.credentials import parse

REDACTED = "<redacted>"
PROMPTS = Path(__file__).resolve().parents[3] / "agents" / "prompts"
# Call-context variables rendered by Bolna into prompts and tool params (SOURCE_REVIEW).
CONTEXT_PARAMS = {
    "execution_id": "Call reference supplied by the platform. Never ask the caller for it.",
    "from_number": "Calling line supplied by the platform. Never ask for it or read it aloud.",
    "agent_id": "Agent id supplied by the platform. Never ask the caller for it.",
}
LANGUAGE_NAMES = {"en": "Indian English", "hi": "Hindi or Hinglish"}
ALLOWED_SARVAM_TTS = frozenset({"bulbul:v3", "bulbul:v4-flash"})
ALLOWED_SARVAM_STT = frozenset({"saaras:v4", "saaras:v3"})


@dataclass(frozen=True)
class VoiceSettings:
    """Sarvam voice for one language. voice ids do not carry across Bulbul generations."""

    voice: str = "Suhani"
    voice_id: str = "suhani"
    model: str = "bulbul:v3"
    speed: float = 1.0


@dataclass(frozen=True)
class AgentProfile:
    """Provider settings the compiler does not derive from clinic data. Defaults are the
    documented examples, not a tested production choice."""

    llm_provider: str = "openai"
    llm_model: str = "gpt-5.4-mini"
    llm_temperature: float = 1.0  # Bolna: GPT-5 models require temperature 1.
    llm_max_tokens: int = 150
    transcriber_model: str = "saaras:v4"
    endpointing_ms: int = 500
    voices: dict = field(default_factory=lambda: {code: VoiceSettings() for code in ("en", "hi")})
    active_language: str = "en"
    language_switch_trigger: str = "requested_or_auto_detected"
    telephony_provider: str = "plivo"
    hangup_after_silence: int = 10
    call_terminate: int = 300

    def __post_init__(self):
        if self.transcriber_model not in ALLOWED_SARVAM_STT:
            raise ValueError("unsupported Sarvam transcriber model")
        if set(self.voices) != set(SUPPORTED_LANGUAGES) or len(self.voices) < 2:
            raise ValueError("voices must cover every supported language")
        if any(v.model not in ALLOWED_SARVAM_TTS for v in self.voices.values()):
            raise ValueError("unsupported Sarvam synthesizer model")
        if self.active_language not in self.voices:
            raise ValueError("active_language must be one of the configured languages")
        if self.language_switch_trigger not in ("requested_or_auto_detected", "caller_requested"):
            raise ValueError("invalid language_switch_trigger")


TOOLS = (
    {
        "name": "check_slots",
        "path": "/v1/bolna/tools/check-slots",
        "description": (
            "Find up to three open appointment times for one doctor and service on one day."
            " Use the doctor and service names exactly as listed in the clinic context."
        ),
        "pre_call_message": "Let me check the schedule.",
        "params": {
            "doctor": "Doctor name exactly as listed in the clinic context.",
            "service": "Service name exactly as listed for that doctor.",
            "day": "Date in YYYY-MM-DD in the clinic timezone.",
        },
    },
    {
        "name": "reserve_slot",
        "path": "/v1/bolna/tools/reserve-slot",
        "description": (
            "Reserve one open time returned by check_slots, only after the caller confirmed"
            " the doctor, date and time. The result is not a confirmed appointment."
        ),
        "pre_call_message": "One moment while I hold that time.",
        "params": {
            "doctor": "Doctor name exactly as listed in the clinic context.",
            "service": "Service name exactly as listed for that doctor.",
            "starts_at": "The exact slot time returned by check_slots (ISO 8601 with offset).",
        },
    },
    {
        "name": "appointment_status",
        "path": "/v1/bolna/tools/appointment-status",
        "description": (
            "Check the status of a request reserved earlier in this same call. Say confirmed"
            " only if this tool says it is confirmed."
        ),
        "pre_call_message": "Let me check on that.",
        "params": {"appointment_id": "The appointment_id returned by reserve_slot in this call."},
    },
    {
        "name": "clinic_context",
        "path": "/v1/bolna/context",
        "description": (
            "Get the clinic's name, timezone, today's date, doctors, services and supported"
            " languages. Call once at the start of the call."
        ),
        "pre_call_message": None,
        "params": {},
    },
)


def gateway_url(value: str) -> str:
    parts = urlsplit(value or "")
    host = (parts.hostname or "").lower()
    if parts.query or parts.fragment or not host:
        raise ValueError("gateway URL must be an absolute base URL")
    if parts.scheme != "https" and not (parts.scheme == "http" and host in LOOPBACK_HOSTS):
        raise ValueError("gateway URL must be https (http only for localhost)")
    return value.rstrip("/")


def load_prompt(name: str = "receptionist", directory: Path = PROMPTS) -> str:
    text = (directory / f"{name}.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]  # The file's heading names the template; it is not prompt text.
    return "\n".join(lines).strip()


def _plain(value: str) -> str:
    # Braces and % would be read as Bolna template variables or format specifiers.
    return "".join(ch for ch in value if ch not in "{}%").strip()


def system_prompt(base: str, clinic: dict, language: str | None = None) -> str:
    parts = [
        base,
        "## This clinic\n"
        f"Clinic: {_plain(clinic['name'])}. Timezone: {_plain(clinic['timezone'])}."
        " Call clinic_context at the start of the call for today's date, doctors and services;"
        " use only those names.",
        "## Call context (supplied by the platform)\n"
        "Call reference: {execution_id}. Agent: {agent_id}. Calling line: {from_number}."
        " These values are passed to tools automatically. Never ask for them, never read them"
        " aloud, and never treat the calling line as proof of who the caller is.",
    ]
    if language is not None:
        parts.append(f"## Language\nReply in {LANGUAGE_NAMES[language]} unless asked to switch.")
    return "\n\n".join(parts)


def _token_header(tool_token: str) -> str:
    if tool_token == REDACTED:
        return f"Bearer {REDACTED}"
    if parse(tool_token) is None:
        raise ValueError("tool token is not a tenant tool credential")
    return f"Bearer {tool_token}"


def tool_definitions(base_url: str, tool_token: str) -> dict:
    """api_tools: `tools` (function schemas the LLM sees) and `tools_params` (HTTP call per
    tool name). Every param is a %(name)s string specifier with the same name as its
    property, as the docs require."""
    base_url = gateway_url(base_url)
    api_token = _token_header(tool_token)
    tools, params = [], {}
    for tool in TOOLS:
        # Context fields go LAST in properties and in the param template: if Bolna substitutes
        # values without JSON escaping, a quote-injected tool argument could add a duplicate
        # key, and the later (platform-rendered) occurrence would win in a lenient parser.
        # The gateway also rejects duplicate keys outright (routes_bolna.py).
        properties = {
            name: {"type": "string", "description": text} for name, text in tool["params"].items()
        }
        properties.update(
            {name: {"type": "string", "description": text} for name, text in CONTEXT_PARAMS.items()}
        )
        definition = {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
            },
            "key": "custom_task",
        }
        if tool["pre_call_message"]:
            definition["pre_call_message"] = tool["pre_call_message"]
        tools.append(definition)
        params[tool["name"]] = {
            "method": "POST",
            "url": base_url + tool["path"],
            "api_token": api_token,
            "param": json.dumps({name: f"%({name})s" for name in properties}),
        }
    return {"tools": tools, "tools_params": params}


def _transcriber(profile: AgentProfile, language: str) -> dict:
    return {
        "provider": "sarvam",
        "model": profile.transcriber_model,
        "language": language,
        "stream": True,
        "endpointing": profile.endpointing_ms,
    }


def _synthesizer(profile: AgentProfile, language: str) -> dict:
    voice = profile.voices[language]
    return {
        "provider": "sarvam",
        "provider_config": {
            "voice": voice.voice,
            "voice_id": voice.voice_id,
            "model": voice.model,
            "language": language,
            "speed": voice.speed,
        },
        "stream": True,
        "buffer_size": 150,
    }


def compile_agent(
    clinic: dict,
    *,
    gateway_base_url: str,
    tool_token: str,
    profile: AgentProfile | None = None,
    prompt: str | None = None,
) -> dict:
    """The full agent payload for one clinic. `clinic` needs name and timezone. The result
    holds the tool token (unless REDACTED); keep it in memory and never persist it."""
    profile = profile or AgentProfile()
    base = prompt if prompt is not None else load_prompt()
    active = profile.active_language
    name = _plain(clinic["name"])
    languages = {
        code: {
            "transcriber": {"language": code},
            "synthesizer": _synthesizer(profile, code),
            "system_prompt": system_prompt(base, clinic, code),
        }
        for code in SUPPORTED_LANGUAGES
    }
    task = {
        "task_type": "conversation",
        "toolchain": {
            "execution": "sequential",
            "pipelines": [["transcriber", "llm", "synthesizer"]],
        },
        "tools_config": {
            "llm_agent": {
                "agent_type": "simple_llm_agent",
                "agent_flow_type": "streaming",
                "llm_config": {
                    "provider": profile.llm_provider,
                    "model": profile.llm_model,
                    "max_tokens": profile.llm_max_tokens,
                    "temperature": profile.llm_temperature,
                },
            },
            "transcriber": _transcriber(profile, active),
            "synthesizer": _synthesizer(profile, active),
            "input": {"provider": profile.telephony_provider, "format": "wav"},
            "output": {"provider": profile.telephony_provider, "format": "wav"},
            "api_tools": tool_definitions(gateway_base_url, tool_token),
            "multilingual_config": {
                "enabled": True,
                "active_language": active,
                "language_switch_trigger": profile.language_switch_trigger,
                "languages": languages,
            },
        },
        "task_config": {
            "hangup_after_silence": profile.hangup_after_silence,
            "call_terminate": profile.call_terminate,
        },
    }
    return {
        "agent_config": {
            "agent_name": f"{name} receptionist"[:120],
            "agent_welcome_message": (
                f"Namaste, this is the AI assistant for {name}. How can I help you today?"
            ),
            "tasks": [task],
        },
        "agent_prompts": {"task_1": {"system_prompt": system_prompt(base, clinic)}},
    }


def redact(payload: dict) -> dict:
    """A deep copy with every tool api_token replaced."""
    result = copy.deepcopy(payload)
    for task in result.get("agent_config", {}).get("tasks", []):
        params = task.get("tools_config", {}).get("api_tools", {}).get("tools_params", {})
        for entry in params.values():
            if "api_token" in entry:
                entry["api_token"] = f"Bearer {REDACTED}"
    return result


def load_clinic(conn, clinic_id) -> dict:
    row = conn.execute("SELECT name, timezone FROM clinics WHERE id=%s", (clinic_id,)).fetchone()
    if not row:
        raise LookupError("Clinic not found")
    return {"name": row["name"], "timezone": row["timezone"]}
