from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
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

models_module = ModuleType("custom_components.electricity_price_suite.models")
models_module.ConsumptionDailyRollup = dict
models_module.ConsumptionMonthlyRollup = dict
models_module.ConsumptionPowerActiveBlock = dict
models_module.ConsumptionPowerBucketRow = dict
models_module.ConsumptionSlotRow = dict
sys.modules[models_module.__name__] = models_module

time_utils_module = ModuleType("custom_components.electricity_price_suite.time_utils")
time_utils_module.parse_iso_in_tz = lambda value, tz: datetime.fromisoformat(str(value)).astimezone(tz)
sys.modules[time_utils_module.__name__] = time_utils_module

spec = importlib.util.spec_from_file_location(
    "custom_components.electricity_price_suite.consumption_stats",
    PKG_ROOT / "consumption_stats.py",
)
consumption_stats_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = consumption_stats_module
assert spec.loader is not None
spec.loader.exec_module(consumption_stats_module)

build_consumption_metrics = consumption_stats_module.build_consumption_metrics


def _today_slot(*, consumption_kwh: float, energy_cost: float) -> dict:
    tz = ZoneInfo("Europe/Berlin")
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=15)
    return {
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "consumption_kwh": consumption_kwh,
        "price_per_kwh": energy_cost / consumption_kwh,
        "energy_cost": energy_cost,
        "observed_at": now.isoformat(timespec="seconds"),
    }


def test_consumption_metrics_preserve_negative_energy_costs():
    metrics = build_consumption_metrics(
        slots=[_today_slot(consumption_kwh=1.0, energy_cost=-0.25)],
        daily_rollups={},
        monthly_rollups={},
        power_buckets=[],
        power_active_block=None,
        timezone_name="Europe/Berlin",
        round_decimals=4,
        fixed_fee_monthly_amount=0.0,
        fixed_fee_daily_amount=0.0,
        fixed_fee_tax_percent=0.0,
        fixed_fee_values_include_tax=True,
        current_month_fixed_fee_mode="prorated",
        avg_price_include_basic_fee=False,
        consumption_energy_entity="sensor.energy_total",
    )

    assert metrics["cost_today"] == -0.25
    assert metrics["cost_month"] == -0.25
    assert metrics["avg_paid_price_today"] == -0.25
    assert metrics["avg_paid_price_month"] == -0.25


def test_consumption_metrics_mix_negative_energy_costs_with_positive_fixed_fees():
    metrics = build_consumption_metrics(
        slots=[_today_slot(consumption_kwh=1.0, energy_cost=-0.25)],
        daily_rollups={},
        monthly_rollups={},
        power_buckets=[],
        power_active_block=None,
        timezone_name="Europe/Berlin",
        round_decimals=4,
        fixed_fee_monthly_amount=0.0,
        fixed_fee_daily_amount=0.10,
        fixed_fee_tax_percent=0.0,
        fixed_fee_values_include_tax=True,
        current_month_fixed_fee_mode="prorated",
        avg_price_include_basic_fee=True,
        consumption_energy_entity="sensor.energy_total",
    )

    assert metrics["cost_today"] == -0.25
    assert metrics["cost_today_incl_basic_fee"] == -0.15
    assert metrics["avg_paid_price_today"] == -0.15


def test_consumption_metrics_include_daily_rollups_for_yesterday():
    tz = ZoneInfo("Europe/Berlin")
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()
    metrics = build_consumption_metrics(
        slots=[],
        daily_rollups={
            yesterday.isoformat(): {
                "date": yesterday.isoformat(),
                "consumption_kwh": 2.0,
                "energy_cost": 0.6,
                "updated_at": datetime.now(tz).isoformat(timespec="seconds"),
            }
        },
        monthly_rollups={},
        power_buckets=[],
        power_active_block=None,
        timezone_name="Europe/Berlin",
        round_decimals=4,
        fixed_fee_monthly_amount=0.0,
        fixed_fee_daily_amount=0.0,
        fixed_fee_tax_percent=0.0,
        fixed_fee_values_include_tax=True,
        current_month_fixed_fee_mode="prorated",
        avg_price_include_basic_fee=False,
        consumption_energy_entity="sensor.energy_total",
    )

    assert metrics["consumption_yesterday_kwh"] == 2.0
    assert metrics["cost_yesterday"] == 0.6
    assert metrics["avg_paid_price_yesterday"] == 0.3
