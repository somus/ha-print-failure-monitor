"""Actions for automations: run a check, snooze monitoring."""

from __future__ import annotations

from typing import TYPE_CHECKING

import probatio as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .const import CONF_SNOOZE, DOMAIN

if TYPE_CHECKING:
    from .coordinator import PrintFailureCoordinator

SERVICE_CHECK = "check"
SERVICE_SNOOZE = "snooze"
ATTR_ENTRY = "config_entry_id"
ATTR_CAMERA = "camera"
ATTR_MINUTES = "minutes"

CHECK_SCHEMA = vol.Schema({vol.Required(ATTR_ENTRY): cv.string, vol.Optional(ATTR_CAMERA): cv.entity_domain("camera")})
SNOOZE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTRY): cv.string,
        vol.Optional(ATTR_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=0, max=1440)),
    }
)


def _coordinator(call: ServiceCall) -> PrintFailureCoordinator:
    return service.async_get_config_entry(call.hass, DOMAIN, call.data[ATTR_ENTRY]).runtime_data


async def _check(call: ServiceCall) -> ServiceResponse:
    coordinator = _coordinator(call)
    camera = call.data.get(ATTR_CAMERA)
    if camera:
        return await coordinator.async_check_camera(camera)
    snapshot = await coordinator.async_check_now()
    answers = {} if snapshot.error else snapshot.answers  # the snapshot keeps the last good answers on error
    return {"result": snapshot.result, "answers": answers, "error": snapshot.error}


async def _snooze(call: ServiceCall) -> None:
    coordinator = _coordinator(call)
    await coordinator.async_snooze(call.data.get(ATTR_MINUTES, coordinator.option(CONF_SNOOZE)))


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN, SERVICE_CHECK, _check, schema=CHECK_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_SNOOZE, _snooze, schema=SNOOZE_SCHEMA)
