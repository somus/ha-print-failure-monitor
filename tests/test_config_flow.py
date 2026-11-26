"""UI setup and options."""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.print_failure_monitor.const import (
    CONF_AUTO_PAUSE,
    CONF_MODEL,
    CONF_QUESTIONS,
    CONF_REQUIRED,
    CONF_WINDOW,
    DEFAULT_OPTIONS,
    DOMAIN,
)

SETUP = "custom_components.print_failure_monitor.async_setup_entry"


async def test_setup_flow(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "P1S", "provider": "google", "api_key": "k", "model": " "}
    )
    assert result["step_id"] == "printer"
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "camera": "camera.p1s_camera",
                "print_status": "sensor.p1s_print_status",
                "active_states": ["running"],
                "pause_button": "button.p1s_pause",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "P1S"
    assert result["data"]["provider"] == "google"
    assert result["data"]["pause_button"] == "button.p1s_pause"
    assert result["options"] == {**DEFAULT_OPTIONS, CONF_MODEL: ""}


async def test_same_camera_twice_aborts(hass: HomeAssistant, make_entry):
    make_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "P1S", "provider": "openrouter", "api_key": "k"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"camera": "camera.p1s_camera", "print_status": "sensor.p1s_print_status", "active_states": ["running"]},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _options(**changes):
    o = {**DEFAULT_OPTIONS, **changes}
    return {
        "alerts": {k: o[k] for k in ("auto_pause", "notify_services", "snooze_minutes", "print_report")},
        "detection": {
            k: o[k]
            for k in (
                "interval",
                "early_minutes",
                "later_interval",
                "window",
                "required",
                "yes_threshold",
                "no_threshold",
                "compare_layers",
                "daily_limit",
            )
        },
        "history": {k: o[k] for k in ("save_frames", "retention_days", "max_storage_mb")},
        "advanced": {
            k: o[k] for k in ("model", "base_url", "camera_description", "max_image_size", "timeout", "questions")
        },
    }


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({CONF_REQUIRED: 5, CONF_WINDOW: 4}, "required_gt_window"),
        ({"no_threshold": 0.8, "yes_threshold": 0.7}, "thresholds"),
        ({CONF_QUESTIONS: {"plate_visible": False}}, "questions"),
        ({CONF_QUESTIONS: {"Layer Shift": {"instructions": "x", "yes_means": "y", "no_means": "z"}}}, "questions"),
        ({CONF_QUESTIONS: {"layer_shift": {"instructions": "x", True: "y", False: "z"}}}, "questions"),
        ({CONF_QUESTIONS: {"not_growing": {"instructions": "x", "yes_means": "y", "no_means": "z"}}}, "questions"),
    ],
)
async def test_options_rejected(hass: HomeAssistant, make_entry, changes, error):
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], _options(**changes))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_options_saved(hass: HomeAssistant, make_entry):
    entry = make_entry({"crop": [0.1, 0.3, 0.9, 1.0]})
    entry.add_to_hass(hass)
    questions = {"nozzle_blob": False, "layer_shift": {"instructions": "x?", "yes_means": "y", "no_means": "z"}}
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            _options(
                auto_pause=True,
                window=5.0,
                questions=questions,
                save_frames="not_clean",
                retention_days=30.0,
                later_interval=90.0,
                daily_limit=500.0,
                print_report=False,
            ),
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AUTO_PAUSE] is True
    assert entry.options[CONF_WINDOW] == 5 and isinstance(entry.options[CONF_WINDOW], int)
    assert entry.options[CONF_QUESTIONS] == questions
    assert entry.options["save_frames"] == "not_clean"
    assert entry.options["retention_days"] == 30 and isinstance(entry.options["retention_days"], int)
    assert entry.options["later_interval"] == 90 and isinstance(entry.options["later_interval"], int)
    assert entry.options["daily_limit"] == 500 and entry.options["print_report"] is False
    assert entry.options["enabled"] is True  # untouched options survive
    assert entry.options["crop"] == [0.1, 0.3, 0.9, 1.0]  # set in the panel, not on this form


async def test_optional_options_can_be_cleared(hass: HomeAssistant, make_entry):
    """The frontend leaves emptied optional fields out; that has to clear them, not keep the old value."""
    entry = make_entry(
        {
            "model": "custom/model",
            "base_url": "https://gateway.example/v1",
            "questions": {"nozzle_blob": False},
            "notify_services": ["notify.mobile_app_phone"],
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    payload = _options()
    for key in ("model", "base_url", "questions"):
        del payload["advanced"][key]
    del payload["alerts"]["notify_services"]
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], payload)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {k: entry.options[k] for k in ("model", "base_url", "questions", "notify_services")} == {
        "model": "",
        "base_url": "",
        "questions": {},
        "notify_services": [],
    }


async def test_reconfigure_keeps_history_and_key(hass: HomeAssistant, make_entry):
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "provider": "google",
                "camera": "camera.p1s_chamber",
                "print_status": "sensor.p1s_print_status",
                "active_states": ["running"],
                "paused_states": ["pause"],
                "stage": "sensor.p1s_current_stage",
                "stage_states": ["printing"],
                "pause_button": "button.p1s_pause",
            },
        )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.unique_id == "camera.p1s_chamber"
    assert entry.data["api_key"] == "test-key"  # left empty: kept
    assert entry.data["provider"] == "google" and entry.data["stage"] == "sensor.p1s_current_stage"
    assert "stop_button" not in entry.data and "resume_button" not in entry.data  # cleared


async def test_reconfigure_rejects_another_printers_camera(hass: HomeAssistant, make_entry):
    entry = make_entry()
    entry.add_to_hass(hass)
    other = make_entry(data={"camera": "camera.voron"})
    other.add_to_hass(hass)
    hass.config_entries.async_update_entry(other, unique_id="camera.voron")
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "provider": "openrouter",
            "camera": "camera.voron",
            "print_status": "sensor.p1s_print_status",
            "active_states": ["running"],
        },
    )
    assert result["errors"] == {"camera": "camera_taken"}


async def test_reauth_sets_new_key(hass: HomeAssistant, make_entry):
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "new-key"})
    assert result["reason"] == "reauth_successful"
    assert entry.data["api_key"] == "new-key"
