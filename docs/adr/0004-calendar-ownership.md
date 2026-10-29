# ADR 0004 Calendar ownership and external busy time

Status: accepted, 29 October 2026.

Google Calendar is a convenience view for clinic staff, not the source of truth, and FreeBusy is never a lock: a read followed by an insert can race with any human or system editing the same calendar.

Decisions:

- The database is the reservation ledger. `appointments` and its exclusion constraint decide who holds a doctor's time. Calendar events are projections of ledger rows written by the worker (T4) and are never read back to decide who holds the time (reading an event back to recover an ambiguous write is fine).
- Each doctor should have a dedicated booking calendar (`doctors.booking_calendar_id`) that only this system writes to. Event ids are deterministic (`event_id_for(appointment_id, version)`) so retries and ambiguous timeouts are recovered by fetching the id, never by inserting a second event. Summaries are opaque references; no patient data goes to the calendar.
- A doctor's personal or other calendars (`doctors.busy_calendar_ids`, at most 10) are read-only advisory inputs. Their busy time is fetched with FreeBusy into the `external_busy` cache and unioned into slot math. The cache is only as fresh as its last refresh, so it narrows offers but guarantees nothing. The booking calendar is also queried; its own events then show as busy, which is acceptable because the same times already block locally.
- The worker re-checks external busy time just before inserting an event. A conflict found then, or any later human edit that collides with a ledger row, moves the appointment to `needs_review` for staff; the system does not silently move or cancel patient bookings.
- Calendar-side deletion or edits by humans can diverge from the ledger. Reconciliation and incremental sync (sync tokens, watch channels) are future work and are required before a pilot.

Consequences: a slot can be offered that an external calendar already blocks (stale cache) and a caller can be told to wait while staff resolve a conflict; both are visible in `needs_review` rather than hidden. Calendar mapping is optional per doctor; an unmapped doctor has no external busy input and no calendar projection.
