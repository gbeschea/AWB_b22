"""Characterization tests for crypto.py — the at-rest encryption boundary."""
import pytest

import crypto


def test_round_trip():
    assert crypto.decrypt(crypto.encrypt("hello world")) == "hello world"


def test_unicode_round_trip():
    s = "Str. Ș. ăîâț — 100% RON"
    assert crypto.decrypt(crypto.encrypt(s)) == s


def test_ciphertext_is_prefixed_and_detected():
    ct = crypto.encrypt("secret")
    assert ct.startswith(crypto.ENC_PREFIX)
    assert crypto.is_encrypted(ct) is True
    assert crypto.is_encrypted("secret") is False


def test_legacy_plaintext_passes_through():
    # Rows written before encryption existed must still read.
    assert crypto.decrypt("legacy-plaintext-token") == "legacy-plaintext-token"


def test_none_passthrough():
    assert crypto.decrypt(None) is None


def test_nonce_is_random_same_input_differs():
    assert crypto.encrypt("x") != crypto.encrypt("x")


def test_tampered_ciphertext_raises():
    ct = crypto.encrypt("secret")
    tampered = ct[:-4] + ("AAAA" if ct[-4:] != "AAAA" else "BBBB")
    with pytest.raises(Exception):
        crypto.decrypt(tampered)
