"""Application-level encryption for stored secrets (OAuth refresh tokens, PKCE verifiers).

AES-256-GCM with a random 96-bit nonce per message. The blob is nonce || ciphertext+tag.
Associated data (AAD) binds a ciphertext to its row context, e.g.
b"calendar_account:" + clinic_id + account_id, so a blob copied into another row or another
clinic fails to decrypt. Keys come from a keyring setting "kid1:<base64 32 bytes>,kid2:...";
the first entry is the active (encrypting) key and the rest stay available for decryption,
which is how keys rotate: prepend a new key, re-encrypt at leisure, then drop the old one.

Errors never include key material, plaintext or ciphertext.
"""

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class CryptoError(Exception):
    """Configuration or decryption failure. The message is a fixed short description."""


def _decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise CryptoError("key is not valid base64") from None
    if len(key) != KEY_BYTES:
        raise CryptoError("key must be 32 bytes")
    return key


class Keyring:
    def __init__(self, keys: list[tuple[str, bytes]]):
        if not keys:
            raise CryptoError("keyring is empty")
        seen = {}
        for key_id, key in keys:
            if not KEY_ID.match(key_id):
                raise CryptoError("invalid key id")
            if len(key) != KEY_BYTES:
                raise CryptoError("key must be 32 bytes")
            if key_id in seen:
                raise CryptoError("duplicate key id")
            seen[key_id] = key
        self.active_id = keys[0][0]
        self._keys = seen

    @classmethod
    def from_setting(cls, value: str) -> "Keyring":
        entries = []
        for item in value.split(","):
            if not item.strip():
                continue
            key_id, sep, encoded = item.strip().partition(":")
            if not sep:
                raise CryptoError("keyring entries must be kid:base64key")
            entries.append((key_id, _decode_key(encoded)))
        return cls(entries)

    def __repr__(self):
        return f"Keyring(active={self.active_id!r}, keys={len(self._keys)})"

    def encrypt(self, plaintext: bytes, aad: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(NONCE_BYTES)
        blob = nonce + AESGCM(self._keys[self.active_id]).encrypt(nonce, plaintext, aad)
        return self.active_id, blob

    def decrypt(self, key_id: str, blob: bytes, aad: bytes) -> bytes:
        key = self._keys.get(key_id)
        if key is None:
            raise CryptoError("unknown key id")
        blob = bytes(blob)
        if len(blob) < NONCE_BYTES + 16:
            raise CryptoError("decryption failed")
        try:
            return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
        except InvalidTag:
            raise CryptoError("decryption failed") from None


def generate_key() -> str:
    """A new random 32-byte key, base64-encoded (for TOKEN_ENCRYPTION_KEYS or PHONE_HASH_KEY)."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()


def decode_hash_key(value: str) -> bytes:
    return _decode_key(value)


def phone_hash(key: bytes, e164: str) -> bytes:
    """Keyed HMAC-SHA256 of an E.164 number, for lookups without storing the number in clear."""
    if len(key) != KEY_BYTES:
        raise CryptoError("key must be 32 bytes")
    if not re.fullmatch(r"\+[1-9]\d{6,14}", e164):
        raise ValueError("phone number must be E.164")
    return hmac.new(key, e164.encode(), hashlib.sha256).digest()
