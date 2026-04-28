"""Runtime objects for electricity_price_suite."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import slugify
from homeassistant.util import dt as dt_util

from .consumption_stats import build_consumption_metrics
from .const import (
    ATTR_PRICE_PER_KWH,
    ATTR_START_TIME,
    CONF_BASIC_FEE_AMOUNT,
    CONF_BASIC_FEE_MODE,
    CONF_AVG_PRICE_INCLUDE_BASIC_FEE,
    CONF_CACHE_RETENTION_DAYS,
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_CURRENCY,
    CONF_ENABLE_CURRENT_PRICE_SENSOR,
    CONF_PLANNER_DEVICES,
    CONF_SOURCE_CHAIN,
    CONF_ROUND_DECIMALS,
    DEFAULT_BASIC_FEE_AMOUNT,
    DEFAULT_BASIC_FEE_MODE,
    DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE,
    DEFAULT_BILLING_SLOT_MINUTES,
    DEFAULT_CONSUMPTION_RETENTION_DAYS,
    DEFAULT_ENABLE_CURRENT_PRICE_SENSOR,
    DEFAULT_PLANNER_DEVICES,
    DEFAULT_ROUND_DECIMALS,
    DOMAIN,
)
from .models import PlanPayload, PlanResult, SlotRecord, SourceConfig, TimelineStats
from .optimizer import optimize_runtime
from .plan_manager import (
    build_no_candidate_result,
    build_plan_payload,
    build_reset_payload,
    load_profile_logger_profile,
    reoptimize_plan_payload,
)
from .providers import fetch_from_source, normalize_slots
from .resolvers import resolve_logger_runtime
from .store import TimelineStore
from .timeline_stats import (
    build_timeline_stats,
    current_price_coverage_end,
    detect_billing_slot_minutes,
    filter_slots_for_missing_days,
    filter_today_tomorrow_slots,
    has_primary_tomorrow_rows,
    missing_today_tomorrow_primary,
    next_slot_start_after,
    parse_iso_local,
    pending_primary,
)
from .time_utils import format_iso

_LOGGER = logging.getLogger(__name__)


class TimelineRuntime:
    """One runtime timeline bound to one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
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
        self.consumption_energy_entity = str(
            entry.options.get(
                CONF_CONSUMPTION_ENERGY_ENTITY,
                entry.data.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""),
            )
            or ""
        ).strip()
        self.basic_fee_mode = str(
            entry.options.get(CONF_BASIC_FEE_MODE, entry.data.get(CONF_BASIC_FEE_MODE, DEFAULT_BASIC_FEE_MODE))
        )
        self.basic_fee_amount = float(
            entry.options.get(CONF_BASIC_FEE_AMOUNT, entry.data.get(CONF_BASIC_FEE_AMOUNT, DEFAULT_BASIC_FEE_AMOUNT))
        )
        self.avg_price_include_basic_fee = bool(
            entry.options.get(
                CONF_AVG_PRICE_INCLUDE_BASIC_FEE,
                entry.data.get(CONF_AVG_PRICE_INCLUDE_BASIC_FEE, DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE),
            )
        )
        self.planner_devices = self._normalize_planner_devices(
            entry.options.get(CONF_PLANNER_DEVICES, entry.data.get(CONF_PLANNER_DEVICES, DEFAULT_PLANNER_DEVICES))
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
        self.consumption_sensors: dict[str, Any] = {}
        self.plan_sensors: dict[str, Any] = {}
        self._add_entities = None
        self._unsub_scheduled_update = None
        self._unsub_scheduled_poll = None
        self._unsub_consumption_sample = None

        self.latest_stats = TimelineStats(
            state=None,
            attributes={},
            current_price=None,
            current_price_start_time=None,
            status="no_data",
        )
        self.latest_consumption_metrics: dict[str, Any] = {}

    def _normalize_planner_devices(self, raw: Any) -> list[str]:
        items = raw if isinstance(raw, list) else []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            name = str(item).strip()
            if not name:
                continue
            slug = slugify(name)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            normalized.append(name)
        return normalized

    def resolve_planner(self, planner_name: str | None) -> tuple[str, str] | None:
        name = str(planner_name or "").strip()
        if not name:
            return None
        requested_slug = slugify(name)
        if not requested_slug:
            return None
        for configured_name in self.planner_devices:
            configured_slug = slugify(configured_name)
            if configured_name == name or configured_slug == requested_slug:
                return configured_name, configured_slug
        return None

    def plan_key(self, planner_slug: str, device_slug: str) -> str:
        return f"{planner_slug}__{device_slug}"

    def _detect_billing_slot_minutes(self, rows: list[dict[str, float | str]]) -> int:
        return detect_billing_slot_minutes(rows, self.timezone, DEFAULT_BILLING_SLOT_MINUTES)

    async def async_initialize(self) -> None:
        await self.store.async_load()
        if not self.store.get_sources():
            for idx, source in enumerate(self.source_chain):
                self.store.upsert_source(self._normalize_source(source, idx))
            await self.store.async_save()
        await self._rebuild_from_store()
        await self._async_update_consumption_metrics(sample_now=True)
        self._schedule_next_time_update()
        self._schedule_next_poll_update()
        self._schedule_next_consumption_sample()

    async def async_shutdown(self) -> None:
        if self._unsub_scheduled_update is not None:
            self._unsub_scheduled_update()
            self._unsub_scheduled_update = None
        if self._unsub_scheduled_poll is not None:
            self._unsub_scheduled_poll()
            self._unsub_scheduled_poll = None
        if self._unsub_consumption_sample is not None:
            self._unsub_consumption_sample()
            self._unsub_consumption_sample = None

    def register_add_entities(self, add_entities: Any) -> None:
        self._add_entities = add_entities

    async def async_cleanup_orphan_planner_devices(self) -> None:
        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)
        current_identifiers = {
            self.plan_device_identifier(slugify(planner_name))
            for planner_name in self.planner_devices
        }
        planner_prefix = f"{self.entry.entry_id}:{self.timeline_slug}:planner:"

        for device in dr.async_entries_for_config_entry(device_registry, self.entry.entry_id):
            planner_identifier = next(
                (
                    identifier
                    for identifier in device.identifiers
                    if identifier[0] == DOMAIN and identifier[1].startswith(planner_prefix)
                ),
                None,
            )
            if planner_identifier is None:
                continue
            if planner_identifier in current_identifiers:
                continue
            if er.async_entries_for_device(entity_registry, device.id, include_disabled_entities=True):
                continue
            device_registry.async_remove_device(device.id)

    async def _rebuild_from_store(self) -> None:
        self.latest_stats = self._compute_timeline_stats()
        self.latest_consumption_metrics = self._compute_consumption_metrics()
        self._schedule_next_time_update()

    def _normalize_source(self, source: dict[str, Any], fallback_priority: int) -> SourceConfig:
        normalized: SourceConfig = dict(source)
        normalized.setdefault("id", f"source_{fallback_priority}")
        normalized.setdefault("priority", fallback_priority)
        normalized.setdefault("enabled", True)
        normalized.setdefault("slot_mapping", {"time_key": "start_time", "price_key": "price_per_kwh"})
        return normalized

    def _enabled_sources(self, override_sources: list[Any] | None = None) -> list[SourceConfig]:
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
        overwrite: bool = False,
    ) -> dict[str, Any]:
        attempt_log: list[dict[str, Any]] = []
        merged_debug: dict[str, int] = {"inserted": 0, "replaced": 0, "ignored": 0}
        used_sources: list[str] = []
        fetched_source_chain = False
        active_sources = self._enabled_sources(override_sources)
        cleared_rows = 0

        if not active_sources:
            self.latest_stats = self._compute_timeline_stats()
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
                "cleared_rows": cleared_rows,
                "reason": "no_sources_configured",
                "hint": "Configure a primary source via config flow or manage_sources service.",
            }

        if overwrite and only_today_tomorrow:
            tz = ZoneInfo(self.timezone)
            today = datetime.now(tz).date()
            tomorrow = today + timedelta(days=1)
            cleared_rows = self.store.clear_slots_for_dates(self.timezone, {today, tomorrow})
            need_today, need_tomorrow = True, True
        else:
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

        self.latest_stats = self._compute_timeline_stats()
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
            "cleared_rows": cleared_rows,
        }

    async def async_add_source(self, source: dict[str, Any]) -> dict[str, Any]:
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
        slots_payload: list[dict[str, Any]],
        source_name: str,
        source_priority: int,
        is_primary: bool,
        overwrite: bool = False,
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

        cleared_rows = 0
        if overwrite and normalized:
            tz = ZoneInfo(self.timezone)
            dates = {
                dt.date()
                for slot in normalized
                if (dt := parse_iso_local(slot.start_time, tz)) is not None
            }
            cleared_rows = self.store.clear_slots_for_dates(self.timezone, dates)

        merged = self.store.upsert_slots(normalized)
        self.store.set_last_successful_source(source_name)
        if is_primary or int(source_priority) == 0:
            self.store.set_last_primary_refresh()
        self.store.purge_old_slots(self.timezone)
        await self.store.async_save()

        self.latest_stats = self._compute_timeline_stats()
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
            "cleared_rows": cleared_rows,
        }

    async def async_optimize_device(
        self,
        *,
        planner_name: str,
        device_name: str,
        duration_minutes: float | None,
        energy_profile: list[float] | None,
        profile_slot_minutes: int | None,
        billing_slot_minutes: int | None,
        profile_logger_entity: str | None,
        program_key: str | None,
        program_display_name: str | None,
        align_start_to_billing_slot: bool,
        max_extra_cost_percent: float,
        prefer_earliest: bool,
        start_mode: str,
        start_in_minutes: float,
        deadline_mode: str,
        deadline_minutes: float | None,
        latest_start: str | None,
        latest_finish: str | None,
    ) -> dict[str, Any]:
        resolved_planner = self.resolve_planner(planner_name)
        if resolved_planner is None:
            raise ValueError(f"unknown planner: {planner_name}")
        planner_name, planner_slug = resolved_planner
        profile_source = "service_payload"
        profile_meta: dict[str, Any] | None = None
        program_key_used = program_key
        program_display_name_used = program_display_name

        if profile_logger_entity:
            logger_runtime, implicit_program_key = resolve_logger_runtime(
                self.hass.data.get(DOMAIN, {}),
                profile_logger_entity,
            )
            if logger_runtime is None:
                result = self._build_no_candidate_result("profile_logger_not_found")
                return await self._persist_plan_result(
                    planner_name=planner_name,
                    device_name=device_name,
                    result=result,
                    deadline_mode=deadline_mode,
                    deadline_minutes=deadline_minutes,
                    latest_start=latest_start,
                    latest_finish=latest_finish,
                    max_extra_cost_percent=max_extra_cost_percent,
                    prefer_earliest=prefer_earliest,
                    align_start_to_billing_slot=align_start_to_billing_slot,
                    profile_source="profile_logger",
                    profile_meta={"entity_id": profile_logger_entity},
                    program_key_used=program_key_used,
                    program_display_name_used=program_display_name_used,
                )
            effective_program_key = program_key or implicit_program_key
            (
                loaded_profile,
                loaded_duration,
                loaded_slot_minutes,
                profile_meta,
                load_reason,
            ) = load_profile_logger_profile(
                logger_runtime,
                profile_logger_entity=profile_logger_entity,
                program_key=effective_program_key,
            )
            program_key_used = effective_program_key
            program_display_name_used = (
                program_display_name
                or logger_runtime.get_program_display_name(effective_program_key)
            )
            if load_reason is not None:
                estimated_runtime = logger_runtime.get_estimated_runtime_minutes(effective_program_key)
                if estimated_runtime is not None:
                    duration_minutes = estimated_runtime
                    energy_profile = None
                    profile_slot_minutes = None
                    profile_source = "estimated_runtime"
                    profile_meta = {
                        "entity_id": profile_logger_entity,
                        "program_key": effective_program_key,
                        "program_name": program_display_name_used,
                        "estimated_runtime_minutes": estimated_runtime,
                    }
                else:
                    result = self._build_no_candidate_result(load_reason)
                    return await self._persist_plan_result(
                        planner_name=planner_name,
                        device_name=device_name,
                        result=result,
                        deadline_mode=deadline_mode,
                        deadline_minutes=deadline_minutes,
                        latest_start=latest_start,
                        latest_finish=latest_finish,
                        max_extra_cost_percent=max_extra_cost_percent,
                        prefer_earliest=prefer_earliest,
                        align_start_to_billing_slot=align_start_to_billing_slot,
                        profile_source="profile_logger",
                        profile_meta=profile_meta,
                        program_key_used=program_key_used,
                        program_display_name_used=program_display_name_used,
                    )
            else:
                energy_profile = loaded_profile
                duration_minutes = loaded_duration
                profile_slot_minutes = loaded_slot_minutes
                profile_source = "profile_logger"

        slot_rows = self._slot_dicts_for_optimizer()
        bill_slot = int(billing_slot_minutes or self._detect_billing_slot_minutes(slot_rows))

        result = optimize_runtime(
            slots=slot_rows,
            timezone_name=self.timezone,
            billing_slot_minutes=bill_slot,
            duration_minutes=duration_minutes,
            energy_profile=energy_profile,
            profile_slot_minutes=profile_slot_minutes,
            max_extra_cost_percent=max_extra_cost_percent,
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
            planner_name=planner_name,
            device_name=device_name,
            result=result,
            deadline_mode=deadline_mode,
            deadline_minutes=deadline_minutes,
            latest_start=latest_start,
            latest_finish=latest_finish,
            max_extra_cost_percent=max_extra_cost_percent,
            prefer_earliest=prefer_earliest,
            align_start_to_billing_slot=align_start_to_billing_slot,
            profile_source=profile_source,
            profile_meta=profile_meta,
            program_key_used=program_key_used,
            program_display_name_used=program_display_name_used,
        )

    async def _persist_plan_result(
        self,
        *,
        planner_name: str,
        device_name: str,
        result: PlanResult,
        deadline_mode: str,
        deadline_minutes: float | None,
        latest_start: str | None,
        latest_finish: str | None,
        max_extra_cost_percent: float,
        prefer_earliest: bool,
        align_start_to_billing_slot: bool,
        profile_source: str,
        profile_meta: dict[str, Any] | None,
        program_key_used: str | None,
        program_display_name_used: str | None,
    ) -> dict[str, Any]:
        planner_slug = slugify(planner_name)
        device_slug = slugify(device_name)
        plan_key = self.plan_key(planner_slug, device_slug)
        entity_id = self.plan_entity_id(planner_slug, device_slug)

        plan_payload = build_plan_payload(
            planner_name=planner_name,
            planner_slug=planner_slug,
            device_name=device_name,
            device_slug=device_slug,
            result=result,
            deadline_mode=deadline_mode,
            deadline_minutes=deadline_minutes,
            latest_start=latest_start,
            latest_finish=latest_finish,
            max_extra_cost_percent=max_extra_cost_percent,
            prefer_earliest=prefer_earliest,
            align_start_to_billing_slot=align_start_to_billing_slot,
            profile_source=profile_source,
            profile_meta=profile_meta,
            program_key_used=program_key_used,
            program_display_name_used=program_display_name_used,
            timeline_entity_id=self.timeline_entity_id,
            timezone_name=self.timezone,
        )

        self.store.set_plan(plan_key, plan_payload)
        await self.store.async_save()

        if plan_key in self.plan_sensors:
            self.plan_sensors[plan_key].async_update_from_payload(plan_payload)
        elif self._add_entities is not None:
            sensor = self._create_plan_sensor(plan_key, plan_payload)
            self.plan_sensors[plan_key] = sensor
            self._add_entities([sensor])

        return {
            "status": result.status,
            "plan_entity_id": entity_id,
            "best_start": result.best_start,
            "best_end": result.best_end,
            "best_cost": result.best_cost,
            "reason": result.reason,
        }

    async def async_manage_plan(self, *, plan_key: str, reset: bool, delete: bool) -> dict[str, Any]:
        plans = self.store.get_plans()
        existing = plans.get(plan_key)
        planner_slug = str(existing.get("planner_slug")) if existing else ""
        device_slug = str(existing.get("device_slug")) if existing else ""
        entity_id = self.plan_entity_id(planner_slug, device_slug) if existing else None

        if existing is None:
            return {
                "status": "not_found",
                "plan_entity_id": None,
                "reason": "plan_not_found",
            }

        if delete:
            self.store.delete_plan(plan_key)
            await self.store.async_save()
            self.plan_sensors.pop(plan_key, None)
            registry = er.async_get(self.hass)
            unique_id = f"{self.entry.entry_id}_plan_{plan_key}"
            stale_entity = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if stale_entity:
                registry.async_remove(stale_entity)
            return {
                "status": "deleted",
                "plan_entity_id": entity_id,
                "reason": "manual_delete",
            }

        payload = self._build_reset_payload(
            planner_name=str(existing.get("planner_name", "")),
            planner_slug=str(existing.get("planner_slug", "")),
            device_name=str(existing.get("device_name", device_slug)),
            device_slug=device_slug,
        )
        self.store.set_plan(plan_key, payload)
        await self.store.async_save()

        if plan_key in self.plan_sensors:
            self.plan_sensors[plan_key].async_update_from_payload(payload)

        return {
            "status": "reset",
            "plan_entity_id": entity_id,
            "reason": "manual_reset",
        }

    async def async_reoptimize_plan(self, *, plan_key: str) -> dict[str, Any]:
        plans = self.store.get_plans()
        payload = plans.get(plan_key)
        planner_slug = str(payload.get("planner_slug")) if payload else ""
        device_slug = str(payload.get("device_slug")) if payload else ""
        entity_id = self.plan_entity_id(planner_slug, device_slug) if payload else None

        if payload is None:
            return {
                "status": "not_found",
                "plan_entity_id": None,
                "reason": "plan_not_found",
            }

        if payload.get("status") != "ok":
            return {
                "status": "not_reoptimized",
                "plan_entity_id": entity_id,
                "reason": f"plan_status_{payload.get('status', 'unknown')}",
            }

        result, profile_source, profile_meta = self._reoptimize_plan_result(payload)
        return await self._persist_plan_result(
            planner_name=str(payload.get("planner_name", "")),
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
            max_extra_cost_percent=float(payload.get("max_extra_cost_percent", 1.0)),
            prefer_earliest=bool(payload.get("prefer_earliest", True)),
            align_start_to_billing_slot=bool(payload.get("align_start_to_billing_slot", False)),
            profile_source=profile_source,
            profile_meta=profile_meta,
            program_key_used=payload.get("program_key_used"),
            program_display_name_used=payload.get("program_display_name_used"),
        )

    def _build_reset_payload(
        self,
        *,
        planner_name: str,
        planner_slug: str,
        device_name: str,
        device_slug: str,
    ) -> PlanPayload:
        return build_reset_payload(
            planner_name,
            planner_slug,
            device_name,
            device_slug,
            self.timeline_entity_id,
            self.timezone,
        )

    def _build_no_candidate_result(self, reason: str) -> PlanResult:
        return build_no_candidate_result(self.timezone, reason)

    def _current_price_coverage_end(self) -> datetime | None:
        return current_price_coverage_end(
            self.store.get_slots(),
            self.timezone,
            DEFAULT_BILLING_SLOT_MINUTES,
        )

    async def _maybe_reoptimize_plans_after_data_update(self) -> None:
        coverage_end = self._current_price_coverage_end()
        if coverage_end is None:
            return

        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)

        for plan_key, payload in list(self.store.get_plans().items()):
            if payload.get("status") != "ok":
                continue
            if not bool(payload.get("window_truncated_by_data")):
                continue

            best_start = parse_iso_local(str(payload.get("best_start")), tz)
            if best_start is None or now >= best_start:
                continue

            prev_coverage = parse_iso_local(str(payload.get("price_coverage_end_at_compute")), tz)
            if prev_coverage is not None and coverage_end <= prev_coverage:
                continue

            requested_latest_start = payload.get("requested_latest_start")
            if not isinstance(requested_latest_start, str) or not requested_latest_start:
                continue

            try:
                result, profile_source, profile_meta = self._reoptimize_plan_result(payload)
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("plan re-optimize failed for %s/%s: %s", self.timeline_slug, plan_key, err, exc_info=True)
                continue

            await self._persist_plan_result(
                planner_name=str(payload.get("planner_name", "")),
                device_name=str(payload.get("device_name", payload.get("device_slug", plan_key))),
                result=result,
                deadline_mode=str(payload.get("deadline_mode", "none")),
                deadline_minutes=(
                    float(payload.get("deadline_minutes"))
                    if payload.get("deadline_minutes") is not None
                    else None
                ),
                latest_start=payload.get("latest_start"),
                latest_finish=payload.get("latest_finish"),
                max_extra_cost_percent=float(payload.get("max_extra_cost_percent", 1.0)),
                prefer_earliest=bool(payload.get("prefer_earliest", True)),
                align_start_to_billing_slot=bool(payload.get("align_start_to_billing_slot", False)),
                profile_source=profile_source,
                profile_meta=profile_meta,
                program_key_used=payload.get("program_key_used"),
                program_display_name_used=payload.get("program_display_name_used"),
            )
            _LOGGER.info(
                "re-optimized plan %s/%s because price coverage extended to %s",
                self.timeline_slug,
                plan_key,
                format_iso(coverage_end, timespec="minutes"),
            )

    def _slot_dicts_for_optimizer(self) -> list[dict[str, float | str]]:
        return [
            {
                "start_time": item["start_time"],
                "price_per_kwh": item["price_per_kwh"],
            }
            for item in self.store.get_slots()
        ]

    def _reoptimize_plan_result(
        self,
        payload: PlanPayload,
    ) -> tuple[PlanResult, str, dict[str, Any] | None]:
        profile_source = str(payload.get("profile_source", "service_payload"))
        profile_meta = payload.get("profile_meta")

        if profile_source in {"profile_logger", "estimated_runtime"} and isinstance(profile_meta, dict):
            profile_logger_entity = profile_meta.get("entity_id")
            program_key = profile_meta.get("program_key")
            if isinstance(profile_logger_entity, str) and isinstance(program_key, str):
                logger_runtime, implicit_program_key = resolve_logger_runtime(
                    self.hass.data.get(DOMAIN, {}),
                    profile_logger_entity,
                )
                if logger_runtime is not None:
                    (
                        energy_profile,
                        duration_minutes,
                        profile_slot_minutes,
                        current_profile_meta,
                        load_reason,
                    ) = load_profile_logger_profile(
                        logger_runtime,
                        profile_logger_entity=profile_logger_entity,
                        program_key=program_key or implicit_program_key,
                    )
                    if load_reason is None:
                        return (
                            reoptimize_plan_payload(
                                slots=self._slot_dicts_for_optimizer(),
                                payload=payload,
                                timezone_name=self.timezone,
                                duration_minutes=duration_minutes,
                                energy_profile=energy_profile,
                                profile_slot_minutes=profile_slot_minutes,
                            ),
                            "profile_logger",
                            current_profile_meta,
                        )
                    estimated_runtime = logger_runtime.get_estimated_runtime_minutes(program_key or implicit_program_key)
                    if estimated_runtime is not None:
                        return (
                            reoptimize_plan_payload(
                                slots=self._slot_dicts_for_optimizer(),
                                payload=payload,
                                timezone_name=self.timezone,
                                duration_minutes=estimated_runtime,
                                energy_profile=None,
                                profile_slot_minutes=None,
                            ),
                            "estimated_runtime",
                            {
                                "entity_id": profile_logger_entity,
                                "program_key": program_key or implicit_program_key,
                                "program_name": payload.get("program_display_name_used"),
                                "estimated_runtime_minutes": estimated_runtime,
                            },
                        )
                    return self._build_no_candidate_result(load_reason), "profile_logger", current_profile_meta
                return self._build_no_candidate_result("profile_logger_not_found"), profile_source, profile_meta
            return self._build_no_candidate_result("missing_profile_logger_metadata"), profile_source, profile_meta

        return (
            reoptimize_plan_payload(
                slots=self._slot_dicts_for_optimizer(),
                payload=payload,
                timezone_name=self.timezone,
            ),
            profile_source,
            profile_meta if isinstance(profile_meta, dict) else None,
        )

    @property
    def has_consumption_tracking(self) -> bool:
        return bool(self.consumption_energy_entity)

    def _read_consumption_energy_kwh(self) -> float | None:
        if not self.has_consumption_tracking:
            return None
        state = self.hass.states.get(self.consumption_energy_entity)
        if state is None:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit == UnitOfEnergy.WATT_HOUR:
            return value / 1000.0
        if unit == UnitOfEnergy.KILO_WATT_HOUR:
            return value
        return None

    def _price_segments(self) -> list[tuple[datetime, datetime, float]]:
        tz = ZoneInfo(self.timezone)
        rows = self.store.get_slots()
        slot_minutes = self._detect_billing_slot_minutes(rows)
        segments: list[tuple[datetime, datetime, float]] = []
        for row in rows:
            start = parse_iso_local(row["start_time"], tz)
            if start is None:
                continue
            segments.append(
                (
                    start,
                    start + timedelta(minutes=slot_minutes),
                    float(row["price_per_kwh"]),
                )
            )
        return segments

    def _book_consumption_delta(
        self,
        *,
        start: datetime,
        end: datetime,
        delta_kwh: float,
    ) -> bool:
        if delta_kwh <= 0:
            return False

        segments = self._price_segments()
        total_seconds = max((end - start).total_seconds(), 0.0)
        if total_seconds <= 0:
            return False

        booked = False
        booked_seconds = 0.0
        for seg_start, seg_end, price in segments:
            overlap_start = max(start, seg_start)
            overlap_end = min(end, seg_end)
            overlap_seconds = (overlap_end - overlap_start).total_seconds()
            if overlap_seconds <= 0:
                continue
            share = delta_kwh * (overlap_seconds / total_seconds)
            if share <= 0:
                continue
            self.store.add_consumption_slot(
                start_time=format_iso(seg_start, timespec="seconds"),
                end_time=format_iso(seg_end, timespec="seconds"),
                consumption_kwh=share,
                price_per_kwh=price,
                energy_cost=share * price,
            )
            booked = True
            booked_seconds += overlap_seconds

        remaining_seconds = max(total_seconds - booked_seconds, 0.0)
        if remaining_seconds > 0:
            remaining_share = delta_kwh * (remaining_seconds / total_seconds)
            self.store.add_consumption_slot(
                start_time=format_iso(start, timespec="seconds"),
                end_time=format_iso(end, timespec="seconds"),
                consumption_kwh=remaining_share,
                price_per_kwh=None,
                energy_cost=None,
            )
            booked = True
        return booked

    async def _async_update_consumption_metrics(self, *, sample_now: bool) -> None:
        if not self.has_consumption_tracking:
            self.latest_consumption_metrics = {}
            return

        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)
        current_energy_kwh = self._read_consumption_energy_kwh()
        snapshot = self.store.get_consumption_last_snapshot()

        if current_energy_kwh is None:
            self.latest_consumption_metrics = self._compute_consumption_metrics()
            return

        if sample_now and snapshot is not None:
            prev_taken = parse_iso_local(snapshot.get("taken_at"), tz)
            prev_energy = snapshot.get("energy_kwh")
            if prev_taken is not None and isinstance(prev_energy, (int, float)):
                delta = float(current_energy_kwh) - float(prev_energy)
                if delta > 0:
                    self._book_consumption_delta(start=prev_taken, end=now, delta_kwh=delta)

        self.store.set_consumption_last_snapshot(
            taken_at=format_iso(now, timespec="seconds"),
            energy_kwh=current_energy_kwh,
        )
        self.store.purge_old_consumption_slots(
            self.timezone,
            DEFAULT_CONSUMPTION_RETENTION_DAYS,
        )
        await self.store.async_save()
        self.latest_consumption_metrics = self._compute_consumption_metrics()

    @property
    def timeline_entity_id(self) -> str:
        return f"sensor.{self.timeline_slug}_pricing_meta"

    @property
    def status_entity_id(self) -> str:
        return f"sensor.{self.timeline_slug}_status"

    def consumption_entity_id(self, metric_key: str) -> str:
        suffix = metric_key
        for prefix in ("consumption_", "cost_", "avg_paid_price_"):
            if suffix.startswith(prefix):
                suffix = suffix
                break
        return f"sensor.{self.timeline_slug}_{suffix}"

    def plan_entity_id(self, planner_slug: str, device_slug: str) -> str:
        return f"sensor.{self.timeline_slug}_{planner_slug}_{device_slug}"

    def timeline_device_identifier(self) -> tuple[str, str]:
        return (DOMAIN, f"{self.entry.entry_id}:{self.timeline_slug}")

    def plan_device_identifier(self, planner_slug: str) -> tuple[str, str]:
        return (DOMAIN, f"{self.entry.entry_id}:{self.timeline_slug}:planner:{planner_slug}")

    def build_device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {self.timeline_device_identifier()},
            "name": self.timeline_name,
            "manufacturer": "Electricity Price Suite",
            "model": "Price Timeline",
        }

    def build_plan_device_info(self, planner_name: str, planner_slug: str) -> dict[str, Any]:
        return {
            "identifiers": {self.plan_device_identifier(planner_slug)},
            "name": f"{self.timeline_name} {planner_name}".strip(),
            "manufacturer": "Electricity Price Suite",
            "model": "Optimization Planner",
            "via_device": self.timeline_device_identifier(),
        }

    def _compute_timeline_stats(self) -> TimelineStats:
        return build_timeline_stats(
            store=self.store,
            timezone_name=self.timezone,
            currency=self.currency,
            round_decimals=self.round_decimals,
            fallback_slot_minutes=DEFAULT_BILLING_SLOT_MINUTES,
        )

    def _compute_consumption_metrics(self) -> dict[str, Any]:
        if not self.has_consumption_tracking:
            return {}
        return build_consumption_metrics(
            slots=self.store.get_consumption_slots(),
            monthly_rollups=self.store.get_consumption_monthly_rollups(),
            timezone_name=self.timezone,
            round_decimals=self.round_decimals,
            basic_fee_mode=self.basic_fee_mode,
            basic_fee_amount=self.basic_fee_amount,
            avg_price_include_basic_fee=self.avg_price_include_basic_fee,
            consumption_energy_entity=self.consumption_energy_entity,
        )

    def _filter_today_tomorrow_slots(self, slots: list[SlotRecord]) -> list[SlotRecord]:
        return filter_today_tomorrow_slots(slots, self.timezone)

    def _missing_today_tomorrow_primary(self) -> tuple[bool, bool]:
        return missing_today_tomorrow_primary(self.store.get_slots(), self.timezone)

    def _filter_slots_for_missing_days(
        self,
        slots: list[SlotRecord],
        need_today: bool,
        need_tomorrow: bool,
    ) -> list[SlotRecord]:
        return filter_slots_for_missing_days(slots, need_today, need_tomorrow, self.timezone)

    def _write_time_based_sensors(self) -> None:
        if self.timeline_sensor is not None:
            self.timeline_sensor.async_write_ha_state()
        if self.status_sensor is not None:
            self.status_sensor.async_write_ha_state()
        if self.current_price_sensor is not None:
            self.current_price_sensor.async_write_ha_state()
        for sensor in self.consumption_sensors.values():
            sensor.async_write_ha_state()

    @callback
    def write_state_entities(self) -> None:
        self._write_time_based_sensors()

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
        return next_slot_start_after(self.store.get_slots(), now, self.timezone)

    async def _handle_scheduled_time_update(self, _now: datetime) -> None:
        self.latest_stats = self._compute_timeline_stats()
        if self.has_consumption_tracking:
            self.latest_consumption_metrics = self._compute_consumption_metrics()
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

    @callback
    def _schedule_next_consumption_sample(self) -> None:
        if self._unsub_consumption_sample is not None:
            self._unsub_consumption_sample()
            self._unsub_consumption_sample = None
        if not self.has_consumption_tracking:
            return

        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)
        next_second = 30 if now.second < 30 else 60
        next_sample = now.replace(microsecond=0)
        if next_second == 60:
            next_sample = (next_sample + timedelta(minutes=1)).replace(second=0)
        else:
            next_sample = next_sample.replace(second=30)
        if next_sample <= now:
            next_sample = now + timedelta(seconds=30)
        self._unsub_consumption_sample = async_track_point_in_time(
            self.hass,
            self._handle_consumption_sample,
            dt_util.as_utc(next_sample),
        )

    async def _handle_consumption_sample(self, _now: datetime) -> None:
        await self._async_update_consumption_metrics(sample_now=True)
        self._write_time_based_sensors()
        self._schedule_next_consumption_sample()

    def _has_primary_tomorrow_rows(self) -> bool:
        return has_primary_tomorrow_rows(self.store.get_slots(), self.timezone)

    def _pending_primary(self) -> bool:
        return pending_primary(self.store.get_slots(), self.timezone)

    def _create_plan_sensor(self, plan_key: str, payload: PlanPayload):
        from .sensor import PlanSensor

        return PlanSensor(self, plan_key, payload)
