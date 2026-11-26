"""UI setup: provider and key, then the printer's entities. Everything else lives in the options."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import probatio as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .api import PROVIDERS
from .const import (
    CONF_ACTIVE_STATES,
    CONF_API_KEY,
    CONF_AUTO_PAUSE,
    CONF_BASE_URL,
    CONF_CAMERA,
    CONF_CAMERA_DESCRIPTION,
    CONF_COMPARE_LAYERS,
    CONF_DAILY_LIMIT,
    CONF_EARLY_MINUTES,
    CONF_INTERVAL,
    CONF_LATER_INTERVAL,
    CONF_LAYER,
    CONF_MAX_IMAGE_SIZE,
    CONF_MAX_STORAGE_MB,
    CONF_MODEL,
    CONF_NO,
    CONF_NOTIFY,
    CONF_PAUSE_BUTTON,
    CONF_PAUSED_STATES,
    CONF_PRINT_REPORT,
    CONF_PRINT_STATUS,
    CONF_PROVIDER,
    CONF_QUESTIONS,
    CONF_REQUIRED,
    CONF_RESUME_BUTTON,
    CONF_RETENTION_DAYS,
    CONF_SAVE_FRAMES,
    CONF_SNOOZE,
    CONF_STAGE,
    CONF_STAGE_STATES,
    CONF_STOP_BUTTON,
    CONF_TIMEOUT,
    CONF_WINDOW,
    CONF_YES,
    DEFAULT_OPTIONS,
    DEFAULT_PAUSED_STATES,
    DEFAULT_STAGE_STATES,
    DOMAIN,
)
from .history import SAVE_MODES
from .questions import COMPARE, build_questions

CONF_NAME = "name"
QUESTION_FIELDS = {"instructions", "yes_means", "no_means"}
QUESTION_ID = re.compile(r"^[a-z0-9_]{1,64}$")


def _number(minimum: float, maximum: float, step: float = 1, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _entity(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _suggested(values: dict, key: str, fallback: Any = None) -> dict:
    """Prefill without a default, so clearing an optional field really clears it."""
    value = values.get(key, fallback)
    return {"description": {"suggested_value": value}} if value is not None else {}


def _provider_fields(values: dict, key_required: bool) -> dict:
    key = vol.Required(CONF_API_KEY) if key_required else vol.Optional(CONF_API_KEY)
    return {
        vol.Required(CONF_PROVIDER, default=values.get(CONF_PROVIDER, "openrouter")): selector.SelectSelector(
            selector.SelectSelectorConfig(options=list(PROVIDERS), translation_key=CONF_PROVIDER)
        ),
        key: selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
    }


def _printer_fields(values: dict) -> dict:
    states = selector.TextSelector(selector.TextSelectorConfig(multiple=True))
    return {
        vol.Required(CONF_CAMERA, **_suggested(values, CONF_CAMERA)): _entity("camera"),
        vol.Required(CONF_PRINT_STATUS, **_suggested(values, CONF_PRINT_STATUS)): _entity(["sensor", "binary_sensor"]),
        vol.Required(CONF_ACTIVE_STATES, default=values.get(CONF_ACTIVE_STATES, ["running"])): states,
        vol.Optional(CONF_PAUSED_STATES, default=values.get(CONF_PAUSED_STATES, DEFAULT_PAUSED_STATES)): states,
        vol.Optional(CONF_STAGE, **_suggested(values, CONF_STAGE)): _entity("sensor"),
        vol.Optional(CONF_STAGE_STATES, default=values.get(CONF_STAGE_STATES, DEFAULT_STAGE_STATES)): states,
        vol.Optional(CONF_LAYER, **_suggested(values, CONF_LAYER)): _entity("sensor"),
        vol.Optional(CONF_PAUSE_BUTTON, **_suggested(values, CONF_PAUSE_BUTTON)): _entity("button"),
        vol.Optional(CONF_RESUME_BUTTON, **_suggested(values, CONF_RESUME_BUTTON)): _entity("button"),
        vol.Optional(CONF_STOP_BUTTON, **_suggested(values, CONF_STOP_BUTTON)): _entity("button"),
    }


def validate_questions(value: Any) -> dict:
    """Check the shape of the questions option; build_questions checks the combinations."""
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("questions must be a mapping of question IDs")
    for qid, override in value.items():
        if not isinstance(qid, str) or not QUESTION_ID.match(qid):
            raise ValueError(f"question ID {qid!r} must use lowercase letters, numbers and underscores")
        if qid in COMPARE:
            raise ValueError(f"{qid} is used by the frame comparison; choose another ID")
        if isinstance(override, bool):
            continue
        if not isinstance(override, dict) or not set(override) <= QUESTION_FIELDS:
            raise ValueError(f"{qid} must be true, false or a mapping of instructions, yes_means and no_means")
        if not all(isinstance(v, str) and v.strip() for v in override.values()):
            raise ValueError(f"{qid}: every field must be non-empty text")
    build_questions(value)
    return value


def validate_options(options: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Return (errors, description placeholders) for an options dict."""
    if options[CONF_REQUIRED] > options[CONF_WINDOW]:
        return {"base": "required_gt_window"}, {}
    if options[CONF_NO] >= options[CONF_YES]:
        return {"base": "thresholds"}, {}
    try:
        validate_questions(options[CONF_QUESTIONS])
    except ValueError as err:
        return {"base": "questions"}, {"error": str(err)}
    return {}, {}


class PrintFailureConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._title = ""
        self._model = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._title = user_input[CONF_NAME]
            self._model = user_input.get(CONF_MODEL, "").strip()
            self._data = {CONF_PROVIDER: user_input[CONF_PROVIDER], CONF_API_KEY: user_input[CONF_API_KEY]}
            return await self.async_step_printer()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="3D printer"): selector.TextSelector(),
                **_provider_fields({}, key_required=True),
                vol.Optional(CONF_MODEL): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_printer(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_CAMERA])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._title,
                data={**self._data, **user_input},
                options={**DEFAULT_OPTIONS, CONF_MODEL: self._model},
            )

        return self.async_show_form(step_id="printer", data_schema=vol.Schema(_printer_fields({})))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the provider, key and printer entities without losing the frame history."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            camera = user_input[CONF_CAMERA]
            taken = any(e.unique_id == camera and e.entry_id != entry.entry_id for e in self._async_current_entries())
            if taken:
                errors[CONF_CAMERA] = "camera_taken"
            else:
                # Built from the form alone, so emptied optional fields drop out; a blank key keeps the old one.
                data = {**user_input, CONF_API_KEY: user_input.get(CONF_API_KEY) or entry.data[CONF_API_KEY]}
                return self.async_update_reload_and_abort(entry, unique_id=camera, data=data)
        values = {**entry.data, **(user_input or {})}
        schema = vol.Schema({**_provider_fields(values, key_required=False), **_printer_fields(values)})
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]})
        schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, description_placeholders={"name": entry.title}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> PrintFailureOptionsFlow:
        return PrintFailureOptionsFlow()


class PrintFailureOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        errors: dict[str, str] = {}
        placeholders = {"error": ""}

        if user_input is not None:
            flat = {k: v for group in user_input.values() for k, v in group.items()}
            candidate = {
                **current,
                **flat,
                # Optional fields are left out when emptied, so a missing key means cleared.
                CONF_NOTIFY: flat.get(CONF_NOTIFY) or [],
                CONF_MODEL: flat.get(CONF_MODEL, "").strip(),
                CONF_BASE_URL: flat.get(CONF_BASE_URL, "").strip(),
                CONF_QUESTIONS: flat.get(CONF_QUESTIONS) or {},
            }
            for key in (
                CONF_SNOOZE,
                CONF_INTERVAL,
                CONF_EARLY_MINUTES,
                CONF_LATER_INTERVAL,
                CONF_COMPARE_LAYERS,
                CONF_DAILY_LIMIT,
                CONF_WINDOW,
                CONF_REQUIRED,
                CONF_MAX_IMAGE_SIZE,
                CONF_TIMEOUT,
                CONF_RETENTION_DAYS,
                CONF_MAX_STORAGE_MB,
            ):
                candidate[key] = int(candidate[key])
            errors, found = validate_options(candidate)
            placeholders.update(found)
            if not errors:
                return self.async_create_entry(data=candidate)
            current = candidate

        def suggested(key: str) -> dict:
            # A default would put the old value back when the field is emptied; a suggestion doesn't.
            return {"description": {"suggested_value": current[key]}}

        notify_services = sorted(f"notify.{name}" for name in self.hass.services.async_services_for_domain("notify"))
        schema = vol.Schema(
            {
                vol.Required("alerts"): section(
                    vol.Schema(
                        {
                            vol.Required(CONF_AUTO_PAUSE, default=current[CONF_AUTO_PAUSE]): selector.BooleanSelector(),
                            vol.Optional(CONF_NOTIFY, **suggested(CONF_NOTIFY)): selector.SelectSelector(
                                selector.SelectSelectorConfig(options=notify_services, multiple=True, custom_value=True)
                            ),
                            vol.Required(CONF_SNOOZE, default=current[CONF_SNOOZE]): _number(1, 240, unit="min"),
                            vol.Required(
                                CONF_PRINT_REPORT, default=current[CONF_PRINT_REPORT]
                            ): selector.BooleanSelector(),
                        }
                    )
                ),
                vol.Required("detection"): section(
                    vol.Schema(
                        {
                            vol.Required(CONF_INTERVAL, default=current[CONF_INTERVAL]): _number(10, 600, unit="s"),
                            vol.Required(CONF_EARLY_MINUTES, default=current[CONF_EARLY_MINUTES]): _number(
                                0, 600, unit="min"
                            ),
                            vol.Required(CONF_LATER_INTERVAL, default=current[CONF_LATER_INTERVAL]): _number(
                                10, 600, unit="s"
                            ),
                            vol.Required(CONF_WINDOW, default=current[CONF_WINDOW]): _number(1, 20),
                            vol.Required(CONF_REQUIRED, default=current[CONF_REQUIRED]): _number(1, 20),
                            vol.Required(CONF_YES, default=current[CONF_YES]): _number(0.05, 0.98, 0.01),
                            vol.Required(CONF_NO, default=current[CONF_NO]): _number(0.02, 0.95, 0.01),
                            vol.Required(CONF_COMPARE_LAYERS, default=current[CONF_COMPARE_LAYERS]): _number(0, 500),
                            vol.Required(CONF_DAILY_LIMIT, default=current[CONF_DAILY_LIMIT]): _number(0, 100000),
                        }
                    )
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(CONF_SAVE_FRAMES, default=current[CONF_SAVE_FRAMES]): selector.SelectSelector(
                                selector.SelectSelectorConfig(options=SAVE_MODES, translation_key=CONF_SAVE_FRAMES)
                            ),
                            vol.Required(CONF_RETENTION_DAYS, default=current[CONF_RETENTION_DAYS]): _number(
                                1, 365, unit="d"
                            ),
                            vol.Required(CONF_MAX_STORAGE_MB, default=current[CONF_MAX_STORAGE_MB]): _number(
                                50, 100000, unit="MB"
                            ),
                        }
                    )
                ),
                vol.Required("advanced"): section(
                    vol.Schema(
                        {
                            vol.Optional(CONF_MODEL, **suggested(CONF_MODEL)): selector.TextSelector(),
                            vol.Optional(CONF_BASE_URL, **suggested(CONF_BASE_URL)): selector.TextSelector(
                                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                            ),
                            vol.Required(
                                CONF_CAMERA_DESCRIPTION, default=current[CONF_CAMERA_DESCRIPTION]
                            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
                            vol.Required(CONF_MAX_IMAGE_SIZE, default=current[CONF_MAX_IMAGE_SIZE]): _number(
                                280, 1568, unit="px"
                            ),
                            vol.Required(CONF_TIMEOUT, default=current[CONF_TIMEOUT]): _number(5, 120, unit="s"),
                            vol.Optional(CONF_QUESTIONS, **suggested(CONF_QUESTIONS)): selector.ObjectSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors, description_placeholders=placeholders
        )
