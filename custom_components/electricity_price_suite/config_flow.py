"""Config flow for electricity_price_suite."""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CACHE_RETENTION_DAYS,
    CONF_CURRENCY,
    CONF_ENABLE_CURRENT_PRICE_SENSOR,
    CONF_ROUND_DECIMALS,
    CONF_SOURCE_CHAIN,
    CONF_TIMELINE_NAME,
    DEFAULT_CACHE_RETENTION_DAYS,
    DEFAULT_CURRENCY,
    DEFAULT_ENABLE_CURRENT_PRICE_SENSOR,
    DEFAULT_ROUND_DECIMALS,
    DOMAIN,
)


class ElectricityPriceSuiteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._draft: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            title = user_input[CONF_TIMELINE_NAME]
            await self.async_set_unique_id(title.lower())
            self._abort_if_unique_id_configured()
            self._draft = {
                CONF_TIMELINE_NAME: title,
                CONF_CURRENCY: user_input[CONF_CURRENCY],
                CONF_CACHE_RETENTION_DAYS: int(user_input[CONF_CACHE_RETENTION_DAYS]),
                CONF_ROUND_DECIMALS: int(user_input[CONF_ROUND_DECIMALS]),
                CONF_ENABLE_CURRENT_PRICE_SENSOR: bool(user_input[CONF_ENABLE_CURRENT_PRICE_SENSOR]),
            }
            return await self.async_step_primary_type()

        schema = vol.Schema(
            {
                vol.Required(CONF_TIMELINE_NAME): str,
                vol.Required(CONF_CURRENCY, default=DEFAULT_CURRENCY): str,
                vol.Required(CONF_CACHE_RETENTION_DAYS, default=DEFAULT_CACHE_RETENTION_DAYS): int,
                vol.Required(CONF_ROUND_DECIMALS, default=DEFAULT_ROUND_DECIMALS): int,
                vol.Required(
                    CONF_ENABLE_CURRENT_PRICE_SENSOR,
                    default=DEFAULT_ENABLE_CURRENT_PRICE_SENSOR,
                ): bool,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors={})

    async def async_step_primary_type(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            source_type = user_input["source_type"]
            self._draft["primary_source_type"] = source_type
            if source_type == "entity_attribute":
                return await self.async_step_primary_attribute()
            if source_type == "inject_only":
                return await self.async_step_primary_inject()
            return await self.async_step_primary_action()

        schema = vol.Schema(
            {
                vol.Required("source_type", default="entity_attribute"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["entity_attribute", "entity_action", "inject_only"],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="primary_type", data_schema=schema, errors={})

    async def async_step_primary_inject(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            source = {
                "id": user_input["id"],
                "type": "inject_only",
                "priority": int(user_input.get("priority", 0)),
                "enabled": True,
                "slot_mapping": {
                    "time_key": user_input["time_key"],
                    "price_key": user_input["price_key"],
                },
            }
            return self._finish_create_entry(source)

        schema = vol.Schema(
            {
                vol.Required("id", default="primary"): str,
                vol.Required("priority", default=0): int,
                vol.Required("time_key", default="start_time"): str,
                vol.Required("price_key", default="price_per_kwh"): str,
            }
        )
        return self.async_show_form(step_id="primary_inject", data_schema=schema, errors={})

    async def async_step_primary_attribute(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            source = {
                "id": user_input["id"],
                "type": "entity_attribute",
                "priority": int(user_input.get("priority", 0)),
                "enabled": True,
                "entity_id": user_input["source_entity_id"],
                "attribute": user_input["attribute"],
                "slot_mapping": {
                    "time_key": user_input["time_key"],
                    "price_key": user_input["price_key"],
                },
            }
            return self._finish_create_entry(source)

        schema = vol.Schema(
            {
                vol.Required("id", default="primary"): str,
                vol.Required("priority", default=0): int,
                vol.Required("source_entity_id"): selector.EntitySelector(
                    selector.EntitySelectorConfig()
                ),
                vol.Required("attribute", default="data"): str,
                vol.Required("time_key", default="start_time"): str,
                vol.Required("price_key", default="price_per_kwh"): str,
            }
        )
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
                    "slot_mapping": {
                        "time_key": user_input["time_key"],
                        "price_key": user_input["price_key"],
                    },
                }
                if user_input.get("source_entity_id"):
                    source["entity_id"] = user_input["source_entity_id"]
                return self._finish_create_entry(source)

        schema = vol.Schema(
            {
                vol.Required("id", default="primary"): str,
                vol.Required("priority", default=0): int,
                vol.Required("action", default="tibber.get_prices"): str,
                vol.Required("response_path", default="prices.tibber-home"): str,
                vol.Optional("source_entity_id"): selector.EntitySelector(
                    selector.EntitySelectorConfig()
                ),
                vol.Required("request_payload_json", default="{}"): str,
                vol.Required("inject_time_window", default=True): bool,
                vol.Required("start_key", default="start"): str,
                vol.Required("end_key", default="end"): str,
                vol.Required("time_format", default="%Y-%m-%d %H:%M:%S"): str,
                vol.Required("time_key", default="start_time"): str,
                vol.Required("price_key", default="price"): str,
            }
        )
        return self.async_show_form(step_id="primary_action", data_schema=schema, errors=errors)

    def _finish_create_entry(self, primary_source: dict[str, Any]):
        data = {
            **self._draft,
            CONF_SOURCE_CHAIN: [primary_source],
        }
        title = data[CONF_TIMELINE_NAME]
        return self.async_create_entry(title=title, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ElectricityPriceSuiteOptionsFlow()


class ElectricityPriceSuiteOptionsFlow(config_entries.OptionsFlow):
    """Options flow."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_CURRENCY: user_input[CONF_CURRENCY],
                    CONF_CACHE_RETENTION_DAYS: int(user_input[CONF_CACHE_RETENTION_DAYS]),
                    CONF_ROUND_DECIMALS: int(user_input[CONF_ROUND_DECIMALS]),
                    CONF_ENABLE_CURRENT_PRICE_SENSOR: user_input[CONF_ENABLE_CURRENT_PRICE_SENSOR],
                },
            )

        current = {**self.config_entry.data, **self.config_entry.options}

        schema = vol.Schema(
            {
                vol.Required(CONF_CURRENCY, default=current.get(CONF_CURRENCY, DEFAULT_CURRENCY)): str,
                vol.Required(
                    CONF_CACHE_RETENTION_DAYS,
                    default=current.get(CONF_CACHE_RETENTION_DAYS, DEFAULT_CACHE_RETENTION_DAYS),
                ): int,
                vol.Required(
                    CONF_ROUND_DECIMALS,
                    default=current.get(CONF_ROUND_DECIMALS, DEFAULT_ROUND_DECIMALS),
                ): int,
                vol.Required(
                    CONF_ENABLE_CURRENT_PRICE_SENSOR,
                    default=current.get(CONF_ENABLE_CURRENT_PRICE_SENSOR, DEFAULT_ENABLE_CURRENT_PRICE_SENSOR),
                ): bool,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors={})
