"""The last camera frame that was checked, exactly as sent to the model."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrintFailureConfigEntry
from .coordinator import PrintFailureCoordinator
from .entity import PrintFailureEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PrintFailureConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([LastFrameImage(hass, entry.runtime_data)])


class LastFrameImage(PrintFailureEntity, ImageEntity):
    def __init__(self, hass: HomeAssistant, coordinator: PrintFailureCoordinator) -> None:
        PrintFailureEntity.__init__(self, coordinator, "last_frame")
        ImageEntity.__init__(self, hass)
        self._sync()

    @callback
    def _sync(self) -> None:
        self._attr_image_last_updated = self.coordinator.data.frame_at
        self._attr_content_type = self.coordinator.data.frame_type

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        return self.coordinator.data.frame
