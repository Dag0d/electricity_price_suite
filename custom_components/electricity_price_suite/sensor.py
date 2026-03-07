"""Sensor entities for electricity_price_suite."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .runtime import TimelineRuntime

_CURRENCY_ICON_CODES = {
    "aud",
    "brl",
    "cad",
    "chf",
    "cny",
    "czk",
    "dkk",
    "eur",
    "gbp",
    "hkd",
    "inr",
    "jpy",
    "nok",
    "nzd",
    "pln",
    "rub",
    "sek",
    "sgd",
    "try",
    "usd",
}


def _currency_mdi_icon(currency_code: str | None) -> str:
    code = str(currency_code or "").strip().lower()
    if code in _CURRENCY_ICON_CODES:
        return f"mdi:currency-{code}"
    return "mdi:currency-usd"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: TimelineRuntime = hass.data[DOMAIN][entry.entry_id]
    runtime.register_add_entities(async_add_entities)
    registry = er.async_get(hass)

    timeline = TimelineSensor(runtime)
    entities: list[SensorEntity] = [timeline]
    status_sensor = TimelineStatusSensor(runtime)
    entities.append(status_sensor)
    runtime.status_sensor = status_sensor

    if runtime.enable_current_price_sensor:
        current_price = CurrentPriceSensor(runtime)
        entities.append(current_price)
        runtime.current_price_sensor = current_price
    else:
        runtime.current_price_sensor = None
        unique_id = f"{runtime.entry.entry_id}_current_price"
        stale_entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if stale_entity_id:
            registry.async_remove(stale_entity_id)

    plan_entities = []
    for device_slug, payload in runtime.store.get_plans().items():
        device_name = payload.get("device_name", device_slug)
        sensor = PlanSensor(runtime, device_slug, device_name, payload)
        runtime.plan_sensors[device_slug] = sensor
        plan_entities.append(sensor)

    async_add_entities([*entities, *plan_entities])
    runtime.timeline_sensor = timeline


class BaseSuiteEntity(SensorEntity):
    """Base for suite entities."""

    _attr_has_entity_name = True

    def __init__(self, runtime: TimelineRuntime) -> None:
        self.runtime = runtime

    @property
    def device_info(self) -> dict[str, Any]:
        return self.runtime.build_device_info()


class TimelineSensor(BaseSuiteEntity):
    """Timeline metrics entity."""

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
    """Per-device optimization plan entity."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-time-three"

    def __init__(
        self,
        runtime: TimelineRuntime,
        device_slug: str,
        device_name: str,
        payload: dict[str, Any] | None,
    ) -> None:
        super().__init__(runtime)
        self._device_slug = device_slug
        self._attr_name = f"Plan {device_name}"
        self._attr_unique_id = f"{runtime.entry.entry_id}_plan_{device_slug}"
        self._payload = payload or {}
        self.entity_id = self.runtime.plan_entity_id(self._device_slug)

    @property
    def native_value(self):
        raw = self._payload.get("best_start")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._payload)

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
    """Current price entity."""

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
        return {
            "start_time": self.runtime.latest_stats.current_price_start_time,
            "currency": self.runtime.currency,
        }


class TimelineStatusSensor(BaseSuiteEntity):
    """High-level timeline status for automation-friendly checks."""

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
            "status_values": [
                "no_data",
                "today_only",
                "tomorrow_only",
                "tomorrow_not_from_prio0",
                "today_and_tomorrow",
            ],
            "today_rows": self.runtime.latest_stats.attributes.get("today_rows", 0),
            "tomorrow_rows": self.runtime.latest_stats.attributes.get("tomorrow_rows", 0),
            "last_source_chain_fetch_at": self.runtime.latest_stats.attributes.get("last_source_chain_fetch_at"),
        }
