"""Timeline statistics helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .models import SlotRecord, SlotRow, TimelineStats
from .time_utils import format_iso, parse_iso_in_tz

if TYPE_CHECKING:
    from .store import TimelineStore


def parse_iso_local(value: str, tz: ZoneInfo) -> datetime | None:
    """Parse an ISO string and normalize it into the given timezone."""

    return parse_iso_in_tz(value, tz)


def detect_billing_slot_minutes(rows: list[SlotRow], timezone_name: str, fallback: int) -> int:
    """Detect the minimum reasonable slot delta from sorted timeline rows."""

    if len(rows) < 2:
        return fallback

    tz = ZoneInfo(timezone_name)
    parsed: list[datetime] = []
    for row in rows:
        dt = parse_iso_local(row["start_time"], tz)
        if dt is not None:
            parsed.append(dt)
    parsed.sort()
    if len(parsed) < 2:
        return fallback

    deltas: list[int] = []
    for idx in range(1, len(parsed)):
        diff = int(round((parsed[idx] - parsed[idx - 1]).total_seconds() / 60.0))
        if 1 <= diff <= 240:
            deltas.append(diff)
    return min(deltas) if deltas else fallback


def filter_today_tomorrow_slots(slots: list[SlotRecord], timezone_name: str) -> list[SlotRecord]:
    """Keep only slots belonging to today or tomorrow in the HA timezone."""

    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    return [
        slot
        for slot in slots
        if (dt := parse_iso_local(slot.start_time, tz)) is not None and dt.date() in {today, tomorrow}
    ]


def missing_today_tomorrow_primary(rows: list[SlotRow], timezone_name: str) -> tuple[bool, bool]:
    """Return whether primary rows are missing for today and tomorrow."""

    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    has_primary_today = False
    has_primary_tomorrow = False

    for row in rows:
        if not bool(row.get("is_primary_source")):
            continue
        dt = parse_iso_local(row["start_time"], tz)
        if dt is None:
            continue
        if dt.date() == today:
            has_primary_today = True
        elif dt.date() == tomorrow:
            has_primary_tomorrow = True
        if has_primary_today and has_primary_tomorrow:
            break

    return (not has_primary_today, not has_primary_tomorrow)


def filter_slots_for_missing_days(
    slots: list[SlotRecord],
    need_today: bool,
    need_tomorrow: bool,
    timezone_name: str,
) -> list[SlotRecord]:
    """Keep only rows for days that are still missing on the primary source."""

    if not need_today and not need_tomorrow:
        return []

    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    filtered: list[SlotRecord] = []
    for slot in slots:
        dt = parse_iso_local(slot.start_time, tz)
        if dt is None:
            continue
        if need_today and dt.date() == today:
            filtered.append(slot)
        elif need_tomorrow and dt.date() == tomorrow:
            filtered.append(slot)
    return filtered


def has_primary_tomorrow_rows(rows: list[SlotRow], timezone_name: str) -> bool:
    """Return whether tomorrow currently has at least one primary row."""

    tz = ZoneInfo(timezone_name)
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    for row in rows:
        dt = parse_iso_local(row["start_time"], tz)
        if dt is None or dt.date() != tomorrow:
            continue
        if bool(row.get("is_primary_source")):
            return True
    return False


def pending_primary(rows: list[SlotRow], timezone_name: str) -> bool:
    """Return whether today/tomorrow still contain fallback rows."""

    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    for row in rows:
        dt = parse_iso_local(row["start_time"], tz)
        if dt is None or dt.date() not in {today, tomorrow}:
            continue
        if not bool(row.get("is_primary_source")):
            return True
    return False


def next_slot_start_after(rows: list[SlotRow], now: datetime, timezone_name: str) -> datetime | None:
    """Find the next available slot boundary after now."""

    tz = ZoneInfo(timezone_name)
    next_slot: datetime | None = None
    for row in rows:
        dt = parse_iso_local(row["start_time"], tz)
        if dt is None or dt <= now:
            continue
        if next_slot is None or dt < next_slot:
            next_slot = dt
    return next_slot


def current_price_coverage_end(rows: list[SlotRow], timezone_name: str, fallback: int) -> datetime | None:
    """Return the end of the latest known price segment."""

    if not rows:
        return None

    tz = ZoneInfo(timezone_name)
    slot_minutes = detect_billing_slot_minutes(rows, timezone_name, fallback)
    max_end: datetime | None = None
    for row in rows:
        dt = parse_iso_local(row["start_time"], tz)
        if dt is None:
            continue
        end = dt + timedelta(minutes=slot_minutes)
        if max_end is None or end > max_end:
            max_end = end
    return max_end


def build_timeline_stats(
    *,
    store: TimelineStore,
    timezone_name: str,
    currency: str,
    round_decimals: int,
    fallback_slot_minutes: int,
) -> TimelineStats:
    """Build the current timeline state and all exposed attributes."""

    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    all_rows = store.get_slots()
    today = now.date()
    tomorrow = today + timedelta(days=1)

    today_rows = rows_for_day(all_rows, today, tz)
    tomorrow_rows = rows_for_day(all_rows, tomorrow, tz)
    detected_slot_minutes = detect_billing_slot_minutes(all_rows, timezone_name, fallback_slot_minutes)
    current_price, current_price_start = current_price_for_now(all_rows, now, tz, detected_slot_minutes)

    card = [
        {"start_time": row["start_time"], "price_per_kwh": round_value(row["price_per_kwh"], round_decimals)}
        for row in [*today_rows, *tomorrow_rows]
    ]
    card.sort(key=lambda item: item["start_time"])

    w_today = weighted_for_rows(today_rows, tz, detected_slot_minutes)
    w_tomorrow = weighted_for_rows(tomorrow_rows, tz, detected_slot_minutes)

    past_3: list[tuple[float, float]] = []
    past_7: list[tuple[float, float]] = []
    for offset in range(1, 8):
        history_rows = rows_for_day(all_rows, today - timedelta(days=offset), tz)
        weighted = weighted_for_rows(history_rows, tz, detected_slot_minutes)
        if offset <= 3:
            past_3.extend(weighted)
        past_7.extend(weighted)

    avg_today = weighted_avg(w_today)
    tomorrow_primary = has_primary_tomorrow_rows(all_rows, timezone_name)
    timeline_state = compute_timeline_status(
        today_rows=len(today_rows),
        tomorrow_rows=len(tomorrow_rows),
        has_primary_tomorrow=tomorrow_primary,
    )

    attrs: dict[str, object] = {
        "currency": currency,
        "data": card,
        "avg_today": round_value(avg_today, round_decimals),
        "min_today": round_value(min((v for v, _ in w_today), default=None), round_decimals),
        "max_today": round_value(max((v for v, _ in w_today), default=None), round_decimals),
        "p20_today": round_value(weighted_q(w_today, 0.2), round_decimals),
        "p70_today": round_value(weighted_q(w_today, 0.7), round_decimals),
        "min_today_time": extreme_time(today_rows, pick="min"),
        "max_today_time": extreme_time(today_rows, pick="max"),
        "avg_tomorrow": round_value(weighted_avg(w_tomorrow), round_decimals),
        "min_tomorrow": round_value(min((v for v, _ in w_tomorrow), default=None), round_decimals),
        "max_tomorrow": round_value(max((v for v, _ in w_tomorrow), default=None), round_decimals),
        "p20_tomorrow": round_value(weighted_q(w_tomorrow, 0.2), round_decimals),
        "p70_tomorrow": round_value(weighted_q(w_tomorrow, 0.7), round_decimals),
        "min_tomorrow_time": extreme_time(tomorrow_rows, pick="min"),
        "max_tomorrow_time": extreme_time(tomorrow_rows, pick="max"),
        "avg_last_3d": round_value(weighted_avg(past_3) if past_3 else None, round_decimals),
        "avg_last_7d": round_value(weighted_avg(past_7) if past_7 else None, round_decimals),
        "p20_last_3d": round_value(weighted_q(past_3, 0.2) if past_3 else None, round_decimals),
        "p70_last_3d": round_value(weighted_q(past_3, 0.7) if past_3 else None, round_decimals),
        "p20_last_7d": round_value(weighted_q(past_7, 0.2) if past_7 else None, round_decimals),
        "p70_last_7d": round_value(weighted_q(past_7, 0.7) if past_7 else None, round_decimals),
        "today_rows": len(today_rows),
        "tomorrow_rows": len(tomorrow_rows),
        "tomorrow_status": "ok" if tomorrow_rows else "absent",
        "pending_primary": pending_primary(all_rows, timezone_name),
        "last_primary_refresh_at": store.last_primary_refresh_at,
        "last_source_chain_fetch_at": store.last_source_chain_fetch_at,
        "last_successful_source_id": store.last_successful_source_id,
        "source_health": store.source_health,
        "timeline_status": timeline_state,
        "updated_at": format_iso(now, timespec="seconds"),
    }

    state = round_value(avg_today, round_decimals)
    return TimelineStats(
        state=state,
        attributes=attrs,
        current_price=round_value(current_price, round_decimals),
        current_price_start_time=current_price_start,
        status=timeline_state,
    )


def compute_timeline_status(*, today_rows: int, tomorrow_rows: int, has_primary_tomorrow: bool) -> str:
    """Compute the high-level timeline status."""

    if today_rows <= 0 and tomorrow_rows <= 0:
        return "no_data"
    if today_rows > 0 and tomorrow_rows <= 0:
        return "today_only"
    if today_rows <= 0 and tomorrow_rows > 0:
        return "tomorrow_only"
    if tomorrow_rows > 0 and not has_primary_tomorrow:
        return "tomorrow_not_from_prio0"
    return "today_and_tomorrow"


def rows_for_day(rows: list[SlotRow], target_day: date, tz: ZoneInfo) -> list[SlotRow]:
    """Return rows belonging to one local day."""

    out: list[SlotRow] = []
    for row in rows:
        dt = parse_iso_local(row["start_time"], tz)
        if dt is not None and dt.date() == target_day:
            out.append(row)
    out.sort(key=lambda item: item["start_time"])
    return out


def weighted_for_rows(
    rows: list[SlotRow],
    tz: ZoneInfo,
    fallback_slot_minutes: int,
) -> list[tuple[float, float]]:
    """Convert slot rows into value/weight pairs using slot duration in hours."""

    weighted: list[tuple[float, float]] = []
    for idx, row in enumerate(rows):
        dt = parse_iso_local(row["start_time"], tz)
        if dt is None:
            continue
        if idx + 1 < len(rows):
            next_dt = parse_iso_local(rows[idx + 1]["start_time"], tz)
            duration_h = (next_dt - dt).total_seconds() / 3600.0 if next_dt is not None else fallback_slot_minutes / 60.0
        else:
            duration_h = fallback_slot_minutes / 60.0

        weighted.append((float(row["price_per_kwh"]), max(0.05, duration_h)))
    return weighted


def weighted_avg(values: list[tuple[float, float]]) -> float | None:
    """Calculate the weighted average for value/weight pairs."""

    if not values:
        return None
    total_w = sum(weight for _, weight in values if weight > 0)
    if total_w <= 0:
        return None
    return sum(value * weight for value, weight in values if weight > 0) / total_w


def weighted_q(values: list[tuple[float, float]], q: float) -> float | None:
    """Calculate a weighted quantile for value/weight pairs."""

    pairs = sorted((value, weight) for value, weight in values if weight > 0)
    if not pairs:
        return None
    total = sum(weight for _, weight in pairs)
    target = total * q
    seen = 0.0
    for value, weight in pairs:
        seen += weight
        if seen >= target:
            return value
    return pairs[-1][0]


def current_price_for_now(
    rows: list[SlotRow],
    now: datetime,
    tz: ZoneInfo,
    fallback_slot_minutes: int,
) -> tuple[float | None, str | None]:
    """Return the price segment covering the current instant."""

    parsed_rows: list[tuple[datetime, SlotRow]] = []
    for row in rows:
        dt = parse_iso_local(row["start_time"], tz)
        if dt is not None:
            parsed_rows.append((dt, row))
    parsed_rows.sort(key=lambda item: item[0])
    if not parsed_rows:
        return None, None

    for idx, (dt, row) in enumerate(parsed_rows):
        next_dt = parsed_rows[idx + 1][0] if idx + 1 < len(parsed_rows) else dt + timedelta(minutes=fallback_slot_minutes)
        if dt <= now < next_dt:
            try:
                return float(row["price_per_kwh"]), row["start_time"]
            except (TypeError, ValueError):
                return None, None

    return None, None


def extreme_time(rows: list[SlotRow], *, pick: str) -> str | None:
    """Return the start time of the first min/max price row."""

    if not rows:
        return None

    values = [float(row["price_per_kwh"]) for row in rows]
    target = min(values) if pick == "min" else max(values)
    for row in rows:
        if float(row["price_per_kwh"]) == target:
            return row["start_time"]
    return None


def round_value(value: float | None, decimals: int) -> float | None:
    """Round a value or keep None."""

    return None if value is None else round(float(value), decimals)
