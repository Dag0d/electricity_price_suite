"""Runtime optimization engine."""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from .models import PlanResult, SlotRow
from .time_utils import format_iso, parse_iso_in_tz

ROUND_FLOOR = "floor"
ROUND_CEIL = "ceil"
ROUND_7_8 = "7/8"
REMAINING_COVERAGE_EPSILON_MINUTES = 0.001


def _parse_iso(value: str, tz: ZoneInfo) -> datetime | None:
    return parse_iso_in_tz(value, tz)


def _round_to_grid(dt: datetime, grid_minutes: int, mode: str) -> datetime:
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    total_minutes = (dt - day_start).total_seconds() / 60.0
    floor_value = int(total_minutes // grid_minutes) * grid_minutes
    rem = total_minutes - floor_value
    ceil_value = floor_value if rem <= 0 else floor_value + grid_minutes

    if mode == ROUND_FLOOR:
        out = floor_value
    elif mode == ROUND_CEIL:
        out = ceil_value
    else:  # 7/8 default
        out = floor_value if rem <= 7 else ceil_value

    return day_start + timedelta(minutes=out)


def _extract_price_segments(
    slots: list[SlotRow | dict], billing_slot_minutes: int, tz: ZoneInfo
) -> list[tuple[datetime, datetime, float]]:
    segments: list[tuple[datetime, datetime, float]] = []
    for raw in slots:
        dt = _parse_iso(raw["start_time"], tz)
        if dt is None:
            continue
        segments.append((dt, dt + timedelta(minutes=billing_slot_minutes), float(raw["price_per_kwh"])))
    segments.sort(key=lambda item: item[0])
    return segments


def _build_profile(
    duration_minutes: float | None,
    energy_profile: list[float] | None,
    profile_slot_minutes: int,
) -> tuple[list[float], float | None, str | None]:
    profile: list[float] | None = None

    if energy_profile:
        try:
            profile = [float(v) for v in energy_profile]
        except (TypeError, ValueError):
            return [], None, "invalid_energy_profile"

    if duration_minutes is None and profile:
        duration_minutes = len(profile) * profile_slot_minutes

    if duration_minutes is None:
        return [], None, "no_duration_or_profile"

    if not math.isfinite(float(duration_minutes)) or float(duration_minutes) <= 0:
        return [], None, "invalid_duration_minutes"

    exact_slots = float(duration_minutes) / float(profile_slot_minutes)
    full_slots = int(exact_slots)
    has_fraction = abs(exact_slots - full_slots) > 1e-9
    need = full_slots + (1 if has_fraction else 0)

    if profile is None:
        profile = [1.0] * need
    elif len(profile) < need:
        profile += [1.0] * (need - len(profile))
    elif len(profile) > need:
        profile = profile[:need]

    if has_fraction and profile:
        profile[-1] *= exact_slots - full_slots

    return profile, duration_minutes, None


def _profile_cost_for_start(
    start: datetime,
    profile: list[float],
    profile_slot_minutes: int,
    price_segments: list[tuple[datetime, datetime, float]],
) -> float | None:
    total = 0.0
    price_idx = 0
    for idx, weight in enumerate(profile):
        seg_start = start + timedelta(minutes=idx * profile_slot_minutes)
        seg_end = seg_start + timedelta(minutes=profile_slot_minutes)
        remaining = profile_slot_minutes
        weighted_price = 0.0

        while price_idx < len(price_segments) and price_segments[price_idx][1] <= seg_start:
            price_idx += 1

        for p_start, p_end, price in price_segments[price_idx:]:
            if p_start >= seg_end:
                break
            overlap_start = max(seg_start, p_start)
            overlap_end = min(seg_end, p_end)
            overlap_minutes = (overlap_end - overlap_start).total_seconds() / 60.0
            if overlap_minutes <= 0:
                continue
            weighted_price += price * (overlap_minutes / profile_slot_minutes)
            remaining -= overlap_minutes

        if remaining > REMAINING_COVERAGE_EPSILON_MINUTES:
            return None

        total += weighted_price * weight

    return total


def _resolve_start_anchor(
    *,
    now: datetime,
    candidate_step: int,
    start_mode: str,
    start_in_minutes: float,
) -> datetime:
    raw = now + timedelta(minutes=start_in_minutes) if start_mode == "in" else now
    round_mode = ROUND_7_8 if start_mode == "in" else ROUND_CEIL
    return _round_to_grid(raw, candidate_step, round_mode)


def _resolve_earliest_start(
    *,
    now: datetime,
    first_price: datetime,
    candidate_step: int,
    start_mode: str,
    start_in_minutes: float,
) -> datetime:
    return max(
        _resolve_start_anchor(
            now=now,
            candidate_step=candidate_step,
            start_mode=start_mode,
            start_in_minutes=start_in_minutes,
        ),
        first_price,
    )


def _resolve_requested_latest_start(
    *,
    now: datetime,
    start_anchor: datetime,
    candidate_step: int,
    effective_duration: float,
    deadline_mode: str,
    deadline_minutes: float | None,
    latest_start: str | None,
    latest_finish: str | None,
    tz: ZoneInfo,
) -> tuple[datetime | None, str | None]:
    if deadline_minutes is not None and (
        not math.isfinite(float(deadline_minutes)) or float(deadline_minutes) < 0
    ):
        return None, "invalid_deadline_minutes"

    if latest_start:
        parsed_latest_start = _parse_iso(latest_start, tz)
        if parsed_latest_start is None:
            return None, "invalid_latest_start"
        return _round_to_grid(parsed_latest_start, candidate_step, ROUND_FLOOR), None
    if latest_finish:
        parsed_latest_finish = _parse_iso(latest_finish, tz)
        if parsed_latest_finish is None:
            return None, "invalid_latest_finish"
        finish_dt = _round_to_grid(parsed_latest_finish, candidate_step, ROUND_FLOOR)
        return finish_dt - timedelta(minutes=effective_duration), None
    if deadline_mode == "start_within" and deadline_minutes is not None:
        return (
            _round_to_grid(start_anchor + timedelta(minutes=deadline_minutes), candidate_step, ROUND_FLOOR),
            None,
        )
    if deadline_mode == "finish_within" and deadline_minutes is not None:
        finish_dt = _round_to_grid(now + timedelta(minutes=deadline_minutes), candidate_step, ROUND_FLOOR)
        return finish_dt - timedelta(minutes=effective_duration), None
    return None, None


def optimize_runtime(
    *,
    slots: list[SlotRow | dict],
    timezone_name: str,
    billing_slot_minutes: int,
    duration_minutes: float | None,
    energy_profile: list[float] | None,
    profile_slot_minutes: int | None,
    max_extra_cost_percent: float,
    prefer_earliest: bool,
    start_mode: str,
    start_in_minutes: float,
    deadline_mode: str,
    deadline_minutes: float | None,
    latest_start: str | None,
    latest_finish: str | None,
    align_start_to_billing_slot: bool,
    reference_time: str | None = None,
) -> PlanResult:
    tz = ZoneInfo(timezone_name)
    now = _parse_iso(reference_time, tz) if reference_time else datetime.now(tz)
    if now is None:
        now = datetime.now(tz)

    if not math.isfinite(float(max_extra_cost_percent)) or float(max_extra_cost_percent) < 0:
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason="invalid_max_extra_cost_percent",
            candidates=0,
            profile_used=[],
            window_start=format_iso(now, timespec="minutes"),
            window_end=format_iso(now, timespec="minutes"),
            duration_minutes=duration_minutes,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=profile_slot_minutes or billing_slot_minutes,
        )

    prof_slot = profile_slot_minutes or billing_slot_minutes
    prof_slot = max(1, int(prof_slot))
    candidate_step = billing_slot_minutes if align_start_to_billing_slot else prof_slot
    candidate_step = max(1, int(candidate_step))

    price_segments = _extract_price_segments(slots, billing_slot_minutes, tz)
    if not price_segments:
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason="no_valid_slots_after_parse",
            candidates=0,
            profile_used=[],
            window_start=format_iso(now, timespec="minutes"),
            window_end=format_iso(now, timespec="minutes"),
            duration_minutes=duration_minutes,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
        )

    profile, effective_duration, reason = _build_profile(duration_minutes, energy_profile, prof_slot)
    if reason:
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason=reason,
            candidates=0,
            profile_used=[],
            window_start=format_iso(now, timespec="minutes"),
            window_end=format_iso(now, timespec="minutes"),
            duration_minutes=duration_minutes,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
        )

    first_price = price_segments[0][0]
    last_price_end = price_segments[-1][1]

    earliest_start = _resolve_earliest_start(
        now=now,
        first_price=first_price,
        candidate_step=candidate_step,
        start_mode=start_mode,
        start_in_minutes=start_in_minutes,
    )

    start_anchor = _resolve_start_anchor(
        now=now,
        candidate_step=candidate_step,
        start_mode=start_mode,
        start_in_minutes=start_in_minutes,
    )

    requested_latest_start, latest_start_reason = _resolve_requested_latest_start(
        now=now,
        start_anchor=start_anchor,
        candidate_step=candidate_step,
        effective_duration=effective_duration,
        deadline_mode=deadline_mode,
        deadline_minutes=deadline_minutes,
        latest_start=latest_start,
        latest_finish=latest_finish,
        tz=tz,
    )
    if latest_start_reason is not None:
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason=latest_start_reason,
            candidates=0,
            profile_used=profile,
            window_start=format_iso(earliest_start, timespec="minutes"),
            window_end=format_iso(last_price_end, timespec="minutes"),
            duration_minutes=effective_duration,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
        )
    latest_available_start = last_price_end - timedelta(minutes=effective_duration)
    if requested_latest_start is None:
        latest_start_eval = latest_available_start
    else:
        latest_start_eval = min(requested_latest_start, latest_available_start)
    requested_latest_start_iso = (
        format_iso(requested_latest_start, timespec="minutes") if requested_latest_start else None
    )
    window_truncated_by_data = (
        requested_latest_start is not None and latest_start_eval < requested_latest_start
    )

    if latest_start_eval < earliest_start:
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason="window_too_short_for_duration",
            candidates=0,
            profile_used=profile,
            window_start=format_iso(earliest_start, timespec="minutes"),
            window_end=format_iso(latest_start_eval, timespec="minutes"),
            duration_minutes=effective_duration,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
            requested_latest_start=requested_latest_start_iso,
            window_truncated_by_data=window_truncated_by_data,
            price_coverage_end=format_iso(last_price_end, timespec="minutes"),
        )

    candidates: list[tuple[datetime, float]] = []
    skipped_in_past = 0
    uncovered_candidates = 0
    current = earliest_start
    while current <= latest_start_eval:
        if current <= now:
            skipped_in_past += 1
            current += timedelta(minutes=candidate_step)
            continue
        cost = _profile_cost_for_start(current, profile, prof_slot, price_segments)
        if cost is not None:
            candidates.append((current, cost))
        else:
            uncovered_candidates += 1
        current += timedelta(minutes=candidate_step)

    if not candidates:
        reason = "no_candidate_after_constraints"
        if skipped_in_past > 0 and uncovered_candidates == 0:
            reason = "all_candidates_in_past"
        elif uncovered_candidates > 0 and skipped_in_past == 0:
            reason = "incomplete_price_coverage_for_candidates"
        elif uncovered_candidates > 0 and skipped_in_past > 0:
            reason = "candidates_blocked_by_time_and_price_coverage"
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason=reason,
            candidates=0,
            profile_used=profile,
            window_start=format_iso(earliest_start, timespec="minutes"),
            window_end=format_iso(latest_start_eval, timespec="minutes"),
            duration_minutes=effective_duration,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
            requested_latest_start=requested_latest_start_iso,
            window_truncated_by_data=window_truncated_by_data,
            price_coverage_end=format_iso(last_price_end, timespec="minutes"),
        )

    min_cost = min(item[1] for item in candidates)
    threshold = min_cost * (1.0 + (float(max_extra_cost_percent) / 100.0))

    if prefer_earliest:
        picked = next(item for item in candidates if item[1] <= threshold)
    else:
        picked = min(candidates, key=lambda item: item[1])

    best_start_dt = picked[0]
    best_end_dt = best_start_dt + timedelta(minutes=effective_duration)

    return PlanResult(
        status="ok",
        best_start=format_iso(best_start_dt, timespec="minutes"),
        best_end=format_iso(best_end_dt, timespec="minutes"),
        best_cost=round(picked[1], 6),
        reason=None,
        candidates=len(candidates),
        profile_used=profile,
        window_start=format_iso(earliest_start, timespec="minutes"),
        window_end=format_iso(latest_start_eval, timespec="minutes"),
        duration_minutes=effective_duration,
        billing_slot_minutes=billing_slot_minutes,
        profile_slot_minutes=prof_slot,
        requested_latest_start=requested_latest_start_iso,
        window_truncated_by_data=window_truncated_by_data,
        price_coverage_end=format_iso(last_price_end, timespec="minutes"),
    )
