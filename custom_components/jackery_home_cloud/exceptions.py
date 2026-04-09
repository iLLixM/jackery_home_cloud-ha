"""Custom exceptions for the Jackery Home Cloud integration."""


class JackeryHomeError(Exception):
    """Base exception for Jackery Home Cloud."""


class JackeryHomeAuthError(JackeryHomeError):
    """Authentication with the Jackery cloud failed."""


class JackeryHomeApiError(JackeryHomeError):
    """The REST API returned an error or invalid payload."""


class JackeryHomeMqttError(JackeryHomeError):
    """Placeholder for future MQTT related failures."""
