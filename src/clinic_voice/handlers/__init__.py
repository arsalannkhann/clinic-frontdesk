"""Outbox handler registry: job kind -> Handler(run, on_dead).

Kinds the outbox CHECK allows but that have no entry here (reminder.schedule, provision.step)
are reserved for later tasks; a job of such a kind is dead-lettered with 'unknown_kind'.
Nothing enqueues them.
"""

from clinic_voice.handlers import bolna, calendar, reminders
from clinic_voice.outbox import Handler

HANDLERS: dict[str, Handler] = {
    "calendar.create": Handler(calendar.create, on_dead=calendar.needs_review_on_dead),
    "calendar.cancel": Handler(calendar.cancel),
    "calendar.refresh_busy": Handler(calendar.refresh_busy),
    "calendar.revoke_grant": Handler(calendar.revoke_grant),
    "webhook.process": Handler(bolna.process_webhook, on_dead=bolna.webhook_failed_on_dead),
    "bolna.reconcile": Handler(bolna.reconcile),
    "notification.send": Handler(reminders.send_message, on_dead=reminders.contact_on_dead),
    "reminder.call": Handler(reminders.place_call, on_dead=reminders.contact_on_dead),
    "reminder.stop": Handler(reminders.stop),
}
