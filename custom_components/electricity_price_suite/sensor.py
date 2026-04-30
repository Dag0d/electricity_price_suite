"""Sensor entities for electricity_price_suite."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, ENTRY_TYPE_PROFILE_LOGGER, ENTRY_TYPE_TIMELINE
from .logger_runtime import ProfileLoggerRuntime
from .runtime import TimelineRuntime
from .time_utils import parse_iso_aware

_CURRENCY_ICON_CODES = {
    "aud", "brl", "cad", "chf", "cny", "czk", "dkk", "eur", "gbp", "hkd", "inr", "jpy", "nok", "nzd", "pln", "rub", "sek", "sgd", "try", "usd",
}

_CONSUMPTION_SENSOR_SPECS: tuple[dict[str, str], ...] = (
    {"key": "consumption_today_kwh", "translation_key": "consumption_today", "kind": "energy"},
    {"key": "consumption_current_hour_kwh", "translation_key": "consumption_current_hour", "kind": "energy"},
    {"key": "consumption_month_kwh", "translation_key": "consumption_month", "kind": "energy"},
    {"key": "consumption_yesterday_kwh", "translation_key": "consumption_yesterday", "kind": "energy"},
    {"key": "cost_today", "translation_key": "cost_today", "kind": "money"},
    {"key": "cost_month", "translation_key": "cost_month", "kind": "money"},
    {"key": "cost_yesterday", "translation_key": "cost_yesterday", "kind": "money"},
    {"key": "cost_today_incl_basic_fee", "translation_key": "cost_today_incl_basic_fee", "kind": "money"},
    {"key": "cost_month_incl_basic_fee", "translation_key": "cost_month_incl_basic_fee", "kind": "money"},
    {"key": "cost_yesterday_incl_basic_fee", "translation_key": "cost_yesterday_incl_basic_fee", "kind": "money"},
    {"key": "avg_paid_price_today", "translation_key": "avg_paid_price_today", "kind": "price"},
    {"key": "avg_paid_price_month", "translation_key": "avg_paid_price_month", "kind": "price"},
    {"key": "avg_paid_price_yesterday", "translation_key": "avg_paid_price_yesterday", "kind": "price"},
    {"key": "avg_paid_price_last_month", "translation_key": "avg_paid_price_last_month", "kind": "price"},
    {"key": "cost_last_month", "translation_key": "cost_last_month", "kind": "money"},
    {"key": "cost_last_month_incl_basic_fee", "translation_key": "cost_last_month_incl_basic_fee", "kind": "money"},
)


def _currency_mdi_icon(currency_code: str | None) -> str:
    code = str(currency_code or "").strip().lower()
    return f"mdi:currency-{code}" if code in _CURRENCY_ICON_CODES else "mdi:currency-usd"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    if entry.data.get("entry_type", ENTRY_TYPE_TIMELINE) == ENTRY_TYPE_PROFILE_LOGGER:
        await _async_setup_logger_entry(runtime, async_add_entities)
        return
    await _async_setup_timeline_entry(runtime, async_add_entities)


async def _async_setup_timeline_entry(runtime: TimelineRuntime, async_add_entities: AddEntitiesCallback) -> None:
    runtime.register_add_entities(async_add_entities)
    registry = er.async_get(runtime.hass)
    timeline = TimelineSensor(runtime)
    entities: list[SensorEntity] = [timeline]
    status_sensor = TimelineStatusSensor(runtime)
    entities.append(status_sensor)
    runtime.status_sensor = status_sensor
    current_price = CurrentPriceSensor(runtime)
    entities.append(current_price)
    runtime.current_price_sensor = current_price
    current_market_price = CurrentMarketPriceSensor(runtime)
    entities.append(current_market_price)
    runtime.current_market_price_sensor = current_market_price
    if runtime.has_consumption_tracking:
        for spec in _CONSUMPTION_SENSOR_SPECS:
            sensor = TimelineConsumptionSensor(runtime, spec["key"], spec["translation_key"], spec["kind"])
            runtime.consumption_sensors[spec["key"]] = sensor
            entities.append(sensor)
    else:
        for spec in _CONSUMPTION_SENSOR_SPECS:
            unique_id = f"{runtime.entry.entry_id}_{spec['key']}"
            stale_entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if stale_entity_id:
                registry.async_remove(stale_entity_id)
    plan_entities: list[SensorEntity] = []
    for plan_key, payload in runtime.store.get_plans().items():
        sensor = PlanSensor(runtime, plan_key, payload)
        runtime.plan_sensors[plan_key] = sensor
        plan_entities.append(sensor)
    async_add_entities([*entities, *plan_entities])
    runtime.timeline_sensor = timeline


async def _async_setup_logger_entry(runtime: ProfileLoggerRuntime, async_add_entities: AddEntitiesCallback) -> None:
    created_programs: set[str] = set()
    program_entities: dict[str, LoggerProfileSensor] = {}
    entities: list[SensorEntity] = [LoggerMetaSensor(runtime)]
    for program_key in runtime.program_keys:
        entity = LoggerProfileSensor(runtime, program_key)
        entities.append(entity)
        program_entities[program_key] = entity
        created_programs.add(program_key)
    async_add_entities(entities)

    @callback
    def async_handle_new_program(program_key: str) -> None:
        if program_key in created_programs:
            return
        created_programs.add(program_key)
        entity = LoggerProfileSensor(runtime, program_key)
        program_entities[program_key] = entity
        async_add_entities([entity])

    @callback
    def async_handle_removed_program(program_key: str) -> None:
        created_programs.discard(program_key)
        entity = program_entities.pop(program_key, None)
        if entity is not None:
            runtime.hass.async_create_task(entity.async_remove())

    runtime.entry.async_on_unload(runtime.add_program_listener(async_handle_new_program))
    runtime.entry.async_on_unload(runtime.add_program_removed_listener(async_handle_removed_program))


class BaseSuiteEntity(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, runtime: TimelineRuntime) -> None:
        self.runtime = runtime

    @property
    def device_info(self) -> dict[str, Any]:
        return self.runtime.build_device_info()


class TimelineSensor(BaseSuiteEntity):
    _attr_should_poll = False
    _attr_translation_key = "pricing_meta"
    _attr_icon = "mdi:cash"

    def __init__(self, runtime: TimelineRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry.entry_id}_pricing_meta"
        self._attr_native_unit_of_measurement = f"{runtime.currency}/kWh"
        self.entity_id = runtime.timeline_entity_id

    @property
    def native_value(self):
        return self.runtime.latest_stats.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.runtime.latest_stats.attributes


class PlanSensor(BaseSuiteEntity, RestoreEntity):
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-time-three"

    def __init__(self, runtime: TimelineRuntime, plan_key: str, payload: dict[str, Any] | None) -> None:
        super().__init__(runtime)
        self._plan_key = plan_key
        self._payload = payload or {}
        device_name = str(self._payload.get("device_name", ""))
        planner_slug = str(self._payload.get("planner_slug", "planner"))
        device_slug = str(self._payload.get("device_slug", "device"))
        self._attr_name = device_name
        self._attr_unique_id = f"{runtime.entry.entry_id}_plan_{plan_key}"
        self.entity_id = self.runtime.plan_entity_id(planner_slug, device_slug)

    @property
    def native_value(self):
        raw = self._payload.get("best_start")
        if not raw:
            return None
        return parse_iso_aware(raw)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._payload)

    @property
    def device_info(self) -> dict[str, Any]:
        planner_name = str(self._payload.get("planner_name", ""))
        planner_slug = str(self._payload.get("planner_slug", ""))
        return self.runtime.build_plan_device_info(planner_name, planner_slug)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._payload:
            return
        if old_state := await self.async_get_last_state():
            attrs = dict(old_state.attributes)
            if attrs:
                self._payload = attrs

    def async_update_from_payload(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.async_write_ha_state()


class CurrentPriceSensor(BaseSuiteEntity):
    _attr_should_poll = False
    _attr_translation_key = "current_price"

    def __init__(self, runtime: TimelineRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry.entry_id}_current_price"
        self.entity_id = f"sensor.{runtime.timeline_slug}_current_price"
        self._attr_native_unit_of_measurement = f"{runtime.currency}/kWh"

    @property
    def icon(self) -> str:
        return _currency_mdi_icon(self.runtime.currency)

    @property
    def native_value(self):
        return self.runtime.latest_stats.current_price

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"start_time": self.runtime.latest_stats.current_price_start_time, "currency": self.runtime.currency}


class CurrentMarketPriceSensor(BaseSuiteEntity):
    _attr_should_poll = False
    _attr_translation_key = "current_market_price"

    def __init__(self, runtime: TimelineRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry.entry_id}_current_market_price"
        self.entity_id = f"sensor.{runtime.timeline_slug}_current_market_price"
        self._attr_native_unit_of_measurement = f"{runtime.currency}/kWh"

    @property
    def icon(self) -> str:
        return "mdi:chart-line"

    @property
    def native_value(self):
        return self.runtime.latest_stats.current_market_price

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "start_time": self.runtime.latest_stats.current_market_price_start_time,
            "currency": self.runtime.currency,
        }


class TimelineConsumptionSensor(BaseSuiteEntity):
    _attr_should_poll = False
    _attr_suggested_display_precision = 4

    def __init__(self, runtime: TimelineRuntime, metric_key: str, translation_key: str, kind: str) -> None:
        super().__init__(runtime)
        self.metric_key = metric_key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{runtime.entry.entry_id}_{metric_key}"
        self.entity_id = runtime.consumption_entity_id(metric_key)
        if kind == "energy":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_icon = "mdi:lightning-bolt"
        elif kind == "money":
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_native_unit_of_measurement = runtime.currency
            self._attr_icon = _currency_mdi_icon(runtime.currency)
        else:
            self._attr_native_unit_of_measurement = f"{runtime.currency}/kWh"
            self._attr_icon = _currency_mdi_icon(runtime.currency)

    @property
    def native_value(self):
        return self.runtime.latest_consumption_metrics.get(self.metric_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {
            "last_updated": self.runtime.latest_consumption_metrics.get("last_updated"),
            "consumption_energy_entity": self.runtime.latest_consumption_metrics.get("consumption_energy_entity"),
        }
        if self.metric_key in {
            "avg_paid_price_today",
            "avg_paid_price_yesterday",
            "avg_paid_price_month",
            "avg_paid_price_last_month",
        }:
            attrs["avg_price_include_basic_fee"] = self.runtime.latest_consumption_metrics.get(
                "avg_price_include_basic_fee"
            )
        if self.metric_key in {
            "cost_today_incl_basic_fee",
            "cost_month_incl_basic_fee",
            "cost_yesterday_incl_basic_fee",
            "cost_last_month_incl_basic_fee",
        }:
            attrs["fixed_fee_monthly_amount"] = self.runtime.latest_consumption_metrics.get(
                "fixed_fee_monthly_amount"
            )
            attrs["fixed_fee_daily_amount"] = self.runtime.latest_consumption_metrics.get(
                "fixed_fee_daily_amount"
            )
            attrs["fixed_fee_tax_percent"] = self.runtime.latest_consumption_metrics.get(
                "fixed_fee_tax_percent"
            )
            attrs["fixed_fee_values_include_tax"] = self.runtime.latest_consumption_metrics.get(
                "fixed_fee_values_include_tax"
            )
            attrs["current_month_fixed_fee_mode"] = self.runtime.latest_consumption_metrics.get(
                "current_month_fixed_fee_mode"
            )
        return attrs


class TimelineStatusSensor(BaseSuiteEntity):
    _attr_should_poll = False
    _attr_translation_key = "status"
    _attr_icon = "mdi:information"

    def __init__(self, runtime: TimelineRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry.entry_id}_status"
        self.entity_id = runtime.status_entity_id

    @property
    def native_value(self):
        return self.runtime.latest_stats.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "status_values": ["no_data", "today_only", "tomorrow_only", "tomorrow_not_from_provider_1", "today_and_tomorrow"],
            "today_rows": self.runtime.latest_stats.attributes.get("today_rows", 0),
            "tomorrow_rows": self.runtime.latest_stats.attributes.get("tomorrow_rows", 0),
            "last_source_chain_fetch_at": self.runtime.latest_stats.attributes.get("last_source_chain_fetch_at"),
        }


class LoggerBaseSensor(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, runtime: ProfileLoggerRuntime) -> None:
        self.runtime = runtime
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.runtime.add_state_listener(self._handle_runtime_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.runtime.entry.entry_id)},
            name=self.runtime.name,
            manufacturer="Electricity Price Suite",
            model="Consumption Profile Logger",
            entry_type=DeviceEntryType.SERVICE,
        )


class LoggerMetaSensor(LoggerBaseSensor):
    _attr_should_poll = False
    _attr_icon = "mdi:chart-timeline-variant-shimmer"

    def __init__(self, runtime: ProfileLoggerRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry.entry_id}_profile_logger_meta"
        self._attr_name = None
        self.entity_id = runtime.meta_entity_id

    @property
    def native_value(self) -> str:
        return self.runtime.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.runtime.state_attributes


class LoggerProfileSensor(LoggerBaseSensor):
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 5
    _attr_icon = "mdi:chart-bar-stacked"
    _attr_should_poll = False

    def __init__(self, runtime: ProfileLoggerRuntime, program_key: str) -> None:
        super().__init__(runtime)
        self.program_key = program_key
        self._attr_unique_id = f"{runtime.entry.entry_id}_{program_key}_profile"
        summary = runtime.get_profile_sensor_payload(program_key) or {"program_name": program_key}
        self._attr_name = summary["program_name"]
        self.entity_id = runtime.profile_entity_id(program_key)

    @property
    def native_value(self) -> float | None:
        summary = self.runtime.get_profile_sensor_payload(self.program_key)
        if summary is None:
            return None
        return summary["avg_total_kwh"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self.runtime.get_profile_sensor_payload(self.program_key)
        if summary is None:
            return {}
        return summary
