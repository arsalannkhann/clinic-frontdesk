"""`clinic-voice seed-platform`: the synthetic Aster Dental Studio demo (plan "Seed data").

Everything here is fictional: the clinic, its doctors and copy, the patient names, and the
phone numbers, which all come from the reserved range +91 90000 0xxxx. The clinic row is
marked seed_tag='platform-seed'; the command refuses to run while the database holds any
clinic without that mark, so it can never write into a real clinic's data.

Idempotent. Clinic profile, doctors, catalogue, hours and exceptions are upserted on every
run (deterministic ids). Appointments and callback requests are generated only while the
clinic has no appointments, so a re-run never adds or duplicates them.

Appointments (about 70, over the past 7 and next 14 clinic-local days) respect the same
invariants as live bookings:
- future ones are booked through service.reserve (offered slots only, doctor row lock,
  exclusion constraint, appointment + calendar.create job in one commit) and confirmed by
  running those jobs through the outbox with the ledger calendar, exactly as the worker
  tick does; a few are then cancelled through appointments.cancel_locked (calendar.cancel
  job, also run);
- past ones cannot be reserved (slots are future only), so they are inserted under the
  doctor row lock at free starts of the doctor's hours (domain.candidate_slots over the
  ledger's busy time); the exclusion constraint still guards them. Their state is what the
  worker would have left: confirmed with the ledger event recorded and a done
  calendar.create job, or cancelled at version 2 with a done calendar.cancel job.
Each appointment gets its own patient (patients.create_from_form) with contact consent.
No staff user is created (an owner runs `clinic-voice staff create-owner`), and
outbound_enabled stays off, so nothing is ever sent to the fictional numbers.
"""

import random
from datetime import date, datetime, time, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from clinic_voice import appointments, patients, service
from clinic_voice.crypto import CryptoError, Keyring, decode_hash_key
from clinic_voice.db import connect
from clinic_voice.domain import candidate_slots, utc_now
from clinic_voice.locks import lock_doctors
from clinic_voice.outbox import Ports, run_batch
from clinic_voice.providers.calendar import event_id_for
from clinic_voice.providers.ledger import LedgerResolver, ledger_calendar_id
from clinic_voice.public import store_callback
from clinic_voice.schedule_exceptions import create_exception
from clinic_voice.schemas import ReservationRequest

SEED_TAG = "platform-seed"
NAMESPACE = uuid5(NAMESPACE_URL, "https://clinic-voice.invalid/seed-platform")
ZONE_NAME = "Asia/Kolkata"
PAST_TARGET = 24
FUTURE_TARGET = 46
FUTURE_CANCELLED = 4


class SeedRefused(Exception):
    pass


def seed_id(*parts: str) -> UUID:
    return uuid5(NAMESPACE, ":".join(parts))


CLINIC_ID = seed_id("clinic", "aster-dental")

CLINIC = {
    "name": "Aster Dental Studio",
    "public_slug": "aster-dental",
    "public_tagline": "Unhurried, evidence-based dental care in the heart of Indiranagar.",
    "public_address": (
        "2nd Floor, 418 Aster Court, 12th Main Road, HAL 2nd Stage, Indiranagar,"
        " Bengaluru, Karnataka 560038"
    ),
    "public_map_url": "https://www.openstreetmap.org/search?query=Indiranagar%2C%20Bengaluru",
    "public_phone_display": "+91 90000 01000",
    "public_email": "hello@aster-dental.example",
    "public_hours_note": (
        "Monday to Saturday, 10 am to 8 pm, with a quiet lunch hour from 2 to 3 pm."
        " Closed on Sundays and public holidays."
    ),
    "public_about": (
        "Aster Dental Studio is a five-chair practice on 12th Main, run by a small team of"
        " specialists who would rather explain than upsell. Every visit starts with a"
        " conversation and a clear written plan, priced before we begin. We keep"
        " appointments unhurried, sterilise every instrument in-house, and see children,"
        " nervous patients and dental emergencies the same day whenever we can. Book online,"
        " chat with our virtual assistant, or call the front desk."
    ),
}

# key, name, colour, bio, weekly hours {weekday: [(opens, closes), ...]} (0 = Monday)
SPLIT = [("10:00", "14:00"), ("15:00", "20:00")]
DOCTORS = [
    (
        "meera",
        "Dr. Meera Iyer",
        "#2f6f8f",
        "Endodontist (BDS, MDS). Meera looks after root canal treatment and saving teeth"
        " that others might remove, with calm, well-explained care for anxious patients.",
        {0: SPLIT, 1: SPLIT, 2: SPLIT, 3: SPLIT, 4: SPLIT},
    ),
    (
        "arjun",
        "Dr. Arjun Menon",
        "#8a5a44",
        "Prosthodontist (BDS, MDS). Arjun plans crowns, bridges and implant restorations,"
        " and leads the studio's cosmetic and whitening work.",
        {0: SPLIT, 2: SPLIT, 3: SPLIT, 4: SPLIT, 5: [("10:00", "16:00")]},
    ),
    (
        "kavya",
        "Dr. Kavya Reddy",
        "#6b5ca5",
        "Orthodontist (BDS, MDS). Kavya treats crowding and bite problems with clear"
        " aligners and braces, for teenagers and adults alike.",
        {1: SPLIT, 2: SPLIT, 4: SPLIT, 5: [("10:00", "16:00")]},
    ),
    (
        "rohan",
        "Dr. Rohan Shetty",
        "#a14a4a",
        "Oral and maxillofacial surgeon (BDS, MDS). Rohan handles extractions, wisdom teeth"
        " and implant placement, and sees most of our same-day emergencies.",
        {0: SPLIT, 1: SPLIT, 3: SPLIT, 5: [("10:00", "16:00")]},
    ),
    (
        "nisha",
        "Dr. Nisha Kulkarni",
        "#3f8f5e",
        "Paediatric dentist (BDS, MDS). Nisha makes first visits fun and fear-free, with"
        " preventive care, sealants and gentle fillings for children.",
        {0: SPLIT, 1: SPLIT, 2: SPLIT, 3: SPLIT, 5: [("10:00", "16:00")]},
    ),
]

# key, name, minutes, buffer, colour, description, doctor keys
SERVICES = [
    (
        "checkup",
        "Check-up and cleaning",
        30,
        10,
        "#4f8a8b",
        "A full examination, scaling and polishing, and a written plan if anything needs"
        " attention.",
        ("meera", "arjun", "kavya"),
    ),
    (
        "whitening",
        "Teeth whitening",
        60,
        15,
        "#c9a227",
        "In-studio whitening with a shade check before and after; we will tell you honestly"
        " what to expect.",
        ("arjun",),
    ),
    (
        "filling",
        "Tooth-coloured filling",
        45,
        10,
        "#5b7db1",
        "Composite fillings matched to your tooth shade, for cavities and small chips.",
        ("meera", "arjun", "nisha"),
    ),
    (
        "rootcanal",
        "Root canal consultation",
        30,
        10,
        "#8c4a6b",
        "An assessment with X-rays to see whether a tooth can be saved, and what treatment"
        " would involve.",
        ("meera",),
    ),
    (
        "extraction",
        "Tooth extraction",
        45,
        15,
        "#a65d3f",
        "Simple and surgical extractions, including wisdom teeth, with clear aftercare advice.",
        ("rohan",),
    ),
    (
        "implant",
        "Implant consultation",
        30,
        10,
        "#6a7f4f",
        "A planning visit for replacing missing teeth with implants, including options and costs.",
        ("arjun", "rohan"),
    ),
    (
        "aligner",
        "Clear aligner consultation",
        30,
        10,
        "#7a62b8",
        "A bite and smile assessment to see whether clear aligners suit you.",
        ("kavya",),
    ),
    (
        "kids",
        "Kids' check-up",
        30,
        10,
        "#e07a5f",
        "A relaxed check-up for children, with cleaning, fluoride and tips for parents.",
        ("nisha",),
    ),
    (
        "emergency",
        "Emergency pain visit",
        30,
        15,
        "#c0392b",
        "Same-day relief for toothache, swelling or a broken tooth; we treat the pain first.",
        ("meera", "rohan"),
    ),
    (
        "crown",
        "Crown fitting",
        60,
        15,
        "#9c6b30",
        "Fitting and adjustment of a crown prepared at an earlier visit.",
        ("arjun",),
    ),
]

FIRST_NAMES = [
    "Aarav", "Diya", "Vihaan", "Ananya", "Ishaan", "Saanvi", "Kabir", "Myra", "Reyansh",
    "Aadhya", "Arnav", "Kiara", "Dhruv", "Anika", "Atharv", "Pari", "Rudra", "Navya",
    "Shaurya", "Ira", "Vivaan", "Tara", "Advik", "Riya", "Krish", "Meher", "Ayaan", "Sara",
    "Pranav", "Nitya", "Siddharth", "Lakshmi", "Varun", "Pooja", "Karthik", "Divya",
]  # fmt: skip
LAST_NAMES = [
    "Sharma", "Nair", "Rao", "Gowda", "Pillai", "Hegde", "Joshi", "Bhat", "Das", "Patil",
    "Menon", "Iyengar", "Kamath", "Shenoy", "Chatterjee", "Verma", "Naidu", "Prasad",
]  # fmt: skip
# channel -> consent source (migration 018)
CHANNELS = {"voice": "voice_call", "web": "web", "chat": "chat", "staff": "staff"}
CHANNEL_WEIGHTS = (("voice", 35), ("web", 35), ("chat", 15), ("staff", 15))
CALLBACKS = [
    ("web", "Asked about the cost of clear aligners for a teenager.", "open"),
    ("voice", "Wants to move a check-up to a Saturday.", "open"),
    ("chat", "Question about sensitivity after whitening.", "done"),
    ("web", None, "open"),
]


def _crypto(settings) -> tuple[Keyring, bytes]:
    keys, hash_key = settings.token_encryption_keys, settings.phone_hash_key
    if not keys or not keys.get_secret_value() or not hash_key or not hash_key.get_secret_value():
        raise SeedRefused("Set TOKEN_ENCRYPTION_KEYS and PHONE_HASH_KEY before seeding")
    try:
        return Keyring.from_setting(keys.get_secret_value()), decode_hash_key(
            hash_key.get_secret_value()
        )
    except CryptoError:
        raise SeedRefused("TOKEN_ENCRYPTION_KEYS or PHONE_HASH_KEY is invalid") from None


def doctor_id(key: str) -> UUID:
    return seed_id("doctor", key)


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _upsert_catalogue(conn) -> None:
    columns = ", ".join(CLINIC)
    updates = ", ".join(f"{column}=EXCLUDED.{column}" for column in CLINIC)
    conn.execute(
        f"""INSERT INTO clinics (id, timezone, seed_tag, public_enabled, {columns})
        VALUES (%s, %s, %s, true, {", ".join(["%s"] * len(CLINIC))})
        ON CONFLICT (id) DO UPDATE SET timezone=EXCLUDED.timezone, public_enabled=true,
        {updates}""",  # noqa: S608
        (CLINIC_ID, ZONE_NAME, SEED_TAG, *CLINIC.values()),
    )
    for key, name, colour, bio, _ in DOCTORS:
        did = doctor_id(key)
        conn.execute(
            """INSERT INTO doctors (id, clinic_id, name, active, booking_calendar_id,
            public_bio, colour) VALUES (%s, %s, %s, true, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, active=true,
            booking_calendar_id=EXCLUDED.booking_calendar_id, public_bio=EXCLUDED.public_bio,
            colour=EXCLUDED.colour""",
            (did, CLINIC_ID, name, ledger_calendar_id(did), bio, colour),
        )
    lock_doctors(conn, CLINIC_ID)  # Hours change what can be booked (locks.py, level 3).
    for key, *_, hours in DOCTORS:
        conn.execute(
            "DELETE FROM working_hours WHERE clinic_id=%s AND doctor_id=%s",
            (CLINIC_ID, doctor_id(key)),
        )
        for weekday, windows in hours.items():
            for opens, closes in windows:
                conn.execute(
                    "INSERT INTO working_hours VALUES (%s, %s, %s, %s, %s)",
                    (CLINIC_ID, doctor_id(key), weekday, _clock(opens), _clock(closes)),
                )
    for key, name, minutes, buffer, colour, description, doctors in SERVICES:
        for doctor in doctors:
            conn.execute(
                """INSERT INTO services (id, clinic_id, doctor_id, name, duration_minutes,
                buffer_after_minutes, active, catalog_id, description, public, colour)
                VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s, true, %s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,
                duration_minutes=EXCLUDED.duration_minutes,
                buffer_after_minutes=EXCLUDED.buffer_after_minutes, active=true,
                catalog_id=EXCLUDED.catalog_id, description=EXCLUDED.description,
                public=true, colour=EXCLUDED.colour""",
                (
                    seed_id("service", key, doctor),
                    CLINIC_ID,
                    doctor_id(doctor),
                    name,
                    minutes,
                    buffer,
                    seed_id("catalog", key),
                    description,
                    colour,
                ),
            )


def _exceptions(conn, today: date) -> None:
    """Diwali (8 November 2026, clinic-wide) and one doctor's three-day leave starting
    nine days after the first seeding. Created once each (matched by label)."""
    zone = ZoneInfo(ZONE_NAME)
    wanted = [
        (None, "holiday", date(2026, 11, 8), 1, "Diwali"),
        (doctor_id("kavya"), "leave", today + timedelta(days=9), 3, "Annual leave"),
    ]
    staff = {"clinic_id": CLINIC_ID, "id": None}  # The seed acts as no staff user.
    for doctor, kind, first, days, label in wanted:
        exists = conn.execute(
            "SELECT 1 FROM schedule_exceptions WHERE clinic_id=%s AND kind=%s AND label=%s"
            " AND doctor_id IS NOT DISTINCT FROM %s",
            (CLINIC_ID, kind, label, doctor),
        ).fetchone()
        if exists:
            continue
        starts = datetime.combine(first, time(0), zone)
        ends = datetime.combine(first + timedelta(days=days), time(0), zone)
        create_exception(conn, staff, doctor, kind, starts, ends, label)
        conn.commit()


class _People:
    """Synthetic patients: distinct names and numbers from +91 90000 0xxxx."""

    def __init__(self, rng: random.Random):
        self._rng = rng
        self._next = 1001

    def next(self) -> tuple[str, str]:
        name = f"{self._rng.choice(FIRST_NAMES)} {self._rng.choice(LAST_NAMES)}"
        phone = f"+91900000{self._next:04d}"
        self._next += 1
        return name, phone


def _channel(rng) -> str:
    return rng.choices([c for c, _ in CHANNEL_WEIGHTS], [w for _, w in CHANNEL_WEIGHTS])[0]


def _patient(conn, keyring, hash_key, person, channel, starts_at, settings):
    from clinic_voice.crypto import phone_hash

    name, phone = person
    return patients.create_from_form(
        conn,
        keyring,
        CLINIC_ID,
        patients.PatientDetails(
            name=name,
            consents={
                p: True for p in ("booking_confirmation", "reminder_call", "reminder_message")
            },
        ),
        (phone, phone_hash(hash_key, phone)),
        starts_at + timedelta(days=settings.patient_retention_days),
        CHANNELS[channel],
    )


def _doctor_services(conn) -> dict[UUID, list[UUID]]:
    rows = conn.execute(
        "SELECT doctor_id, id FROM services WHERE clinic_id=%s AND active ORDER BY id",
        (CLINIC_ID,),
    ).fetchall()
    result: dict[UUID, list[UUID]] = {}
    for row in rows:
        result.setdefault(row["doctor_id"], []).append(row["id"])
    return result


def _doctor_days(rng, conn, days) -> list[tuple[date, UUID]]:
    hours = {
        (row["doctor_id"], row["weekday"])
        for row in conn.execute(
            "SELECT doctor_id, weekday FROM working_hours WHERE clinic_id=%s", (CLINIC_ID,)
        ).fetchall()
    }
    pairs = [
        (day, doctor_id(key))
        for day in days
        for key, *_ in DOCTORS
        if (doctor_id(key), day.weekday()) in hours
    ]
    rng.shuffle(pairs)
    return pairs


def _past(conn, rng, keyring, hash_key, people, settings, today) -> int:
    """Past appointments at free starts of each doctor's hours (see the module docstring)."""
    zone = ZoneInfo(ZONE_NAME)
    now = utc_now()
    services = _doctor_services(conn)
    created = 0
    pairs = _doctor_days(rng, conn, [today - timedelta(days=n) for n in range(7, 0, -1)])
    for day, doctor in pairs + pairs:  # Second pass adds a second visit to some days.
        if created >= PAST_TARGET:
            break
        service_id = rng.choice(services[doctor])
        lock_doctors(conn, CLINIC_ID, [doctor])
        context = service.get_context(conn, CLINIC_ID, doctor, service_id)
        hours = conn.execute(
            "SELECT opens, closes FROM working_hours WHERE clinic_id=%s AND doctor_id=%s"
            " AND weekday=%s",
            (CLINIC_ID, doctor, day.weekday()),
        ).fetchall()
        day_start = datetime.combine(day, time(0), zone)
        busy = service.busy_intervals(
            conn, CLINIC_ID, doctor, day_start, day_start + timedelta(days=1, hours=2)
        )
        starts = [
            s
            for s in candidate_slots(
                day,
                [(r["opens"], r["closes"]) for r in hours],
                context["duration_minutes"],
                zone,
                busy,
                day_start - timedelta(minutes=1),
                buffer=context["buffer_after_minutes"],
            )
            if s + timedelta(minutes=context["duration_minutes"]) <= now
        ]
        if not starts:
            conn.rollback()
            continue
        starts_at = rng.choice(starts)
        channel = _channel(rng)
        cancelled = rng.random() < 0.15
        appointment_id = uuid4()
        ends_at = starts_at + timedelta(minutes=context["duration_minutes"])
        patient_id = _patient(conn, keyring, hash_key, people.next(), channel, starts_at, settings)
        service._insert_appointment(
            conn,
            (
                appointment_id,
                CLINIC_ID,
                doctor,
                service_id,
                starts_at,
                ends_at,
                ends_at + timedelta(minutes=context["buffer_after_minutes"]),
                f"seed:{appointment_id}",
                "seed",
                None,
                patient_id,
                None,
                None,
                channel,
            ),
        )
        event_id = event_id_for(appointment_id, 1)
        booked_at = starts_at - timedelta(days=rng.randint(1, 10), hours=rng.randint(0, 8))
        conn.execute(
            """UPDATE appointments SET status=%s, version=%s, calendar_event_id=%s,
            calendar_id=%s, created_at=%s, updated_at=%s WHERE clinic_id=%s AND id=%s""",
            (
                "cancelled" if cancelled else "confirmed",
                2 if cancelled else 1,
                event_id,
                ledger_calendar_id(doctor),
                booked_at,
                booked_at,
                CLINIC_ID,
                appointment_id,
            ),
        )
        # service._insert_appointment's caller normally enqueues calendar.create; record it
        # as the worker would have left it.
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,appointment_id,kind,status,attempts,"
            "appointment_version,payload,created_at,available_at)"
            " VALUES (%s,%s,%s,'calendar.create','done',1,1,%s,%s,%s)",
            (
                uuid4(),
                CLINIC_ID,
                appointment_id,
                Jsonb({"calendar_id": ledger_calendar_id(doctor)}),
                booked_at,
                booked_at,
            ),
        )
        if cancelled:
            conn.execute(
                "INSERT INTO outbox (id,clinic_id,appointment_id,kind,status,attempts,"
                "appointment_version,payload) VALUES (%s,%s,%s,'calendar.cancel','done',1,1,%s)",
                (
                    uuid4(),
                    CLINIC_ID,
                    appointment_id,
                    Jsonb({"event_id": event_id, "calendar_id": ledger_calendar_id(doctor)}),
                ),
            )
        conn.commit()
        created += 1
    return created


def _future(url, conn, rng, keyring, hash_key, people, settings, today) -> list[UUID]:
    services = _doctor_services(conn)
    booked: list[UUID] = []
    pairs = _doctor_days(rng, conn, [today + timedelta(days=n) for n in range(0, 15)])
    for day, doctor in pairs + pairs:
        if len(booked) >= FUTURE_TARGET:
            break
        service_id = rng.choice(services[doctor])
        try:
            starts = service.slots(conn, CLINIC_ID, doctor, service_id, day)
        except HTTPException:
            starts = []
        conn.commit()
        if not starts:
            continue
        starts_at = rng.choice(starts)
        channel = _channel(rng)
        person = people.next()

        def attach(conn, starts_at, person=person, channel=channel):
            return _patient(conn, keyring, hash_key, person, channel, starts_at, settings)

        try:
            result = service.reserve(
                url,
                CLINIC_ID,
                ReservationRequest(doctor_id=doctor, service_id=service_id, starts_at=starts_at),
                f"seed:{uuid4()}",
                attach_patient=attach,
                channel=channel,
            )
        except HTTPException:
            continue
        booked.append(result["appointment_id"])
    return booked


def _run_calendar_jobs(conn) -> int:
    """Run the clinic's calendar jobs with the ledger calendar until none is due."""
    ports = Ports(calendar_for=LedgerResolver())
    total = 0
    while True:
        count = run_batch(
            conn, "seed-platform", ports, 20, kinds=("calendar.create", "calendar.cancel")
        )
        total += count
        if count == 0:
            return total


def _cancel_some(conn, rng, booked) -> int:
    count = 0
    for appointment_id in rng.sample(booked, min(FUTURE_CANCELLED, len(booked))):
        row = appointments.lock_appointment(conn, CLINIC_ID, appointment_id)
        if row is not None and row["status"] == "confirmed":
            appointments.cancel_locked(conn, row)
            count += 1
        conn.commit()
    return count


def _callbacks(conn, keyring, hash_key, people) -> int:
    for source, reason, status in CALLBACKS:
        name, phone = people.next()
        request_id, _ = store_callback(
            conn, keyring, hash_key, CLINIC_ID, name, phone, reason, source
        )
        if status == "done":
            conn.execute(
                "UPDATE callback_requests SET status='done', resolved_at=now()"
                " WHERE clinic_id=%s AND id=%s",
                (CLINIC_ID, request_id),
            )
    conn.commit()
    return len(CALLBACKS)


def seed(url, settings) -> dict[str, int]:
    """Seed (or refresh) the demo clinic. Raises SeedRefused when the database holds a
    clinic that is not seed data, or the encryption settings are missing."""
    keyring, hash_key = _crypto(settings)
    zone = ZoneInfo(ZONE_NAME)
    today = utc_now().astimezone(zone).date()
    counts = {"past": 0, "future": 0, "cancelled": 0, "callbacks": 0, "jobs": 0}
    with connect(url) as conn:
        foreign = conn.execute(
            "SELECT count(*) AS n FROM clinics WHERE seed_tag IS DISTINCT FROM %s", (SEED_TAG,)
        ).fetchone()["n"]
        if foreign:
            raise SeedRefused("Refusing to seed: the database holds clinics that are not seed data")
        _upsert_catalogue(conn)
        conn.commit()
        _exceptions(conn, today)
        existing = conn.execute(
            "SELECT count(*) AS n FROM appointments WHERE clinic_id=%s", (CLINIC_ID,)
        ).fetchone()["n"]
        conn.commit()
        if existing == 0:
            # Seeded from the date, so a re-seed on the same day picks the same pattern.
            rng = random.Random(f"aster-dental:{today.isoformat()}")
            people = _People(rng)
            counts["past"] = _past(conn, rng, keyring, hash_key, people, settings, today)
            booked = _future(url, conn, rng, keyring, hash_key, people, settings, today)
            counts["future"] = len(booked)
            counts["jobs"] += _run_calendar_jobs(conn)
            counts["cancelled"] = _cancel_some(conn, rng, booked)
            counts["callbacks"] = _callbacks(conn, keyring, hash_key, people)
        counts["jobs"] += _run_calendar_jobs(conn)
    return counts
