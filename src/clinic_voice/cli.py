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
                credential_id, token = credentials.issue(conn, args.clinic, args.label)
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


def build_ports(calendar):
    if calendar == "fake":
        from clinic_voice.providers.calendar import FakeCalendar
        from clinic_voice.worker import Ports

        print(
            "WARNING: --calendar fake uses an in-process FakeCalendar. Events are not written to"
            " any real calendar; development only.",
            file=sys.stderr,
        )
        return Ports(calendar=FakeCalendar())
    raise SystemExit(
        "No calendar provider configured; the worker will not start. Use --calendar fake for"
        " local development only (a live provider arrives with the Google adapter)."
    )


def worker_command(url, args):
    from psycopg import Error as DatabaseError

    from clinic_voice.worker import run_once

    ports = build_ports(args.calendar)
    worker_id = args.worker_id or f"{socket.gethostname()}:{os.getpid()}"
    if args.once:
        count = run_once(url, worker_id, ports)
        print(f"processed {count} job(s)")
        return
    stop = threading.Event()

    def request_stop(signum, frame):
        stop.set()  # The current batch finishes; no new batch starts.

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    while not stop.is_set():
        try:
            count = run_once(url, worker_id, ports)
        except DatabaseError as error:
            logging.getLogger("clinic_voice.worker").error(
                "worker batch failed error=%s", type(error).__name__
            )
            count = 0
        if count == 0:
            stop.wait(args.interval)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="clinic-voice")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("seed-demo")
    commands.add_parser("export-openapi")
    credential = commands.add_parser("credential", help="Manage tenant tool credentials")
    actions = credential.add_subparsers(dest="action", required=True)
    issue = actions.add_parser("issue")
    issue.add_argument("--clinic", type=UUID, required=True)
    issue.add_argument("--label", required=True)
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
    worker.add_argument("--calendar", choices=["fake"], help="Calendar provider (fake: dev only)")
    worker.add_argument("--worker-id", help="Lease owner name (default host:pid)")
    worker.add_argument("--interval", type=float, default=2.0, help="Idle poll seconds")
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
    url = get_settings().database_url.get_secret_value()
    if args.command == "migrate":
        migrate(url, Path("migrations"))
    elif args.command == "seed-demo":
        seed(url)
    elif args.command == "staff":
        staff_command(url, args)
    elif args.command == "worker":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        worker_command(url, args)
    else:
        credential_command(url, args)
