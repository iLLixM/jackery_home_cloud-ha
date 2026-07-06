"""Helpers for deriving Jackery MQTT subscriptions.

Starting with v0.2.3-fix7 the integration intentionally avoids broad '#'
wildcard subscriptions. Instead, the topic list is derived from the main
device serial number discovered from the REST API.
"""

from __future__ import annotations

from .const import MQTT_GW_DATA_TOPIC_TEMPLATE, MQTT_LWT_TOPIC_TEMPLATE


def build_default_subscriptions(device_serial: str) -> tuple[tuple[str, int], ...]:
    """Build the minimal targeted MQTT subscription set.

    For this iteration we deliberately keep the topic surface narrow:
    - gateway LWT/online state of the main device
    - gateway data topic of the main device
    """
    normalized = str(device_serial).strip()
    if not normalized:
        return ()

    return (
        (MQTT_LWT_TOPIC_TEMPLATE.format(device_serial=normalized), 0),
        (MQTT_GW_DATA_TOPIC_TEMPLATE.format(device_serial=normalized), 1),
    )
