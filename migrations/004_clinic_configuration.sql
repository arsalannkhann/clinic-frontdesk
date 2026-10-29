ALTER TABLE clinics
    ADD COLUMN lead_time_minutes integer NOT NULL DEFAULT 60
        CHECK (lead_time_minutes BETWEEN 0 AND 10080),
    ADD COLUMN horizon_days integer NOT NULL DEFAULT 90
        CHECK (horizon_days BETWEEN 1 AND 365);
ALTER TABLE doctors ADD COLUMN active boolean NOT NULL DEFAULT true;
ALTER TABLE services
    ADD COLUMN active boolean NOT NULL DEFAULT true,
    ADD COLUMN buffer_after_minutes integer NOT NULL DEFAULT 0
        CHECK (buffer_after_minutes BETWEEN 0 AND 120);
