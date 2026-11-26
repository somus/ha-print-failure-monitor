"""Failure sensor: on once enough recent frames show a failed print."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrintFailureConfigEntry
from .entity import PrintFailureEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PrintFailureConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([FailureSensor(entry.runtime_data, "failure")])


class FailureSensor(PrintFailureEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.failing
