"""Bolna platform API port, an in-memory fake and an httpx adapter.

Endpoints follow the official API reference recorded in docs/SOURCE_REVIEW.md (checked
2026-10-29); none has been exercised against the live API. Tests use httpx.MockTransport
only. Number purchase is deliberately not implemented.

Error mapping (shared ProviderError classes from providers/calendar.py). Writes are
create/update agent, inbound setup, start call and stop call; reads are get execution and
list phone numbers.
- Reads: 429 and 5xx are ProviderRetryable (with Retry-After when present); nothing changed.
- Writes: 5xx is ProviderTimeout('http_<status>'): the provider may have acted before
  failing, so the outcome is unknown and must be looked up before any retry. 429 is
  ProviderRetryable only where Bolna documents that nothing was done (POST /call: the
  number is still activating and no call was queued; wait retry_after_seconds); on other
  writes it is ProviderTimeout('http_429') as well.
- 401/403: ProviderPermanent('unauthorized'); other 4xx: ProviderPermanent('http_<status>').
  get_execution 404 is None.
- Any 2xx on a write is accepted. A 2xx whose body lacks the expected id (create agent,
  start call) is ProviderTimeout('malformed'): the write may have happened.
- Timeouts: on writes a timeout after the request may have been sent is ProviderTimeout
  (outcome unknown, never success). On reads, and for connection failures before anything
  was sent, ProviderRetryable.

Exceptions carry fixed short codes only. Bodies (transcripts, numbers, provider messages)
and the API key are never logged or raised; httpx errors are not chained.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

import httpx

from clinic_voice.providers.calendar import (
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
)
from clinic_voice.providers.http import MAX_RETRY_AFTER, retry_after

DEFAULT_BASE = "https://api.bolna.ai"
DEFAULT_TIMEOUT = 10.0
# Statuses we treat as terminal (no further updates expected). This is our reading of the
# documented status enum; Bolna does not document which statuses are final.
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
SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@dataclass(frozen=True)
class Execution:
    """Minimized execution record: no transcript, numbers, recording or extracted data."""

    id: str
    agent_id: str | None
    status: str
    termination_reason: str | None = None
    call_type: str | None = None
    conversation_duration: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class PhoneNumber:
    """A number on the Bolna account (the clinic's line, not a patient's)."""

    id: str
    phone_number: str
    agent_id: str | None
    telephony_provider: str | None


class BolnaPort(Protocol):
    def create_agent(self, payload: dict) -> str:
        """POST /v2/agent. Returns the new agent id."""

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
        against the live API (AGENTS.md); nothing in this repository calls it yet."""

    def stop_scheduled_call(self, execution_id: str) -> None:
        """POST /call/{execution_id}/stop: stop a queued or scheduled call."""


def _safe_id(value: str) -> str:
    # Ids are interpolated into URL paths: reject anything that could change the path.
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError("invalid provider id")
    return value


class FakeBolna:
    """Deterministic in-memory BolnaPort. `calls` records (method, args) for assertions."""

    def __init__(self, phone_numbers: list[PhoneNumber] | None = None):
        self._lock = Lock()
        self._counter = 0
        self.calls: list[tuple[str, tuple]] = []
        self.agents: dict[str, dict] = {}
        self.executions: dict[str, Execution] = {}
        self.phone_numbers = list(phone_numbers or [])

    def __repr__(self):
        return f"FakeBolna(agents={len(self.agents)}, executions={len(self.executions)})"

    def _next_id(self, kind):
        self._counter += 1
        return str(uuid5(NAMESPACE_URL, f"fake-bolna:{kind}:{self._counter}"))

    def create_agent(self, payload):
        with self._lock:
            self.calls.append(("create_agent", (payload,)))
            agent_id = self._next_id("agent")
            self.agents[agent_id] = payload
            return agent_id

    def update_agent(self, agent_id, payload):
        with self._lock:
            self.calls.append(("update_agent", (agent_id, payload)))
            if agent_id not in self.agents:
                raise ProviderPermanent("http_404")
            self.agents[agent_id] = payload

    def get_execution(self, execution_id):
        with self._lock:
            self.calls.append(("get_execution", (execution_id,)))
            return self.executions.get(execution_id)

    def list_phone_numbers(self):
        with self._lock:
            self.calls.append(("list_phone_numbers", ()))
            return list(self.phone_numbers)

    def assign_phone_number(self, phone_number_id, agent_id):
        with self._lock:
            self.calls.append(("assign_phone_number", (phone_number_id, agent_id)))
            for index, number in enumerate(self.phone_numbers):
                if number.id == phone_number_id:
                    self.phone_numbers[index] = PhoneNumber(
                        number.id, number.phone_number, agent_id, number.telephony_provider
                    )
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
            execution_id = self._next_id("execution")
            status = "scheduled" if scheduled_at else "queued"
            self.executions[execution_id] = Execution(execution_id, agent_id, status)
            return execution_id

    def stop_scheduled_call(self, execution_id):
        with self._lock:
            self.calls.append(("stop_scheduled_call", (execution_id,)))
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


class BolnaClient:
    """BolnaPort over the Bolna REST API with a bearer API key."""

    def __init__(
        self,
        api_key: str,
        http: httpx.Client,
        base_url: str = DEFAULT_BASE,
        timeout_seconds: float = DEFAULT_TIMEOUT,
    ):
        if not api_key:
            raise ValueError("Bolna API key is required")
        self._api_key = api_key
        self._http = http
        self._base = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def __repr__(self):
        return "BolnaClient()"

    def _send(self, method, path, *, write, json=None) -> httpx.Response:
        try:
            return self._http.request(
                method,
                self._base + path,
                json=json,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self.timeout_seconds,
            )
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
