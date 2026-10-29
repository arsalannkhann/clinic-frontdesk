"""Bolna platform API port, an in-memory fake and an httpx adapter.

Endpoints follow the official API reference recorded in docs/SOURCE_REVIEW.md (checked
2026-10-29); none has been exercised against the live API. Tests use httpx.MockTransport
only. Number purchase is deliberately not implemented.

Error mapping (shared ProviderError classes from providers/calendar.py). Writes are
create/update agent, inbound setup, start call and stop call; reads are get agent, list
agents, get execution and list phone numbers.
- Reads: 429 and 5xx are ProviderRetryable (with Retry-After when present); nothing changed.
- Writes: 5xx is ProviderTimeout('http_<status>'): the provider may have acted before
  failing, so the outcome is unknown and must be looked up before any retry. 429 is
  ProviderRetryable only where Bolna documents that nothing was done (POST /call: the
  number is still activating and no call was queued; wait retry_after_seconds); on other
  writes it is ProviderTimeout('http_429') as well.
- 401/403: ProviderPermanent('unauthorized'); other 4xx: ProviderPermanent('http_<status>').
  get_execution and get_agent 404 are None (Bolna documents no 404 for GET /v2/agent/{id};
  our assumption).
- Any 2xx on a write is accepted. A 2xx whose body lacks the expected id (create agent,
  start call) is ProviderTimeout('malformed'): the write may have happened.
- Timeouts: on writes a timeout after the request may have been sent is ProviderTimeout
  (outcome unknown, never success). On reads, and for connection failures before anything
  was sent, ProviderRetryable.

summarize_agent() is the single allowlist for agent data (GET /v2/agent/{id} and
/v2/agent/all): id, agent_name, each tool's URL and the credential id parsed from each
tool's api_token. The token's secret part, prompts and every other field are dropped.

minimize_execution() is the single allowlist for execution data (webhook bodies and
GET /executions alike); STATUS_RANK and FINAL_STATUSES define the monotonic status order
used by the webhook processor and reconciliation.

Exceptions carry fixed short codes only. Bodies (transcripts, numbers, provider messages)
and the API key are never logged or raised; httpx errors are not chained.
"""

import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from clinic_voice import credentials
from clinic_voice.providers.calendar import (
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
)
from clinic_voice.providers.http import MAX_RETRY_AFTER, retry_after

DEFAULT_BASE = "https://api.bolna.ai"
DEFAULT_TIMEOUT = 10.0
# Largest response body read from the API; larger bodies are cut off like a timeout.
MAX_RESPONSE_BYTES = 1024 * 1024
# httpx timeouts apply per operation (connect, write, each read), so
# each gets timeout_seconds / 3 and the whole call is also checked against timeout_seconds
# while the body streams. A call therefore ends within about 4/3 of timeout_seconds, which
# check_ports keeps under a quarter of the lease (times 4/3).
HTTP_DEADLINE_PHASES = 3
# Statuses after which the call itself is over. call-disconnected means "ended, post-call
# processing pending" and is normally followed by completed (docs/SOURCE_REVIEW.md).
TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "call-disconnected",
        "balance-low",
        "busy",
        "no-answer",
        "canceled",
        "failed",
        "stopped",
        "error",
    }
)
# Statuses after which nothing changes. The call status page names only `completed` as the
# final status of every conversation; the unanswered/unsuccessful statuses are treated as
# final as well (our reading: the docs describe no transition out of them).
FINAL_STATUSES = TERMINAL_STATUSES - {"call-disconnected"}
# Monotonic progression used by the webhook processor and reconciliation: a status is
# applied only if its rank is at least the current one, and nothing replaces a final one.
# Equal ranks (scheduled/rescheduled, a repeated status) are lateral moves.
STATUS_RANK = {
    "scheduled": 1,
    "rescheduled": 1,
    "queued": 2,
    "initiated": 3,
    "ringing": 4,
    "in-progress": 5,
    "call-disconnected": 6,
    **dict.fromkeys(FINAL_STATUSES, 7),
}
ENDED_RANK = 6
FINAL_RANK = 7
SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


STATUS_PATTERN = re.compile(r"^[a-z][a-z-]{0,31}$")
# Disposition codes (termination_reason, hangup_reason, hangup_by): letters, underscore and
# hyphen, starting with a letter. No digits at all, so no phone number or other number
# can pass; no dots or spaces, so no free text. Bolna does not document these values as
# fixed codes (unverified); anything else is dropped. Migration 013 repeats the pattern.
CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z_-]{0,63}$")
NUMBER_PATTERN = re.compile(r"^[0-9]{1,9}(\.[0-9]{1,6})?$")
MAX_NUMBER = 1e9


def _uuid_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _code(value) -> str | None:
    """A short machine code (termination or hangup reason); anything else, including free
    text with spaces, is dropped."""
    return value if isinstance(value, str) and CODE_PATTERN.fullmatch(value) else None


def _number(value) -> float | None:
    """A finite non-negative number (or numeric string), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and NUMBER_PATTERN.fullmatch(value):
        value = float(value)
    if isinstance(value, int | float) and math.isfinite(value) and 0 <= value <= MAX_NUMBER:
        return float(value)
    return None


def _integer(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -(2**31) < value < 2**31 else None


def _timestamp(value) -> str | None:
    """An offset-aware ISO 8601 timestamp, normalized to UTC; naive or invalid values are
    dropped (the docs do not state the format)."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return None
        return moment.astimezone(UTC).isoformat()
    except (ValueError, OverflowError):  # e.g. 0001-01-01T00:00+05:30 leaves the range.
        return None


def minimize_execution(body) -> dict | None:
    """The allowlisted fields of an execution payload (webhook body or GET /executions),
    or None if it has no UUID `id` or no well-formed `status`.

    Kept: execution id, status, agent id, created/initiated/updated timestamps, conversation
    and telephony durations, termination and hangup codes, hangup_by, the provider's numeric
    hangup code, call type and numeric total cost. Everything else is dropped: transcript,
    summary, extracted_data, recording_url, user_number/agent_number, to/from numbers,
    context_details, error_message, smart_status and any field this list does not name.
    Absent or invalid values are omitted, never guessed."""
    if not isinstance(body, dict):
        return None
    execution_id = _uuid_text(body.get("id"))
    status = body.get("status")
    if execution_id is None or not isinstance(status, str) or not STATUS_PATTERN.fullmatch(status):
        return None
    telephony = body.get("telephony_data")
    telephony = telephony if isinstance(telephony, dict) else {}
    call_type = telephony.get("call_type")
    fields = {
        "execution_id": execution_id,
        "status": status,
        "agent_id": _uuid_text(body.get("agent_id")),
        "created_at": _timestamp(body.get("created_at")),
        "initiated_at": _timestamp(body.get("initiated_at")),
        "updated_at": _timestamp(body.get("updated_at")),
        "conversation_duration": _number(body.get("conversation_duration")),
        "telephony_duration": _number(telephony.get("duration")),
        "termination_reason": _code(body.get("termination_reason")),
        "hangup_reason": _code(telephony.get("hangup_reason")),
        "hangup_by": _code(telephony.get("hangup_by")),
        "hangup_provider_code": _integer(telephony.get("hangup_provider_code")),
        "call_type": call_type if call_type in ("inbound", "outbound") else None,
        "total_cost": _number(body.get("total_cost")),
    }
    return {key: value for key, value in fields.items() if value is not None}


def canonical(minimized: dict) -> str:
    return json.dumps(minimized, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Execution:
    """Minimized execution record: no transcript, numbers, recording or extracted data.
    `details` holds the minimize_execution() fields when the adapter read them."""

    id: str
    agent_id: str | None
    status: str
    termination_reason: str | None = None
    call_type: str | None = None
    conversation_duration: float | None = None
    details: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES

    def minimized(self) -> dict:
        """The same allowlisted shape a webhook produces."""
        if self.details:
            return dict(self.details)
        fields = {
            "execution_id": self.id,
            "status": self.status,
            "agent_id": _uuid_text(self.agent_id),
            "termination_reason": _code(self.termination_reason),
            "call_type": self.call_type if self.call_type in ("inbound", "outbound") else None,
            "conversation_duration": _number(self.conversation_duration),
        }
        return {key: value for key, value in fields.items() if value is not None}


@dataclass(frozen=True)
class PhoneNumber:
    """A number on the Bolna account (the clinic's line, not a patient's)."""

    id: str
    phone_number: str
    agent_id: str | None
    telephony_provider: str | None


@dataclass(frozen=True)
class Agent:
    """Allowlisted agent fields. tool_urls maps tool name -> URL; token_credential_ids holds,
    per tool, the tenant credential id (hex) of its api_token, or None when the token is
    absent, masked or not a tool credential. No secret, prompt or other field is kept."""

    id: str
    name: str | None
    tool_urls: dict = field(default_factory=dict)
    token_credential_ids: tuple = ()


def _maybe_json(value):
    # The API reference types api_tools.tools as "a JSON string"; accept either form.
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _credential_id(api_token) -> str | None:
    if not isinstance(api_token, str):
        return None
    scheme, _, token = api_token.partition(" ")
    parsed = credentials.parse(token if scheme == "Bearer" else api_token)
    return None if parsed is None else parsed[0].hex


def summarize_agent(body) -> Agent | None:
    """The allowlisted fields of an AgentV2 body (GET /v2/agent/{id}, items of
    /v2/agent/all), or None without a usable id. The api_token secret is never kept."""
    if not isinstance(body, dict):
        return None
    agent_id = body.get("id")
    if not isinstance(agent_id, str) or not SAFE_ID.fullmatch(agent_id):
        return None
    name = body.get("agent_name")
    urls, token_ids = {}, []
    tasks = body.get("tasks")
    for task in tasks if isinstance(tasks, list) else []:
        tools_config = task.get("tools_config") if isinstance(task, dict) else None
        api_tools = _maybe_json((tools_config or {}).get("api_tools"))
        params = _maybe_json(api_tools.get("tools_params")) if isinstance(api_tools, dict) else None
        for tool_name, entry in (params or {}).items() if isinstance(params, dict) else ():
            if not isinstance(entry, dict) or not isinstance(tool_name, str):
                continue
            url = entry.get("url")
            urls[tool_name] = url if isinstance(url, str) else None
            token_ids.append(_credential_id(entry.get("api_token")))
    return Agent(agent_id, name if isinstance(name, str) else None, urls, tuple(token_ids))


class BolnaPort(Protocol):
    def create_agent(self, payload: dict) -> str:
        """POST /v2/agent. Returns the new agent id."""

    def get_agent(self, agent_id: str) -> Agent | None:
        """GET /v2/agent/{agent_id}, summarized; None when unknown."""

    def list_agents(self) -> list[Agent]:
        """GET /v2/agent/all, summarized (used to find an agent by its deterministic name
        after an ambiguous create)."""

    def update_agent(self, agent_id: str, payload: dict) -> None:
        """PUT /v2/agent/{agent_id}: full replacement (PATCH cannot change tools_config)."""

    def get_execution(self, execution_id: str) -> Execution | None:
        """GET /executions/{execution_id}; None when unknown."""

    def list_phone_numbers(self) -> list[PhoneNumber]:
        """GET /phone-numbers/all."""

    def assign_phone_number(self, phone_number_id: str, agent_id: str) -> None:
        """POST /inbound/setup: route inbound calls on an existing number to the agent."""

    def start_outbound_call(
        self,
        agent_id: str,
        recipient_phone_number: str,
        *,
        from_phone_number: str | None = None,
        scheduled_at: datetime | None = None,
        user_data: dict | None = None,
    ) -> str:
        """POST /call. Returns the execution id. Requires explicit owner authorization to use
        against the live API (AGENTS.md). Only the reminder.call handler calls it, and only
        for a clinic with outbound_enabled (off by default)."""

    def stop_scheduled_call(self, execution_id: str) -> None:
        """POST /call/{execution_id}/stop: stop a queued or scheduled call."""


def _safe_id(value: str) -> str:
    # Ids are interpolated into URL paths: reject anything that could change the path.
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError("invalid provider id")
    return value


class FakeBolna:
    """Deterministic in-memory BolnaPort. `calls` records (method, args) for assertions.

    Fault injection: fail_next(op, n, error_type) makes the next n calls of that method raise
    error_type (recorded in `calls`, nothing changed); timeout_after_call makes the next
    start_outbound_call queue the call and then raise ProviderTimeout (outcome unknown);
    timeout_after_write.add(op) does the same once for create_agent, update_agent or
    assign_phone_number (the write happens, the caller sees a timeout).

    consistent_agent_lookup: whether list_agents reflects every completed create at once.
    The fake's listing is consistent; Bolna documents no such guarantee (BolnaClient: False)."""

    consistent_agent_lookup = True

    def __init__(self, phone_numbers: list[PhoneNumber] | None = None):
        self._lock = Lock()
        self._counter = 0
        self._failures: dict[str, list[type[ProviderError]]] = {}
        self.timeout_after_call = False
        self.timeout_after_write: set[str] = set()
        self.calls: list[tuple[str, tuple]] = []
        self.agents: dict[str, dict] = {}
        self.executions: dict[str, Execution] = {}
        self.phone_numbers = list(phone_numbers or [])

    def __repr__(self):
        return f"FakeBolna(agents={len(self.agents)}, executions={len(self.executions)})"

    def fail_next(
        self, op: str, n: int = 1, error_type: type[ProviderError] = ProviderTimeout
    ) -> None:
        with self._lock:
            self._failures.setdefault(op, []).extend([error_type] * n)

    def _injected(self, op):
        pending = self._failures.get(op)
        if pending:
            raise pending.pop(0)()

    def _next_id(self, kind):
        self._counter += 1
        return str(uuid5(NAMESPACE_URL, f"fake-bolna:{kind}:{self._counter}"))

    def _written(self, op):
        if op in self.timeout_after_write:
            self.timeout_after_write.discard(op)
            raise ProviderTimeout()

    def create_agent(self, payload):
        with self._lock:
            self.calls.append(("create_agent", (payload,)))
            self._injected("create_agent")
            agent_id = self._next_id("agent")
            self.agents[agent_id] = payload
            self._written("create_agent")
            return agent_id

    def update_agent(self, agent_id, payload):
        with self._lock:
            self.calls.append(("update_agent", (agent_id, payload)))
            self._injected("update_agent")
            if agent_id not in self.agents:
                raise ProviderPermanent("http_404")
            self.agents[agent_id] = payload
            self._written("update_agent")

    def _summary(self, agent_id):
        config = (self.agents[agent_id] or {}).get("agent_config") or {}
        return summarize_agent(
            {"id": agent_id, "agent_name": config.get("agent_name"), "tasks": config.get("tasks")}
        )

    def get_agent(self, agent_id):
        with self._lock:
            self.calls.append(("get_agent", (agent_id,)))
            self._injected("get_agent")
            return self._summary(agent_id) if agent_id in self.agents else None

    def list_agents(self):
        with self._lock:
            self.calls.append(("list_agents", ()))
            self._injected("list_agents")
            return [self._summary(agent_id) for agent_id in self.agents]

    def get_execution(self, execution_id):
        with self._lock:
            self.calls.append(("get_execution", (execution_id,)))
            return self.executions.get(execution_id)

    def list_phone_numbers(self):
        with self._lock:
            self.calls.append(("list_phone_numbers", ()))
            self._injected("list_phone_numbers")
            return list(self.phone_numbers)

    def assign_phone_number(self, phone_number_id, agent_id):
        with self._lock:
            self.calls.append(("assign_phone_number", (phone_number_id, agent_id)))
            self._injected("assign_phone_number")
            for index, number in enumerate(self.phone_numbers):
                if number.id == phone_number_id:
                    self.phone_numbers[index] = PhoneNumber(
                        number.id, number.phone_number, agent_id, number.telephony_provider
                    )
                    self._written("assign_phone_number")
                    return
            raise ProviderPermanent("http_400")

    def start_outbound_call(
        self,
        agent_id,
        recipient_phone_number,
        *,
        from_phone_number=None,
        scheduled_at=None,
        user_data=None,
    ):
        with self._lock:
            self.calls.append(
                (
                    "start_outbound_call",
                    (agent_id, recipient_phone_number, from_phone_number, scheduled_at, user_data),
                )
            )
            self._injected("start_outbound_call")
            execution_id = self._next_id("execution")
            status = "scheduled" if scheduled_at else "queued"
            self.executions[execution_id] = Execution(execution_id, agent_id, status)
            if self.timeout_after_call:
                self.timeout_after_call = False
                raise ProviderTimeout()
            return execution_id

    def stop_scheduled_call(self, execution_id):
        with self._lock:
            self.calls.append(("stop_scheduled_call", (execution_id,)))
            self._injected("stop_scheduled_call")
            current = self.executions.get(execution_id)
            if current is None or current.status not in ("queued", "scheduled", "rescheduled"):
                raise ProviderPermanent("http_400")
            self.executions[execution_id] = Execution(execution_id, current.agent_id, "stopped")


def _body_retry_after(response: httpx.Response) -> float | None:
    try:
        value = response.json().get("retry_after_seconds")
    except (ValueError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return min(max(float(value), 0.0), MAX_RETRY_AFTER)


def _error_for(
    response: httpx.Response, *, write: bool = False, safe_429: bool = False
) -> ProviderError:
    """safe_429: the endpoint documents that a 429 means nothing was done."""
    status = response.status_code
    if write and (status >= 500 or (status == 429 and not safe_429)):
        return ProviderTimeout(f"http_{status}")  # Outcome unknown: look up before retrying.
    if status == 429 or status >= 500:
        wait = retry_after(response)
        if wait is None and status == 429:
            wait = _body_retry_after(response)
        return ProviderRetryable(f"http_{status}", wait)
    if status in (401, 403):
        return ProviderPermanent("unauthorized")
    return ProviderPermanent(f"http_{status}")


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None


def _optional_str(value) -> str | None:
    return value if isinstance(value, str) else None


class _Overrun(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class BolnaClient:
    """BolnaPort over the Bolna REST API with a bearer API key."""

    # Bolna documents no read-after-write guarantee for GET /v2/agent/all, so a create whose
    # outcome is unknown and that the listing does not show is not retried (manual review).
    consistent_agent_lookup = False

    def __init__(
        self,
        api_key: str,
        http: httpx.Client,
        base_url: str = DEFAULT_BASE,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        clock=time.monotonic,
    ):
        if not api_key:
            raise ValueError("Bolna API key is required")
        self._api_key = api_key
        self._http = http
        self._base = base_url.rstrip("/")
        self._clock = clock
        self.timeout_seconds = timeout_seconds

    def __repr__(self):
        return "BolnaClient()"

    def _request(self, method, path, json) -> httpx.Response:
        """One request with a total deadline of timeout_seconds over the streamed body (see
        HTTP_DEADLINE_PHASES). Raises _Overrun when the deadline or MAX_RESPONSE_BYTES is
        exceeded; the returned response holds the decoded body."""
        started = self._clock()
        with self._http.stream(
            method,
            self._base + path,
            json=json,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(self.timeout_seconds / HTTP_DEADLINE_PHASES),
        ) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise _Overrun("too_large")
                if self._clock() - started > self.timeout_seconds:
                    raise _Overrun("deadline")
            if self._clock() - started > self.timeout_seconds:
                raise _Overrun("deadline")
            # iter_bytes() already decoded the body: drop the framing headers that described
            # the encoded form.
            headers = [
                (key, value)
                for key, value in response.headers.multi_items()
                if key.lower() not in ("content-encoding", "content-length", "transfer-encoding")
            ]
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(body),
                request=response.request,
            )

    def _send(self, method, path, *, write, json=None) -> httpx.Response:
        try:
            return self._request(method, path, json)
        except _Overrun as error:
            if write:
                raise ProviderTimeout(error.code) from None  # The request was sent.
            raise ProviderRetryable(error.code) from None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise ProviderRetryable("network") from None  # Nothing was sent.
        except httpx.TimeoutException:
            if write:
                raise ProviderTimeout() from None
            raise ProviderRetryable("timeout") from None
        except httpx.HTTPError:
            if write:
                raise ProviderTimeout("transport") from None
            raise ProviderRetryable("network") from None

    def create_agent(self, payload):
        response = self._send("POST", "/v2/agent", write=True, json=payload)
        if not response.is_success:
            raise _error_for(response, write=True)
        body = _json(response)
        agent_id = body.get("agent_id") if isinstance(body, dict) else None
        if not isinstance(agent_id, str) or not SAFE_ID.fullmatch(agent_id):
            raise ProviderTimeout("malformed")
        return agent_id

    def update_agent(self, agent_id, payload):
        response = self._send("PUT", f"/v2/agent/{_safe_id(agent_id)}", write=True, json=payload)
        if not response.is_success:
            raise _error_for(response, write=True)

    def get_agent(self, agent_id):
        response = self._send("GET", f"/v2/agent/{_safe_id(agent_id)}", write=False)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _error_for(response)
        agent = summarize_agent(_json(response))
        if agent is None:
            raise ProviderRetryable("malformed")
        return agent

    def list_agents(self):
        response = self._send("GET", "/v2/agent/all", write=False)
        if response.status_code != 200:
            raise _error_for(response)
        body = _json(response)
        if not isinstance(body, list):
            raise ProviderRetryable("malformed")
        agents = [summarize_agent(item) for item in body]
        if any(agent is None for agent in agents):
            raise ProviderRetryable("malformed")
        return agents

    def get_execution(self, execution_id):
        response = self._send("GET", f"/executions/{_safe_id(execution_id)}", write=False)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _error_for(response)
        body = _json(response)
        if not isinstance(body, dict) or not isinstance(body.get("status"), str):
            raise ProviderRetryable("malformed")
        telephony = body.get("telephony_data")
        telephony = telephony if isinstance(telephony, dict) else {}
        duration = body.get("conversation_duration")
        if isinstance(duration, bool) or not isinstance(duration, int | float):
            duration = None
        return Execution(
            id=_optional_str(body.get("id")) or execution_id,
            agent_id=_optional_str(body.get("agent_id")),
            status=body["status"],
            termination_reason=_optional_str(body.get("termination_reason")),
            call_type=_optional_str(telephony.get("call_type")),
            conversation_duration=float(duration) if duration is not None else None,
            details=minimize_execution(body) or {},
        )

    def list_phone_numbers(self):
        response = self._send("GET", "/phone-numbers/all", write=False)
        if response.status_code != 200:
            raise _error_for(response)
        body = _json(response)
        if not isinstance(body, list):
            raise ProviderRetryable("malformed")
        numbers = []
        for item in body:
            if not isinstance(item, dict):
                raise ProviderRetryable("malformed")
            number_id, number = item.get("id"), item.get("phone_number")
            if not isinstance(number_id, str) or not isinstance(number, str):
                raise ProviderRetryable("malformed")
            numbers.append(
                PhoneNumber(
                    number_id,
                    number,
                    _optional_str(item.get("agent_id")),
                    _optional_str(item.get("telephony_provider")),
                )
            )
        return numbers

    def assign_phone_number(self, phone_number_id, agent_id):
        response = self._send(
            "POST",
            "/inbound/setup",
            write=True,
            json={"agent_id": _safe_id(agent_id), "phone_number_id": _safe_id(phone_number_id)},
        )
        if not response.is_success:
            raise _error_for(response, write=True)

    def start_outbound_call(
        self,
        agent_id,
        recipient_phone_number,
        *,
        from_phone_number=None,
        scheduled_at=None,
        user_data=None,
    ):
        body = {"agent_id": _safe_id(agent_id), "recipient_phone_number": recipient_phone_number}
        if from_phone_number is not None:
            body["from_phone_number"] = from_phone_number
        if scheduled_at is not None:
            if scheduled_at.tzinfo is None:
                raise ValueError("scheduled_at must be timezone-aware")
            body["scheduled_at"] = scheduled_at.isoformat()
        if user_data is not None:
            body["user_data"] = user_data
        response = self._send("POST", "/call", write=True, json=body)
        if not response.is_success:
            # 429 here is documented as "number still activating; no call was queued".
            raise _error_for(response, write=True, safe_429=True)
        data = _json(response)
        execution_id = data.get("execution_id") if isinstance(data, dict) else None
        if not isinstance(execution_id, str) or not SAFE_ID.fullmatch(execution_id):
            raise ProviderTimeout("malformed")
        return execution_id

    def stop_scheduled_call(self, execution_id):
        response = self._send("POST", f"/call/{_safe_id(execution_id)}/stop", write=True)
        if not response.is_success:
            raise _error_for(response, write=True)
