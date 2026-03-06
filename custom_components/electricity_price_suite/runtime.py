"""Runtime objects for electricity_price_suite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import slugify
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_PRICE_PER_KWH,
    ATTR_START_TIME,
    CONF_CACHE_RETENTION_DAYS,
    CONF_CURRENCY,
    CONF_ENABLE_CURRENT_PRICE_SENSOR,
    CONF_SOURCE_CHAIN,
    CONF_ROUND_DECIMALS,
    DEFAULT_BILLING_SLOT_MINUTES,
    DEFAULT_ENABLE_CURRENT_PRICE_SENSOR,
    DEFAULT_ROUND_DECIMALS,
    DOMAIN,
)
from .models import PlanResult, SlotRecord
from .optimizer import optimize_runtime
from .providers import fetch_from_source, normalize_slots
from .store import TimelineStore

_LOGGER = logging.getLogger(__name__)


def _parse_iso_local(value: str, tz: ZoneInfo) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(tz)


def _weighted_avg(values: list[tuple[float, float]]) -> float | None:
    if not values:
        return None
    total_w = sum(w for _, w in values if w > 0)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in values if w > 0) / total_w


def _weighted_q(values: list[tuple[float, float]], q: float) -> float | None:
    pairs = sorted((v, w) for v, w in values if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    target = total * q
    seen = 0.0
    for value, weight in pairs:
        seen += weight
        if seen >= target:
            return value
    return pairs[-1][0]


@dataclass(slots=True)
class TimelineStats:
    state: float | str
    attributes: dict[str, Any]
    current_price: float | None
    current_price_start_time: str | None
    status: str


class TimelineRuntime:
    """One runtime timeline bound to one config entry."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.timeline_id = entry.entry_id
        self.timeline_name = entry.title
        self.timeline_slug = slugify(self.timeline_name)

        self.timezone = hass.config.time_zone
        self.currency = entry.options.get(CONF_CURRENCY, entry.data.get(CONF_CURRENCY, "EUR"))
        self.round_decimals = int(
            entry.options.get(CONF_ROUND_DECIMALS, entry.data.get(CONF_ROUND_DECIMALS, DEFAULT_ROUND_DECIMALS))
        )
        self.enable_current_price_sensor = bool(
            entry.options.get(
                CONF_ENABLE_CURRENT_PRICE_SENSOR,
                entry.data.get(CONF_ENABLE_CURRENT_PRICE_SENSOR, DEFAULT_ENABLE_CURRENT_PRICE_SENSOR),
            )
        )
        self.source_chain = list(
            entry.options.get(CONF_SOURCE_CHAIN, entry.data.get(CONF_SOURCE_CHAIN, []))
        )
        retention = int(
            entry.options.get(
                CONF_CACHE_RETENTION_DAYS,
                entry.data.get(CONF_CACHE_RETENTION_DAYS, 7),
            )
        )
        self.store = TimelineStore(hass, self.timeline_id, retention)

        self.timeline_sensor = None
        self.current_price_sensor = None
        self.status_sensor = None
        self.plan_sensors: dict[str, Any] = {}
        self._add_entities = None
        self._unsub_scheduled_update = None
        self._unsub_scheduled_poll = None

        self.latest_stats = TimelineStats(
            state="unknown",
            attributes={},
            current_price=None,
            current_price_start_time=None,
            status="no_data",
        )

    def _detect_billing_slot_minutes(self, rows: list[dict]) -> int:
        if len(rows) < 2:
            return DEFAULT_BILLING_SLOT_MINUTES
        tz = ZoneInfo(self.timezone)
        parsed: list[datetime] = []
        for row in rows:
            dt = _parse_iso_local(row["start_time"], tz)
            if dt is not None:
                parsed.append(dt)
        parsed.sort()
        if len(parsed) < 2:
            return DEFAULT_BILLING_SLOT_MINUTES
        deltas: list[int] = []
        for idx in range(1, len(parsed)):
            diff = int(round((parsed[idx] - parsed[idx - 1]).total_seconds() / 60.0))
            if 1 <= diff <= 240:
                deltas.append(diff)
        if not deltas:
            return DEFAULT_BILLING_SLOT_MINUTES
        return min(deltas)

    async def async_initialize(self) -> None:
        await self.store.async_load()
        if not self.store.get_sources():
            for idx, source in enumerate(self.source_chain):
                self.store.upsert_source(self._normalize_source(source, idx))
            await self.store.async_save()
        await self._rebuild_from_store()
        self._schedule_next_time_update()
        self._schedule_next_poll_update()

    async def async_shutdown(self) -> None:
        if self._unsub_scheduled_update is not None:
            self._unsub_scheduled_update()
            self._unsub_scheduled_update = None
        if self._unsub_scheduled_poll is not None:
            self._unsub_scheduled_poll()
            self._unsub_scheduled_poll = None

    def register_add_entities(self, add_entities) -> None:
        self._add_entities = add_entities

    async def _rebuild_from_store(self) -> None:
        self.latest_stats = self._build_timeline_stats()
        self._schedule_next_time_update()

    def _round(self, value: float | None) -> float | None:
        return None if value is None else round(float(value), self.round_decimals)

    def _normalize_source(self, source: dict, fallback_priority: int) -> dict:
        normalized = dict(source)
        normalized.setdefault("id", f"source_{fallback_priority}")
        normalized.setdefault("priority", fallback_priority)
        normalized.setdefault("enabled", True)
        normalized.setdefault("slot_mapping", {"time_key": "start_time", "price_key": "price_per_kwh"})
        return normalized

    def _enabled_sources(self, override_sources: list[Any] | None = None) -> list[dict]:
        if override_sources and all(isinstance(item, dict) for item in override_sources):
            chain = [
                self._normalize_source(dict(item), idx)
                for idx, item in enumerate(override_sources)
                if item.get("enabled", True)
            ]
            chain.sort(key=lambda s: int(s.get("priority", 9999)))
            return chain

        chain = [
            self._normalize_source(s, idx)
            for idx, s in enumerate(self.store.get_sources() or self.source_chain)
            if s.get("enabled", True)
        ]
        chain.sort(key=lambda s: int(s.get("priority", 9999)))
        if override_sources:
            wanted = set(override_sources)
            chain = [s for s in chain if str(s.get("id")) in wanted]
        return chain

    async def async_refresh_timeline(
        self,
        *,
        override_sources: list[Any] | None,
        only_today_tomorrow: bool = True,
    ) -> dict[str, Any]:
        attempt_log: list[dict[str, Any]] = []
        merged_debug: dict[str, int] = {"inserted": 0, "replaced": 0, "ignored": 0}
        used_sources: list[str] = []
        fetched_source_chain = False
        active_sources = self._enabled_sources(override_sources)

        if not active_sources:
            self.latest_stats = self._build_timeline_stats()
            self._schedule_next_poll_update()
            return {
                "status": "no_data",
                "timeline_entity": self.timeline_entity_id,
                "timeline_status": self.latest_stats.status,
                "used_source": None,
                "used_sources": [],
                "attempt_log": [],
                "rows_today": self.latest_stats.attributes.get("today_rows", 0),
                "rows_tomorrow": self.latest_stats.attributes.get("tomorrow_rows", 0),
                "has_primary_data_for_tomorrow": self._has_primary_tomorrow_rows(),
                "pending_primary": self._pending_primary(),
                "merge_debug": merged_debug,
                "last_source_chain_fetch_at": self.store.last_source_chain_fetch_at,
                "reason": "no_sources_configured",
                "hint": "Configure a primary source via config flow or add_source service.",
            }

        need_today, need_tomorrow = self._missing_today_tomorrow_primary()

        for source in active_sources:
            # If primary already covers both days, no fallback query is needed.
            if need_today is False and need_tomorrow is False:
                break

            slots, attempt = await fetch_from_source(self.hass, source)
            attempt_log.append(attempt.to_dict())
            fetched_source_chain = True
            self.store.set_source_health(str(source.get("id")), attempt.success, attempt.reason)

            if not slots:
                continue

            if only_today_tomorrow:
                slots = self._filter_today_tomorrow_slots(slots)
            if not slots:
                continue

            # For fallback sources, keep only days still missing on primary level.
            if int(source.get("priority", 9999)) > 0:
                slots = self._filter_slots_for_missing_days(slots, need_today, need_tomorrow)
                if not slots:
                    continue

            used_source = str(source.get("id"))
            used_sources.append(used_source)
            merged = self.store.upsert_slots(slots)
            for key in merged_debug:
                merged_debug[key] += merged[key]
            self.store.set_last_successful_source(used_source)
            if int(source.get("priority", 9999)) == 0:
                self.store.set_last_primary_refresh()

            need_today, need_tomorrow = self._missing_today_tomorrow_primary()

        self.store.purge_old_slots(self.timezone)
        if fetched_source_chain:
            self.store.set_last_source_chain_fetch()
        await self.store.async_save()

        self.latest_stats = self._build_timeline_stats()
        await self._maybe_reoptimize_plans_after_data_update()
        self._schedule_next_poll_update()

        has_rows = bool(self.store.get_slots())
        rows_today = self.latest_stats.attributes.get("today_rows", 0)
        rows_tomorrow = self.latest_stats.attributes.get("tomorrow_rows", 0)

        has_primary_tomorrow = self._has_primary_tomorrow_rows()
        pending_primary = self._pending_primary()

        status = "ok" if has_rows else "no_data"

        _LOGGER.info(
            "timeline refresh %s: status=%s used_sources=%s pending_primary=%s merged=%s",
            self.timeline_slug,
            status,
            used_sources,
            pending_primary,
            merged_debug,
        )

        return {
            "status": status,
            "timeline_entity": self.timeline_entity_id,
            "timeline_status": self.latest_stats.status,
            "used_source": used_sources[0] if used_sources else None,
            "used_sources": used_sources,
            "attempt_log": attempt_log,
            "rows_today": rows_today,
            "rows_tomorrow": rows_tomorrow,
            "has_primary_data_for_tomorrow": has_primary_tomorrow,
            "pending_primary": pending_primary,
            "merge_debug": merged_debug,
            "last_source_chain_fetch_at": self.store.last_source_chain_fetch_at,
        }

    async def async_add_source(self, source: dict) -> dict[str, Any]:
        next_priority = len(self.store.get_sources())
        normalized = self._normalize_source(source, fallback_priority=next_priority)
        self.store.upsert_source(normalized)
        await self.store.async_save()
        return {
            "status": "ok",
            "timeline_entity": self.timeline_entity_id,
            "source": normalized,
            "source_count": len(self.store.get_sources()),
        }

    async def async_list_sources(self, source_id: str | None = None) -> dict[str, Any]:
        if source_id:
            source = self.store.get_source(source_id)
            return {
                "status": "ok" if source else "not_found",
                "timeline_entity": self.timeline_entity_id,
                "source": source,
            }
        sources = self.store.get_sources()
        return {
            "status": "ok",
            "timeline_entity": self.timeline_entity_id,
            "source_ids": [str(item.get("id")) for item in sources],
            "count": len(sources),
        }

    async def async_delete_source(self, source_id: str) -> dict[str, Any]:
        deleted = self.store.delete_source(source_id)
        if deleted:
            await self.store.async_save()
        return {
            "status": "ok" if deleted else "not_found",
            "timeline_entity": self.timeline_entity_id,
            "deleted_source_id": source_id if deleted else None,
            "source_count": len(self.store.get_sources()),
        }

    async def async_inject_slots(
        self,
        *,
        slots_payload: list[dict],
        source_name: str,
        source_priority: int,
        is_primary: bool,
    ) -> dict[str, Any]:
        source = {
            "id": source_name,
            "priority": source_priority,
            "slot_mapping": {"time_key": ATTR_START_TIME, "price_key": ATTR_PRICE_PER_KWH},
        }
        normalized = normalize_slots(slots_payload, source)
        if is_primary:
            normalized = [
                SlotRecord(
                    start_time=s.start_time,
                    price_per_kwh=s.price_per_kwh,
                    source_id=s.source_id,
                    source_priority=s.source_priority,
                    is_primary_source=True,
                    observed_at=s.observed_at,
                )
                for s in normalized
            ]

        merged = self.store.upsert_slots(normalized)
        self.store.set_last_successful_source(source_name)
        if is_primary or int(source_priority) == 0:
            self.store.set_last_primary_refresh()
        self.store.purge_old_slots(self.timezone)
        await self.store.async_save()

        self.latest_stats = self._build_timeline_stats()
        self._schedule_next_time_update()
        self._schedule_next_poll_update()
        await self._maybe_reoptimize_plans_after_data_update()

        _LOGGER.info(
            "slots injected %s: source=%s merged=%s",
            self.timeline_slug,
            source_name,
            merged,
        )

        return {
            "status": "ok" if normalized else "no_data",
            "timeline_entity": self.timeline_entity_id,
            "rows_received": len(normalized),
            "merge_debug": merged,
            "pending_primary": self._pending_primary(),
        }

    async def async_optimize_device(
        self,
        *,
        device_name: str,
        duration_minutes: float | None,
        energy_profile: list[float] | None,
        profile_slot_minutes: int | None,
        billing_slot_minutes: int | None,
        consumption_profile_logger: bool,
        consumption_profile_entity: str | None,
        consumption_profile_desired_slot_minutes: int | None,
        align_start_to_billing_slot: bool,
        epsilon_rel: float,
        prefer_earliest: bool,
        start_mode: str,
        start_in_minutes: float,
        deadline_mode: str,
        deadline_minutes: float | None,
        latest_start: str | None,
        latest_finish: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        profile_source = "service_payload"
        profile_meta: dict[str, Any] | None = None

        if consumption_profile_logger:
            (
                loaded_profile,
                loaded_duration,
                loaded_slot_minutes,
                profile_meta,
                load_reason,
            ) = await self._load_consumption_profile_logger(
                consumption_profile_entity=consumption_profile_entity,
                desired_slot_minutes=consumption_profile_desired_slot_minutes,
            )
            if load_reason is not None:
                result = self._build_no_candidate_result(load_reason)
                return await self._persist_plan_result(
                    device_name=device_name,
                    result=result,
                    deadline_mode=deadline_mode,
                    deadline_minutes=deadline_minutes,
                    latest_start=latest_start,
                    latest_finish=latest_finish,
                    epsilon_rel=epsilon_rel,
                    prefer_earliest=prefer_earliest,
                    dry_run=dry_run,
                    align_start_to_billing_slot=align_start_to_billing_slot,
                    profile_source="consumption_profile_logger",
                    profile_meta=profile_meta,
                )
            energy_profile = loaded_profile
            duration_minutes = loaded_duration
            profile_slot_minutes = loaded_slot_minutes
            profile_source = "consumption_profile_logger"

        slot_rows = self._slot_dicts_for_optimizer()
        bill_slot = int(billing_slot_minutes or self._detect_billing_slot_minutes(slot_rows))

        result = optimize_runtime(
            slots=slot_rows,
            timezone_name=self.timezone,
            billing_slot_minutes=bill_slot,
            duration_minutes=duration_minutes,
            energy_profile=energy_profile,
            profile_slot_minutes=profile_slot_minutes,
            epsilon_rel=epsilon_rel,
            prefer_earliest=prefer_earliest,
            start_mode=start_mode,
            start_in_minutes=start_in_minutes,
            deadline_mode=deadline_mode,
            deadline_minutes=deadline_minutes,
            latest_start=latest_start,
            latest_finish=latest_finish,
            align_start_to_billing_slot=align_start_to_billing_slot,
            reference_time=None,
        )

        return await self._persist_plan_result(
            device_name=device_name,
            result=result,
            deadline_mode=deadline_mode,
            deadline_minutes=deadline_minutes,
            latest_start=latest_start,
            latest_finish=latest_finish,
            epsilon_rel=epsilon_rel,
            prefer_earliest=prefer_earliest,
            dry_run=dry_run,
            align_start_to_billing_slot=align_start_to_billing_slot,
            profile_source=profile_source,
            profile_meta=profile_meta,
        )

    async def _persist_plan_result(
        self,
        *,
        device_name: str,
        result: PlanResult,
        deadline_mode: str,
        deadline_minutes: float | None,
        latest_start: str | None,
        latest_finish: str | None,
        epsilon_rel: float,
        prefer_earliest: bool,
        dry_run: bool,
        align_start_to_billing_slot: bool,
        profile_source: str,
        profile_meta: dict[str, Any] | None,
    ) -> dict[str, Any]:
        device_slug = slugify(device_name)
        entity_id = self.plan_entity_id(device_slug)

        plan_payload = {
            "device_name": device_name,
            "status": result.status,
            "reason": result.reason,
            "best_start": result.best_start,
            "best_end": result.best_end,
            "best_cost": result.best_cost,
            "window_start": result.window_start,
            "window_end": result.window_end,
            "deadline_mode": deadline_mode,
            "deadline_minutes": deadline_minutes,
            "latest_start": latest_start,
            "latest_finish": latest_finish,
            "duration_minutes": result.duration_minutes,
            "billing_slot_minutes": result.billing_slot_minutes,
            "profile_slot_minutes": result.profile_slot_minutes,
            "epsilon_rel": epsilon_rel,
            "prefer_earliest": prefer_earliest,
            "align_start_to_billing_slot": align_start_to_billing_slot,
            "candidates": result.candidates,
            "profile_used": result.profile_used,
            "profile_source": profile_source,
            "profile_meta": profile_meta,
            "requested_window_end": result.requested_window_end,
            "window_truncated_by_data": result.window_truncated_by_data,
            "price_coverage_end_at_compute": result.price_coverage_end,
            "computed_at": datetime.now(ZoneInfo(self.timezone)).isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "timeline_entity": self.timeline_entity_id,
        }

        self.store.set_plan(device_slug, plan_payload)
        await self.store.async_save()

        if device_slug in self.plan_sensors:
            self.plan_sensors[device_slug].async_update_from_payload(plan_payload)
        elif self._add_entities is not None:
            sensor = self._create_plan_sensor(device_slug, device_name)
            self.plan_sensors[device_slug] = sensor
            self._add_entities([sensor])

        return {
            "status": result.status,
            "plan_entity_id": entity_id,
            "best_start": result.best_start,
            "best_end": result.best_end,
            "best_cost": result.best_cost,
            "reason": result.reason,
        }

    async def async_manage_plan(self, *, device_slug: str, reset: bool, delete: bool) -> dict[str, Any]:
        plans = self.store.get_plans()
        existing = plans.get(device_slug)
        entity_id = self.plan_entity_id(device_slug)

        if existing is None:
            return {
                "status": "not_found",
                "plan_entity_id": entity_id,
                "reason": "plan_not_found",
            }

        if delete:
            self.store.delete_plan(device_slug)
            await self.store.async_save()
            self.plan_sensors.pop(device_slug, None)
            registry = er.async_get(self.hass)
            unique_id = f"{self.entry.entry_id}_plan_{device_slug}"
            stale_entity = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if stale_entity:
                registry.async_remove(stale_entity)
            return {
                "status": "deleted",
                "plan_entity_id": entity_id,
                "reason": "manual_delete",
            }

        payload = self._build_reset_payload(str(existing.get("device_name", device_slug)))
        self.store.set_plan(device_slug, payload)
        await self.store.async_save()

        if device_slug in self.plan_sensors:
            self.plan_sensors[device_slug].async_update_from_payload(payload)

        return {
            "status": "reset",
            "plan_entity_id": entity_id,
            "reason": "manual_reset",
        }

    def _build_reset_payload(self, device_name: str) -> dict[str, Any]:
        return {
            "device_name": device_name,
            "status": "reset",
            "reason": "manual_reset",
            "best_start": None,
            "best_end": None,
            "best_cost": None,
            "window_start": None,
            "window_end": None,
            "deadline_mode": "none",
            "deadline_minutes": None,
            "latest_start": None,
            "latest_finish": None,
            "duration_minutes": None,
            "billing_slot_minutes": None,
            "profile_slot_minutes": None,
            "epsilon_rel": None,
            "prefer_earliest": None,
            "align_start_to_billing_slot": None,
            "candidates": 0,
            "profile_used": [],
            "profile_source": "reset",
            "profile_meta": None,
            "requested_window_end": None,
            "window_truncated_by_data": False,
            "price_coverage_end_at_compute": None,
            "computed_at": datetime.now(ZoneInfo(self.timezone)).isoformat(timespec="seconds"),
            "dry_run": False,
            "timeline_entity": self.timeline_entity_id,
        }

    def _build_no_candidate_result(self, reason: str) -> PlanResult:
        now = datetime.now(ZoneInfo(self.timezone)).isoformat(timespec="minutes")
        return PlanResult(
            status="no-candidate",
            best_start=None,
            best_end=None,
            best_cost=None,
            reason=reason,
            candidates=0,
            profile_used=[],
            window_start=now,
            window_end=now,
            duration_minutes=None,
            billing_slot_minutes=DEFAULT_BILLING_SLOT_MINUTES,
            profile_slot_minutes=DEFAULT_BILLING_SLOT_MINUTES,
        )

    async def _load_consumption_profile_logger(
        self,
        *,
        consumption_profile_entity: str | None,
        desired_slot_minutes: int | None,
    ) -> tuple[list[float] | None, float | None, int | None, dict[str, Any] | None, str | None]:
        if not consumption_profile_entity:
            return None, None, None, None, "missing_consumption_profile_entity"

        payload: dict[str, Any] = {"entity_id": consumption_profile_entity}
        if desired_slot_minutes is not None:
            payload["desired_slot_minutes"] = int(desired_slot_minutes)

        try:
            response = await self.hass.services.async_call(
                "consumption_profile_logger",
                "get_profile",
                payload,
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # pragma: no cover - defensive
            return None, None, None, None, f"consumption_profile_call_failed:{err}"
        if not isinstance(response, dict):
            return None, None, None, None, "consumption_profile_invalid_response"

        ok = bool(response.get("ok"))
        if not ok:
            code = response.get("code")
            message = response.get("message")
            reason = "consumption_profile_not_ok"
            if code:
                reason += f":{code}"
            if message:
                reason += f":{message}"
            return None, None, None, None, reason

        profile = response.get("profile")
        if not isinstance(profile, dict):
            return None, None, None, None, "consumption_profile_missing_profile"

        slots_kwh = profile.get("slots_kwh")
        if not isinstance(slots_kwh, list) or not slots_kwh:
            return None, None, None, None, "consumption_profile_missing_slots"
        try:
            energy_profile = [float(v) for v in slots_kwh]
        except (TypeError, ValueError):
            return None, None, None, None, "consumption_profile_invalid_slots"

        try:
            slot_minutes = int(profile.get("slot_minutes"))
        except (TypeError, ValueError):
            return None, None, None, None, "consumption_profile_invalid_slot_minutes"
        if slot_minutes <= 0:
            return None, None, None, None, "consumption_profile_invalid_slot_minutes"

        runtime_minutes_raw = profile.get("runtime_minutes")
        runtime_minutes: float
        try:
            runtime_minutes = float(runtime_minutes_raw)
        except (TypeError, ValueError):
            runtime_minutes = float(len(energy_profile) * slot_minutes)

        profile_meta = {
            "entity_id": consumption_profile_entity,
            "logger_id": profile.get("logger_id"),
            "logger_name": profile.get("logger_name"),
            "program_key": profile.get("program_key"),
            "program_name": profile.get("program_name"),
            "avg_total_kwh": profile.get("avg_total_kwh"),
            "last_updated": profile.get("last_updated"),
        }
        return energy_profile, runtime_minutes, slot_minutes, profile_meta, None

    def _current_price_coverage_end(self) -> datetime | None:
        rows = self._slot_dicts_for_optimizer()
        if not rows:
            return None
        billing_slot = self._detect_billing_slot_minutes(rows)
        tz = ZoneInfo(self.timezone)
        max_end: datetime | None = None
        for row in rows:
            dt = _parse_iso_local(str(row.get("start_time")), tz)
            if dt is None:
                continue
            end = dt + timedelta(minutes=billing_slot)
            if max_end is None or end > max_end:
                max_end = end
        return max_end

    async def _maybe_reoptimize_plans_after_data_update(self) -> None:
        coverage_end = self._current_price_coverage_end()
        if coverage_end is None:
            return

        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)

        for device_slug, payload in list(self.store.get_plans().items()):
            if payload.get("status") != "ok":
                continue
            if not bool(payload.get("window_truncated_by_data")):
                continue

            best_start = _parse_iso_local(str(payload.get("best_start")), tz)
            if best_start is None or now >= best_start:
                continue

            prev_coverage = _parse_iso_local(str(payload.get("price_coverage_end_at_compute")), tz)
            if prev_coverage is not None and coverage_end <= prev_coverage:
                continue

            requested_window_end = payload.get("requested_window_end")
            if not isinstance(requested_window_end, str) or not requested_window_end:
                continue

            try:
                result = optimize_runtime(
                    slots=self._slot_dicts_for_optimizer(),
                    timezone_name=self.timezone,
                    billing_slot_minutes=int(payload.get("billing_slot_minutes") or DEFAULT_BILLING_SLOT_MINUTES),
                    duration_minutes=float(payload.get("duration_minutes")) if payload.get("duration_minutes") is not None else None,
                    energy_profile=[float(v) for v in (payload.get("profile_used") or [])],
                    profile_slot_minutes=int(payload.get("profile_slot_minutes") or DEFAULT_BILLING_SLOT_MINUTES),
                    epsilon_rel=float(payload.get("epsilon_rel", 0.01)),
                    prefer_earliest=bool(payload.get("prefer_earliest", True)),
                    start_mode="now",
                    start_in_minutes=0.0,
                    deadline_mode="none",
                    deadline_minutes=None,
                    latest_start=requested_window_end,
                    latest_finish=None,
                    align_start_to_billing_slot=bool(payload.get("align_start_to_billing_slot", False)),
                    reference_time=None,
                )
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("plan re-optimize failed for %s/%s: %s", self.timeline_slug, device_slug, err, exc_info=True)
                continue

            await self._persist_plan_result(
                device_name=str(payload.get("device_name", device_slug)),
                result=result,
                deadline_mode=str(payload.get("deadline_mode", "none")),
                deadline_minutes=(
                    float(payload.get("deadline_minutes"))
                    if payload.get("deadline_minutes") is not None
                    else None
                ),
                latest_start=payload.get("latest_start"),
                latest_finish=payload.get("latest_finish"),
                epsilon_rel=float(payload.get("epsilon_rel", 0.01)),
                prefer_earliest=bool(payload.get("prefer_earliest", True)),
                dry_run=bool(payload.get("dry_run", False)),
                align_start_to_billing_slot=bool(payload.get("align_start_to_billing_slot", False)),
                profile_source=str(payload.get("profile_source", "service_payload")),
                profile_meta=payload.get("profile_meta"),
            )
            _LOGGER.info(
                "re-optimized plan %s/%s because price coverage extended to %s",
                self.timeline_slug,
                device_slug,
                coverage_end.isoformat(timespec="minutes"),
            )

    def _slot_dicts_for_optimizer(self) -> list[dict]:
        return [
            {
                "start_time": item["start_time"],
                "price_per_kwh": item["price_per_kwh"],
            }
            for item in self.store.get_slots()
        ]

    @property
    def timeline_entity_id(self) -> str:
        return f"sensor.{self.timeline_slug}_pricing_meta"

    @property
    def status_entity_id(self) -> str:
        return f"sensor.{self.timeline_slug}_status"

    def plan_entity_id(self, device_slug: str) -> str:
        return f"sensor.{self.timeline_slug}_plan_{device_slug}"

    def build_device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {("electricity_price_suite", f"{self.entry.entry_id}:{self.timeline_slug}")},
            "name": self.timeline_name,
            "manufacturer": "Electricity Price Suite",
            "model": "Price Timeline",
        }

    def _rows_for_day(self, day: datetime.date, tz: ZoneInfo) -> list[dict]:
        out = []
        for row in self.store.get_slots():
            dt = _parse_iso_local(row["start_time"], tz)
            if dt is None:
                continue
            if dt.date() == day:
                out.append(row)
        out.sort(key=lambda item: item["start_time"])
        return out

    def _weighted_for_rows(
        self,
        rows: list[dict],
        tz: ZoneInfo,
        fallback_slot_minutes: int,
    ) -> list[tuple[float, float]]:
        weighted: list[tuple[float, float]] = []
        for idx, row in enumerate(rows):
            dt = _parse_iso_local(row["start_time"], tz)
            if dt is None:
                continue
            if idx + 1 < len(rows):
                next_dt = _parse_iso_local(rows[idx + 1]["start_time"], tz)
                if next_dt is not None:
                    duration_h = (next_dt - dt).total_seconds() / 3600.0
                else:
                    duration_h = fallback_slot_minutes / 60.0
            else:
                duration_h = fallback_slot_minutes / 60.0

            duration_h = max(0.05, duration_h)
            weighted.append((float(row["price_per_kwh"]), duration_h))

        return weighted

    def _build_timeline_stats(self) -> TimelineStats:
        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)

        today_rows = self._rows_for_day(today, tz)
        tomorrow_rows = self._rows_for_day(tomorrow, tz)
        detected_slot_minutes = self._detect_billing_slot_minutes(self.store.get_slots())
        current_price, current_price_start = self._current_price_for_now(
            self.store.get_slots(),
            now,
            tz,
            detected_slot_minutes,
        )

        card = [
            {"start_time": row["start_time"], "price_per_kwh": self._round(row["price_per_kwh"])}
            for row in [*today_rows, *tomorrow_rows]
        ]
        card.sort(key=lambda item: item["start_time"])

        w_today = self._weighted_for_rows(today_rows, tz, detected_slot_minutes)
        w_tomorrow = self._weighted_for_rows(tomorrow_rows, tz, detected_slot_minutes)

        past_3: list[tuple[float, float]] = []
        past_7: list[tuple[float, float]] = []
        for offset in range(1, 8):
            day_rows = self._rows_for_day(today - timedelta(days=offset), tz)
            weighted = self._weighted_for_rows(day_rows, tz, detected_slot_minutes)
            if offset <= 3:
                past_3.extend(weighted)
            past_7.extend(weighted)

        def min_time(rows: list[dict]) -> str | None:
            if not rows:
                return None
            lowest = min(float(r["price_per_kwh"]) for r in rows)
            for row in rows:
                if float(row["price_per_kwh"]) == lowest:
                    return row["start_time"]
            return None

        def max_time(rows: list[dict]) -> str | None:
            if not rows:
                return None
            highest = max(float(r["price_per_kwh"]) for r in rows)
            for row in rows:
                if float(row["price_per_kwh"]) == highest:
                    return row["start_time"]
            return None

        avg_today = _weighted_avg(w_today)
        attrs: dict[str, Any] = {
            "friendly_name": f"{self.timeline_name} Pricing Meta",
            "currency": self.currency,
            "unit_of_measurement": f"{self.currency}/kWh",
            "data": card,
            "avg_today": self._round(avg_today),
            "min_today": self._round(min((v for v, _ in w_today), default=None)),
            "max_today": self._round(max((v for v, _ in w_today), default=None)),
            "p20_today": self._round(_weighted_q(w_today, 0.2)),
            "p70_today": self._round(_weighted_q(w_today, 0.7)),
            "min_today_time": min_time(today_rows),
            "max_today_time": max_time(today_rows),
            "avg_tomorrow": self._round(_weighted_avg(w_tomorrow)),
            "min_tomorrow": self._round(min((v for v, _ in w_tomorrow), default=None)),
            "max_tomorrow": self._round(max((v for v, _ in w_tomorrow), default=None)),
            "p20_tomorrow": self._round(_weighted_q(w_tomorrow, 0.2)),
            "p70_tomorrow": self._round(_weighted_q(w_tomorrow, 0.7)),
            "min_tomorrow_time": min_time(tomorrow_rows),
            "max_tomorrow_time": max_time(tomorrow_rows),
            "avg_last_3d": self._round(_weighted_avg(past_3) if len(past_3) > 0 else None),
            "avg_last_7d": self._round(_weighted_avg(past_7) if len(past_7) > 0 else None),
            "p20_last_3d": self._round(_weighted_q(past_3, 0.2) if len(past_3) > 0 else None),
            "p70_last_3d": self._round(_weighted_q(past_3, 0.7) if len(past_3) > 0 else None),
            "p20_last_7d": self._round(_weighted_q(past_7, 0.2) if len(past_7) > 0 else None),
            "p70_last_7d": self._round(_weighted_q(past_7, 0.7) if len(past_7) > 0 else None),
            "today_rows": len(today_rows),
            "tomorrow_rows": len(tomorrow_rows),
            "tomorrow_status": "ok" if tomorrow_rows else "absent",
            "pending_primary": self._pending_primary(),
            "last_primary_refresh_at": self.store.last_primary_refresh_at,
            "last_source_chain_fetch_at": self.store.last_source_chain_fetch_at,
            "last_successful_source_id": self.store.last_successful_source_id,
            "source_health": self.store.source_health,
            "timeline_status": self._compute_timeline_status(
                len(today_rows),
                len(tomorrow_rows),
                self._has_primary_tomorrow_rows(),
            ),
            "updated_at": now.isoformat(timespec="seconds"),
        }

        state: float | str = self._round(avg_today) if avg_today is not None else "unknown"
        status = str(attrs["timeline_status"])
        return TimelineStats(
            state=state,
            attributes=attrs,
            current_price=self._round(current_price),
            current_price_start_time=current_price_start,
            status=status,
        )

    def _compute_timeline_status(
        self,
        today_rows: int,
        tomorrow_rows: int,
        has_primary_tomorrow: bool,
    ) -> str:
        if today_rows <= 0 and tomorrow_rows <= 0:
            return "no_data"
        if today_rows > 0 and tomorrow_rows <= 0:
            return "today_only"
        if today_rows <= 0 and tomorrow_rows > 0:
            return "tomorrow_only"
        if tomorrow_rows > 0 and not has_primary_tomorrow:
            return "tomorrow_not_from_prio0"
        return "today_and_tomorrow"

    def _current_price_for_now(
        self,
        rows: list[dict],
        now: datetime,
        tz: ZoneInfo,
        fallback_slot_minutes: int,
    ) -> tuple[float | None, str | None]:
        parsed_rows: list[tuple[datetime, dict]] = []
        for row in rows:
            dt = _parse_iso_local(row["start_time"], tz)
            if dt is not None:
                parsed_rows.append((dt, row))
        parsed_rows.sort(key=lambda item: item[0])
        if not parsed_rows:
            return None, None

        for idx, (dt, row) in enumerate(parsed_rows):
            if idx + 1 < len(parsed_rows):
                next_dt = parsed_rows[idx + 1][0]
            else:
                next_dt = dt + timedelta(minutes=fallback_slot_minutes)
            if dt <= now < next_dt:
                try:
                    return float(row["price_per_kwh"]), row["start_time"]
                except (TypeError, ValueError):
                    return None, None

        return None, None

    def _filter_today_tomorrow_slots(self, slots: list[SlotRecord]) -> list[SlotRecord]:
        tz = ZoneInfo(self.timezone)
        today = datetime.now(tz).date()
        tomorrow = today + timedelta(days=1)
        out: list[SlotRecord] = []
        for slot in slots:
            dt = _parse_iso_local(slot.start_time, tz)
            if not dt:
                continue
            if dt.date() in {today, tomorrow}:
                out.append(slot)
        return out

    def _missing_today_tomorrow_primary(self) -> tuple[bool, bool]:
        tz = ZoneInfo(self.timezone)
        today = datetime.now(tz).date()
        tomorrow = today + timedelta(days=1)
        has_primary_today = False
        has_primary_tomorrow = False
        for row in self.store.get_slots():
            if not bool(row.get("is_primary_source")):
                continue
            dt = _parse_iso_local(row["start_time"], tz)
            if not dt:
                continue
            if dt.date() == today:
                has_primary_today = True
            elif dt.date() == tomorrow:
                has_primary_tomorrow = True
            if has_primary_today and has_primary_tomorrow:
                break
        return (not has_primary_today, not has_primary_tomorrow)

    def _filter_slots_for_missing_days(
        self,
        slots: list[SlotRecord],
        need_today: bool,
        need_tomorrow: bool,
    ) -> list[SlotRecord]:
        if not need_today and not need_tomorrow:
            return []
        tz = ZoneInfo(self.timezone)
        today = datetime.now(tz).date()
        tomorrow = today + timedelta(days=1)
        out: list[SlotRecord] = []
        for slot in slots:
            dt = _parse_iso_local(slot.start_time, tz)
            if not dt:
                continue
            if need_today and dt.date() == today:
                out.append(slot)
            elif need_tomorrow and dt.date() == tomorrow:
                out.append(slot)
        return out

    def _write_time_based_sensors(self) -> None:
        if self.timeline_sensor is not None:
            self.timeline_sensor.async_write_ha_state()
        if self.status_sensor is not None:
            self.status_sensor.async_write_ha_state()
        if self.current_price_sensor is not None:
            self.current_price_sensor.async_write_ha_state()

    @callback
    def _schedule_next_time_update(self) -> None:
        if self._unsub_scheduled_update is not None:
            self._unsub_scheduled_update()
            self._unsub_scheduled_update = None

        next_update = self._next_time_update_dt()
        if next_update is None:
            return

        self._unsub_scheduled_update = async_track_point_in_time(
            self.hass,
            self._handle_scheduled_time_update,
            dt_util.as_utc(next_update),
        )

    def _next_time_update_dt(self) -> datetime | None:
        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)
        candidates: list[datetime] = []

        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        candidates.append(next_midnight)

        next_slot = self._next_slot_start_after(now)
        if next_slot is not None:
            candidates.append(next_slot)

        if not candidates:
            return None
        return min(candidates)

    def _next_slot_start_after(self, now: datetime) -> datetime | None:
        tz = ZoneInfo(self.timezone)
        next_slot: datetime | None = None
        for row in self.store.get_slots():
            dt = _parse_iso_local(row["start_time"], tz)
            if dt is None or dt <= now:
                continue
            if next_slot is None or dt < next_slot:
                next_slot = dt
        return next_slot

    async def _handle_scheduled_time_update(self, _now: datetime) -> None:
        self.latest_stats = self._build_timeline_stats()
        self._write_time_based_sensors()
        self._schedule_next_time_update()
        self._schedule_next_poll_update()

    @callback
    def _schedule_next_poll_update(self) -> None:
        if self._unsub_scheduled_poll is not None:
            self._unsub_scheduled_poll()
            self._unsub_scheduled_poll = None

        next_poll = self._next_poll_dt(self.latest_stats.status)
        if next_poll is None:
            return

        self._unsub_scheduled_poll = async_track_point_in_time(
            self.hass,
            self._handle_scheduled_poll,
            dt_util.as_utc(next_poll),
        )

    def _next_poll_dt(self, status: str) -> datetime | None:
        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)

        def next_minute_mark(minute_marks: tuple[int, ...], from_dt: datetime) -> datetime:
            for minute in minute_marks:
                candidate = from_dt.replace(minute=minute, second=0, microsecond=0)
                if candidate > from_dt:
                    return candidate
            return (from_dt + timedelta(hours=1)).replace(
                minute=minute_marks[0],
                second=0,
                microsecond=0,
            )

        if status in {"no_data", "tomorrow_only"}:
            return next_minute_mark((1, 31), now)

        if status == "today_only":
            start_window = now.replace(hour=12, minute=1, second=0, microsecond=0)
            if now < start_window:
                return start_window
            end_window = now.replace(hour=23, minute=31, second=0, microsecond=0)
            if now > end_window:
                return (now + timedelta(days=1)).replace(hour=12, minute=1, second=0, microsecond=0)
            return next_minute_mark((1, 31), now)

        if status == "tomorrow_not_from_prio0":
            return next_minute_mark((1,), now)

        return None

    async def _handle_scheduled_poll(self, _now: datetime) -> None:
        await self.async_refresh_timeline(override_sources=None)
        self._write_time_based_sensors()
        self._schedule_next_poll_update()

    def _has_primary_tomorrow_rows(self) -> bool:
        tz = ZoneInfo(self.timezone)
        tomorrow = datetime.now(tz).date() + timedelta(days=1)
        for row in self.store.get_slots():
            dt = _parse_iso_local(row["start_time"], tz)
            if not dt or dt.date() != tomorrow:
                continue
            if bool(row.get("is_primary_source")):
                return True
        return False

    def _pending_primary(self) -> bool:
        tz = ZoneInfo(self.timezone)
        today = datetime.now(tz).date()
        tomorrow = today + timedelta(days=1)
        has_non_primary = False
        for row in self.store.get_slots():
            dt = _parse_iso_local(row["start_time"], tz)
            if not dt or dt.date() not in {today, tomorrow}:
                continue
            if not bool(row.get("is_primary_source")):
                has_non_primary = True
        return has_non_primary

    def _create_plan_sensor(self, device_slug: str, device_name: str):
        from .sensor import PlanSensor

        payload = self.store.get_plans().get(device_slug)
        return PlanSensor(self, device_slug, device_name, payload)
