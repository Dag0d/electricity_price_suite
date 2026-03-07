"""Runtime optimization engine."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import PlanResult


def _parse_iso(value: str, tz: ZoneInfo) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _round_to_grid(dt: datetime, grid_minutes: int, mode: str) -> datetime:
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    total_minutes = int((dt - day_start).total_seconds() // 60)
    rem = total_minutes % grid_minutes
    floor_value = total_minutes - rem
    ceil_value = floor_value if rem == 0 else floor_value + grid_minutes

    if mode == "floor":
        out = floor_value
    elif mode == "ceil":
        out = ceil_value
    else:  # 7/8 default
        out = floor_value if rem <= 7 else ceil_value

    return day_start + timedelta(minutes=out)


def _extract_price_segments(slots: list[dict], billing_slot_minutes: int, tz: ZoneInfo) -> list[tuple[datetime, datetime, float]]:
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
        profile = [float(v) for v in energy_profile]

    if duration_minutes is None and profile:
        duration_minutes = len(profile) * profile_slot_minutes

    if duration_minutes is None:
        return [], None, "no_duration_or_profile"

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
    for idx, weight in enumerate(profile):
        seg_start = start + timedelta(minutes=idx * profile_slot_minutes)
        seg_end = seg_start + timedelta(minutes=profile_slot_minutes)
        remaining = profile_slot_minutes
        weighted_price = 0.0

        for p_start, p_end, price in price_segments:
            overlap_start = max(seg_start, p_start)
            overlap_end = min(seg_end, p_end)
            overlap_minutes = (overlap_end - overlap_start).total_seconds() / 60.0
            if overlap_minutes <= 0:
                continue
            weighted_price += price * (overlap_minutes / profile_slot_minutes)
            remaining -= overlap_minutes

        if remaining > 0.001:
            return None

        total += weighted_price * weight

    return total


def optimize_runtime(
    *,
    slots: list[dict],
    timezone_name: str,
    billing_slot_minutes: int,
    duration_minutes: float | None,
    energy_profile: list[float] | None,
    profile_slot_minutes: int | None,
    epsilon_rel: float,
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
            window_start=now.isoformat(timespec="minutes"),
            window_end=now.isoformat(timespec="minutes"),
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
            window_start=now.isoformat(timespec="minutes"),
            window_end=now.isoformat(timespec="minutes"),
            duration_minutes=duration_minutes,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
        )

    first_price = price_segments[0][0]
    last_price_end = price_segments[-1][1]

    if start_mode == "in":
        ws = _round_to_grid(now + timedelta(minutes=start_in_minutes), candidate_step, "7/8")
    else:
        ws = _round_to_grid(now, candidate_step, "ceil")
    ws = max(ws, first_price)

    if effective_duration is None:
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason="no_duration_or_profile",
            candidates=0,
            profile_used=profile,
            window_start=ws.isoformat(timespec="minutes"),
            window_end=last_price_end.isoformat(timespec="minutes"),
            duration_minutes=duration_minutes,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
        )

    latest_start_limit: datetime | None = None

    raw_start_anchor = now + timedelta(minutes=start_in_minutes) if start_mode == "in" else now
    if start_mode == "in":
        start_anchor = _round_to_grid(raw_start_anchor, candidate_step, "7/8")
    else:
        start_anchor = _round_to_grid(raw_start_anchor, candidate_step, "ceil")

    if latest_start:
        latest_start_limit = _round_to_grid(_parse_iso(latest_start, tz) or ws, candidate_step, "floor")
    elif latest_finish:
        finish_dt = _round_to_grid(_parse_iso(latest_finish, tz) or ws, candidate_step, "floor")
        latest_start_limit = finish_dt - timedelta(minutes=effective_duration)
    elif deadline_mode == "start_within" and deadline_minutes is not None:
        latest_start_limit = _round_to_grid(start_anchor + timedelta(minutes=deadline_minutes), candidate_step, "floor")
    elif deadline_mode == "finish_within" and deadline_minutes is not None:
        finish_dt = _round_to_grid(now + timedelta(minutes=deadline_minutes), candidate_step, "floor")
        latest_start_limit = finish_dt - timedelta(minutes=effective_duration)

    latest_start_possible = last_price_end - timedelta(minutes=effective_duration)
    if latest_start_limit is None:
        latest_start_eval = latest_start_possible
    else:
        latest_start_eval = min(latest_start_limit, latest_start_possible)
    requested_window_end = latest_start_limit.isoformat(timespec="minutes") if latest_start_limit else None
    window_truncated_by_data = latest_start_limit is not None and latest_start_eval < latest_start_limit

    if latest_start_eval < ws:
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason="window_too_short_for_duration",
            candidates=0,
            profile_used=profile,
            window_start=ws.isoformat(timespec="minutes"),
            window_end=latest_start_eval.isoformat(timespec="minutes"),
            duration_minutes=effective_duration,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
            requested_window_end=requested_window_end,
            window_truncated_by_data=window_truncated_by_data,
            price_coverage_end=last_price_end.isoformat(timespec="minutes"),
        )

    candidates: list[tuple[datetime, float]] = []
    current = ws
    while current <= latest_start_eval:
        cost = _profile_cost_for_start(current, profile, prof_slot, price_segments)
        if cost is not None:
            candidates.append((current, cost))
        current += timedelta(minutes=candidate_step)

    if not candidates:
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason="no_candidate",
            candidates=0,
            profile_used=profile,
            window_start=ws.isoformat(timespec="minutes"),
            window_end=latest_start_eval.isoformat(timespec="minutes"),
            duration_minutes=effective_duration,
            billing_slot_minutes=billing_slot_minutes,
            profile_slot_minutes=prof_slot,
            requested_window_end=requested_window_end,
            window_truncated_by_data=window_truncated_by_data,
            price_coverage_end=last_price_end.isoformat(timespec="minutes"),
        )

    min_cost = min(item[1] for item in candidates)
    threshold = min_cost * (1.0 + float(epsilon_rel))

    if prefer_earliest:
        picked = next(item for item in candidates if item[1] <= threshold)
    else:
        picked = min(candidates, key=lambda item: item[1])

    best_start_dt = picked[0]
    best_end_dt = best_start_dt + timedelta(minutes=effective_duration)

    return PlanResult(
        status="ok",
        best_start=best_start_dt.isoformat(timespec="minutes"),
        best_end=best_end_dt.isoformat(timespec="minutes"),
        best_cost=round(picked[1], 6),
        reason=None,
        candidates=len(candidates),
        profile_used=profile,
        window_start=ws.isoformat(timespec="minutes"),
        window_end=latest_start_eval.isoformat(timespec="minutes"),
        duration_minutes=effective_duration,
        billing_slot_minutes=billing_slot_minutes,
        profile_slot_minutes=prof_slot,
        requested_window_end=requested_window_end,
        window_truncated_by_data=window_truncated_by_data,
        price_coverage_end=last_price_end.isoformat(timespec="minutes"),
    )
