import os
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from clinic_voice.cli import seed
from clinic_voice.db import connect, migrate


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
            "TRUNCATE reminders, contact_preferences, webhook_inbox, appointment_changes, call_session_verifications, call_session_verification_failures, consents, patients, call_sessions, oauth_states, calendar_accounts, external_busy, schedule_exceptions, audit_events, staff_sessions, staff_users, tenant_credentials, outbox, appointments, working_hours, services, doctors,"
            " clinics CASCADE"
        )
    seed(url)
    yield url
