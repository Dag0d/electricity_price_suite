"""Consumption and cost aggregation helpers."""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .models import ConsumptionMonthlyRollup, ConsumptionSlotRow
from .time_utils import parse_iso_in_tz


def month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def previous_month(dt: datetime) -> tuple[int, int]:
    if dt.month == 1:
        return (dt.year - 1, 12)
    return (dt.year, dt.month - 1)


def monthly_basic_fee_share(
    *,
    mode: str,
    amount: float,
    year: int,
    month: int,
    elapsed_days: int,
) -> float:
    if amount <= 0:
        return 0.0
    elapsed = max(0, elapsed_days)
    if mode == "daily":
        return float(amount) * float(elapsed)
    if mode == "monthly":
        days_in_month = monthrange(year, month)[1]
        return (float(amount) / float(days_in_month)) * float(elapsed)
    return 0.0


def build_consumption_metrics(
    *,
    slots: list[ConsumptionSlotRow],
    monthly_rollups: dict[str, ConsumptionMonthlyRollup],
    timezone_name: str,
    round_decimals: int,
    basic_fee_mode: str,
    basic_fee_amount: float,
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

    month_fee = monthly_basic_fee_share(
        mode=basic_fee_mode,
        amount=basic_fee_amount,
        year=now.year,
        month=now.month,
        elapsed_days=today.day,
    )
    today_fee = monthly_basic_fee_share(
        mode=basic_fee_mode,
        amount=basic_fee_amount,
        year=today.year,
        month=today.month,
        elapsed_days=1,
    )
    yesterday_fee = monthly_basic_fee_share(
        mode=basic_fee_mode,
        amount=basic_fee_amount,
        year=yesterday.year,
        month=yesterday.month,
        elapsed_days=1,
    )
    last_month_fee = monthly_basic_fee_share(
        mode=basic_fee_mode,
        amount=basic_fee_amount,
        year=last_month_year,
        month=last_month_month,
        elapsed_days=monthrange(last_month_year, last_month_month)[1],
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
        "last_updated": now.isoformat(timespec="seconds"),
        "basic_fee_mode": basic_fee_mode,
        "basic_fee_amount": float(basic_fee_amount),
        "avg_price_include_basic_fee": bool(avg_price_include_basic_fee),
    }
