"""The electricity_price_suite integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later

from .const import (
    ATTR_SLOTS,
    DEFAULT_MAX_EXTRA_COST_PERCENT,
    DEFAULT_PREFER_EARLIEST,
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_SOURCE,
    SERVICE_DELETE_SOURCE,
    SERVICE_INJECT_SLOTS,
    SERVICE_LIST_SOURCES,
    SERVICE_MANAGE_PLAN,
    SERVICE_OPTIMIZE_DEVICE,
    SERVICE_REOPTIMIZE_PLAN,
    SERVICE_REFRESH_TIMELINE,
)
from .resolvers import resolve_plan_target, resolve_timeline_runtime
from .runtime import TimelineRuntime

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


REFRESH_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Optional("sources"): [dict],
        vol.Optional("overwrite", default=False): cv.boolean,
    }
)

INJECT_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Required(ATTR_SLOTS): [dict],
        vol.Optional("source_name", default="manual_inject"): cv.string,
        vol.Optional("source_priority", default=9999): vol.Coerce(int),
        vol.Optional("is_primary", default=False): cv.boolean,
        vol.Optional("overwrite", default=False): cv.boolean,
    }
)

OPTIMIZE_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Required("device_name"): cv.string,
        vol.Optional("duration_minutes"): vol.Coerce(float),
        vol.Optional("energy_profile"): [vol.Coerce(float)],
        vol.Optional("profile_slot_minutes"): vol.Coerce(int),
        vol.Optional("billing_slot_minutes"): vol.Coerce(int),
        vol.Optional("consumption_profile_logger", default=False): cv.boolean,
        vol.Optional("consumption_profile_entity"): cv.entity_id,
        vol.Optional("consumption_profile_desired_slot_minutes"): vol.Coerce(int),
        vol.Optional("align_start_to_billing_slot", default=False): cv.boolean,
        vol.Optional("max_extra_cost_percent", default=DEFAULT_MAX_EXTRA_COST_PERCENT): vol.All(
            vol.Coerce(float), vol.Range(min=0)
        ),
        vol.Optional("prefer_earliest", default=DEFAULT_PREFER_EARLIEST): cv.boolean,
        vol.Optional("start_mode", default="now"): vol.In(["now", "in"]),
        vol.Optional("start_in_minutes", default=0.0): vol.Coerce(float),
        vol.Optional("deadline_mode", default="none"): vol.In(["none", "start_within", "finish_within"]),
        vol.Optional("deadline_minutes"): vol.Coerce(float),
        vol.Optional("latest_start"): cv.string,
        vol.Optional("latest_finish"): cv.string,
    }
)

MANAGE_PLAN_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Optional("reset", default=False): cv.boolean,
        vol.Optional("delete", default=False): cv.boolean,
    }
)

REOPTIMIZE_PLAN_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
    }
)

ADD_SOURCE_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Required("id"): cv.string,
        vol.Required("source_type"): vol.In(["entity_attribute", "entity_action"]),
        vol.Optional("priority"): vol.Coerce(int),
        vol.Optional("source_entity_id"): cv.entity_id,
        vol.Optional("attribute"): cv.string,
        vol.Optional("action"): cv.string,
        vol.Optional("response_path"): cv.string,
        vol.Optional("request_payload", default={}): dict,
        vol.Optional("time_key", default="start_time"): cv.string,
        vol.Optional("price_key", default="price_per_kwh"): cv.string,
        vol.Optional("enabled", default=True): cv.boolean,
        vol.Optional("inject_time_window", default=True): cv.boolean,
        vol.Optional("start_key", default="start"): cv.string,
        vol.Optional("end_key", default="end"): cv.string,
        vol.Optional("time_format", default="%Y-%m-%d %H:%M:%S"): cv.string,
    }
)

LIST_SOURCES_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Optional("id"): cv.string,
    }
)

DELETE_SOURCE_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Required("id"): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})

    @callback
    def _write_runtime_entities(runtime: TimelineRuntime) -> None:
        runtime.write_state_entities()

    async def _resolve_runtime(call: ServiceCall) -> TimelineRuntime:
        raw = call.data.get("entity_id")
        if raw is None:
            raise HomeAssistantError("target with one timeline entity is required")
        if isinstance(raw, str):
            entity_ids = [raw]
        else:
            entity_ids = list(raw)
        if len(entity_ids) != 1:
            raise HomeAssistantError("exactly one timeline target entity is required")
        target_entity = entity_ids[0]
        runtime = resolve_timeline_runtime(hass.data[DOMAIN], target_entity)
        if runtime is not None:
            return runtime
        raise HomeAssistantError(f"unknown timeline target: {target_entity}")

    async def handle_refresh(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_runtime(call)
        response = await runtime.async_refresh_timeline(
            override_sources=call.data.get("sources"),
            overwrite=call.data["overwrite"],
        )
        _write_runtime_entities(runtime)
        return response

    async def handle_inject(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_runtime(call)
        response = await runtime.async_inject_slots(
            slots_payload=call.data[ATTR_SLOTS],
            source_name=call.data["source_name"],
            source_priority=call.data["source_priority"],
            is_primary=call.data["is_primary"],
            overwrite=call.data["overwrite"],
        )
        _write_runtime_entities(runtime)
        return response

    async def handle_optimize(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_runtime(call)

        profile_from_logger = bool(call.data.get("consumption_profile_logger"))
        if profile_from_logger and not call.data.get("consumption_profile_entity"):
            raise HomeAssistantError("consumption_profile_entity is required when consumption_profile_logger=true")

        if (
            not profile_from_logger
            and call.data.get("duration_minutes") is None
            and not call.data.get("energy_profile")
        ):
            raise HomeAssistantError(
                "either duration_minutes/energy_profile or consumption_profile_logger=true is required"
            )

        response = await runtime.async_optimize_device(
            device_name=call.data["device_name"],
            duration_minutes=call.data.get("duration_minutes"),
            energy_profile=call.data.get("energy_profile"),
            profile_slot_minutes=call.data.get("profile_slot_minutes"),
            billing_slot_minutes=call.data.get("billing_slot_minutes"),
            consumption_profile_logger=profile_from_logger,
            consumption_profile_entity=call.data.get("consumption_profile_entity"),
            consumption_profile_desired_slot_minutes=call.data.get("consumption_profile_desired_slot_minutes"),
            align_start_to_billing_slot=call.data["align_start_to_billing_slot"],
            max_extra_cost_percent=call.data["max_extra_cost_percent"],
            prefer_earliest=call.data["prefer_earliest"],
            start_mode=call.data["start_mode"],
            start_in_minutes=call.data["start_in_minutes"],
            deadline_mode=call.data["deadline_mode"],
            deadline_minutes=call.data.get("deadline_minutes"),
            latest_start=call.data.get("latest_start"),
            latest_finish=call.data.get("latest_finish"),
        )
        return response

    async def handle_manage_plan(call: ServiceCall) -> dict[str, Any]:
        reset = bool(call.data.get("reset", False))
        delete = bool(call.data.get("delete", False))
        if reset == delete:
            raise HomeAssistantError("exactly one of reset/delete must be true")

        raw = call.data.get("entity_id")
        if raw is None:
            raise HomeAssistantError("target with one or more plan entities is required")
        if isinstance(raw, str):
            target_entities = [raw]
        else:
            target_entities = list(raw)
        if not target_entities:
            raise HomeAssistantError("target with one or more plan entities is required")

        managed: list[dict[str, Any]] = []
        for entity_id in target_entities:
            resolved = resolve_plan_target(hass.data[DOMAIN], entity_id)
            if resolved is None:
                managed.append(
                    {
                        "status": "not_found",
                        "plan_entity_id": entity_id,
                        "reason": "plan_not_found",
                    }
                )
                continue
            runtime, device_slug = resolved
            managed.append(
                await runtime.async_manage_plan(
                    device_slug=device_slug,
                    reset=reset,
                    delete=delete,
                )
            )
        return {"results": managed}

    async def handle_reoptimize_plan(call: ServiceCall) -> dict[str, Any]:
        raw = call.data.get("entity_id")
        if raw is None:
            raise HomeAssistantError("target with one or more plan entities is required")
        if isinstance(raw, str):
            target_entities = [raw]
        else:
            target_entities = list(raw)
        if not target_entities:
            raise HomeAssistantError("target with one or more plan entities is required")

        results: list[dict[str, Any]] = []
        for entity_id in target_entities:
            resolved = resolve_plan_target(hass.data[DOMAIN], entity_id)
            if resolved is None:
                results.append(
                    {
                        "status": "not_found",
                        "plan_entity_id": entity_id,
                        "reason": "plan_not_found",
                    }
                )
                continue
            runtime, device_slug = resolved
            results.append(await runtime.async_reoptimize_plan(device_slug=device_slug))
        return {"results": results}

    async def handle_add_source(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_runtime(call)
        source_type = call.data["source_type"]
        source = {
            "id": call.data["id"],
            "type": source_type,
            "priority": call.data.get("priority", 9999),
            "enabled": call.data["enabled"],
            "slot_mapping": {
                "time_key": call.data["time_key"],
                "price_key": call.data["price_key"],
            },
        }

        if source_type == "entity_attribute":
            if not call.data.get("source_entity_id") or not call.data.get("attribute"):
                raise HomeAssistantError("entity_attribute requires source_entity_id and attribute")
            source["entity_id"] = call.data["source_entity_id"]
            source["attribute"] = call.data["attribute"]
        elif source_type == "entity_action":
            if not call.data.get("action") or not call.data.get("response_path"):
                raise HomeAssistantError("entity_action requires action and response_path")
            source["action"] = call.data["action"]
            source["response_path"] = call.data["response_path"]
            source["request_payload"] = call.data["request_payload"]
            source["inject_time_window"] = call.data["inject_time_window"]
            source["start_key"] = call.data["start_key"]
            source["end_key"] = call.data["end_key"]
            source["time_format"] = call.data["time_format"]
            source["timezone"] = hass.config.time_zone
            if call.data.get("source_entity_id"):
                source["entity_id"] = call.data["source_entity_id"]

        return await runtime.async_add_source(source)

    async def handle_list_sources(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_runtime(call)
        return await runtime.async_list_sources(call.data.get("id"))

    async def handle_delete_source(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_runtime(call)
        return await runtime.async_delete_source(call.data["id"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_TIMELINE,
        handle_refresh,
        schema=REFRESH_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_INJECT_SLOTS,
        handle_inject,
        schema=INJECT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_OPTIMIZE_DEVICE,
        handle_optimize,
        schema=OPTIMIZE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MANAGE_PLAN,
        handle_manage_plan,
        schema=MANAGE_PLAN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REOPTIMIZE_PLAN,
        handle_reoptimize_plan,
        schema=REOPTIMIZE_PLAN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_SOURCE,
        handle_add_source,
        schema=ADD_SOURCE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_SOURCES,
        handle_list_sources,
        schema=LIST_SOURCES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_SOURCE,
        handle_delete_source,
        schema=DELETE_SOURCE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = TimelineRuntime(hass, entry)
    await runtime.async_initialize()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _schedule_initial_refresh(_now):
        hass.async_create_task(_run_initial_refresh())

    async def _run_initial_refresh() -> None:
        # Skip if entry was unloaded before delayed startup refresh.
        if hass.data.get(DOMAIN, {}).get(entry.entry_id) is not runtime:
            return
        try:
            await runtime.async_refresh_timeline(override_sources=None)
            if runtime.timeline_sensor is not None:
                runtime.timeline_sensor.async_write_ha_state()
            if runtime.status_sensor is not None:
                runtime.status_sensor.async_write_ha_state()
            if runtime.current_price_sensor is not None:
                runtime.current_price_sensor.async_write_ha_state()
        except Exception as err:  # pragma: no cover - defensive startup guard
            _LOGGER.warning(
                "initial refresh failed for timeline %s: %s",
                runtime.timeline_slug,
                err,
            )

    async_call_later(hass, 5.0, _schedule_initial_refresh)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = hass.data[DOMAIN].get(entry.entry_id)
    if runtime is not None:
        await runtime.async_shutdown()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
