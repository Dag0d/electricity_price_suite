"""Consumption and cost aggregation helpers."""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    ConsumptionDailyRollup,
    ConsumptionMonthlyRollup,
    ConsumptionPowerActiveBlock,
    ConsumptionPowerBucketRow,
    ConsumptionSlotRow,
)
from .time_utils import parse_iso_in_tz


def month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def previous_month(dt: datetime) -> tuple[int, int]:
    if dt.month == 1:
        return (dt.year - 1, 12)
    return (dt.year, dt.month - 1)


def _gross_fixed_fee(amount: float, tax_percent: float, values_include_tax: bool) -> float:
    base = float(amount or 0.0)
    if base <= 0:
        return 0.0
    if values_include_tax:
        return base
    return base * (1.0 + (float(tax_percent or 0.0) / 100.0))


def _fixed_fee_shares(
    *,
    year: int,
    month: int,
    elapsed_days: int,
    monthly_amount: float,
    daily_amount: float,
    tax_percent: float,
    values_include_tax: bool,
    current_month_mode: str,
    is_current_month: bool,
) -> tuple[float, float]:
    days_in_month = monthrange(year, month)[1]
    elapsed = max(0, min(elapsed_days, days_in_month))
    monthly_gross = _gross_fixed_fee(monthly_amount, tax_percent, values_include_tax)
    daily_gross = _gross_fixed_fee(daily_amount, tax_percent, values_include_tax)

    day_share = daily_gross
    if monthly_gross > 0:
        day_share += monthly_gross / float(days_in_month)

    if is_current_month and current_month_mode == "full":
        month_share = monthly_gross + (daily_gross * float(elapsed))
    else:
        month_share = ((monthly_gross / float(days_in_month)) * float(elapsed)) + (daily_gross * float(elapsed))

    return (day_share, month_share)


def build_consumption_metrics(
    *,
    slots: list[ConsumptionSlotRow],
    daily_rollups: dict[str, ConsumptionDailyRollup],
    monthly_rollups: dict[str, ConsumptionMonthlyRollup],
    power_buckets: list[ConsumptionPowerBucketRow],
    power_active_block: ConsumptionPowerActiveBlock | None,
    timezone_name: str,
    round_decimals: int,
    fixed_fee_monthly_amount: float,
    fixed_fee_daily_amount: float,
    fixed_fee_tax_percent: float,
    fixed_fee_values_include_tax: bool,
    current_month_fixed_fee_mode: str,
    avg_price_include_basic_fee: bool,
    consumption_energy_entity: str | None,
) -> dict[str, Any]:
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    today = now.date()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    last_month_year, last_month_month = previous_month(now)
    last_month_key = f"{last_month_year:04d}-{last_month_month:02d}"

    day_energy: dict[datetime.date, float] = {}
    day_cost: dict[datetime.date, float] = {}
    month_energy = 0.0
    month_cost = 0.0
    current_hour_energy = 0.0
    last_month_energy_raw = 0.0
    last_month_cost_raw = 0.0

    for row in daily_rollups.values():
        try:
            local_day = datetime.fromisoformat(str(row.get("date"))).date()
        except ValueError:
            continue
        energy = float(row.get("consumption_kwh", 0.0) or 0.0)
        cost = float(row.get("energy_cost", 0.0) or 0.0)
        day_energy[local_day] = day_energy.get(local_day, 0.0) + energy
        day_cost[local_day] = day_cost.get(local_day, 0.0) + cost

        if local_day >= month_start:
            month_energy += energy
            month_cost += cost

        if local_day.year == last_month_year and local_day.month == last_month_month:
            last_month_energy_raw += energy
            last_month_cost_raw += cost

    for slot in slots:
        start = parse_iso_in_tz(slot.get("start_time"), tz)
        if start is None:
            continue
        local_day = start.date()
        energy = float(slot.get("consumption_kwh", 0.0) or 0.0)
        cost = float(slot.get("energy_cost", 0.0) or 0.0)

        day_energy[local_day] = day_energy.get(local_day, 0.0) + energy
        day_cost[local_day] = day_cost.get(local_day, 0.0) + cost

        if local_day >= month_start:
            month_energy += energy
            month_cost += cost

        if start >= current_hour_start:
            current_hour_energy += energy

        if start.year == last_month_year and start.month == last_month_month:
            last_month_energy_raw += energy
            last_month_cost_raw += cost

    rollup = monthly_rollups.get(last_month_key)
    last_month_energy = last_month_energy_raw + (
        float(rollup.get("consumption_kwh", 0.0) or 0.0) if rollup else 0.0
    )
    last_month_cost = last_month_cost_raw + (
        float(rollup.get("energy_cost", 0.0) or 0.0) if rollup else 0.0
    )

    today_energy = day_energy.get(today, 0.0)
    yesterday_energy = day_energy.get(yesterday, 0.0)
    today_cost = day_cost.get(today, 0.0)
    yesterday_cost = day_cost.get(yesterday, 0.0)

    today_fee, month_fee = _fixed_fee_shares(
        year=now.year,
        month=now.month,
        elapsed_days=today.day,
        monthly_amount=fixed_fee_monthly_amount,
        daily_amount=fixed_fee_daily_amount,
        tax_percent=fixed_fee_tax_percent,
        values_include_tax=fixed_fee_values_include_tax,
        current_month_mode=current_month_fixed_fee_mode,
        is_current_month=True,
    )
    yesterday_fee, _ = _fixed_fee_shares(
        year=yesterday.year,
        month=yesterday.month,
        elapsed_days=1,
        monthly_amount=fixed_fee_monthly_amount,
        daily_amount=fixed_fee_daily_amount,
        tax_percent=fixed_fee_tax_percent,
        values_include_tax=fixed_fee_values_include_tax,
        current_month_mode="prorated",
        is_current_month=False,
    )
    _, last_month_fee = _fixed_fee_shares(
        year=last_month_year,
        month=last_month_month,
        elapsed_days=monthrange(last_month_year, last_month_month)[1],
        monthly_amount=fixed_fee_monthly_amount,
        daily_amount=fixed_fee_daily_amount,
        tax_percent=fixed_fee_tax_percent,
        values_include_tax=fixed_fee_values_include_tax,
        current_month_mode="full",
        is_current_month=False,
    )

    def avg(cost_value: float, energy_value: float) -> float | None:
        if energy_value <= 0:
            return None
        return cost_value / energy_value

    def rounded(value: float | None) -> float | None:
        if value is None:
            return None
        return round(float(value), round_decimals)

    avg_today_cost = today_cost + today_fee if avg_price_include_basic_fee else today_cost
    avg_yesterday_cost = yesterday_cost + yesterday_fee if avg_price_include_basic_fee else yesterday_cost
    avg_month_cost = month_cost + month_fee if avg_price_include_basic_fee else month_cost
    avg_last_month_cost = last_month_cost + last_month_fee if avg_price_include_basic_fee else last_month_cost

    rolling_cutoff = now - timedelta(hours=24)
    bucket_segments: list[tuple[datetime, datetime, float, float, float]] = []
    for bucket in power_buckets:
        start = parse_iso_in_tz(bucket.get("start_time"), tz)
        end = parse_iso_in_tz(bucket.get("end_time"), tz)
        if start is None or end is None or end <= rolling_cutoff or start >= now:
            continue
        overlap_start = max(start, rolling_cutoff)
        overlap_end = min(end, now)
        if overlap_end <= overlap_start:
            continue
        bucket_segments.append(
            (
                overlap_start,
                overlap_end,
                float(bucket.get("avg_power_w", 0.0) or 0.0),
                float(bucket.get("max_power_w", 0.0) or 0.0),
                float(bucket.get("min_5m_power_w", 0.0) or 0.0),
            )
        )

    if isinstance(power_active_block, dict):
        start = parse_iso_in_tz(power_active_block.get("start_time"), tz)
        end = parse_iso_in_tz(power_active_block.get("end_time"), tz)
        total_seconds = float(power_active_block.get("duration_seconds_total", 0.0) or 0.0)
        total_energy = float(power_active_block.get("energy_kwh_total", 0.0) or 0.0)
        max_power = float(power_active_block.get("max_power_w", 0.0) or 0.0)
        if start is not None and end is not None and end > rolling_cutoff and start < now and total_seconds > 0:
            overlap_start = max(start, rolling_cutoff)
            overlap_end = min(end, now)
            if overlap_end > overlap_start:
                avg_power = (total_energy * 1000.0) / (total_seconds / 3600.0)
                min_candidates: list[float] = []
                for window in (power_active_block.get("window_stats") or {}).values():
                    window_seconds = float(window.get("duration_seconds", 0.0) or 0.0)
                    window_energy = float(window.get("energy_kwh", 0.0) or 0.0)
                    if window_seconds < 300.0:
                        continue
                    min_candidates.append((window_energy * 1000.0) / (window_seconds / 3600.0))
                if min_candidates:
                    min_power = min(min_candidates)
                    bucket_segments.append((overlap_start, overlap_end, avg_power, max_power, min_power))

    power_avg_24h: float | None = None
    power_min_24h: float | None = None
    power_max_24h: float | None = None
    if bucket_segments:
        total_seconds = sum((end - start).total_seconds() for start, end, _avg, _max, _min in bucket_segments)
        if total_seconds > 0:
            weighted_sum = sum(avg * (end - start).total_seconds() for start, end, avg, _max, _min in bucket_segments)
            power_avg_24h = weighted_sum / total_seconds
        power_max_24h = max(max_power for _start, _end, _avg, max_power, _min in bucket_segments)
        power_min_24h = min(min_power for _start, _end, _avg, _max, min_power in bucket_segments)

    def rounded_power(value: float | None) -> float | None:
        if value is None:
            return None
        return round(float(value), 0)

    return {
        "consumption_energy_entity": consumption_energy_entity,
        "consumption_today_kwh": rounded(today_energy),
        "consumption_yesterday_kwh": rounded(yesterday_energy),
        "consumption_month_kwh": rounded(month_energy),
        "consumption_current_hour_kwh": rounded(current_hour_energy),
        "cost_today": rounded(today_cost),
        "cost_yesterday": rounded(yesterday_cost),
        "cost_month": rounded(month_cost),
        "cost_today_incl_basic_fee": rounded(today_cost + today_fee),
        "cost_yesterday_incl_basic_fee": rounded(yesterday_cost + yesterday_fee),
        "cost_month_incl_basic_fee": rounded(month_cost + month_fee),
        "cost_last_month": rounded(last_month_cost),
        "cost_last_month_incl_basic_fee": rounded(last_month_cost + last_month_fee),
        "avg_paid_price_today": rounded(avg(avg_today_cost, today_energy)),
        "avg_paid_price_yesterday": rounded(avg(avg_yesterday_cost, yesterday_energy)),
        "avg_paid_price_month": rounded(avg(avg_month_cost, month_energy)),
        "avg_paid_price_last_month": rounded(avg(avg_last_month_cost, last_month_energy)),
        "avg_power_24h_w": rounded_power(power_avg_24h),
        "min_power_24h_w": rounded_power(power_min_24h),
        "max_power_24h_w": rounded_power(power_max_24h),
        "last_updated": now.isoformat(timespec="seconds"),
        "fixed_fee_monthly_amount": float(fixed_fee_monthly_amount),
        "fixed_fee_daily_amount": float(fixed_fee_daily_amount),
        "fixed_fee_tax_percent": float(fixed_fee_tax_percent),
        "fixed_fee_values_include_tax": bool(fixed_fee_values_include_tax),
        "current_month_fixed_fee_mode": str(current_month_fixed_fee_mode),
        "avg_price_include_basic_fee": bool(avg_price_include_basic_fee),
    }
