import argparse
import logging
import os
import signal
import socket
import sys
import threading
from datetime import timedelta
from getpass import getpass
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from psycopg.errors import ForeignKeyViolation

from clinic_voice import credentials, staff
from clinic_voice.config import get_settings
from clinic_voice.db import connect, migrate

DEMO_CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
DOCTOR_ID = UUID("00000000-0000-4000-8000-000000000002")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000003")


def seed(url, clinic_id=DEMO_CLINIC_ID):
    with connect(url) as conn:
        conn.execute(
            "INSERT INTO clinics (id,name) VALUES (%s,'Synthetic Demo Clinic') ON CONFLICT DO NOTHING",
            (clinic_id,),
        )
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Demo Doctor') ON CONFLICT DO NOTHING",
            (DOCTOR_ID, clinic_id),
        )
        conn.execute(
            "INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes) VALUES (%s,%s,%s,'Consultation',15) ON CONFLICT DO NOTHING",
            (SERVICE_ID, clinic_id, DOCTOR_ID),
        )
        for day in range(7):
            conn.execute(
                "INSERT INTO working_hours VALUES (%s,%s,%s,'09:00','17:00') ON CONFLICT DO NOTHING",
                (clinic_id, DOCTOR_ID, day),
            )


def credential_command(url, args):
    with connect(url) as conn:
        if args.action == "issue":
            try:
                credential_id, token = credentials.issue(
                    conn, args.clinic, args.label, scope=args.scope
                )
            except ForeignKeyViolation:
                raise SystemExit("Clinic not found") from None
        elif args.action == "rotate":
            grace = timedelta(minutes=args.grace_minutes)
            try:
                credential_id, token = credentials.rotate(conn, args.credential, grace)
            except LookupError as error:
                raise SystemExit(str(error)) from None
        elif args.action == "revoke":
            if not credentials.revoke(conn, args.credential):
                raise SystemExit("Credential not found or already revoked")
            print(f"revoked {args.credential}")
            return
        else:
            for row in credentials.list_for_clinic(conn, args.clinic):
                print(" ".join(f"{key}={value}" for key, value in row.items()))
            return
    # Printed exactly once, to stdout only; store it in the gateway's secret manager.
    print(f"credential_id={credential_id}")
    print(f"token={token}")
    if credentials.parse(token, scope="webhook") is not None:
        # The webhook credential is a URL path segment: Bolna's webhook_url is
        # <public base>/v1/webhooks/bolna/<token>. Treat the whole URL as a secret.
        print(f"webhook_path=/v1/webhooks/bolna/{token}")


def read_password():
    # Never accept passwords as arguments: they leak into shell history and process lists.
    if sys.stdin.isatty():
        password = getpass("Password: ")
        if getpass("Repeat password: ") != password:
            raise SystemExit("Passwords do not match")
    else:
        password = sys.stdin.readline().rstrip("\n")
    if not 12 <= len(password) <= 256:
        raise SystemExit("Password must be 12 to 256 characters")
    return password


def staff_command(url, args):
    password = read_password()
    with connect(url) as conn:
        if args.action == "create-owner":
            try:
                user = staff.create_user(conn, args.clinic, args.email, "owner", password)
            except ForeignKeyViolation:
                raise SystemExit("Clinic not found") from None
            except HTTPException as error:
                raise SystemExit(error.detail) from None
            print(f"created owner {user['id']}")
        else:
            try:
                staff.set_password(conn, args.email, password)
            except LookupError as error:
                raise SystemExit(str(error)) from None
            print("password reset; existing sessions revoked")


FAKE_DATABASE_SUFFIXES = ("_test", "_dev")


def fake_calendar_allowed(url, settings):
    """The fake calendar confirms appointments without any real calendar, so it may only run
    against a development or test database, or with APP_ENV=development set explicitly."""
    from psycopg.conninfo import conninfo_to_dict

    name = conninfo_to_dict(url).get("dbname") or ""
    return name.endswith(FAKE_DATABASE_SUFFIXES) or settings.app_env == "development"


def build_ports(calendar, url, settings):
    if calendar == "fake":
        from clinic_voice.outbox import Ports
        from clinic_voice.providers.calendar import FakeCalendar

        if not fake_calendar_allowed(url, settings):
            raise SystemExit(
                "Refusing --calendar fake: it would confirm appointments without a real calendar."
                " Use a database whose name ends in _test or _dev, or set APP_ENV=development."
            )
        print(
            "WARNING: --calendar fake uses an in-process FakeCalendar. Events are not written to"
            " any real calendar; development only.",
            file=sys.stderr,
        )
        return Ports(calendar=FakeCalendar())
    if calendar == "google":
        import httpx
        from fastapi import HTTPException as ConfigError

        from clinic_voice.calendar_accounts import GoogleCalendarResolver
        from clinic_voice.outbox import Ports
        from clinic_voice.providers.google_calendar import DEFAULT_TIMEOUT

        http = httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=False)
        try:
            resolver = GoogleCalendarResolver(settings, http)
        except ConfigError:
            raise SystemExit(
                "Google Calendar is not configured: set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,"
                " GOOGLE_REDIRECT_URI and a valid TOKEN_ENCRYPTION_KEYS."
            ) from None
        return Ports(calendar_for=resolver)
    raise SystemExit(
        "No calendar provider configured; the worker will not start. Use --calendar google, or"
        " --calendar fake for local development only."
    )


def build_bolna(choice, url, settings):
    """The Bolna port for reconciliation (bolna.reconcile), or None when not requested."""
    if choice is None:
        return None
    if choice == "fake":
        from clinic_voice.providers.bolna import FakeBolna

        if not fake_calendar_allowed(url, settings):
            raise SystemExit(
                "Refusing --bolna fake: it would reconcile call sessions against a fake"
                " provider. Use a database whose name ends in _test or _dev, or set"
                " APP_ENV=development."
            )
        print(
            "WARNING: --bolna fake uses an in-process FakeBolna that knows no executions;"
            " development only.",
            file=sys.stderr,
        )
        return FakeBolna()
    key = settings.bolna_api_key
    if key is None or not key.get_secret_value():
        raise SystemExit("Set BOLNA_API_KEY to use --bolna live.")
    import httpx

    from clinic_voice.providers.bolna import DEFAULT_TIMEOUT, BolnaClient

    http = httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=False)
    return BolnaClient(key.get_secret_value(), http, settings.bolna_api_base)


def build_messaging(choice, url, settings):
    """The patient messaging port for notification.send, or None. Only the fake exists: no
    live messaging provider has been chosen (an owner action, docs/ROADMAP.md)."""
    if choice is None:
        return None
    from clinic_voice.providers.messaging import FakeMessaging

    if not fake_calendar_allowed(url, settings):
        raise SystemExit(
            "Refusing --messaging fake: it would mark confirmations and reminders as sent"
            " without sending anything. Use a database whose name ends in _test or _dev, or set"
            " APP_ENV=development."
        )
    print(
        "WARNING: --messaging fake uses an in-process FakeMessaging. No message reaches any"
        " patient; development only.",
        file=sys.stderr,
    )
    return FakeMessaging()


def worker_command(url, args, settings):
    from dataclasses import replace

    from clinic_voice.outbox import run_loop, run_once

    ports = build_ports(args.calendar, url, settings)
    ports = replace(
        ports,
        bolna=build_bolna(args.bolna, url, settings),
        messaging=build_messaging(args.messaging, url, settings),
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    worker_id = args.worker_id or f"{socket.gethostname()}:{os.getpid()}"
    if args.once:
        count = run_once(url, worker_id, ports)
        print(f"processed {count} job(s)")
        return
    stop = threading.Event()

    def request_stop(signum, frame):
        stop.set()  # The current job finishes; unstarted jobs of the batch are released.

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run_loop(url, worker_id, ports, stop, interval=args.interval)


def bolna_command(url, args, settings):
    """Print a compiled agent payload for review. The tool token is never needed or printed:
    the payload is compiled with a redaction placeholder. Nothing is sent to Bolna."""
    import json

    from clinic_voice.bolna import agent_config

    base = args.gateway_url or settings.bolna_gateway_base_url
    if not base:
        raise SystemExit("Set BOLNA_GATEWAY_BASE_URL or pass --gateway-url")
    with connect(url) as conn:
        try:
            clinic = agent_config.load_clinic(conn, args.clinic)
        except LookupError as error:
            raise SystemExit(str(error)) from None
    try:
        payload = agent_config.compile_agent(
            clinic, gateway_base_url=base, tool_token=agent_config.REDACTED
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(agent_config.redact(payload), indent=2, ensure_ascii=False))


def bolna_agent_command(url, args):
    """Record (or clear) the clinic's Bolna agent id. Webhooks naming another agent are
    then refused. Nothing is sent to Bolna."""
    agent_id = None if args.clear else str(args.agent_id)
    with connect(url) as conn:
        row = conn.execute(
            "UPDATE clinics SET bolna_agent_id=%s WHERE id=%s RETURNING id",
            (agent_id, args.clinic),
        ).fetchone()
    if row is None:
        raise SystemExit("Clinic not found")
    print(f"bolna_agent_id={agent_id or ''}")


def retention_command(url, args, settings):
    """Purge expired patient records and old call sessions. Prints counts only."""
    from clinic_voice.retention import purge

    with connect(url) as conn:
        counts = purge(conn, settings.call_session_retention_days, dry_run=args.dry_run)
    line = " ".join(f"{key}={value}" for key, value in counts.items())
    print(f"{line} dry_run={str(args.dry_run).lower()}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="clinic-voice")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("seed-demo")
    commands.add_parser("export-openapi")
    commands.add_parser(
        "generate-key", help="Print a new random base64 32-byte key (nothing is stored)"
    )
    credential = commands.add_parser("credential", help="Manage tenant tool credentials")
    actions = credential.add_subparsers(dest="action", required=True)
    issue = actions.add_parser("issue")
    issue.add_argument("--clinic", type=UUID, required=True)
    issue.add_argument("--label", required=True)
    issue.add_argument(
        "--scope",
        choices=["tools", "webhook"],
        default="tools",
        help="tools: Bolna tool/API bearer token; webhook: Bolna webhook URL credential",
    )
    rotate = actions.add_parser("rotate")
    rotate.add_argument("credential", type=UUID)
    rotate.add_argument("--grace-minutes", type=int, default=60, help="0 to 10080")
    revoke = actions.add_parser("revoke")
    revoke.add_argument("credential", type=UUID)
    listing = actions.add_parser("list")
    listing.add_argument("--clinic", type=UUID, required=True)
    staff_parser = commands.add_parser("staff", help="Bootstrap and recover staff accounts")
    staff_actions = staff_parser.add_subparsers(dest="action", required=True)
    owner = staff_actions.add_parser("create-owner", help="Password is read from stdin/prompt")
    owner.add_argument("--clinic", type=UUID, required=True)
    owner.add_argument("--email", required=True)
    reset = staff_actions.add_parser("set-password", help="Password is read from stdin/prompt")
    reset.add_argument("--email", required=True)
    worker = commands.add_parser("worker", help="Process outbox jobs")
    worker.add_argument("--once", action="store_true", help="Process one batch and exit")
    worker.add_argument(
        "--calendar", choices=["fake", "google"], help="Calendar provider (fake: dev only)"
    )
    worker.add_argument(
        "--bolna",
        choices=["fake", "live"],
        help="Bolna port for call reconciliation and reminder calls (fake: dev only; live:"
        " BOLNA_API_KEY)",
    )
    worker.add_argument(
        "--messaging",
        choices=["fake"],
        help="Messaging port for confirmations and reminder messages (fake: dev only; no live"
        " provider exists)",
    )
    worker.add_argument("--worker-id", help="Lease owner name (default host:pid)")
    worker.add_argument("--interval", type=float, default=2.0, help="Idle poll seconds")
    bolna = commands.add_parser("bolna", help="Bolna agent configuration (offline only)")
    bolna_actions = bolna.add_subparsers(dest="action", required=True)
    compile_agent = bolna_actions.add_parser(
        "compile-agent", help="Print the agent payload with the tool token redacted"
    )
    compile_agent.add_argument("--clinic", type=UUID, required=True)
    compile_agent.add_argument("--gateway-url", help="Public base URL (default from settings)")
    set_agent = bolna_actions.add_parser(
        "set-agent", help="Record the clinic's Bolna agent id (webhook cross-check)"
    )
    set_agent.add_argument("--clinic", type=UUID, required=True)
    agent_choice = set_agent.add_mutually_exclusive_group(required=True)
    agent_choice.add_argument("--agent-id", type=UUID)
    agent_choice.add_argument("--clear", action="store_true")
    retention = commands.add_parser("retention", help="Data retention (operator-run only)")
    retention_actions = retention.add_subparsers(dest="action", required=True)
    purge = retention_actions.add_parser(
        "purge", help="Delete expired patient records and old call sessions"
    )
    purge.add_argument("--dry-run", action="store_true", help="Count only; change nothing")
    args = parser.parse_args(argv)
    if args.command == "worker" and not 0.1 <= args.interval <= 300:
        parser.error("--interval must be between 0.1 and 300 seconds")
    if (
        args.command == "worker"
        and args.worker_id is not None
        and not 1 <= len(args.worker_id) <= 128
    ):
        parser.error("--worker-id must be 1 to 128 characters")
    if getattr(args, "grace_minutes", 0) not in range(0, 10081):
        parser.error("--grace-minutes must be between 0 and 10080")
    if args.command == "export-openapi":
        import json

        from clinic_voice.api import app

        print(json.dumps(app.openapi(), indent=2))
        return
    if args.command == "generate-key":
        from clinic_voice.crypto import generate_key

        print(generate_key())
        return
    settings = get_settings()
    url = settings.database_url.get_secret_value()
    if args.command == "migrate":
        migrate(url, Path("migrations"))
    elif args.command == "seed-demo":
        seed(url)
    elif args.command == "staff":
        staff_command(url, args)
    elif args.command == "worker":
        worker_command(url, args, settings)
    elif args.command == "bolna" and args.action == "set-agent":
        bolna_agent_command(url, args)
    elif args.command == "bolna":
        bolna_command(url, args, settings)
    elif args.command == "retention":
        retention_command(url, args, settings)
    else:
        credential_command(url, args)
