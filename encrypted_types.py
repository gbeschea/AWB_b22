# encrypted_types.py
"""
SQLAlchemy column types that transparently encrypt secret values at rest.

The encrypt-on-WRITE / decrypt-on-READ boundary is centralized here at the column
type, so every existing read/write site (crud/*, routes/*, services/couriers/*)
keeps using `store.access_token` / `account.credentials` exactly as before.

- EncryptedString: wraps TEXT. Encrypt on write, decrypt on read. Used for the
  Store.access_token / Store.shared_secret String columns.
- EncryptedJSON: wraps JSONB. The whole JSON document is serialized, encrypted and
  stored as a JSON *string scalar* inside the JSONB column. On read it is decrypted
  and parsed back into the original Python object (dict/list).

Both pass LEGACY PLAINTEXT through unchanged on read (crypto.is_encrypted guards it),
so the running app is not broken before/during the migration.
"""
from __future__ import annotations

import json

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Text, TypeDecorator

import crypto


class EncryptedString(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return crypto.encrypt(value if isinstance(value, str) else str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        # crypto.decrypt is a no-op passthrough for legacy plaintext.
        return crypto.decrypt(value)


class EncryptedJSON(TypeDecorator):
    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # Stored as an encrypted JSON string scalar; JSONB serializes the str for us.
        return crypto.encrypt(json.dumps(value, ensure_ascii=False))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if crypto.is_encrypted(value):
            return json.loads(crypto.decrypt(value))
        # Legacy plaintext row: JSONB already gave us the dict/list. Return as-is.
        return value
