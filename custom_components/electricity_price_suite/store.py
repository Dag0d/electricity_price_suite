"""Persistent store for timeline and plan data."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY_PREFIX, STORAGE_VERSION
from .models import PlanPayload, SlotRecord, SlotRow, SourceConfig


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
            "updated_at": dt_util.utcnow().isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

    def set_last_successful_source(self, source_id: str) -> None:
        self._data["last_successful_source_id"] = source_id

    def set_last_primary_refresh(self) -> None:
        self._data["last_primary_refresh_at"] = dt_util.utcnow().isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    def set_last_source_chain_fetch(self) -> None:
        self._data["last_source_chain_fetch_at"] = dt_util.utcnow().isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

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

    def purge_old_slots(self, timezone_name: str) -> int:
        tz = ZoneInfo(timezone_name)
        cutoff_date = (datetime.now(tz) - timedelta(days=self._retention_days)).date()
        by_start: dict[str, dict] = self._data.setdefault("slots", {})
        old_keys: list[str] = []
        for key in by_start:
            try:
                dt = datetime.fromisoformat(key)
            except ValueError:
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
            try:
                dt = datetime.fromisoformat(key)
            except ValueError:
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

    def set_plan(self, device_slug: str, payload: PlanPayload) -> None:
        self._data.setdefault("plans", {})[device_slug] = payload

    def get_plans(self) -> dict[str, PlanPayload]:
        return self._data.get("plans", {})

    def delete_plan(self, device_slug: str) -> bool:
        plans = self._data.setdefault("plans", {})
        if device_slug in plans:
            plans.pop(device_slug, None)
            return True
        return False

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
