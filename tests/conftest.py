import os
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.conninfo import conninfo_to_dict

from clinic_voice.cli import seed
from clinic_voice.config import Settings, get_settings
from clinic_voice.db import connect, migrate

# Tests never read a .env file: a developer's .env holds real secrets (Bolna API key,
# encryption keys) and would leak into tests that expect a setting to be unset. Settings
# reads model_config["env_file"] on every instantiation, so clearing it here, at conftest
# import and before any test module builds settings, covers get_settings() and direct
# Settings(...) calls alike. Tests configure settings through monkeypatch.setenv only.
# Production keeps reading .env from the working directory (config.py).
PRODUCTION_ENV_FILE = Settings.model_config.get("env_file")
Settings.model_config["env_file"] = None
get_settings.cache_clear()


@pytest.fixture
def database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not configured; PostgreSQL required")
    if not conninfo_to_dict(url).get("dbname", "").endswith("_test"):
        pytest.fail("Refusing destructive integration tests against a database not ending _test")
    migrate(url, Path("migrations"))
    with connect(url) as conn:
        conn.execute(
            "TRUNCATE provisioning_events, provisioning, reminders, contact_preferences, webhook_inbox, appointment_changes, call_session_verifications, call_session_verification_failures, consents, patients, call_sessions, oauth_states, calendar_accounts, external_busy, schedule_exceptions, audit_events, staff_sessions, staff_users, tenant_credentials, outbox, appointments, working_hours, services, doctors,"
            " clinics CASCADE"
        )
    seed(url)
    yield url


def activate_lines(conn, *clinics):
    """Mark clinics' provisioning active so the Bolna gateway and the line-gated /v1/tools
    API serve them (T10/T12 gating). Tests that are not about gating start from an open line."""
    for clinic in clinics:
        conn.execute(
            "INSERT INTO provisioning (clinic_id,state,resume_state,agent_id)"
            " VALUES (%s,'active','verified',%s)"
            " ON CONFLICT (clinic_id) DO UPDATE SET state='active'",
            (clinic, str(uuid4())),
        )


@pytest.fixture
def open_line():
    return activate_lines
