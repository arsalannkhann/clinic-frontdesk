# Tests never read a .env file: a developer's .env holds real secrets (Bolna API key,
# encryption keys) and would leak into tests that expect a setting to be unset. Settings
# reads model_config["env_file"] on every instantiation, so clearing it here covers
# get_settings() and direct Settings(...) calls alike. It runs before any other clinic_voice
# import (and so before any test module is collected), so not even an import-time Settings
# could read .env; clinic_voice.config itself builds none at import. Tests configure
# settings through monkeypatch.setenv only. Production keeps reading .env from the working
# directory (config.ENV_FILE).
from clinic_voice.config import Settings, get_settings

Settings.model_config["env_file"] = None
get_settings.cache_clear()

import os  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from psycopg.conninfo import conninfo_to_dict  # noqa: E402

from clinic_voice.cli import seed  # noqa: E402
from clinic_voice.db import connect, migrate  # noqa: E402


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
            "TRUNCATE chat_proposals, chat_messages, chat_sessions, public_rate_limits, callback_requests, provisioning_events, provisioning, reminders, contact_preferences, webhook_inbox, appointment_changes, call_session_verifications, call_session_verification_failures, consents, patients, call_sessions, oauth_states, calendar_accounts, external_busy, schedule_exceptions, audit_events, staff_sessions, staff_users, tenant_credentials, outbox, appointments, working_hours, services, doctors,"
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
