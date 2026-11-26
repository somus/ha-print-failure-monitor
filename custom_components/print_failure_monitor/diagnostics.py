"""Diagnostics download: settings, the current state and recent checks, without the API key or any images."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import PrintFailureConfigEntry
from .const import CONF_API_KEY
from .panel import label_stats

RECENT_RECORDS = 50


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PrintFailureConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    snapshot = {k: v for k, v in coordinator.snapshot.__dict__.items() if k != "frame"}  # no image bytes
    history = coordinator.history
    return {
        "data": async_redact_data(dict(entry.data), {CONF_API_KEY}),
        "options": dict(entry.options),
        "model": coordinator.model,
        "questions": coordinator.questions,
        "snapshot": snapshot,
        "saved_state": coordinator.saved_state(),
        "history": {
            "records": len(history.records),
            "labels": label_stats(history.records, history.labels),
            "recent": history.records[-RECENT_RECORDS:],
        },
    }
