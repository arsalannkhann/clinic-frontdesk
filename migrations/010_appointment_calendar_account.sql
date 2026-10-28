-- The calendar account that wrote an appointment's event. Cancels and withdrawals must use
-- the credentials that own the event even after the doctor is remapped to another account.
ALTER TABLE appointments
    ADD COLUMN calendar_account_id uuid,
    ADD CONSTRAINT appointments_calendar_account_fkey
        FOREIGN KEY (clinic_id, calendar_account_id) REFERENCES calendar_accounts(clinic_id, id);

-- A start with ?replace=true revokes the clinic's previously active accounts once the new
-- account is stored.
ALTER TABLE oauth_states ADD COLUMN replace_existing boolean NOT NULL DEFAULT false;

-- calendar.revoke_grant: revoke a revoked account's refresh token at Google, durably, then
-- forget the ciphertext. Per-account job; no appointment.
ALTER TABLE outbox
    DROP CONSTRAINT outbox_kind_check,
    ADD CONSTRAINT outbox_kind_check CHECK (kind IN (
        'calendar.create', 'calendar.cancel', 'calendar.refresh_busy', 'calendar.revoke_grant',
        'notification.send', 'reminder.schedule', 'reminder.stop', 'reminder.call',
        'webhook.process', 'reconcile.execution', 'provision.step'
    ));
