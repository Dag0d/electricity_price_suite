"""Data models for electricity_price_suite."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, TypedDict

from homeassistant.util import dt as dt_util


@dataclass(slots=True)
class SlotRecord:
    """Normalized slot entry persisted in internal store."""

    start_time: str
    price_per_kwh: float
    source_id: str
    source_priority: int
    is_primary_source: bool
    observed_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class SlotRow(TypedDict):
    """Normalized slot row shape used across store/runtime boundaries."""

    start_time: str
    price_per_kwh: float
    source_id: str
    source_priority: int
    is_primary_source: bool
    observed_at: str


class SourceConfig(TypedDict, total=False):
    """Normalized source config stored by the integration."""

    id: str
    type: str
    priority: int
    enabled: bool
    entity_id: str
    attribute: str
    action: str
    response_path: str
    request_payload: dict[str, Any]
    inject_time_window: bool
    start_key: str
    end_key: str
    time_format: str
    timezone: str
    slot_mapping: dict[str, str]


class PlanPayload(TypedDict):
    """Persisted plan payload for one plan entity."""

    device_name: str
    status: str
    reason: str | None
    best_start: str | None
    best_end: str | None
    best_cost: float | None
    window_start: str | None
    window_end: str | None
    deadline_mode: str
    deadline_minutes: float | None
    latest_start: str | None
    latest_finish: str | None
    duration_minutes: float | None
    billing_slot_minutes: int | None
    profile_slot_minutes: int | None
    max_extra_cost_percent: float | None
    prefer_earliest: bool | None
    align_start_to_billing_slot: bool | None
    candidates: int
    profile_used: list[float]
    profile_source: str
    profile_meta: dict[str, Any] | None
    requested_latest_start: str | None
    window_truncated_by_data: bool
    price_coverage_end_at_compute: str | None
    computed_at: str
    timeline_entity: str


@dataclass(slots=True)
class TimelineStats:
    """Computed sensor state snapshot for one timeline."""

    state: float | None
    attributes: dict[str, Any]
    current_price: float | None
    current_price_start_time: str | None
    status: str


@dataclass(slots=True)
class PlanResult:
    """Result of a runtime optimization run."""

    status: str
    best_start: str | None
    best_end: str | None
    best_cost: float | None
    reason: str | None
    candidates: int
    profile_used: list[float]
    window_start: str
    window_end: str
    duration_minutes: float | None
    billing_slot_minutes: int
    profile_slot_minutes: int
    requested_latest_start: str | None = None
    window_truncated_by_data: bool = False
    price_coverage_end: str | None = None


@dataclass(slots=True)
class SourceAttempt:
    """Provider execution attempt details."""

    source_id: str
    source_type: str
    success: bool
    rows: int
    reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def utc_now_iso() -> str:
    """Return UTC timestamp in ISO format."""

    return dt_util.utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")
