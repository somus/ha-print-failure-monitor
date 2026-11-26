"""Check now: run one check immediately, e.g. to test the setup between prints."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrintFailureConfigEntry
from .entity import PrintFailureEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PrintFailureConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([CheckNowButton(entry.runtime_data, "check_now")])


class CheckNowButton(PrintFailureEntity, ButtonEntity):
    async def async_press(self) -> None:
        await self.coordinator.async_check_now()
