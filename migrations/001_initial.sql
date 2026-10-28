CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE clinics (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    timezone text NOT NULL DEFAULT 'Asia/Kolkata'
);
CREATE TABLE doctors (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    name text NOT NULL,
    UNIQUE (clinic_id, id)
);
CREATE TABLE services (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    doctor_id uuid NOT NULL,
    name text NOT NULL,
    duration_minutes integer NOT NULL CHECK (duration_minutes BETWEEN 5 AND 240),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors(clinic_id, id),
    UNIQUE (clinic_id, doctor_id, id)
);
CREATE TABLE working_hours (
    clinic_id uuid NOT NULL,
    doctor_id uuid NOT NULL,
    weekday integer NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    opens time NOT NULL,
    closes time NOT NULL,
    CHECK (opens < closes),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors(clinic_id, id),
    PRIMARY KEY (clinic_id, doctor_id, weekday, opens)
);
CREATE TABLE appointments (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL,
    doctor_id uuid NOT NULL,
    service_id uuid NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('pending_sync', 'confirmed', 'cancelled', 'needs_review')),
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (starts_at < ends_at),
    FOREIGN KEY (clinic_id, doctor_id, service_id) REFERENCES services(clinic_id, doctor_id, id),
    UNIQUE (clinic_id, idempotency_key),
    UNIQUE (clinic_id, id),
    CONSTRAINT no_overlapping_appointments EXCLUDE USING gist (
        clinic_id WITH =, doctor_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (status IN ('pending_sync', 'confirmed', 'needs_review'))
);
CREATE TABLE outbox (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    appointment_id uuid NOT NULL,
    FOREIGN KEY (clinic_id, appointment_id) REFERENCES appointments(clinic_id, id),
    kind text NOT NULL CHECK (kind IN ('calendar.create', 'calendar.cancel', 'notification.send', 'reminder.schedule', 'reminder.stop')),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'done', 'dead')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (appointment_id, kind)
);
CREATE INDEX outbox_pending ON outbox (available_at) WHERE status = 'pending';
