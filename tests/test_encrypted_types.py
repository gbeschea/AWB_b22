"""Characterization tests for encrypted_types.py — transparent column encryption."""
import crypto
from encrypted_types import EncryptedJSON, EncryptedString


def test_string_bind_encrypts_result_decrypts():
    col = EncryptedString()
    stored = col.process_bind_param("my-token", None)
    assert crypto.is_encrypted(stored)
    assert col.process_result_value(stored, None) == "my-token"


def test_string_none_passthrough():
    col = EncryptedString()
    assert col.process_bind_param(None, None) is None
    assert col.process_result_value(None, None) is None


def test_string_legacy_plaintext_read():
    col = EncryptedString()
    assert col.process_result_value("legacy-plain", None) == "legacy-plain"


def test_json_round_trip():
    col = EncryptedJSON()
    payload = {"username": "u", "password": "p", "n": 3}
    stored = col.process_bind_param(payload, None)
    assert crypto.is_encrypted(stored)
    assert col.process_result_value(stored, None) == payload


def test_json_none_passthrough():
    col = EncryptedJSON()
    assert col.process_bind_param(None, None) is None
    assert col.process_result_value(None, None) is None
