"""Plan-related helpers for electricity_price_suite."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .const import DEFAULT_BILLING_SLOT_MINUTES
from .logger_runtime import ProfileLoggerRuntime
from .models import PlanPayload, PlanResult
from .optimizer import optimize_runtime
from .profile_utils import loaded_profile_from_export
from .time_utils import format_iso


def build_no_candidate_result(timezone_name: str, reason: str) -> PlanResult:
    """Create a minimal no-candidate result for failed upstream profile loading."""

    now = format_iso(datetime.now(ZoneInfo(timezone_name)), timespec="minutes")
    return PlanResult(
        status="no-candidate",
        best_start=None,
        best_end=None,
        best_cost=None,
        reason=reason,
        candidates=0,
        profile_used=[],
        window_start=now,
        window_end=now,
        duration_minutes=None,
        billing_slot_minutes=DEFAULT_BILLING_SLOT_MINUTES,
        profile_slot_minutes=DEFAULT_BILLING_SLOT_MINUTES,
    )


def build_reset_payload(device_name: str, timeline_entity_id: str, timezone_name: str) -> PlanPayload:
    """Create the reset payload for a persisted plan entity."""

    return {
        "device_name": device_name,
        "status": "reset",
        "reason": "manual_reset",
        "best_start": None,
        "best_end": None,
        "best_cost": None,
        "window_start": None,
        "window_end": None,
        "deadline_mode": "none",
        "deadline_minutes": None,
        "latest_start": None,
        "latest_finish": None,
        "duration_minutes": None,
        "billing_slot_minutes": None,
        "profile_slot_minutes": None,
        "max_extra_cost_percent": None,
        "prefer_earliest": None,
        "align_start_to_billing_slot": None,
        "candidates": 0,
        "profile_used": [],
        "profile_source": "reset",
        "profile_meta": None,
        "requested_latest_start": None,
        "window_truncated_by_data": False,
        "price_coverage_end_at_compute": None,
        "computed_at": format_iso(datetime.now(ZoneInfo(timezone_name)), timespec="seconds"),
        "timeline_entity": timeline_entity_id,
    }


def build_plan_payload(
    *,
    device_name: str,
    result: PlanResult,
    deadline_mode: str,
    deadline_minutes: float | None,
    latest_start: str | None,
    latest_finish: str | None,
    max_extra_cost_percent: float,
    prefer_earliest: bool,
    align_start_to_billing_slot: bool,
    profile_source: str,
    profile_meta: dict[str, Any] | None,
    timeline_entity_id: str,
    timezone_name: str,
) -> PlanPayload:
    """Build the persisted plan payload from an optimization result."""

    return {
        "device_name": device_name,
        "status": result.status,
        "reason": result.reason,
        "best_start": result.best_start,
        "best_end": result.best_end,
        "best_cost": result.best_cost,
        "window_start": result.window_start,
        "window_end": result.window_end,
        "deadline_mode": deadline_mode,
        "deadline_minutes": deadline_minutes,
        "latest_start": latest_start,
        "latest_finish": latest_finish,
        "duration_minutes": result.duration_minutes,
        "billing_slot_minutes": result.billing_slot_minutes,
        "profile_slot_minutes": result.profile_slot_minutes,
        "max_extra_cost_percent": max_extra_cost_percent,
        "prefer_earliest": prefer_earliest,
        "align_start_to_billing_slot": align_start_to_billing_slot,
        "candidates": result.candidates,
        "profile_used": result.profile_used,
        "profile_source": profile_source,
        "profile_meta": profile_meta,
        "requested_latest_start": result.requested_latest_start,
        "window_truncated_by_data": result.window_truncated_by_data,
        "price_coverage_end_at_compute": result.price_coverage_end,
        "computed_at": format_iso(datetime.now(ZoneInfo(timezone_name)), timespec="seconds"),
        "timeline_entity": timeline_entity_id,
    }


def load_profile_logger_profile(
    logger_runtime: ProfileLoggerRuntime,
    *,
    profile_logger_entity: str,
    program_key: str | None,
) -> tuple[list[float] | None, float | None, int | None, dict[str, Any] | None, str | None]:
    """Load one profile directly from an internal logger runtime."""

    if not program_key:
        return None, None, None, None, "missing_program_key"

    payload = logger_runtime.get_profile_export(program_key)
    if payload is None:
        runtime_data = logger_runtime.get_profile_runtime_data(program_key)
        if runtime_data is None:
            return None, None, None, None, "profile_not_found"
        return None, None, None, None, "profile_export_unavailable"
    loaded = loaded_profile_from_export(payload, entity_id=profile_logger_entity)
    if loaded is None:
        if not isinstance(payload.get("slots_kwh"), list) or not payload.get("slots_kwh"):
            return None, None, None, None, "profile_missing_slots"
        try:
            slot_minutes = int(payload.get("slot_minutes"))
        except (TypeError, ValueError):
            slot_minutes = None
        if slot_minutes is None or slot_minutes <= 0:
            return None, None, None, None, "profile_invalid_slot_minutes"
        return None, None, None, None, "profile_invalid_slots"
    return (
        loaded.energy_profile,
        loaded.runtime_minutes,
        loaded.slot_minutes,
        loaded.profile_meta,
        None,
    )


def reoptimize_plan_payload(
    *,
    slots: list[dict[str, float | str]],
    payload: PlanPayload,
    timezone_name: str,
    duration_minutes: float | None = None,
    energy_profile: list[float] | None = None,
    profile_slot_minutes: int | None = None,
) -> PlanResult:
    """Re-run optimization for an existing plan payload when price coverage expands."""

    return optimize_runtime(
        slots=slots,
        timezone_name=timezone_name,
        billing_slot_minutes=int(payload.get("billing_slot_minutes") or DEFAULT_BILLING_SLOT_MINUTES),
        duration_minutes=(
            duration_minutes
            if duration_minutes is not None
            else (float(payload["duration_minutes"]) if payload.get("duration_minutes") is not None else None)
        ),
        energy_profile=(
            energy_profile
            if energy_profile is not None
            else [float(value) for value in payload.get("profile_used", [])]
        ),
        profile_slot_minutes=(
            int(profile_slot_minutes)
            if profile_slot_minutes is not None
            else int(payload.get("profile_slot_minutes") or DEFAULT_BILLING_SLOT_MINUTES)
        ),
        max_extra_cost_percent=float(payload.get("max_extra_cost_percent") or 1.0),
        prefer_earliest=bool(payload.get("prefer_earliest", True)),
        start_mode="now",
        start_in_minutes=0.0,
        deadline_mode="none",
        deadline_minutes=None,
        latest_start=payload.get("requested_latest_start"),
        latest_finish=None,
        align_start_to_billing_slot=bool(payload.get("align_start_to_billing_slot", False)),
        reference_time=None,
    )
