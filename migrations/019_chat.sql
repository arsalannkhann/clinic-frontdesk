-- Chat assistant, staff-initiated outbound callbacks and synthetic contact guard (P2,
-- docs/plans/2026-10-31-platform.md, ADR 0011).

-- Synthetic contacts. `clinic-voice seed-platform` writes fictional patients and callback
-- requests whose +91 90000 0xxxx numbers may belong to real people. They are marked here,
-- and every outbound path (reminder planning and sending, staff "Call now") refuses a
-- synthetic target with code synthetic_contact, whatever the clinic's outbound settings.
ALTER TABLE patients ADD COLUMN synthetic boolean NOT NULL DEFAULT false;
ALTER TABLE callback_requests ADD COLUMN synthetic boolean NOT NULL DEFAULT false;

-- Outbound callbacks. The caller id (Bolna from_phone_number, E.164) staff callbacks are
-- placed from, configured by the owner (`clinic-voice bolna set-outbound-number`); a call is
-- refused without it. The callback row records who asked for the call and when, and the
-- clinic-local day it counts against in the daily contact budget (shared with reminders).
ALTER TABLE clinics
    ADD COLUMN bolna_outbound_from_number text CHECK (
        bolna_outbound_from_number IS NULL OR bolna_outbound_from_number ~ '^\+[1-9][0-9]{6,14}$'
    );
ALTER TABLE callback_requests
    ADD COLUMN call_requested_at timestamptz,
    ADD COLUMN call_requested_by uuid,
    ADD COLUMN call_contact_day date,
    ADD COLUMN call_outcome text CHECK (call_outcome IS NULL OR call_outcome ~ '^[a-z_]{1,32}$'),
    ADD CONSTRAINT callback_requests_called_by_fkey
        FOREIGN KEY (clinic_id, call_requested_by) REFERENCES staff_users(clinic_id, id),
    ADD CONSTRAINT callback_requests_call_check
        CHECK ((call_requested_at IS NULL) = (call_contact_day IS NULL));
CREATE INDEX callback_requests_contact_day ON callback_requests (clinic_id, call_contact_day)
    WHERE call_contact_day IS NOT NULL;
