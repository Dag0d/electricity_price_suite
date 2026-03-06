"""Data models for electricity_price_suite."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


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
    requested_window_end: str | None = None
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

    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
