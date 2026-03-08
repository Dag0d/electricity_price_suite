"""Shared validation helpers for electricity_price_suite."""

from __future__ import annotations

from typing import Any

from homeassistant.const import UnitOfEnergy

from .logger_utils import normalize_program_key


def parse_program_list(raw: Any) -> list[str]:
    """Parse allow/block program lists from selectors or comma-separated text."""

    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
    else:
        values = [item.strip() for item in str(raw).split(",") if item.strip()]
    normalized = [normalize_program_key(item) for item in values]
    return [item for item in normalized if item]


def validate_energy_entity(hass, entity_id: str) -> str | None:
    """Validate a total_increasing energy entity for logger usage."""

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
