"""Patient messaging port and an in-memory fake (T9).

No live messaging provider is implemented: none has been chosen, and sending messages to
patients needs explicit owner authorization (AGENTS.md). Choosing a provider, recording its
official documentation in docs/SOURCE_REVIEW.md and writing an adapter with contract
fixtures is an owner action (docs/ROADMAP.md).

Adapter obligations (the same error classes and rules as the calendar and Bolna adapters):
- send_message(to, body, reference) sends one message to an E.164 number and returns the
  provider's message id. `reference` is stable across every attempt of one planned contact;
  an adapter must pass it to the provider as its idempotency key or client reference where
  the provider supports one, so a lookup can find the message later.
- A timeout or connection failure after the request may have been sent, a 5xx, or a 2xx
  without a message id is ProviderTimeout (outcome unknown); a failure before anything was
  sent, or a documented "nothing happened" response, is ProviderRetryable; a refusal is
  ProviderPermanent. Exceptions carry fixed short codes only: never the number, the body or
  provider text.
- get_message(reference) returns the message sent under that reference, or None if the
  provider has none. The worker calls it after an ambiguous timeout before any new send.
- Non-fake adapters declare `timeout_seconds` (outbox.check_ports).
- Every adapter declares two capabilities, which are provider acceptance criteria to be
  proven from the provider's official documentation (docs/SOURCE_REVIEW.md) and contract
  fixtures before they are set True:
  - idempotent_reference: the provider deduplicates sends by our reference, so a second
    send with the same reference never delivers a second message (for at least as long as a
    contact can stay unsettled: EXPIRY in reminders.py);
  - consistent_lookup: get_message is read-after-write consistent, so None means the
    provider never accepted a message under that reference (not "not indexed yet").
  After an ambiguous attempt whose lookup returns None, the worker sends again only when
  both are True; otherwise the contact goes to needs_review and is never resent
  automatically (a resend could deliver a duplicate).

Message bodies are composed by the worker at send time and handed to the port only; they are
never stored, logged or put in outbox payloads.
"""

from dataclasses import dataclass
from threading import Lock
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from clinic_voice.providers.calendar import ProviderError, ProviderTimeout


@dataclass(frozen=True)
class SentMessage:
    """What a lookup returns: the provider's id and status, never the body or number."""

    provider_message_id: str
    status: str = "sent"


class MessagingPort(Protocol):
    idempotent_reference: bool
    consistent_lookup: bool

    def send_message(self, to: str, body: str, reference: str) -> str:
        """Send one message; returns the provider message id."""

    def get_message(self, reference: str) -> SentMessage | None:
        """The message sent under `reference`, or None."""


class FakeMessaging:
    """Deterministic in-memory MessagingPort for tests and local development.

    By default it behaves like a provider that meets both acceptance criteria:
    idempotent_reference (a second send with a known reference returns the first message id
    and delivers nothing) and consistent_lookup. Tests set either False to model a provider
    that does not. With idempotent_reference False it delivers every send, so tests prove
    that the worker reconciles instead of re-sending. `sent` records (to, body, reference) of
    every delivered message, in order. Fault injection: fail_next(n, error_type) makes the
    next n operations (send or lookup) raise error_type; timeout_after_send delivers the next
    message and then raises ProviderTimeout; timeout_before_send raises ProviderTimeout
    without delivering. `lookups` counts get_message calls."""

    def __init__(self, idempotent_reference: bool = True, consistent_lookup: bool = True):
        self.idempotent_reference = idempotent_reference
        self.consistent_lookup = consistent_lookup
        self._lock = Lock()
        self._counter = 0
        self._failures: list[type[ProviderError]] = []
        self.sent: list[tuple[str, str, str]] = []
        self.by_reference: dict[str, str] = {}
        self.lookups = 0
        self.timeout_after_send = False
        self.timeout_before_send = False

    def __repr__(self):
        return f"FakeMessaging(sent={len(self.sent)})"

    def fail_next(self, n: int = 1, error_type: type[ProviderError] = ProviderTimeout) -> None:
        with self._lock:
            self._failures.extend([error_type] * n)

    def _injected(self):
        if self._failures:
            raise self._failures.pop(0)()

    def send_message(self, to, body, reference):
        with self._lock:
            self._injected()
            if self.timeout_before_send:
                self.timeout_before_send = False
                raise ProviderTimeout()
            if self.idempotent_reference and reference in self.by_reference:
                return self.by_reference[reference]
            self._counter += 1
            message_id = str(uuid5(NAMESPACE_URL, f"fake-message:{self._counter}"))
            self.sent.append((to, body, reference))
            self.by_reference[reference] = message_id
            if self.timeout_after_send:
                self.timeout_after_send = False
                raise ProviderTimeout()
            return message_id

    def get_message(self, reference):
        with self._lock:
            self.lookups += 1
            self._injected()
            message_id = self.by_reference.get(reference)
            return None if message_id is None else SentMessage(message_id)

    def count(self, reference: str) -> int:
        return sum(1 for _, _, ref in self.sent if ref == reference)
