from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = ROOT / "custom_components" / "electricity_price_suite"

custom_components_pkg = sys.modules.setdefault("custom_components", ModuleType("custom_components"))
custom_components_pkg.__path__ = [str(ROOT / "custom_components")]

suite_pkg = sys.modules.setdefault(
    "custom_components.electricity_price_suite",
    ModuleType("custom_components.electricity_price_suite"),
)
suite_pkg.__path__ = [str(PKG_ROOT)]

time_utils_module = ModuleType("custom_components.electricity_price_suite.time_utils")
time_utils_module.format_iso = lambda dt, timespec="seconds": dt.isoformat(timespec=timespec)
sys.modules[time_utils_module.__name__] = time_utils_module

spec = importlib.util.spec_from_file_location(
    "custom_components.electricity_price_suite.tariff_schedule",
    PKG_ROOT / "tariff_schedule.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)

derive_absolute_surcharge_from_final_prices = module.derive_absolute_surcharge_from_final_prices
parse_effective_from = module.parse_effective_from
parse_final_price_lines = module.parse_final_price_lines
round_absolute_surcharge = module.round_absolute_surcharge


def test_parse_final_price_lines_supports_sequence_format():
    values = parse_final_price_lines(
        effective_from=parse_effective_from("2026-06-01"),
        timezone_name="Europe/Berlin",
        billing_slot_minutes=15,
        final_price_lines="0.2100\n0.3760\n0.2980",
        sequence_start="00:15",
    )

    assert values == [
        ("2026-06-01T00:15:00+02:00", 0.21),
        ("2026-06-01T00:30:00+02:00", 0.376),
        ("2026-06-01T00:45:00+02:00", 0.298),
    ]


def test_derive_absolute_surcharge_from_final_prices_uses_matching_market_slots():
    result = derive_absolute_surcharge_from_final_prices(
        market_prices_by_start={
            "2026-06-01T00:00:00+02:00": 0.10,
            "2026-06-01T00:15:00+02:00": 0.20,
            "2026-06-01T00:30:00+02:00": -0.05,
        },
        final_prices=[
            ("2026-06-01T00:00:00+02:00", 0.1785),
            ("2026-06-01T00:15:00+02:00", 0.2975),
            ("2026-06-01T00:30:00+02:00", 0.0),
        ],
        surcharge_percent=0.0,
        tax_percent=19.0,
    )

    assert result["samples_total"] == 3
    assert result["samples_used"] == 3
    assert round(result["derived_absolute_surcharge"], 6) == 0.05


def test_round_absolute_surcharge_limits_precision_to_5_decimals():
    assert round_absolute_surcharge(0.16582697478991598) == 0.16583
