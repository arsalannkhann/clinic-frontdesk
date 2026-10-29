import os
from pathlib import Path
from uuid import uuid4

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
