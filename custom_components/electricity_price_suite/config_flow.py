"""Config flow for electricity_price_suite."""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_ALLOWED_PROGRAMS,
    CONF_AUTO_CREATE_PROGRAMS,
    CONF_BLOCKED_PROGRAMS,
    CONF_CACHE_RETENTION_DAYS,
    CONF_CURRENCY,
    CONF_ENABLE_CURRENT_PRICE_SENSOR,
    CONF_ENERGY_ENTITY,
    CONF_ENTRY_TYPE,
    CONF_MAX_POWER_KW,
    CONF_NAME,
    CONF_ROUND_DECIMALS,
    CONF_SLUG,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_CHAIN,
    CONF_TIMELINE_NAME,
    DEFAULT_AUTO_CREATE_PROGRAMS,
    DEFAULT_CACHE_RETENTION_DAYS,
    DEFAULT_CURRENCY,
    DEFAULT_ENABLE_CURRENT_PRICE_SENSOR,
    DEFAULT_MAX_POWER_KW,
    DEFAULT_ROUND_DECIMALS,
    DEFAULT_SLOT_MINUTES,
    DOMAIN,
    ENTRY_TYPE_PROFILE_LOGGER,
    ENTRY_TYPE_TIMELINE,
)


def _int_selector(*, min_value: int = 0, max_value: int | None = None) -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(min=min_value, max=max_value, mode=selector.NumberSelectorMode.BOX, step=1))


def _float_selector(*, min_value: float = 0, max_value: float | None = None, step: float = 0.1) -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(min=min_value, max=max_value, mode=selector.NumberSelectorMode.BOX, step=step))


def _parse_program_list(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _program_list_selector() -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(options=[], multiple=True, custom_value=True))


def _validate_energy_entity(hass, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    if state is None:
        return "invalid_energy_entity"
    try:
        float(state.state)
    except (TypeError, ValueError):
        return "non_numeric_energy_state"
    unit = state.attributes.get("unit_of_measurement")
    if unit not in {UnitOfEnergy.KILO_WATT_HOUR, UnitOfEnergy.WATT_HOUR}:
        return "unsupported_energy_unit"
    if state.attributes.get("state_class") != "total_increasing":
        return "invalid_energy_state_class"
    return None


class ElectricityPriceSuiteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._draft: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._draft = {CONF_ENTRY_TYPE: user_input[CONF_ENTRY_TYPE]}
            if user_input[CONF_ENTRY_TYPE] == ENTRY_TYPE_PROFILE_LOGGER:
                return await self.async_step_profile_logger()
            return await self.async_step_timeline()
        schema = vol.Schema({
            vol.Required(CONF_ENTRY_TYPE, default=ENTRY_TYPE_TIMELINE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=ENTRY_TYPE_TIMELINE, label="Price Timeline"),
                        selector.SelectOptionDict(value=ENTRY_TYPE_PROFILE_LOGGER, label="Consumption Profile Logger"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors={})

    async def async_step_timeline(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            title = user_input[CONF_TIMELINE_NAME]
            await self.async_set_unique_id(f"{ENTRY_TYPE_TIMELINE}_{slugify(title)}")
            self._abort_if_unique_id_configured()
            self._draft.update({
                CONF_TIMELINE_NAME: title,
                CONF_CURRENCY: user_input[CONF_CURRENCY],
                CONF_CACHE_RETENTION_DAYS: int(user_input[CONF_CACHE_RETENTION_DAYS]),
                CONF_ROUND_DECIMALS: int(user_input[CONF_ROUND_DECIMALS]),
                CONF_ENABLE_CURRENT_PRICE_SENSOR: bool(user_input[CONF_ENABLE_CURRENT_PRICE_SENSOR]),
            })
            return await self.async_step_primary_type()
        schema = vol.Schema({
            vol.Required(CONF_TIMELINE_NAME): str,
            vol.Required(CONF_CURRENCY, default=DEFAULT_CURRENCY): str,
            vol.Required(CONF_CACHE_RETENTION_DAYS, default=DEFAULT_CACHE_RETENTION_DAYS): _int_selector(min_value=1, max_value=365),
            vol.Required(CONF_ROUND_DECIMALS, default=DEFAULT_ROUND_DECIMALS): _int_selector(min_value=0, max_value=8),
            vol.Required(CONF_ENABLE_CURRENT_PRICE_SENSOR, default=DEFAULT_ENABLE_CURRENT_PRICE_SENSOR): bool,
        })
        return self.async_show_form(step_id="timeline", data_schema=schema, errors={})

    async def async_step_profile_logger(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            title = user_input[CONF_NAME].strip()
            energy_entity = user_input[CONF_ENERGY_ENTITY].strip()
            slot_minutes = int(user_input[CONF_SLOT_MINUTES])
            max_power_kw = float(user_input[CONF_MAX_POWER_KW])
            if not title:
                errors[CONF_NAME] = "required"
            elif not energy_entity:
                errors[CONF_ENERGY_ENTITY] = "required"
            elif slot_minutes <= 0:
                errors[CONF_SLOT_MINUTES] = "invalid"
            elif max_power_kw <= 0:
                errors[CONF_MAX_POWER_KW] = "invalid"
            elif (energy_error := _validate_energy_entity(self.hass, energy_entity)) is not None:
                errors[CONF_ENERGY_ENTITY] = energy_error
            else:
                slug = slugify(title)
                await self.async_set_unique_id(f"{ENTRY_TYPE_PROFILE_LOGGER}_{slug}")
                self._abort_if_unique_id_configured()
                data = {
                    CONF_ENTRY_TYPE: ENTRY_TYPE_PROFILE_LOGGER,
                    CONF_NAME: title,
                    CONF_ENERGY_ENTITY: energy_entity,
                    CONF_SLOT_MINUTES: slot_minutes,
                    CONF_MAX_POWER_KW: max_power_kw,
                    CONF_AUTO_CREATE_PROGRAMS: bool(user_input[CONF_AUTO_CREATE_PROGRAMS]),
                    CONF_ALLOWED_PROGRAMS: _parse_program_list(user_input.get(CONF_ALLOWED_PROGRAMS, [])),
                    CONF_BLOCKED_PROGRAMS: _parse_program_list(user_input.get(CONF_BLOCKED_PROGRAMS, [])),
                    CONF_SLUG: slug,
                }
                return self.async_create_entry(title=title, data=data)
        schema = vol.Schema({
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_ENERGY_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig()),
            vol.Required(CONF_SLOT_MINUTES, default=DEFAULT_SLOT_MINUTES): _int_selector(min_value=1, max_value=120),
            vol.Required(CONF_MAX_POWER_KW, default=DEFAULT_MAX_POWER_KW): _float_selector(min_value=0.001, max_value=50, step=0.001),
            vol.Required(CONF_AUTO_CREATE_PROGRAMS, default=DEFAULT_AUTO_CREATE_PROGRAMS): bool,
            vol.Optional(CONF_ALLOWED_PROGRAMS, default=[]): _program_list_selector(),
            vol.Optional(CONF_BLOCKED_PROGRAMS, default=[]): _program_list_selector(),
        })
        return self.async_show_form(step_id="profile_logger", data_schema=schema, errors=errors)

    async def async_step_primary_type(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            source_type = user_input["source_type"]
            self._draft["primary_source_type"] = source_type
            if source_type == "entity_attribute":
                return await self.async_step_primary_attribute()
            if source_type == "inject_only":
                return await self.async_step_primary_inject()
            return await self.async_step_primary_action()
        schema = vol.Schema({
            vol.Required("source_type", default="entity_attribute"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="entity_attribute", label="Entity Attribute"),
                        selector.SelectOptionDict(value="entity_action", label="Service Action"),
                        selector.SelectOptionDict(value="inject_only", label="Inject Only"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        })
        return self.async_show_form(step_id="primary_type", data_schema=schema, errors={})

    async def async_step_primary_inject(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            source = {"id": user_input["id"], "type": "inject_only", "priority": int(user_input.get("priority", 0)), "enabled": True, "slot_mapping": {"time_key": user_input["time_key"], "price_key": user_input["price_key"]}}
            return self._finish_create_timeline_entry(source)
        schema = vol.Schema({
            vol.Required("id", default="primary"): str,
            vol.Required("priority", default=0): _int_selector(min_value=0, max_value=9999),
            vol.Required("time_key", default="start_time"): str,
            vol.Required("price_key", default="price_per_kwh"): str,
        })
        return self.async_show_form(step_id="primary_inject", data_schema=schema, errors={})

    async def async_step_primary_attribute(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            source = {"id": user_input["id"], "type": "entity_attribute", "priority": int(user_input.get("priority", 0)), "enabled": True, "entity_id": user_input["source_entity_id"], "attribute": user_input["attribute"], "slot_mapping": {"time_key": user_input["time_key"], "price_key": user_input["price_key"]}}
            return self._finish_create_timeline_entry(source)
        schema = vol.Schema({
            vol.Required("id", default="primary"): str,
            vol.Required("priority", default=0): _int_selector(min_value=0, max_value=9999),
            vol.Required("source_entity_id"): selector.EntitySelector(selector.EntitySelectorConfig()),
            vol.Required("attribute", default="data"): str,
            vol.Required("time_key", default="start_time"): str,
            vol.Required("price_key", default="price_per_kwh"): str,
        })
        return self.async_show_form(step_id="primary_attribute", data_schema=schema, errors={})

    async def async_step_primary_action(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            payload_raw = user_input.get("request_payload_json", "{}")
            try:
                request_payload = json.loads(payload_raw) if payload_raw else {}
                if not isinstance(request_payload, dict):
                    raise ValueError
            except (ValueError, TypeError, json.JSONDecodeError):
                errors["request_payload_json"] = "invalid_json"
            else:
                source = {
                    "id": user_input["id"],
                    "type": "entity_action",
                    "priority": int(user_input.get("priority", 0)),
                    "enabled": True,
                    "action": user_input["action"],
                    "response_path": user_input["response_path"],
                    "request_payload": request_payload,
                    "inject_time_window": bool(user_input["inject_time_window"]),
                    "start_key": user_input["start_key"],
                    "end_key": user_input["end_key"],
                    "time_format": user_input["time_format"],
                    "slot_mapping": {"time_key": user_input["time_key"], "price_key": user_input["price_key"]},
                }
                if user_input.get("source_entity_id"):
                    source["entity_id"] = user_input["source_entity_id"]
                return self._finish_create_timeline_entry(source)
        schema = vol.Schema({
            vol.Required("id", default="primary"): str,
            vol.Required("priority", default=0): _int_selector(min_value=0, max_value=9999),
            vol.Required("action", default="tibber.get_prices"): str,
            vol.Required("response_path", default="prices.tibber-home"): str,
            vol.Optional("source_entity_id"): selector.EntitySelector(selector.EntitySelectorConfig()),
            vol.Required("request_payload_json", default="{}"): str,
            vol.Required("inject_time_window", default=True): bool,
            vol.Required("start_key", default="start"): str,
            vol.Required("end_key", default="end"): str,
            vol.Required("time_format", default="%Y-%m-%d %H:%M:%S"): str,
            vol.Required("time_key", default="start_time"): str,
            vol.Required("price_key", default="price"): str,
        })
        return self.async_show_form(step_id="primary_action", data_schema=schema, errors=errors)

    def _finish_create_timeline_entry(self, primary_source: dict[str, Any]):
        data = {**self._draft, CONF_ENTRY_TYPE: ENTRY_TYPE_TIMELINE, CONF_SOURCE_CHAIN: [primary_source]}
        title = data[CONF_TIMELINE_NAME]
        return self.async_create_entry(title=title, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ElectricityPriceSuiteOptionsFlow(config_entry)


class ElectricityPriceSuiteOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        entry_type = self._config_entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_TIMELINE)
        if entry_type == ENTRY_TYPE_PROFILE_LOGGER:
            return await self.async_step_profile_logger(user_input)
        return await self.async_step_timeline(user_input)

    async def async_step_timeline(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data={CONF_CURRENCY: user_input[CONF_CURRENCY], CONF_CACHE_RETENTION_DAYS: int(user_input[CONF_CACHE_RETENTION_DAYS]), CONF_ROUND_DECIMALS: int(user_input[CONF_ROUND_DECIMALS]), CONF_ENABLE_CURRENT_PRICE_SENSOR: user_input[CONF_ENABLE_CURRENT_PRICE_SENSOR]})
        current = {**self._config_entry.data, **self._config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_CURRENCY, default=current.get(CONF_CURRENCY, DEFAULT_CURRENCY)): str,
            vol.Required(CONF_CACHE_RETENTION_DAYS, default=current.get(CONF_CACHE_RETENTION_DAYS, DEFAULT_CACHE_RETENTION_DAYS)): _int_selector(min_value=1, max_value=365),
            vol.Required(CONF_ROUND_DECIMALS, default=current.get(CONF_ROUND_DECIMALS, DEFAULT_ROUND_DECIMALS)): _int_selector(min_value=0, max_value=8),
            vol.Required(CONF_ENABLE_CURRENT_PRICE_SENSOR, default=current.get(CONF_ENABLE_CURRENT_PRICE_SENSOR, DEFAULT_ENABLE_CURRENT_PRICE_SENSOR)): bool,
        })
        return self.async_show_form(step_id="timeline", data_schema=schema, errors={})

    async def async_step_profile_logger(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        current = {**self._config_entry.data, **self._config_entry.options}
        if user_input is not None:
            slot_minutes = int(user_input[CONF_SLOT_MINUTES])
            max_power_kw = float(user_input[CONF_MAX_POWER_KW])
            if slot_minutes <= 0:
                errors[CONF_SLOT_MINUTES] = "invalid"
            elif max_power_kw <= 0:
                errors[CONF_MAX_POWER_KW] = "invalid"
            elif (energy_error := _validate_energy_entity(self.hass, user_input[CONF_ENERGY_ENTITY].strip())) is not None:
                errors[CONF_ENERGY_ENTITY] = energy_error
            else:
                return self.async_create_entry(title="", data={
                    CONF_ENERGY_ENTITY: user_input[CONF_ENERGY_ENTITY].strip(),
                    CONF_SLOT_MINUTES: slot_minutes,
                    CONF_MAX_POWER_KW: max_power_kw,
                    CONF_AUTO_CREATE_PROGRAMS: bool(user_input[CONF_AUTO_CREATE_PROGRAMS]),
                    CONF_ALLOWED_PROGRAMS: _parse_program_list(user_input.get(CONF_ALLOWED_PROGRAMS, [])),
                    CONF_BLOCKED_PROGRAMS: _parse_program_list(user_input.get(CONF_BLOCKED_PROGRAMS, [])),
                })
        schema = vol.Schema({
            vol.Required(CONF_ENERGY_ENTITY, default=current[CONF_ENERGY_ENTITY]): selector.EntitySelector(selector.EntitySelectorConfig()),
            vol.Required(CONF_SLOT_MINUTES, default=current.get(CONF_SLOT_MINUTES, DEFAULT_SLOT_MINUTES)): _int_selector(min_value=1, max_value=120),
            vol.Required(CONF_MAX_POWER_KW, default=current.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW)): _float_selector(min_value=0.001, max_value=50, step=0.001),
            vol.Required(CONF_AUTO_CREATE_PROGRAMS, default=current.get(CONF_AUTO_CREATE_PROGRAMS, DEFAULT_AUTO_CREATE_PROGRAMS)): bool,
            vol.Optional(CONF_ALLOWED_PROGRAMS, default=current.get(CONF_ALLOWED_PROGRAMS, [])): _program_list_selector(),
            vol.Optional(CONF_BLOCKED_PROGRAMS, default=current.get(CONF_BLOCKED_PROGRAMS, [])): _program_list_selector(),
        })
        return self.async_show_form(step_id="profile_logger", data_schema=schema, errors=errors)
