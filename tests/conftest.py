"""Shared fixtures."""

import io

import pytest
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.print_failure_monitor.const import (
    CONF_ACTIVE_STATES,
    CONF_API_KEY,
    CONF_CAMERA,
    CONF_PAUSE_BUTTON,
    CONF_PRINT_STATUS,
    CONF_PROVIDER,
    CONF_RESUME_BUTTON,
    CONF_STOP_BUTTON,
    DEFAULT_OPTIONS,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"

ENTRY_DATA = {
    CONF_PROVIDER: "openrouter",
    CONF_API_KEY: "test-key",
    CONF_CAMERA: "camera.p1s_camera",
    CONF_PRINT_STATUS: "sensor.p1s_print_status",
    CONF_ACTIVE_STATES: ["running"],
    CONF_PAUSE_BUTTON: "button.p1s_pause",
    CONF_RESUME_BUTTON: "button.p1s_resume",
    CONF_STOP_BUTTON: "button.p1s_stop",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def config_dir(hass, tmp_path):
    """Keep saved frames out of the test package's shared config directory."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture
def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (640, 360), (90, 90, 90)).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def make_entry():
    def _make(options: dict | None = None, data: dict | None = None) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            title="P1S",
            data={**ENTRY_DATA, **(data or {})},
            options={**DEFAULT_OPTIONS, **(options or {})},
            unique_id=ENTRY_DATA[CONF_CAMERA],
        )

    return _make
