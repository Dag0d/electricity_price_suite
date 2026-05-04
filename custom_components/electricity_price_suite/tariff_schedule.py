"""Helpers for scheduled tariff changes and manual price calibration."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .time_utils import format_iso

ABSOLUTE_SURCHARGE_DECIMALS = 5


def round_absolute_surcharge(value: float) -> float:
    return round(float(value), ABSOLUTE_SURCHARGE_DECIMALS)


def parse_effective_from(value: object) -> date:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing_effective_from")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as err:
        raise ValueError("invalid_effective_from") from err


def parse_sequence_start(value: object) -> tuple[int, int]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing_sequence_start")
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError) as err:
        raise ValueError("invalid_sequence_start") from err
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("invalid_sequence_start")
    return (hour, minute)


def parse_final_price_lines(
    *,
    effective_from: date,
    timezone_name: str,
    billing_slot_minutes: int,
    final_price_lines: str,
    sequence_start: str | None,
) -> list[tuple[str, float]]:
    tz = ZoneInfo(timezone_name)
    raw_lines = [line.strip() for line in str(final_price_lines or "").splitlines() if line.strip()]
    if not raw_lines:
        raise ValueError("missing_final_price_lines")

    parsed: list[tuple[str | None, float]] = []
    explicit_times = 0
    bare_values = 0
    for line in raw_lines:
        parts = line.replace(",", ".").split()
        if len(parts) == 1:
            bare_values += 1
            try:
                parsed.append((None, float(parts[0])))
            except ValueError as err:
                raise ValueError(f"invalid_final_price_line:{line}") from err
            continue
        if len(parts) != 2:
            raise ValueError(f"invalid_final_price_line:{line}")
        time_text, price_text = parts
        if ":" not in time_text:
            raise ValueError(f"invalid_final_price_line:{line}")
        explicit_times += 1
        try:
            parsed.append((time_text, float(price_text)))
        except ValueError as err:
            raise ValueError(f"invalid_final_price_line:{line}") from err

    if explicit_times and bare_values:
        raise ValueError("mixed_final_price_line_formats")

    resolved: list[tuple[str, float]] = []
    if explicit_times:
        for time_text, price in parsed:
            hour, minute = parse_sequence_start(time_text)
            start = datetime(
                effective_from.year,
                effective_from.month,
                effective_from.day,
                hour,
                minute,
                tzinfo=tz,
            )
            resolved.append((format_iso(start), float(price)))
        return resolved

    start_hour, start_minute = parse_sequence_start(sequence_start or "00:00")
    current = datetime(
        effective_from.year,
        effective_from.month,
        effective_from.day,
        start_hour,
        start_minute,
        tzinfo=tz,
    )
    step = timedelta(minutes=billing_slot_minutes)
    for _time_text, price in parsed:
        resolved.append((format_iso(current), float(price)))
        current += step
    return resolved


def derive_absolute_surcharge_from_final_prices(
    *,
    market_prices_by_start: dict[str, float],
    final_prices: list[tuple[str, float]],
    surcharge_percent: float,
    tax_percent: float,
) -> dict[str, Any]:
    total = len(final_prices)
    if total < 3:
        raise ValueError("need_at_least_3_final_price_samples")

    deltas: list[float] = []
    missing_slots: list[str] = []
    tax_factor = 1.0 + (float(tax_percent) / 100.0)
    percent_factor = 1.0 + (float(surcharge_percent) / 100.0)
    if tax_factor == 0:
        raise ValueError("invalid_tax_factor")

    for start_time, final_price in final_prices:
        market_price = market_prices_by_start.get(start_time)
        if market_price is None:
            missing_slots.append(start_time)
            continue
        net_final = float(final_price) / tax_factor
        implied_absolute = net_final - (float(market_price) * percent_factor)
        deltas.append(implied_absolute)

    if len(deltas) < 3:
        raise ValueError("not_enough_matching_market_slots")

    mean_delta = sum(deltas) / float(len(deltas))
    median_delta = float(median(deltas))
    return {
        "samples_total": total,
        "samples_used": len(deltas),
        "samples_missing": len(missing_slots),
        "missing_slots": missing_slots,
        "derived_absolute_surcharge": round_absolute_surcharge(median_delta),
        "derived_mean_absolute_surcharge": round_absolute_surcharge(mean_delta),
        "derived_median_absolute_surcharge": round_absolute_surcharge(median_delta),
        "min_delta": round_absolute_surcharge(min(deltas)),
        "max_delta": round_absolute_surcharge(max(deltas)),
    }
