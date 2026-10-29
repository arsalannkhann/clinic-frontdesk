-- Confirmations, reminders and opt-outs (T9).

-- Per-clinic contact policy. outbound_enabled is false by default: no message is sent and no
-- call is placed for a clinic until an owner or admin turns it on (and the owner has
-- authorized live messaging/calls; see docs/OPERATIONS.md). Quiet hours are clinic-local
-- wall times; a window whose start is after its end wraps midnight (21:00-09:00).
-- reminder_lead_minutes is how long before the appointment a reminder is due.
-- daily_contact_budget caps contacts per clinic-local day (0: none).
ALTER TABLE clinics
    ADD COLUMN outbound_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN quiet_hours_start time NOT NULL DEFAULT '21:00',
    ADD COLUMN quiet_hours_end time NOT NULL DEFAULT '09:00',
    ADD COLUMN reminder_lead_minutes integer NOT NULL DEFAULT 1440
        CHECK (reminder_lead_minutes BETWEEN 60 AND 10080),
    ADD COLUMN daily_contact_budget integer NOT NULL DEFAULT 200
        CHECK (daily_contact_budget BETWEEN 0 AND 100000),
    ADD CONSTRAINT clinics_quiet_hours_check CHECK (quiet_hours_start <> quiet_hours_end);

-- Opt-outs. One row per (patient, purpose, channel); 'all' matches every purpose or channel.
-- An opt-out is checked together with consent: contact needs an active consent grant and no
-- matching opt-out. source: tool (Bolna opt-out tool after booking verification), webhook
-- (reserved: Bolna documents no opt-out disposition, so nothing writes it yet), staff.
CREATE TABLE contact_preferences (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    patient_id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('booking_confirmation', 'reminder', 'all')),
    channel text NOT NULL CHECK (channel IN ('call', 'message', 'all')),
    source text NOT NULL CHECK (source IN ('tool', 'webhook', 'staff')),
    staff_user_id uuid,
    opted_out_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id),
    UNIQUE (clinic_id, patient_id, purpose, channel),
    FOREIGN KEY (clinic_id, patient_id) REFERENCES patients(clinic_id, id),
    FOREIGN KEY (clinic_id, staff_user_id) REFERENCES staff_users(clinic_id, id),
    CHECK ((source = 'staff') = (staff_user_id IS NOT NULL))
);

-- One planned contact (a booking confirmation message, or a reminder call or message) of
-- one appointment version. dedupe_key "<appointment>:<version>:<channel>:<purpose>:<lead
-- minutes>" is unique per clinic, so each kind of contact exists at most once per version
-- however many schedulers race. provider_reference is the stable idempotency reference sent
-- with every attempt (and used to look a message up after an ambiguous timeout). No message
-- body, phone number or name is stored. contact_day is the clinic-local date of
-- scheduled_for and is what the daily budget counts.
--
-- status: scheduled -> sending -> sent; sending -> unknown (ambiguous timeout) -> sent or
-- back to scheduled after reconciliation, or needs_review when no lookup exists (calls);
-- scheduled -> cancelled (appointment changed) | suppressed (policy: consent, opt-out,
-- quiet hours, budget, outbound disabled) | failed (provider refused or misconfigured).
CREATE TABLE reminders (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    appointment_id uuid NOT NULL,
    appointment_version integer NOT NULL CHECK (appointment_version >= 1),
    channel text NOT NULL CHECK (channel IN ('call', 'message')),
    purpose text NOT NULL CHECK (purpose IN ('booking_confirmation', 'reminder')),
    scheduled_for timestamptz NOT NULL,
    contact_day date NOT NULL,
    status text NOT NULL DEFAULT 'scheduled' CHECK (status IN (
        'scheduled', 'sending', 'sent', 'unknown', 'needs_review', 'cancelled', 'suppressed',
        'failed'
    )),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    -- Short machine code (reason or error); never provider text.
    last_error text CHECK (last_error IS NULL OR last_error ~ '^[a-z0-9_]{1,64}$'),
    provider_reference text NOT NULL CHECK (provider_reference ~ '^[A-Za-z0-9_-]{1,64}$'),
    provider_execution_id text
        CHECK (provider_execution_id IS NULL OR provider_execution_id ~ '^[A-Za-z0-9-]{1,64}$'),
    provider_message_id text
        CHECK (provider_message_id IS NULL OR char_length(provider_message_id) BETWEEN 1 AND 128),
    -- reminder.stop outcome for a placed call: stopped, or not_stoppable (already ringing,
    -- answered or finished).
    stop_outcome text CHECK (stop_outcome IS NULL OR stop_outcome IN ('stopped', 'not_stoppable')),
    dedupe_key text NOT NULL CHECK (char_length(dedupe_key) BETWEEN 1 AND 160),
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id),
    UNIQUE (clinic_id, dedupe_key),
    UNIQUE (clinic_id, provider_reference),
    FOREIGN KEY (clinic_id, appointment_id) REFERENCES appointments(clinic_id, id),
    CHECK (purpose <> 'booking_confirmation' OR channel = 'message'),
    CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
    CHECK (provider_execution_id IS NULL OR channel = 'call'),
    CHECK (provider_message_id IS NULL OR channel = 'message')
);
CREATE INDEX reminders_appointment ON reminders (clinic_id, appointment_id, appointment_version);
CREATE INDEX reminders_budget ON reminders (clinic_id, contact_day);
CREATE INDEX reminders_status ON reminders (clinic_id, status, updated_at);

-- Contact jobs (notification.send for messages, reminder.call for calls) carry
-- {reminder_id} and may be several per appointment version (a confirmation and a reminder
-- message are both notification.send). The per-(appointment, kind, version) uniqueness
-- from migration 007 therefore excludes them; one job per reminder row holds instead.
ALTER TABLE outbox DROP CONSTRAINT outbox_appointment_version_key;
CREATE UNIQUE INDEX outbox_appointment_version_key ON outbox (appointment_id, kind, appointment_version)
    WHERE kind NOT IN ('notification.send', 'reminder.call');
CREATE UNIQUE INDEX outbox_reminder_job ON outbox (clinic_id, (payload->>'reminder_id'))
    WHERE kind IN ('notification.send', 'reminder.call');
