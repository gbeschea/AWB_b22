# crypto.py
"""
Application-level encryption for secrets at rest.

Protects the courier credentials (courier_accounts.credentials) and the Shopify
tokens (stores.access_token, stores.shared_secret), which were previously stored
in PLAINTEXT.

Scheme: AES-256-GCM, 32-byte key taken from the env var AWB_B2_ENC_KEY (base64),
with a fresh random 12-byte nonce per value. The stored form is tagged:

    enc:v1:<base64(nonce || ciphertext || gcm_tag)>

Backward compatibility (CRITICAL): decrypt() transparently passes through any value
that does NOT carry the `enc:v1:` prefix, so LEGACY PLAINTEXT rows keep working
before, during and after the one-time migration. Rows are encrypted lazily on write
and/or all at once by the Alembic migration.

Fail-loud: if AWB_B2_ENC_KEY is missing or malformed the module raises at import time.
Because models.py imports this module, the app refuses to START instead of silently
reading/writing corrupt secrets.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENC_PREFIX = "enc:v1:"
_ENV_KEY = "AWB_B2_ENC_KEY"
_NONCE_LEN = 12  # 96-bit nonce, recommended for AES-GCM


def _load_key() -> bytes:
    raw = os.environ.get(_ENV_KEY)
    if not raw:
        raise RuntimeError(
            f"{_ENV_KEY} is not set. AWB_b2 refuses to start without the at-rest "
            f"encryption key. Generate one with:\n"
            f'  python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{_ENV_KEY} must be valid base64: {exc}") from exc
    if len(key) != 32:
        raise RuntimeError(
            f"{_ENV_KEY} must decode to exactly 32 bytes (AES-256); got {len(key)} bytes."
        )
    return key


_KEY = _load_key()
_AESGCM = AESGCM(_KEY)


def is_encrypted(value) -> bool:
    """True only for values already produced by encrypt() (str with the enc:v1: prefix)."""
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def encrypt(plaintext: str) -> str:
    """Encrypt a string into the tagged enc:v1: form. None passes through unchanged."""
    if plaintext is None:
        return plaintext
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    nonce = os.urandom(_NONCE_LEN)
    # AESGCM.encrypt returns ciphertext with the 16-byte GCM tag appended.
    ct = _AESGCM.encrypt(nonce, plaintext.encode("utf-8"), None)
    return ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt(value):
    """
    Decrypt an enc:v1: value back to plaintext.

    LEGACY / passthrough: anything that is not a str with the enc:v1: prefix
    (plaintext strings, None, dicts) is returned unchanged.
    """
    if not is_encrypted(value):
        return value
    blob = base64.b64decode(value[len(ENC_PREFIX):])
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return _AESGCM.decrypt(nonce, ct, None).decode("utf-8")
