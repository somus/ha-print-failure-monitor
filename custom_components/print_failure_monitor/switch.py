"""Switches for monitoring and auto-pause. They write the entry's options, so the options screen stays in sync."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrintFailureConfigEntry
from .const import CONF_AUTO_PAUSE, CONF_ENABLED
from .coordinator import PrintFailureCoordinator
from .entity import PrintFailureEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PrintFailureConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([OptionSwitch(coordinator, CONF_ENABLED), OptionSwitch(coordinator, CONF_AUTO_PAUSE)])


class OptionSwitch(PrintFailureEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PrintFailureCoordinator, option: str) -> None:
        super().__init__(coordinator, option)
        self._option = option

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.option(self._option))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_option(self._option, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_option(self._option, False)
