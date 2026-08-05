"""Unit tests for crypto_utils.py.

Covers: successful encrypt/decrypt round-trip, key normalization/validation,
and every documented failure mode (missing key, wrong-length key, empty
ciphertext, invalid base64, corrupted ciphertext).
"""

from __future__ import annotations

import base64

import pytest

from custom_components.jackery_home_cloud.crypto_utils import (
    _normalize_key,
    decrypt_text,
    encrypt_text,
)
from custom_components.jackery_home_cloud.exceptions import JackeryCryptoError

VALID_KEY = "0123456789abcdef"  # exactly 16 bytes once UTF-8 encoded


class TestNormalizeKey:
    def test_valid_key_is_encoded_and_stripped(self):
        assert _normalize_key(f"  {VALID_KEY}  ") == VALID_KEY.encode("utf-8")

    def test_none_key_raises(self):
        with pytest.raises(JackeryCryptoError, match="missing"):
            _normalize_key(None)

    @pytest.mark.parametrize("bad_key", ["short", "way-too-long-for-a-key-1234567890", ""])
    def test_wrong_length_key_raises(self, bad_key):
        with pytest.raises(JackeryCryptoError, match="16 bytes"):
            _normalize_key(bad_key)


class TestEncryptDecryptRoundTrip:
    @pytest.mark.parametrize(
        "plaintext",
        ["", "hello", "a" * 15, "a" * 16, "a" * 17, "unicode: héllo wörld 🔋"],
    )
    def test_round_trip(self, plaintext):
        encrypted = encrypt_text(plaintext, VALID_KEY)
        assert decrypt_text(encrypted, VALID_KEY) == plaintext

    def test_encrypt_output_is_valid_base64(self):
        encrypted = encrypt_text("some text", VALID_KEY)
        # Should not raise.
        base64.b64decode(encrypted)

    def test_encrypt_with_invalid_key_raises(self):
        with pytest.raises(JackeryCryptoError):
            encrypt_text("some text", "too-short")


class TestDecryptFailureModes:
    def test_empty_ciphertext_raises(self):
        with pytest.raises(JackeryCryptoError, match="empty"):
            decrypt_text("", VALID_KEY)

    def test_invalid_base64_raises(self):
        with pytest.raises(JackeryCryptoError, match="base64"):
            decrypt_text("not-valid-base64!!!", VALID_KEY)

    def test_corrupted_ciphertext_raises(self):
        # Valid base64, but not a real ciphertext for any supported mode:
        # padding/unpadding will fail for ecb/cbc_key/cbc_zero alike.
        garbage = base64.b64encode(b"\x01" * 16).decode("utf-8")
        with pytest.raises(JackeryCryptoError, match="All known Jackery AES decrypt strategies failed"):
            decrypt_text(garbage, VALID_KEY)

    def test_wrong_key_raises_or_mismatches(self):
        encrypted = encrypt_text("secret payload", VALID_KEY)
        other_key = "fedcba9876543210"
        # Wrong key must not silently return the original plaintext.
        try:
            result = decrypt_text(encrypted, other_key)
        except JackeryCryptoError:
            return
        assert result != "secret payload"
