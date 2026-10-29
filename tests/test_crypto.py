import base64
import os

import pytest

from clinic_voice import cli
from clinic_voice.crypto import CryptoError, Keyring, generate_key, phone_hash


def key_setting(*ids):
    return ",".join(f"{kid}:{base64.b64encode(os.urandom(32)).decode()}" for kid in ids)


def test_round_trip_and_fresh_nonces():
    ring = Keyring.from_setting(key_setting("k1"))
    kid, blob = ring.encrypt(b"synthetic-secret", b"ctx")
    kid2, blob2 = ring.encrypt(b"synthetic-secret", b"ctx")
    assert kid == kid2 == "k1"
    assert blob != blob2 and b"synthetic-secret" not in blob
    assert ring.decrypt(kid, blob, b"ctx") == b"synthetic-secret"


def test_wrong_aad_or_tampering_fails():
    ring = Keyring.from_setting(key_setting("k1"))
    kid, blob = ring.encrypt(b"synthetic-secret", b"calendar_account:a")
    with pytest.raises(CryptoError, match="decryption failed"):
        ring.decrypt(kid, blob, b"calendar_account:b")
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(CryptoError):
        ring.decrypt(kid, tampered, b"calendar_account:a")
    with pytest.raises(CryptoError):
        ring.decrypt(kid, blob[:10], b"calendar_account:a")


def test_rotation_keeps_old_key_for_decryption():
    old = key_setting("old")
    kid, blob = Keyring.from_setting(old).encrypt(b"t", b"aad")
    rotated = Keyring.from_setting(key_setting("new") + "," + old)
    assert rotated.active_id == "new"
    assert rotated.encrypt(b"t", b"aad")[0] == "new"
    assert rotated.decrypt(kid, blob, b"aad") == b"t"
    with pytest.raises(CryptoError, match="unknown key id"):
        Keyring.from_setting(key_setting("new")).decrypt(kid, blob, b"aad")


@pytest.mark.parametrize(
    "setting",
    [
        "k1:" + base64.b64encode(b"x" * 16).decode(),
        "k1:" + base64.b64encode(b"x" * 33).decode(),
        "k1:not-base64!!",
        "no-separator",
        "",
        "bad id:" + base64.b64encode(b"x" * 32).decode(),
    ],
)
def test_bad_keyring_rejected(setting):
    with pytest.raises(CryptoError) as error:
        Keyring.from_setting(setting)
    assert "x" * 8 not in str(error.value)


def test_duplicate_key_id_rejected():
    with pytest.raises(CryptoError, match="duplicate"):
        Keyring.from_setting(key_setting("k1", "k1"))


def test_repr_hides_keys():
    setting = key_setting("k1")
    assert setting.split(":")[1] not in repr(Keyring.from_setting(setting))


def test_phone_hash():
    key = os.urandom(32)
    assert phone_hash(key, "+910000000001") == phone_hash(key, "+910000000001")
    assert phone_hash(key, "+910000000001") != phone_hash(os.urandom(32), "+910000000001")
    assert len(phone_hash(key, "+910000000001")) == 32
    with pytest.raises(ValueError):
        phone_hash(key, "910000000001")
    with pytest.raises(CryptoError):
        phone_hash(b"short", "+910000000001")


def test_generate_key_cli(capsys):
    assert len(base64.b64decode(generate_key())) == 32
    cli.main(["generate-key"])
    printed = capsys.readouterr().out.strip()
    assert len(base64.b64decode(printed, validate=True)) == 32
