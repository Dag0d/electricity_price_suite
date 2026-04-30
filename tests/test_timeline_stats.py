from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from typing import TypedDict
from types import ModuleType
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = ROOT / "custom_components" / "electricity_price_suite"

custom_components_pkg = sys.modules.setdefault("custom_components", ModuleType("custom_components"))
custom_components_pkg.__path__ = [str(ROOT / "custom_components")]

suite_pkg = sys.modules.setdefault(
    "custom_components.electricity_price_suite",
    ModuleType("custom_components.electricity_price_suite"),
)
suite_pkg.__path__ = [str(PKG_ROOT)]


class SlotRow(TypedDict):
    start_time: str
    market_price_per_kwh: float
    price_per_kwh: float
    provider_final_price_per_kwh: float | None
    source_id: str
    source_priority: int
    is_primary_source: bool
    observed_at: str


class SlotRecord(SlotRow):
    pass


class TimelineStats:
    pass


models_module = ModuleType("custom_components.electricity_price_suite.models")
models_module.SlotRow = SlotRow
models_module.SlotRecord = SlotRecord
models_module.TimelineStats = TimelineStats
sys.modules[models_module.__name__] = models_module

time_utils_module = ModuleType("custom_components.electricity_price_suite.time_utils")
time_utils_module.format_iso = lambda dt, timespec="seconds": dt.isoformat(timespec=timespec)
time_utils_module.parse_iso_in_tz = lambda value, tz: datetime.fromisoformat(value).astimezone(tz)
sys.modules[time_utils_module.__name__] = time_utils_module

spec = importlib.util.spec_from_file_location(
    "custom_components.electricity_price_suite.timeline_stats",
    PKG_ROOT / "timeline_stats.py",
)
timeline_stats_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = timeline_stats_module
assert spec.loader is not None
spec.loader.exec_module(timeline_stats_module)

expected_slots_for_day = timeline_stats_module.expected_slots_for_day
has_primary_tomorrow_rows = timeline_stats_module.has_primary_tomorrow_rows
missing_today_tomorrow_primary = timeline_stats_module.missing_today_tomorrow_primary


def _build_primary_rows_for_day(target_day, tz: ZoneInfo, slot_minutes: int, count: int) -> list[dict]:
    start = datetime.combine(target_day, datetime.min.time(), tzinfo=tz)
    rows: list[dict] = []
    for idx in range(count):
        slot_start = start + timedelta(minutes=slot_minutes * idx)
        rows.append(
            {
                "start_time": slot_start.isoformat(),
                "market_price_per_kwh": 0.1,
                "price_per_kwh": 0.1,
                "provider_final_price_per_kwh": None,
                "source_id": "provider_1_entsoe",
                "source_priority": 0,
                "is_primary_source": True,
                "observed_at": "2026-01-01T00:00:00Z",
            }
        )
    return rows


def test_expected_slots_for_day_handles_dst_boundaries():
    tz = ZoneInfo("Europe/Berlin")

    assert expected_slots_for_day(datetime(2026, 3, 29).date(), tz, 15) == 92
    assert expected_slots_for_day(datetime(2026, 10, 25).date(), tz, 15) == 100
    assert expected_slots_for_day(datetime(2026, 4, 30).date(), tz, 15) == 96


def test_primary_tomorrow_requires_complete_slot_set():
    tz = ZoneInfo("Europe/Berlin")
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    today_expected = expected_slots_for_day(today, tz, 15)
    tomorrow_expected = expected_slots_for_day(tomorrow, tz, 15)

    rows = [
        *_build_primary_rows_for_day(today, tz, 15, today_expected),
        *_build_primary_rows_for_day(tomorrow, tz, 15, max(0, tomorrow_expected - 8)),
    ]

    assert missing_today_tomorrow_primary(rows, "Europe/Berlin", 15) == (False, True)
    assert has_primary_tomorrow_rows(rows, "Europe/Berlin", 15) is False
