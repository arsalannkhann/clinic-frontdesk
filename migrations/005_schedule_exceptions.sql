-- Holidays, leave and breaks. doctor_id NULL means the exception applies to the whole clinic.
-- label is a short operational note (for example "Diwali"); never patient details.
CREATE TABLE schedule_exceptions (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    doctor_id uuid,
    kind text NOT NULL CHECK (kind IN ('holiday', 'leave', 'break')),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    label text NOT NULL DEFAULT '' CHECK (char_length(label) <= 80),
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (starts_at < ends_at),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors(clinic_id, id),
    FOREIGN KEY (clinic_id, created_by) REFERENCES staff_users(clinic_id, id),
    UNIQUE (clinic_id, id)
);
CREATE INDEX schedule_exceptions_lookup ON schedule_exceptions (clinic_id, doctor_id, starts_at);

-- The service buffer is snapshotted at booking time: the doctor is held until blocks_until.
-- Later edits to buffer_after_minutes do not change existing holds.
ALTER TABLE appointments ADD COLUMN blocks_until timestamptz;
UPDATE appointments SET blocks_until = ends_at;
ALTER TABLE appointments
    ALTER COLUMN blocks_until SET NOT NULL,
    ADD CONSTRAINT appointments_blocks_until_check CHECK (blocks_until >= ends_at);
ALTER TABLE appointments DROP CONSTRAINT no_overlapping_appointments;
ALTER TABLE appointments ADD CONSTRAINT no_overlapping_appointments EXCLUDE USING gist (
    clinic_id WITH =, doctor_id WITH =,
    tstzrange(starts_at, blocks_until, '[)') WITH &&
) WHERE (status IN ('pending_sync', 'confirmed', 'needs_review'));
