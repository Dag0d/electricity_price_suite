from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = ROOT / "custom_components" / "electricity_price_suite"

custom_components_pkg = sys.modules.setdefault("custom_components", ModuleType("custom_components"))
custom_components_pkg.__path__ = [str(ROOT / "custom_components")]

suite_pkg = sys.modules.setdefault(
    "custom_components.electricity_price_suite",
    ModuleType("custom_components.electricity_price_suite"),
)
suite_pkg.__path__ = [str(PKG_ROOT)]

ha_core_module = ModuleType("homeassistant.core")
ha_core_module.HomeAssistant = object
sys.modules[ha_core_module.__name__] = ha_core_module

aiohttp_client_module = ModuleType("homeassistant.helpers.aiohttp_client")
aiohttp_client_module.async_get_clientsession = lambda hass: None
sys.modules[aiohttp_client_module.__name__] = aiohttp_client_module

models_module = ModuleType("custom_components.electricity_price_suite.models")
models_module.SlotRecord = object
models_module.SourceAttempt = object
models_module.utc_now_iso = lambda: "2026-01-01T00:00:00Z"
sys.modules[models_module.__name__] = models_module

time_utils_module = ModuleType("custom_components.electricity_price_suite.time_utils")
time_utils_module.format_iso = lambda dt, timespec="seconds": dt.isoformat(timespec=timespec)
time_utils_module.parse_iso_aware = lambda value: datetime.fromisoformat(str(value).replace("Z", "+00:00"))
sys.modules[time_utils_module.__name__] = time_utils_module

spec = importlib.util.spec_from_file_location(
    "custom_components.electricity_price_suite.providers",
    PKG_ROOT / "providers.py",
)
providers_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = providers_module
assert spec.loader is not None
spec.loader.exec_module(providers_module)

append_rows = providers_module._append_entsoe_period_rows


def test_entsoe_gap_fill_repeats_previous_price_for_missing_positions():
    xml = """
    <Period xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
      <Point><position>6</position><price.amount>103.16</price.amount></Point>
      <Point><position>8</position><price.amount>101.10</price.amount></Point>
    </Period>
    """
    period = ET.fromstring(xml)
    rows: list[dict] = []

    append_rows(
        rows,
        period_start=datetime(2026, 4, 30, 22, 0, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        source_minutes=15,
        curve_type="A03",
        points=period.findall("{urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3}Point"),
    )

    assert len(rows) == 3
    assert rows[0]["start_time"] == "2026-04-30T23:15:00+00:00"
    assert rows[0]["market_price_per_kwh"] == 0.10316
    assert rows[1]["start_time"] == "2026-04-30T23:30:00+00:00"
    assert rows[1]["market_price_per_kwh"] == 0.10316
    assert rows[2]["start_time"] == "2026-04-30T23:45:00+00:00"
    assert rows[2]["market_price_per_kwh"] == 0.1011


def test_entsoe_gap_fill_extends_last_block_to_period_end_for_a03():
    xml = """
    <Period xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
      <Point><position>1</position><price.amount>100.00</price.amount></Point>
      <Point><position>6</position><price.amount>103.16</price.amount></Point>
    </Period>
    """
    period = ET.fromstring(xml)
    rows: list[dict] = []

    append_rows(
        rows,
        period_start=datetime(2026, 4, 30, 22, 0, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        source_minutes=15,
        curve_type="A03",
        points=period.findall("{urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3}Point"),
    )

    assert len(rows) == 8
    assert rows[-3]["start_time"] == "2026-04-30T23:15:00+00:00"
    assert rows[-3]["market_price_per_kwh"] == 0.10316
    assert rows[-2]["start_time"] == "2026-04-30T23:30:00+00:00"
    assert rows[-2]["market_price_per_kwh"] == 0.10316
    assert rows[-1]["start_time"] == "2026-04-30T23:45:00+00:00"
    assert rows[-1]["market_price_per_kwh"] == 0.10316
