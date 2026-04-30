"""Persistent store for timeline and plan data."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY_PREFIX, STORAGE_VERSION
from .consumption_stats import month_key
from .models import (
    ConsumptionMonthlyRollup,
    ConsumptionPowerDayStats,
    ConsumptionSlotRow,
    PlanPayload,
    SlotRecord,
    SlotRow,
    SourceConfig,
    utc_now_iso,
)
from .time_utils import format_iso, parse_iso_aware


class TimelineStore:
    """Storage-backed timeline data manager."""

    def __init__(self, hass: HomeAssistant, timeline_id: str, retention_days: int) -> None:
        self._hass = hass
        self._timeline_id = timeline_id
        self._retention_days = retention_days
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}{timeline_id}")
        self._data: dict = {
            "slots": {},
            "last_primary_refresh_at": None,
            "last_source_chain_fetch_at": None,
            "last_successful_source_id": None,
            "source_health": {},
            "plans": {},
            "sources": [],
            "consumption": {
                "last_snapshot": None,
                "slots": {},
                "monthly_rollups": {},
                "power_day": None,
            },
        }

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        if isinstance(loaded, dict):
            self._data.update(loaded)

    async def async_save(self) -> None:
        await self._store.async_save(self._data)

    def set_source_health(self, source_id: str, healthy: bool, reason: str | None) -> None:
        self._data.setdefault("source_health", {})[source_id] = {
            "healthy": healthy,
            "reason": reason,
            "updated_at": utc_now_iso(),
        }

    def set_last_successful_source(self, source_id: str) -> None:
        self._data["last_successful_source_id"] = source_id

    def set_last_primary_refresh(self) -> None:
        self._data["last_primary_refresh_at"] = utc_now_iso()

    def set_last_source_chain_fetch(self) -> None:
        self._data["last_source_chain_fetch_at"] = utc_now_iso()

    @property
    def last_primary_refresh_at(self) -> str | None:
        return self._data.get("last_primary_refresh_at")

    @property
    def last_successful_source_id(self) -> str | None:
        return self._data.get("last_successful_source_id")

    @property
    def last_source_chain_fetch_at(self) -> str | None:
        return self._data.get("last_source_chain_fetch_at")

    @property
    def source_health(self) -> dict:
        return self._data.get("source_health", {})

    def upsert_slots(self, slots: list[SlotRecord]) -> dict[str, int]:
        by_start: dict[str, dict] = self._data.setdefault("slots", {})
        return merge_slot_dicts(by_start, slots)

    def normalize_slot_timezones(self, timezone_name: str) -> int:
        """Normalize persisted slot keys into the HA timezone and collapse duplicates."""

        tz = ZoneInfo(timezone_name)
        by_start: dict[str, SlotRow] = self._data.setdefault("slots", {})
        if not by_start:
            return 0

        normalized: dict[str, SlotRow] = {}
        changed = 0
        for key, row in list(by_start.items()):
            parsed = parse_iso_aware(row.get("start_time") or key)
            if parsed is None:
                by_start.pop(key, None)
                changed += 1
                continue

            local_key = format_iso(parsed.astimezone(tz))
            slot = SlotRecord(
                start_time=local_key,
                market_price_per_kwh=float(row["market_price_per_kwh"]),
                price_per_kwh=float(row["price_per_kwh"]),
                provider_final_price_per_kwh=(
                    float(row["provider_final_price_per_kwh"])
                    if row.get("provider_final_price_per_kwh") is not None
                    else None
                ),
                source_id=str(row["source_id"]),
                source_priority=int(row["source_priority"]),
                is_primary_source=bool(row["is_primary_source"]),
                observed_at=str(row["observed_at"]),
            )
            merge_slot_dicts(normalized, [slot])
            if local_key != key:
                changed += 1

        if changed:
            self._data["slots"] = normalized
        return changed

    def purge_old_slots(self, timezone_name: str) -> int:
        tz = ZoneInfo(timezone_name)
        cutoff_date = (datetime.now(tz) - timedelta(days=self._retention_days)).date()
        by_start: dict[str, dict] = self._data.setdefault("slots", {})
        old_keys: list[str] = []
        for key in by_start:
            dt = parse_iso_aware(key)
            if dt is None:
                old_keys.append(key)
                continue
            if dt.astimezone(tz).date() < cutoff_date:
                old_keys.append(key)

        for key in old_keys:
            by_start.pop(key, None)

        return len(old_keys)

    def clear_slots_for_dates(self, timezone_name: str, dates: set[datetime.date]) -> int:
        """Delete all stored slots that belong to the given local dates."""

        if not dates:
            return 0

        tz = ZoneInfo(timezone_name)
        by_start: dict[str, dict] = self._data.setdefault("slots", {})
        remove_keys: list[str] = []
        for key in by_start:
            dt = parse_iso_aware(key)
            if dt is None:
                continue
            if dt.astimezone(tz).date() in dates:
                remove_keys.append(key)

        for key in remove_keys:
            by_start.pop(key, None)

        return len(remove_keys)

    def get_slots(self) -> list[SlotRow]:
        by_start: dict[str, SlotRow] = self._data.get("slots", {})
        rows = list(by_start.values())
        rows.sort(key=lambda item: item["start_time"])
        return rows

    def get_consumption_slots(self) -> list[ConsumptionSlotRow]:
        by_start: dict[str, ConsumptionSlotRow] = self._data.setdefault("consumption", {}).setdefault("slots", {})
        rows = list(by_start.values())
        rows.sort(key=lambda item: item["start_time"])
        return rows

    def get_consumption_monthly_rollups(self) -> dict[str, ConsumptionMonthlyRollup]:
        return dict(self._data.setdefault("consumption", {}).setdefault("monthly_rollups", {}))

    def get_consumption_last_snapshot(self) -> dict | None:
        return self._data.setdefault("consumption", {}).get("last_snapshot")

    def get_consumption_power_day_stats(self) -> ConsumptionPowerDayStats | None:
        value = self._data.setdefault("consumption", {}).get("power_day")
        if isinstance(value, dict):
            return dict(value)
        return None

    def set_consumption_last_snapshot(self, *, taken_at: str, energy_kwh: float) -> None:
        self._data.setdefault("consumption", {})["last_snapshot"] = {
            "taken_at": taken_at,
            "energy_kwh": float(energy_kwh),
        }

    def add_consumption_power_sample(self, *, local_date: str, power_w: float) -> None:
        consumption = self._data.setdefault("consumption", {})
        existing = consumption.get("power_day")
        if not isinstance(existing, dict) or existing.get("date") != local_date:
            consumption["power_day"] = {
                "date": local_date,
                "sample_count": 1,
                "power_sum_w": float(power_w),
                "power_min_w": float(power_w),
                "power_max_w": float(power_w),
                "updated_at": utc_now_iso(),
            }
            return

        existing["sample_count"] = int(existing.get("sample_count", 0) or 0) + 1
        existing["power_sum_w"] = float(existing.get("power_sum_w", 0.0) or 0.0) + float(power_w)
        existing["power_min_w"] = min(float(existing.get("power_min_w", power_w)), float(power_w))
        existing["power_max_w"] = max(float(existing.get("power_max_w", power_w)), float(power_w))
        existing["updated_at"] = utc_now_iso()

    def add_consumption_slot(
        self,
        *,
        start_time: str,
        end_time: str,
        consumption_kwh: float,
        price_per_kwh: float | None,
        energy_cost: float | None,
    ) -> None:
        if consumption_kwh <= 0:
            return
        by_start: dict[str, ConsumptionSlotRow] = self._data.setdefault("consumption", {}).setdefault("slots", {})
        existing = by_start.get(start_time)
        if existing is None:
            by_start[start_time] = {
                "start_time": start_time,
                "end_time": end_time,
                "consumption_kwh": float(consumption_kwh),
                "price_per_kwh": float(price_per_kwh) if price_per_kwh is not None else None,
                "energy_cost": float(energy_cost) if energy_cost is not None else None,
                "observed_at": utc_now_iso(),
            }
            return

        existing["end_time"] = end_time
        existing["consumption_kwh"] = float(existing.get("consumption_kwh", 0.0) or 0.0) + float(consumption_kwh)
        if energy_cost is not None:
            existing["energy_cost"] = float(existing.get("energy_cost", 0.0) or 0.0) + float(energy_cost)
        elif existing.get("energy_cost") is None:
            existing["energy_cost"] = None
        existing["price_per_kwh"] = float(price_per_kwh) if price_per_kwh is not None else existing.get("price_per_kwh")
        existing["observed_at"] = utc_now_iso()

    def purge_unpriced_consumption_slots(self) -> int:
        consumption = self._data.setdefault("consumption", {})
        by_start: dict[str, ConsumptionSlotRow] = consumption.setdefault("slots", {})
        remove_keys = [
            key
            for key, row in by_start.items()
            if row.get("price_per_kwh") is None and row.get("energy_cost") is None
        ]
        for key in remove_keys:
            by_start.pop(key, None)
        return len(remove_keys)

    def purge_old_consumption_slots(self, timezone_name: str, retention_days: int) -> int:
        tz = ZoneInfo(timezone_name)
        cutoff_date = (datetime.now(tz) - timedelta(days=retention_days)).date()
        consumption = self._data.setdefault("consumption", {})
        by_start: dict[str, ConsumptionSlotRow] = consumption.setdefault("slots", {})
        monthly_rollups: dict[str, ConsumptionMonthlyRollup] = consumption.setdefault("monthly_rollups", {})

        remove_keys: list[str] = []
        purged_month_totals: dict[str, tuple[float, float]] = {}
        for key, row in by_start.items():
            dt = parse_iso_aware(key)
            if dt is None:
                remove_keys.append(key)
                continue
            local_dt = dt.astimezone(tz)
            if local_dt.date() >= cutoff_date:
                continue
            remove_keys.append(key)
            bucket = month_key(local_dt)
            prev_energy, prev_cost = purged_month_totals.get(bucket, (0.0, 0.0))
            purged_month_totals[bucket] = (
                prev_energy + float(row.get("consumption_kwh", 0.0) or 0.0),
                prev_cost + float(row.get("energy_cost", 0.0) or 0.0),
            )

        for key in remove_keys:
            by_start.pop(key, None)

        for bucket, (energy, cost) in purged_month_totals.items():
            existing = monthly_rollups.get(bucket)
            if existing is None:
                monthly_rollups[bucket] = {
                    "month": bucket,
                    "consumption_kwh": float(energy),
                    "energy_cost": float(cost),
                    "updated_at": utc_now_iso(),
                }
                continue
            existing["consumption_kwh"] = float(existing.get("consumption_kwh", 0.0) or 0.0) + float(energy)
            existing["energy_cost"] = float(existing.get("energy_cost", 0.0) or 0.0) + float(cost)
            existing["updated_at"] = utc_now_iso()

        return len(remove_keys)

    def set_plan(self, device_slug: str, payload: PlanPayload) -> None:
        self._data.setdefault("plans", {})[device_slug] = payload

    def get_plans(self) -> dict[str, PlanPayload]:
        return self._data.get("plans", {})

    def replace_plans(self, plans: dict[str, PlanPayload]) -> None:
        self._data["plans"] = dict(plans)

    def delete_plan(self, device_slug: str) -> bool:
        plans = self._data.setdefault("plans", {})
        if device_slug in plans:
            plans.pop(device_slug, None)
            return True
        return False

    def clear_runtime_state(
        self,
        *,
        clear_slots: bool,
        clear_sources: bool,
        clear_consumption: bool,
        preserve_last_snapshot: bool,
        dry_run: bool = False,
    ) -> dict[str, int]:
        result = {
            "cleared_slots": 0,
            "cleared_sources": 0,
            "cleared_consumption_slots": 0,
            "cleared_consumption_monthly_rollups": 0,
            "cleared_source_health": 0,
            "preserved_last_snapshot": 0,
        }

        if clear_slots:
            slots = self._data.setdefault("slots", {})
            result["cleared_slots"] = len(slots)
            if not dry_run:
                self._data["slots"] = {}

        if clear_sources:
            sources = self._data.setdefault("sources", [])
            result["cleared_sources"] = len(sources)
            if not dry_run:
                self._data["sources"] = []

        if clear_consumption:
            consumption = self._data.setdefault("consumption", {})
            slot_rows = consumption.setdefault("slots", {})
            monthly_rollups = consumption.setdefault("monthly_rollups", {})
            result["cleared_consumption_slots"] = len(slot_rows)
            result["cleared_consumption_monthly_rollups"] = len(monthly_rollups)
            last_snapshot = consumption.get("last_snapshot")
            if preserve_last_snapshot and isinstance(last_snapshot, dict):
                result["preserved_last_snapshot"] = 1
            if not dry_run:
                if not preserve_last_snapshot:
                    consumption["last_snapshot"] = None
                consumption["slots"] = {}
                consumption["monthly_rollups"] = {}
                consumption["power_day"] = None

        source_health = self._data.setdefault("source_health", {})
        result["cleared_source_health"] = len(source_health)
        if not dry_run:
            self._data["source_health"] = {}
            self._data["last_primary_refresh_at"] = None
            self._data["last_source_chain_fetch_at"] = None
            self._data["last_successful_source_id"] = None

        return result

    def get_sources(self) -> list[SourceConfig]:
        return list(self._data.get("sources", []))

    def upsert_source(self, source: SourceConfig) -> None:
        sources = self._data.setdefault("sources", [])
        source_id = str(source.get("id"))
        for idx, existing in enumerate(sources):
            if str(existing.get("id")) == source_id:
                sources[idx] = source
                break
        else:
            sources.append(source)
        sources.sort(key=lambda item: int(item.get("priority", 9999)))

    def get_source(self, source_id: str) -> SourceConfig | None:
        for source in self._data.get("sources", []):
            if str(source.get("id")) == str(source_id):
                return dict(source)
        return None

    def delete_source(self, source_id: str) -> bool:
        sources = self._data.setdefault("sources", [])
        for idx, existing in enumerate(sources):
            if str(existing.get("id")) == str(source_id):
                sources.pop(idx)
                return True
        return False


def merge_slot_dicts(by_start: dict[str, SlotRow], slots: list[SlotRecord]) -> dict[str, int]:
    """Apply rank overwrite policy to a slot dictionary."""

    inserted = 0
    replaced = 0
    ignored = 0

    for slot in slots:
        key = slot.start_time
        existing = by_start.get(key)
        if existing is None:
            by_start[key] = slot.to_dict()
            inserted += 1
            continue

        old_prio = int(existing.get("source_priority", 9999))
        new_prio = int(slot.source_priority)
        if new_prio <= old_prio:
            by_start[key] = slot.to_dict()
            replaced += 1
        else:
            ignored += 1

    return {"inserted": inserted, "replaced": replaced, "ignored": ignored}
