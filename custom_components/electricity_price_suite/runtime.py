"""Runtime objects for electricity_price_suite."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
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
            state=None,
            attributes={},
            current_price=None,
            current_price_start_time=None,
            status="no_data",
        )

    def _detect_billing_slot_minutes(self, rows: list[dict[str, float | str]]) -> int:
        return detect_billing_slot_minutes(rows, self.timezone, DEFAULT_BILLING_SLOT_MINUTES)

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

    def register_add_entities(self, add_entities: Any) -> None:
        self._add_entities = add_entities

    async def _rebuild_from_store(self) -> None:
        self.latest_stats = self._compute_timeline_stats()
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
                "hint": "Configure a primary source via config flow or add_source service.",
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
        device_name: str,
        duration_minutes: float | None,
        energy_profile: list[float] | None,
        profile_slot_minutes: int | None,
        billing_slot_minutes: int | None,
        profile_logger_entity: str | None,
        program_key: str | None,
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
        profile_source = "service_payload"
        profile_meta: dict[str, Any] | None = None

        if profile_logger_entity:
            logger_runtime, implicit_program_key = resolve_logger_runtime(
                self.hass.data.get(DOMAIN, {}),
                profile_logger_entity,
            )
            if logger_runtime is None:
                result = self._build_no_candidate_result("profile_logger_not_found")
                return await self._persist_plan_result(
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
                )
            (
                loaded_profile,
                loaded_duration,
                loaded_slot_minutes,
                profile_meta,
                load_reason,
            ) = load_profile_logger_profile(
                logger_runtime,
                profile_logger_entity=profile_logger_entity,
                program_key=program_key or implicit_program_key,
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
                    max_extra_cost_percent=max_extra_cost_percent,
                    prefer_earliest=prefer_earliest,
                    align_start_to_billing_slot=align_start_to_billing_slot,
                    profile_source="profile_logger",
                    profile_meta=profile_meta,
                )
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
        max_extra_cost_percent: float,
        prefer_earliest: bool,
        align_start_to_billing_slot: bool,
        profile_source: str,
        profile_meta: dict[str, Any] | None,
    ) -> dict[str, Any]:
        device_slug = slugify(device_name)
        entity_id = self.plan_entity_id(device_slug)

        plan_payload = build_plan_payload(
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
            timeline_entity_id=self.timeline_entity_id,
            timezone_name=self.timezone,
        )

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

    async def async_reoptimize_plan(self, *, device_slug: str) -> dict[str, Any]:
        plans = self.store.get_plans()
        payload = plans.get(device_slug)
        entity_id = self.plan_entity_id(device_slug)

        if payload is None:
            return {
                "status": "not_found",
                "plan_entity_id": entity_id,
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
        )

    def _build_reset_payload(self, device_name: str) -> PlanPayload:
        return build_reset_payload(device_name, self.timeline_entity_id, self.timezone)

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

        for device_slug, payload in list(self.store.get_plans().items()):
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
                max_extra_cost_percent=float(payload.get("max_extra_cost_percent", 1.0)),
                prefer_earliest=bool(payload.get("prefer_earliest", True)),
                align_start_to_billing_slot=bool(payload.get("align_start_to_billing_slot", False)),
                profile_source=profile_source,
                profile_meta=profile_meta,
            )
            _LOGGER.info(
                "re-optimized plan %s/%s because price coverage extended to %s",
                self.timeline_slug,
                device_slug,
                coverage_end.isoformat(timespec="minutes"),
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

        if profile_source == "profile_logger" and isinstance(profile_meta, dict):
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
                    return self._build_no_candidate_result(load_reason), "profile_logger", current_profile_meta
                return self._build_no_candidate_result("profile_logger_not_found"), "profile_logger", profile_meta
            return self._build_no_candidate_result("missing_profile_logger_metadata"), "profile_logger", profile_meta

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

    def _compute_timeline_stats(self) -> TimelineStats:
        return build_timeline_stats(
            store=self.store,
            timezone_name=self.timezone,
            currency=self.currency,
            round_decimals=self.round_decimals,
            fallback_slot_minutes=DEFAULT_BILLING_SLOT_MINUTES,
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
        return has_primary_tomorrow_rows(self.store.get_slots(), self.timezone)

    def _pending_primary(self) -> bool:
        return pending_primary(self.store.get_slots(), self.timezone)

    def _create_plan_sensor(self, device_slug: str, device_name: str):
        from .sensor import PlanSensor

        payload = self.store.get_plans().get(device_slug)
        return PlanSensor(self, device_slug, device_name, payload)
