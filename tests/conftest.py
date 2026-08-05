"""Shared pytest fixtures for the jackery_home_cloud test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make this custom component loadable by name in every test using `hass`."""
    yield
