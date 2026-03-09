"""Shared profile helpers for electricity_price_suite."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class LoadedProfile:
    """Profile payload normalized for optimizer consumption."""

    energy_profile: list[float]
    runtime_minutes: float
    slot_minutes: int
    profile_meta: dict[str, Any]


@dataclass
class ServiceProfileResult:
    """Normalized response shape for one logger profile service call."""

    ok: bool
    payload: dict[str, Any] | None = None
    reason: str | None = None


def resample_profile_slots(
    slots: list[float],
    from_slot_minutes: int,
    to_slot_minutes: int,
) -> list[float] | None:
    """Resample profile slots if one slot size is a multiple/divisor of the other."""

    if to_slot_minutes <= 0 or from_slot_minutes <= 0:
        return None
    if to_slot_minutes == from_slot_minutes:
        return [float(value) for value in slots]
    if to_slot_minutes > from_slot_minutes:
        if to_slot_minutes % from_slot_minutes != 0:
            return None
        factor = to_slot_minutes // from_slot_minutes
        result: list[float] = []
        for start in range(0, len(slots), factor):
            group = slots[start : start + factor]
            result.append(sum(float(value) for value in group))
        return result
    if from_slot_minutes % to_slot_minutes != 0:
        return None
    factor = from_slot_minutes // to_slot_minutes
    result: list[float] = []
    for value in slots:
        piece = float(value) / float(factor)
        result.extend(piece for _ in range(factor))
    return result


def loaded_profile_from_export(
    payload: dict[str, Any],
    *,
    entity_id: str,
) -> LoadedProfile | None:
    """Convert one exported profile payload into a normalized optimizer profile."""

    slots_kwh = payload.get("slots_kwh")
    if not isinstance(slots_kwh, list) or not slots_kwh:
        return None
    try:
        energy_profile = [float(value) for value in slots_kwh]
    except (TypeError, ValueError):
        return None

    try:
        slot_minutes = int(payload.get("slot_minutes"))
    except (TypeError, ValueError):
        return None
    if slot_minutes <= 0:
        return None

    try:
        runtime_minutes = float(payload.get("runtime_minutes"))
    except (TypeError, ValueError):
        runtime_minutes = float(len(energy_profile) * slot_minutes)

    return LoadedProfile(
        energy_profile=energy_profile,
        runtime_minutes=runtime_minutes,
        slot_minutes=slot_minutes,
        profile_meta={
            "entity_id": entity_id,
            "logger_id": payload.get("logger_id"),
            "logger_name": payload.get("logger_name"),
            "program_key": payload.get("program_key"),
            "program_name": payload.get("program_name"),
            "avg_total_kwh": payload.get("avg_total_kwh"),
            "last_updated": payload.get("last_updated"),
        },
    )


def service_profile_result_from_export(
    payload: dict[str, Any] | None,
    *,
    runtime_data: dict[str, Any] | None,
    desired_slot_minutes: int | None,
) -> ServiceProfileResult:
    """Normalize logger profile service results, including resample errors."""

    if payload is not None:
        return ServiceProfileResult(ok=True, payload={"profile": payload})
    if runtime_data is None:
        return ServiceProfileResult(ok=False, reason="profile_not_found")
    if desired_slot_minutes is not None:
        return ServiceProfileResult(
            ok=False,
            reason="invalid_desired_slot_minutes",
            payload={
                "requested_slot_minutes": desired_slot_minutes,
                "stored_slot_minutes": runtime_data["internal_slot_minutes"],
            },
        )
    return ServiceProfileResult(ok=False, reason="profile_not_found")
