"""Custom exceptions for the Jackery Home Cloud integration."""


class JackeryHomeError(Exception):
    """Base exception for Jackery Home Cloud."""


class JackeryHomeAuthError(JackeryHomeError):
    """Authentication with the Jackery cloud failed."""


class JackeryHomeApiError(JackeryHomeError):
    """The REST API returned an error or invalid payload."""


class JackeryHomeCryptoError(JackeryHomeError):
    """The provided crypto key or encoded text payload is invalid."""


class JackeryHomeMqttError(JackeryHomeError):
    """MQTT connection, parsing, or runtime state handling failed."""


class JackeryCryptoError(Exception):
    """Raised when Jackery-specific credential encryption or decryption fails."""
