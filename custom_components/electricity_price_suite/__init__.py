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
    ABORT_REASON_MANUAL,
    ALLOWED_ABORT_REASONS,
    ATTR_SLOTS,
    CONF_ENTRY_TYPE,
    DEFAULT_MAX_EXTRA_COST_PERCENT,
    DEFAULT_PREFER_EARLIEST,
    DOMAIN,
    ENTRY_TYPE_PROFILE_LOGGER,
    ENTRY_TYPE_TIMELINE,
    PLATFORMS,
    SERVICE_INJECT_SLOTS,
    SERVICE_MANAGE_PROFILE,
    SERVICE_MANAGE_PLAN,
    SERVICE_MANAGE_PROFILE_RUN,
    SERVICE_MANAGE_SOURCES,
    SERVICE_OPTIMIZE_DEVICE,
    SERVICE_REFRESH_TIMELINE,
)
from .logger_runtime import ProfileLoggerRuntime
from .resolvers import resolve_logger_runtime, resolve_plan_target, resolve_timeline_runtime
from .runtime import TimelineRuntime

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REFRESH_SCHEMA = vol.Schema({**cv.TARGET_SERVICE_FIELDS, vol.Optional("sources"): [dict], vol.Optional("overwrite", default=False): cv.boolean})
INJECT_SCHEMA = vol.Schema({**cv.TARGET_SERVICE_FIELDS, vol.Required(ATTR_SLOTS): [dict], vol.Optional("source_name", default="manual_inject"): cv.string, vol.Optional("source_priority", default=9999): vol.Coerce(int), vol.Optional("is_primary", default=False): cv.boolean, vol.Optional("overwrite", default=False): cv.boolean})
OPTIMIZE_SCHEMA = vol.Schema({
    **cv.TARGET_SERVICE_FIELDS,
    vol.Required("device_name"): cv.string,
    vol.Optional("duration_minutes"): vol.Coerce(float),
    vol.Optional("energy_profile"): [vol.Coerce(float)],
    vol.Optional("profile_slot_minutes"): vol.Coerce(int),
    vol.Optional("billing_slot_minutes"): vol.Coerce(int),
    vol.Optional("profile_logger_entity"): cv.entity_id,
    vol.Optional("program_key"): cv.string,
    vol.Optional("program_display_name"): cv.string,
    vol.Optional("align_start_to_billing_slot", default=False): cv.boolean,
    vol.Optional("max_extra_cost_percent", default=DEFAULT_MAX_EXTRA_COST_PERCENT): vol.All(vol.Coerce(float), vol.Range(min=0)),
    vol.Optional("prefer_earliest", default=DEFAULT_PREFER_EARLIEST): cv.boolean,
    vol.Optional("start_mode", default="now"): vol.In(["now", "in"]),
    vol.Optional("start_in_minutes", default=0.0): vol.Coerce(float),
    vol.Optional("deadline_mode", default="none"): vol.In(["none", "start_within", "finish_within"]),
    vol.Optional("deadline_minutes"): vol.Coerce(float),
    vol.Optional("latest_start"): cv.string,
    vol.Optional("latest_finish"): cv.string,
})
MANAGE_PLAN_SCHEMA = vol.Schema({**cv.TARGET_SERVICE_FIELDS, vol.Required("mode"): vol.In(["reset", "delete", "reoptimize"])})
MANAGE_SOURCES_SCHEMA = vol.Schema({
    **cv.TARGET_SERVICE_FIELDS,
    vol.Required("mode"): vol.In(["add", "list", "delete"]),
    vol.Optional("id"): cv.string,
    vol.Optional("source_type"): vol.In(["entity_attribute", "entity_action"]),
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
})
LOGGER_MANAGE_RUN_SCHEMA = vol.Schema({
    **cv.TARGET_SERVICE_FIELDS,
    vol.Required("mode"): vol.In(["start", "finish", "abort"]),
    vol.Optional("reason", default=ABORT_REASON_MANUAL): vol.In(ALLOWED_ABORT_REASONS),
    vol.Optional("program_key"): cv.string,
})
LOGGER_MANAGE_PROFILE_SCHEMA = vol.Schema({
    **cv.TARGET_SERVICE_FIELDS,
    vol.Required("mode"): vol.In([
        "get",
        "reset",
        "delete",
        "add_estimated_runtimes",
        "list_estimated_runtimes",
        "delete_estimated_runtime",
        "clear_estimated_runtimes",
    ]),
    vol.Optional("items"): dict,
    vol.Optional("program_key"): cv.string,
    vol.Optional("desired_slot_minutes"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("debug", default=False): cv.boolean,
})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})

    @callback
    def _write_timeline_entities(runtime: TimelineRuntime) -> None:
        runtime.write_state_entities()

    async def _resolve_timeline(call: ServiceCall) -> TimelineRuntime:
        raw = call.data.get("entity_id")
        if raw is None:
            raise HomeAssistantError("target with one timeline entity is required")
        entity_ids = [raw] if isinstance(raw, str) else list(raw)
        if len(entity_ids) != 1:
            raise HomeAssistantError("exactly one timeline target entity is required")
        runtime = resolve_timeline_runtime(hass.data[DOMAIN], entity_ids[0])
        if runtime is None:
            raise HomeAssistantError(f"unknown timeline target: {entity_ids[0]}")
        return runtime

    async def _resolve_logger(call: ServiceCall) -> tuple[ProfileLoggerRuntime, str | None]:
        raw = call.data.get("entity_id")
        if raw is None:
            raise HomeAssistantError("target with one logger entity is required")
        entity_ids = [raw] if isinstance(raw, str) else list(raw)
        if len(entity_ids) != 1:
            raise HomeAssistantError("exactly one logger target entity is required")
        runtime, implicit_program_key = resolve_logger_runtime(hass.data[DOMAIN], entity_ids[0])
        if runtime is None:
            raise HomeAssistantError(f"unknown logger target: {entity_ids[0]}")
        return runtime, implicit_program_key

    async def handle_refresh(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_timeline(call)
        response = await runtime.async_refresh_timeline(override_sources=call.data.get("sources"), overwrite=call.data["overwrite"])
        _write_timeline_entities(runtime)
        return response

    async def handle_inject(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_timeline(call)
        response = await runtime.async_inject_slots(
            slots_payload=call.data[ATTR_SLOTS],
            source_name=call.data["source_name"],
            source_priority=call.data["source_priority"],
            is_primary=call.data["is_primary"],
            overwrite=call.data["overwrite"],
        )
        _write_timeline_entities(runtime)
        return response

    async def handle_optimize(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_timeline(call)
        profile_logger_entity = call.data.get("profile_logger_entity")
        program_key = call.data.get("program_key")
        if profile_logger_entity and not program_key:
            raise HomeAssistantError("program_key is required when profile_logger_entity is set")
        if not profile_logger_entity and call.data.get("duration_minutes") is None and not call.data.get("energy_profile"):
            raise HomeAssistantError("either duration_minutes/energy_profile or profile_logger_entity + program_key is required")
        response = await runtime.async_optimize_device(
            device_name=call.data["device_name"],
            duration_minutes=call.data.get("duration_minutes"),
            energy_profile=call.data.get("energy_profile"),
            profile_slot_minutes=call.data.get("profile_slot_minutes"),
            billing_slot_minutes=call.data.get("billing_slot_minutes"),
            profile_logger_entity=profile_logger_entity,
            program_key=program_key,
            program_display_name=call.data.get("program_display_name"),
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
        mode = call.data["mode"]
        raw = call.data.get("entity_id")
        if raw is None:
            raise HomeAssistantError("target with one or more plan entities is required")
        target_entities = [raw] if isinstance(raw, str) else list(raw)
        if not target_entities:
            raise HomeAssistantError("target with one or more plan entities is required")
        managed: list[dict[str, Any]] = []
        for entity_id in target_entities:
            resolved = resolve_plan_target(hass.data[DOMAIN], entity_id)
            if resolved is None:
                managed.append({"status": "not_found", "plan_entity_id": entity_id, "reason": "plan_not_found"})
                continue
            runtime, device_slug = resolved
            if mode == "reoptimize":
                managed.append(await runtime.async_reoptimize_plan(device_slug=device_slug))
            else:
                managed.append(
                    await runtime.async_manage_plan(
                        device_slug=device_slug,
                        reset=(mode == "reset"),
                        delete=(mode == "delete"),
                    )
                )
        return {"results": managed}

    async def handle_manage_sources(call: ServiceCall) -> dict[str, Any]:
        runtime = await _resolve_timeline(call)
        mode = call.data["mode"]
        if mode == "list":
            return await runtime.async_list_sources(call.data.get("id"))
        if mode == "delete":
            if not call.data.get("id"):
                raise HomeAssistantError("id is required for mode=delete")
            return await runtime.async_delete_source(call.data["id"])
        if not call.data.get("id"):
            raise HomeAssistantError("id is required for mode=add")
        if not call.data.get("source_type"):
            raise HomeAssistantError("source_type is required for mode=add")
        source_type = call.data["source_type"]
        source = {"id": call.data["id"], "type": source_type, "priority": call.data.get("priority", 9999), "enabled": call.data["enabled"], "slot_mapping": {"time_key": call.data["time_key"], "price_key": call.data["price_key"]}}
        if source_type == "entity_attribute":
            if not call.data.get("source_entity_id") or not call.data.get("attribute"):
                raise HomeAssistantError("entity_attribute requires source_entity_id and attribute")
            source["entity_id"] = call.data["source_entity_id"]
            source["attribute"] = call.data["attribute"]
        else:
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

    async def handle_manage_profile_run(call: ServiceCall) -> dict[str, Any]:
        runtime, implicit_program_key = await _resolve_logger(call)
        mode = call.data["mode"]
        program_key = call.data.get("program_key") or implicit_program_key
        if mode == "start":
            result = await runtime.async_start(program_key)
        elif mode == "finish":
            result = await runtime.async_finish(program_key)
        elif mode == "abort":
            result = await runtime.async_abort(call.data.get("reason", ABORT_REASON_MANUAL), program_key)
        else:
            raise HomeAssistantError(f"unsupported profile run mode: {mode}")
        return result.as_dict()

    async def handle_manage_profile(call: ServiceCall) -> dict[str, Any]:
        runtime, implicit_program_key = await _resolve_logger(call)
        mode = call.data["mode"]
        program_key = call.data.get("program_key") or implicit_program_key
        if mode == "get":
            if program_key is None:
                return {"ok": True, "programs": runtime.get_program_list()}
            return runtime.get_profile_service_response(
                program_key,
                desired_slot_minutes=call.data.get("desired_slot_minutes"),
                debug=bool(call.data.get("debug", False)),
            )
        if mode == "reset":
            result = await runtime.async_reset_profile(program_key)
            return result.as_dict()
        if mode == "delete":
            result = await runtime.async_delete_profile(program_key)
            return result.as_dict()
        if mode == "add_estimated_runtimes":
            if not call.data.get("items"):
                raise HomeAssistantError("items is required for mode=add_estimated_runtimes")
            return await runtime.async_manage_estimated_runtime(mode="add", items=call.data.get("items"))
        if mode == "list_estimated_runtimes":
            return await runtime.async_manage_estimated_runtime(mode="list")
        if mode == "delete_estimated_runtime":
            if not program_key:
                raise HomeAssistantError("program_key is required for mode=delete_estimated_runtime")
            return await runtime.async_manage_estimated_runtime(mode="delete", program_key=program_key)
        if mode == "clear_estimated_runtimes":
            return await runtime.async_manage_estimated_runtime(mode="clear")
        raise HomeAssistantError(f"unsupported profile mode: {mode}")

    service_defs = [
        (SERVICE_REFRESH_TIMELINE, handle_refresh, REFRESH_SCHEMA),
        (SERVICE_INJECT_SLOTS, handle_inject, INJECT_SCHEMA),
        (SERVICE_OPTIMIZE_DEVICE, handle_optimize, OPTIMIZE_SCHEMA),
        (SERVICE_MANAGE_PLAN, handle_manage_plan, MANAGE_PLAN_SCHEMA),
        (SERVICE_MANAGE_SOURCES, handle_manage_sources, MANAGE_SOURCES_SCHEMA),
        (SERVICE_MANAGE_PROFILE_RUN, handle_manage_profile_run, LOGGER_MANAGE_RUN_SCHEMA),
        (SERVICE_MANAGE_PROFILE, handle_manage_profile, LOGGER_MANAGE_PROFILE_SCHEMA),
    ]
    for name, func, schema in service_defs:
        hass.services.async_register(DOMAIN, name, func, schema=schema, supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_TIMELINE)
    if entry_type == ENTRY_TYPE_PROFILE_LOGGER:
        runtime: Any = ProfileLoggerRuntime(hass, entry)
        await runtime.async_initialize()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    runtime = TimelineRuntime(hass, entry)
    await runtime.async_initialize()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _schedule_initial_refresh(_now):
        hass.async_create_task(_run_initial_refresh())

    async def _run_initial_refresh() -> None:
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
        except Exception as err:
            _LOGGER.warning("initial refresh failed for timeline %s: %s", runtime.timeline_slug, err)

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
