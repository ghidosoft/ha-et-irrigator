"""Pytest fixtures for ET Irrigator tests."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def enable_et_irrigator(enable_custom_integrations):
    """Enable loading of the custom integration (request explicitly)."""
    yield
