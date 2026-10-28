import argparse
from pathlib import Path
from uuid import UUID

from clinic_voice.config import get_settings
from clinic_voice.db import connect, migrate

DOCTOR_ID = UUID("00000000-0000-4000-8000-000000000002")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000003")


def seed(url, clinic_id):
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate", "seed-demo", "export-openapi"])
    args = parser.parse_args()
    if args.command == "export-openapi":
        import json

        from clinic_voice.api import app

        print(json.dumps(app.openapi(), indent=2))
        return
    settings = get_settings()
    url = settings.database_url.get_secret_value()
    if args.command == "migrate":
        migrate(url, Path("migrations"))
    else:
        seed(url, settings.clinic_id)
