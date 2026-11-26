"""Monitoring sensors: the latest failure probability and the result of the latest check."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrintFailureConfigEntry
from .coordinator import RESULTS
from .entity import PrintFailureEntity
from .questions import failure_ids


async def async_setup_entry(
    hass: HomeAssistant, entry: PrintFailureConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [FailureProbabilitySensor(coordinator, "failure_probability"), LastCheckSensor(coordinator, "last_check")]
    )


class FailureProbabilitySensor(PrintFailureEntity, SensorEntity):
    """Highest failure-question probability in the latest frame; attributes hold every answer."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        answers = self.coordinator.data.answers
        if not answers:
            return None
        return round(max(answers[q] for q in failure_ids(answers)) * 100, 1)

    @property
    def extra_state_attributes(self):
        return {qid: round(p, 3) for qid, p in self.coordinator.data.answers.items()}


class LastCheckSensor(PrintFailureEntity, SensorEntity):
    """What happened on the latest check, one of RESULTS."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = RESULTS

    @property
    def native_value(self) -> str:
        return self.coordinator.data.result

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        return {
            "recent_verdicts": data.recent,
            "checked_at": data.checked_at.isoformat() if data.checked_at else None,
            "model": self.coordinator.model,
            "last_cost": data.cost,
            "last_error": data.error,
            "checks_today": self.coordinator.calls_today,
        }
