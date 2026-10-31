-- Provisioning sets the agent's webhook_url (POST /v1/webhooks/bolna/{token}).
--
-- The agent step issues a 'webhook'-scope credential (label provisioning:webhook) alongside
-- the agent's 'bolna' tool credential, in the same transaction, and sends
-- <BOLNA_GATEWAY_BASE_URL>/v1/webhooks/bolna/<token> as agent_config.webhook_url. Both ids
-- follow the tool credential's lifecycle: attempt_webhook_credential_id names the one issued
-- for an agent write whose outcome is still unknown (the agent may hold it);
-- webhook_credential_id the one the agent is known to hold (set when Bolna acknowledged the
-- write). No secret is stored; the plaintext lived only in worker memory.
--
-- Rows provisioned before this migration keep webhook_credential_id NULL: their webhook URL
-- was set by hand and is not managed here (verify does not check it) until the agent step
-- runs again. Webhook credentials issued by an operator (other labels) are never touched.
ALTER TABLE provisioning
    ADD COLUMN webhook_credential_id uuid,
    ADD COLUMN attempt_webhook_credential_id uuid,
    ADD CONSTRAINT provisioning_webhook_credential_fkey
        FOREIGN KEY (clinic_id, webhook_credential_id) REFERENCES tenant_credentials(clinic_id, id),
    ADD CONSTRAINT provisioning_attempt_webhook_credential_fkey
        FOREIGN KEY (clinic_id, attempt_webhook_credential_id)
        REFERENCES tenant_credentials(clinic_id, id);
