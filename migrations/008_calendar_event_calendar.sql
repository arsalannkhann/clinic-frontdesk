-- The calendar an appointment's event was written to. A doctor's booking calendar can be
-- remapped after the event exists; cancels and withdrawals must target the original calendar.
ALTER TABLE appointments
    ADD COLUMN calendar_id text
        CHECK (calendar_id IS NULL OR char_length(calendar_id) BETWEEN 1 AND 255);
