"""Utility helpers for Jackery-specific text transformation - encode and decode.
"""

from __future__ import annotations

import base64
import logging
from typing import Iterable

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .exceptions import JackeryCryptoError

_LOGGER = logging.getLogger(__name__)
_BLOCK_SIZE_BYTES = 16


def _normalize_key(crypto_key: str) -> bytes:
    """Normalize and validate the integration-wide crypto key text."""
    if crypto_key is None:
        raise JackeryCryptoError("Crypto key is missing")

    key = crypto_key.strip().encode("utf-8")
    if len(key) != _BLOCK_SIZE_BYTES:
        raise JackeryCryptoError(
            f"Crypto key must be exactly {_BLOCK_SIZE_BYTES} bytes after UTF-8 encoding; "
            f"got {len(key)} bytes"
        )
    return key


def _decrypt_modes(ciphertext: bytes, key: bytes, mode_name: str) -> str:
    """Attempt to decode the ciphertext with one concrete mode."""
    if mode_name == "cbc_key":
        cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    elif mode_name == "cbc_zero":
        cipher = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * _BLOCK_SIZE_BYTES))
    elif mode_name == "ecb":
        cipher = Cipher(algorithms.AES(key), modes.ECB())
    else:
        raise JackeryCryptoError(f"Unsupported crypto mode hint: {mode_name}")

    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    plaintext_bytes = unpadder.update(padded) + unpadder.finalize()
    return plaintext_bytes.decode("utf-8")


def decrypt_text(encrypted_b64: str, crypto_key: str) -> str:
    """Decrypt text and return its UTF-8 plaintext."""
    if not encrypted_b64:
        raise JackeryCryptoError("Encrypted credential is empty")

    key = _normalize_key(crypto_key)
    _LOGGER.debug("Jackery text decode requested with crypto key: %s", crypto_key)
    _LOGGER.debug("Jackery encoded text input: %s", encrypted_b64)

    try:
        ciphertext = base64.b64decode(encrypted_b64)
    except Exception as exc:
        raise JackeryCryptoError(f"Encrypted credential is not valid base64: {exc}") from exc

    errors: list[str] = []
    for mode_name in ("ecb", "cbc_key", "cbc_zero"):
        try:
            plaintext = _decrypt_modes(ciphertext, key, mode_name)
            _LOGGER.debug(
                "Jackery text decode succeeded using mode=%s.",
                mode_name,
            )
            return plaintext
        except Exception as exc:  # detailed stage logging is wanted here
            errors.append(f"{mode_name}={exc}")
            _LOGGER.debug(
                "Jackery text decode attempt failed for mode=%s: %s",
                mode_name,
                exc,
            )

    raise JackeryCryptoError(
        "All known Jackery AES decrypt strategies failed: " + " / ".join(errors)
    )


def encrypt_text(plaintext: str, crypto_key: str) -> str:
    """Encrypt a plaintext"""
    key = _normalize_key(crypto_key)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    encryptor = cipher.encryptor()

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("utf-8")
