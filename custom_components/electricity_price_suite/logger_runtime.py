"""Profile logger runtime for electricity_price_suite."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    ABORT_REASON_DELAY_EXCEEDED,
    ALLOWED_ABORT_REASONS,
    CONF_ALLOWED_PROGRAMS,
    CONF_AUTO_CREATE_PROGRAMS,
    CONF_ENERGY_ENTITY,
    CONF_ENTRY_TYPE,
    CONF_BLOCKED_PROGRAMS,
    CONF_MAX_POWER_KW,
    CONF_NAME,
    CONF_SLOT_MINUTES,
    CONF_SLUG,
    DEFAULT_AUTO_CREATE_PROGRAMS,
    DEFAULT_MAX_POWER_KW,
    DEFAULT_SLOT_MINUTES,
    ENTRY_TYPE_PROFILE_LOGGER,
    ERROR_ABORTED,
    ERROR_ALREADY_RUNNING,
    ERROR_DELAY_EXCEEDED,
    ERROR_ENERGY_COUNTER_DECREASED,
    ERROR_ENERGY_ENTITY_INVALID,
    ERROR_ENERGY_STATE_CLASS_INVALID,
    ERROR_ENERGY_UNAVAILABLE,
    ERROR_MAX_DELTA_EXCEEDED,
    ERROR_NOT_RUNNING,
    ERROR_PROFILE_NOT_FOUND,
    ERROR_PROGRAM_BLOCKED,
    ERROR_PROGRAM_MISMATCH_FINISH,
    ERROR_PROGRAM_MISSING,
    LOGGER_STORAGE_KEY_PREFIX,
    LOGGER_STORAGE_VERSION,
    MIN_DELAY_FLOOR_SEC,
    SLOT_UNUSED_TRIM_RUNS,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RUNNING,
    TOLERANCE_RATIO,
)
from .logger_utils import display_program_name, normalize_program_key
from .profile_utils import resample_profile_slots, service_profile_result_from_export


@dataclass(slots=True)
class LoggerServiceResult:
    ok: bool
    reason: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, **self.data}


class ProfileLoggerRuntime:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(hass, LOGGER_STORAGE_VERSION, f"{LOGGER_STORAGE_KEY_PREFIX}{entry.entry_id}")
        self._data: dict[str, Any] = self._default_data()
        self._listeners: set[Callable[[], None]] = set()
        self._program_listeners: set[Callable[[str], None]] = set()
        self._program_removed_listeners: set[Callable[[str], None]] = set()
        self._unsub_sample: Callable[[], None] | None = None

    @property
    def config(self) -> dict[str, Any]:
        merged = dict(self.entry.data)
        merged.update(self.entry.options)
        merged.setdefault(CONF_ENTRY_TYPE, ENTRY_TYPE_PROFILE_LOGGER)
        merged.setdefault(CONF_SLOT_MINUTES, DEFAULT_SLOT_MINUTES)
        merged.setdefault(CONF_AUTO_CREATE_PROGRAMS, DEFAULT_AUTO_CREATE_PROGRAMS)
        merged.setdefault(CONF_ALLOWED_PROGRAMS, [])
        merged.setdefault(CONF_BLOCKED_PROGRAMS, [])
        return merged

    @property
    def name(self) -> str:
        return self.config[CONF_NAME]

    @property
    def slug(self) -> str:
        return self.config.get(CONF_SLUG, slugify(self.name))

    @property
    def meta_entity_id(self) -> str:
        return f"sensor.{self.slug}_profile_logger_meta"

    @property
    def energy_entity(self) -> str:
        return self.config[CONF_ENERGY_ENTITY]

    @property
    def slot_minutes(self) -> int:
        return int(self.config[CONF_SLOT_MINUTES])

    @property
    def max_power_kw(self) -> float:
        configured = self.config.get(CONF_MAX_POWER_KW)
        if configured is not None:
            return float(configured)
        return DEFAULT_MAX_POWER_KW

    @property
    def max_delta_kwh(self) -> float:
        return self.max_power_kw * (float(self.slot_minutes) / 60.0)

    @property
    def state(self) -> str:
        return self._data.get("meta", {}).get("state", STATE_IDLE)

    @property
    def state_attributes(self) -> dict[str, Any]:
        meta = dict(self._data.get("meta", {}))
        meta.pop("state", None)
        meta.update(
            {
                "logger_id": self.entry.entry_id,
                "energy_entity": self.energy_entity,
                "max_power_kw": round(self.max_power_kw, 6),
                "max_delta_kwh": round(self.max_delta_kwh, 6),
                "known_programs": sorted(self._data.get("profiles", {}).keys()),
                "estimated_runtimes": dict(self._data.get("estimated_runtimes", {})),
            }
        )
        return meta

    @property
    def program_keys(self) -> list[str]:
        return sorted(self._data.get("profiles", {}).keys())

    async def async_initialize(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._data = stored
        else:
            self._data = self._default_data()

        active_run = self._data.get("active_run")
        if active_run:
            now = dt_util.utcnow()
            due_at = dt_util.parse_datetime(active_run["next_sample_at"])
            if due_at is None:
                await self._async_rollback_locked("invalid_next_sample_at")
            else:
                allowed = self._allowed_delay_seconds()
                if (now - due_at).total_seconds() > allowed:
                    await self._async_rollback_locked("restart_recovery")
                else:
                    self._schedule_next_sample(due_at)

        self._notify_state()

    async def async_shutdown(self) -> None:
        self._cancel_next_sample()

    def add_state_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        def remove() -> None:
            self._listeners.discard(listener)

        return remove

    def add_program_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._program_listeners.add(listener)

        def remove() -> None:
            self._program_listeners.discard(listener)

        return remove

    def add_program_removed_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._program_removed_listeners.add(listener)

        def remove() -> None:
            self._program_removed_listeners.discard(listener)

        return remove

    def profile_entity_id(self, program_key: str) -> str:
        return f"sensor.{self.slug}_profile_{program_key}"

    def get_profile_summary(self, program_key: str, desired_slot_minutes: int | None = None) -> dict[str, Any] | None:
        profile = self._data.get("profiles", {}).get(program_key)
        if not profile:
            return None

        internal_slot_minutes = int(profile.get("slot_minutes", self.slot_minutes))
        slots = profile.get("slots_kwh", [])
        target_slot_minutes = desired_slot_minutes or internal_slot_minutes
        resampled_slots = resample_profile_slots(slots, internal_slot_minutes, target_slot_minutes)
        if resampled_slots is None:
            return None

        return {
            "program_key": profile["program_key"],
            "program_name": profile["program_name"],
            "slot_minutes": int(target_slot_minutes),
            "slot_count": len(resampled_slots),
            "runtime_minutes": int(target_slot_minutes) * len(resampled_slots),
            "last_updated": profile.get("last_updated"),
            "avg_total_kwh": round(sum(float(value) for value in resampled_slots), 5),
            "slots_kwh": [round(float(value), 6) for value in resampled_slots],
        }

    def get_profile_export(
        self,
        program_key: str,
        desired_slot_minutes: int | None = None,
        debug: bool = False,
    ) -> dict[str, Any] | None:
        profile = self._data.get("profiles", {}).get(program_key)
        if not profile:
            return None

        summary = self.get_profile_summary(program_key, desired_slot_minutes=desired_slot_minutes)
        if summary is None:
            return None
        payload = {
            "logger_id": self.entry.entry_id,
            "logger_name": self.name,
            "program_key": summary["program_key"],
            "program_name": summary["program_name"],
            "slot_minutes": summary["slot_minutes"],
            "slot_count": summary["slot_count"],
            "runtime_minutes": summary["runtime_minutes"],
            "avg_total_kwh": summary["avg_total_kwh"],
            "slots_kwh": summary["slots_kwh"],
            "last_updated": summary["last_updated"],
        }
        if debug:
            payload["debug"] = {
                "run_count": int(profile.get("run_count", 0)),
                "internal_slot_minutes": int(profile.get("slot_minutes", self.slot_minutes)),
                "internal_slot_count": len(profile.get("slots_kwh", [])),
                "slot_missing_runs": list(profile.get("slot_missing_runs", [])),
            }
        return payload

    def get_profile_service_response(
        self,
        program_key: str,
        *,
        desired_slot_minutes: int | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        """Return the normalized service response for one program profile."""

        payload = self.get_profile_export(
            program_key,
            desired_slot_minutes=desired_slot_minutes,
            debug=debug,
        )
        result = service_profile_result_from_export(
            payload,
            runtime_data=self.get_profile_runtime_data(program_key),
            desired_slot_minutes=desired_slot_minutes,
        )
        if result.ok:
            return {"ok": True, **(result.payload or {})}
        return {
            "ok": False,
            "reason": result.reason,
            **(result.payload or {}),
        }

    def get_program_list(self) -> list[dict[str, Any]]:
        return [
            {
                "program_key": program_key,
                "program_name": profile["program_name"],
            }
            for program_key, profile in sorted(self._data.get("profiles", {}).items())
        ]

    def get_profile_runtime_data(self, program_key: str) -> dict[str, Any] | None:
        profile = self._data.get("profiles", {}).get(program_key)
        if not profile:
            return None
        return {
            "run_count": int(profile.get("run_count", 0)),
            "slot_missing_runs": list(profile.get("slot_missing_runs", [])),
            "internal_slot_minutes": int(profile.get("slot_minutes", self.slot_minutes)),
            "internal_slot_count": len(profile.get("slots_kwh", [])),
        }

    def get_estimated_runtime_minutes(self, program_key: str | None) -> float | None:
        """Return one configured estimated runtime for a program key."""

        normalized_program = normalize_program_key(program_key)
        if not normalized_program:
            return None
        raw = self._data.get("estimated_runtimes", {}).get(normalized_program)
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            return None
        return duration if duration > 0 else None

    def get_program_display_name(self, program_key: str | None) -> str | None:
        """Return the current display name for one program key."""

        normalized_program = normalize_program_key(program_key)
        if not normalized_program:
            return None
        profile = self._data.get("profiles", {}).get(normalized_program)
        if isinstance(profile, dict) and isinstance(profile.get("program_name"), str):
            return profile["program_name"]
        return display_program_name(normalized_program)

    async def async_manage_estimated_runtime(
        self,
        *,
        mode: str,
        items: dict[str, Any] | None = None,
        program_key: str | None = None,
    ) -> dict[str, Any]:
        """Add, delete, list, or clear estimated runtimes for this logger."""

        async with self._lock:
            estimated = self._data.setdefault("estimated_runtimes", {})
            if mode == "list":
                return {
                    "ok": True,
                    "logger_id": self.entry.entry_id,
                    "entity_id": self.meta_entity_id,
                    "estimated_runtimes": dict(sorted(estimated.items())),
                    "count": len(estimated),
                }

            if mode == "clear":
                removed = len(estimated)
                estimated.clear()
                await self._store.async_save(self._data)
                self._notify_state()
                return {
                    "ok": True,
                    "mode": "clear",
                    "logger_id": self.entry.entry_id,
                    "entity_id": self.meta_entity_id,
                    "cleared": removed,
                }

            if mode == "delete":
                normalized_program = normalize_program_key(program_key)
                if not normalized_program:
                    return self._error(ERROR_PROGRAM_MISSING)
                deleted = estimated.pop(normalized_program, None)
                if deleted is None:
                    return {
                        "ok": False,
                        "reason": "estimated_runtime_not_found",
                        "entity_id": self.meta_entity_id,
                        "program_key": normalized_program,
                    }
                await self._store.async_save(self._data)
                self._notify_state()
                return {
                    "ok": True,
                    "mode": "delete",
                    "logger_id": self.entry.entry_id,
                    "entity_id": self.meta_entity_id,
                    "program_key": normalized_program,
                }

            if mode == "add":
                if not isinstance(items, dict) or not items:
                    return {
                        "ok": False,
                        "reason": "missing_items",
                        "entity_id": self.meta_entity_id,
                    }
                added: dict[str, float] = {}
                for raw_key, raw_duration in items.items():
                    normalized_program = normalize_program_key(str(raw_key))
                    if not normalized_program:
                        return {
                            "ok": False,
                            "reason": "invalid_program_key",
                            "entity_id": self.meta_entity_id,
                        }
                    try:
                        duration = float(raw_duration)
                    except (TypeError, ValueError):
                        return {
                            "ok": False,
                            "reason": "invalid_duration_minutes",
                            "entity_id": self.meta_entity_id,
                            "program_key": normalized_program,
                        }
                    if duration <= 0:
                        return {
                            "ok": False,
                            "reason": "invalid_duration_minutes",
                            "entity_id": self.meta_entity_id,
                            "program_key": normalized_program,
                        }
                    estimated[normalized_program] = duration
                    added[normalized_program] = duration
                await self._store.async_save(self._data)
                self._notify_state()
                return {
                    "ok": True,
                    "mode": "add",
                    "logger_id": self.entry.entry_id,
                    "entity_id": self.meta_entity_id,
                    "items": dict(sorted(added.items())),
                    "count": len(estimated),
                }

            return {
                "ok": False,
                "reason": "invalid_mode",
                "entity_id": self.meta_entity_id,
            }

    def get_profile_sensor_payload(self, program_key: str) -> dict[str, Any] | None:
        """Return one UI-friendly profile payload for sensor state and attributes."""

        summary = self.get_profile_summary(program_key)
        runtime_data = self.get_profile_runtime_data(program_key)
        if summary is None:
            return None
        return {
            "program_key": summary["program_key"],
            "program_name": summary["program_name"],
            "avg_total_kwh": summary["avg_total_kwh"],
            "run_count": (runtime_data or {}).get("run_count", 0),
            "slot_minutes": summary["slot_minutes"],
            "slot_count": summary["slot_count"],
            "runtime_minutes": summary["runtime_minutes"],
            "last_updated": summary["last_updated"],
        }

    async def async_start(self, program_key: str | None) -> LoggerServiceResult:
        async with self._lock:
            normalized_program = normalize_program_key(program_key)
            if not normalized_program:
                return await self._async_fail_start(ERROR_PROGRAM_MISSING)
            if self._data.get("active_run"):
                await self._async_rollback_locked(ERROR_ALREADY_RUNNING)
                return self._error(ERROR_ALREADY_RUNNING)
            if not self._is_program_allowed(normalized_program):
                return await self._async_fail_start(ERROR_PROGRAM_BLOCKED)
            current_energy, energy_error = self._read_energy_kwh()
            if energy_error is not None or current_energy is None:
                return await self._async_fail_start(energy_error or ERROR_ENERGY_ENTITY_INVALID)
            profile = self._data["profiles"].get(normalized_program)
            program_created = False
            if profile is None:
                if not self.config.get(CONF_AUTO_CREATE_PROGRAMS, DEFAULT_AUTO_CREATE_PROGRAMS):
                    return await self._async_fail_start(ERROR_PROFILE_NOT_FOUND)
                profile = self._new_profile(normalized_program)
                self._data["profiles"][normalized_program] = profile
                program_created = True
            now = dt_util.utcnow()
            next_sample_at = now + timedelta(minutes=self.slot_minutes)
            self._data["active_run"] = {
                "run_id": dt_util.utcnow().isoformat(),
                "program_key": normalized_program,
                "program_name": profile["program_name"],
                "started_at": now.isoformat(),
                "last_sample_at": now.isoformat(),
                "next_sample_at": next_sample_at.isoformat(),
                "samples_taken": 0,
                "last_total_kwh": current_energy,
                "snapshot_profile": deepcopy(profile),
            }
            self._set_meta(
                STATE_RUNNING,
                active_program=profile["program_name"],
                run_id=self._data["active_run"]["run_id"],
                slot_minutes=self.slot_minutes,
                started_at=now.isoformat(),
                last_sample_at=now.isoformat(),
                next_sample_at=next_sample_at.isoformat(),
                samples_taken=0,
                reason=None,
            )
            await self._store.async_save(self._data)
            self._schedule_next_sample(next_sample_at)
            self._notify_state()
            if program_created:
                self._notify_program(normalized_program)
            return LoggerServiceResult(True, "started", {"logger_id": self.entry.entry_id, "entity_id": self.meta_entity_id, "program_key": normalized_program, "program_name": profile["program_name"]})

    async def async_finish(self, program_key: str | None) -> LoggerServiceResult:
        async with self._lock:
            active = self._data.get("active_run")
            if not active:
                return self._error(ERROR_NOT_RUNNING)
            normalized_program = normalize_program_key(program_key)
            if not normalized_program:
                await self._async_rollback_locked(ERROR_PROGRAM_MISSING)
                return self._error(ERROR_PROGRAM_MISSING)
            if normalized_program != active["program_key"]:
                await self._async_rollback_locked(ERROR_PROGRAM_MISMATCH_FINISH)
                return self._error(ERROR_PROGRAM_MISMATCH_FINISH)
            current_energy, energy_error = self._read_energy_kwh()
            if energy_error is not None or current_energy is None:
                await self._async_rollback_locked(energy_error or ERROR_ENERGY_ENTITY_INVALID)
                return self._error(energy_error or ERROR_ENERGY_ENTITY_INVALID)
            profile = self._data["profiles"][normalized_program]
            prev_runs = int(active["snapshot_profile"].get("run_count", 0))
            new_runs = prev_runs + 1
            raw_delta = current_energy - float(active["last_total_kwh"])
            if raw_delta < 0:
                await self._async_rollback_locked(ERROR_ENERGY_COUNTER_DECREASED)
                return self._error(ERROR_ENERGY_COUNTER_DECREASED)
            if raw_delta > self.max_delta_kwh:
                await self._async_rollback_locked(f"{ERROR_MAX_DELTA_EXCEEDED}:{raw_delta:.5f}>{self.max_delta_kwh:.5f}")
                return self._error(ERROR_MAX_DELTA_EXCEEDED)
            delta = raw_delta
            slot_index = int(active["samples_taken"])
            self._mean_into(profile["slots_kwh"], slot_index, delta, prev_runs, new_runs)
            self._reset_slot_missing_runs(profile, slot_index)
            self._decay_trailing(profile, slot_index + 1, prev_runs, new_runs)
            self._trim_trailing_slots(profile)
            profile["run_count"] = new_runs
            profile["slot_minutes"] = self.slot_minutes
            profile["last_updated"] = dt_util.utcnow().isoformat()
            self._data["active_run"] = None
            self._cancel_next_sample()
            self._set_meta(STATE_IDLE, active_program=None, run_id=None, slot_minutes=self.slot_minutes, started_at=None, last_sample_at=dt_util.utcnow().isoformat(), next_sample_at=None, samples_taken=None, reason=None)
            await self._store.async_save(self._data)
            self._notify_state()
            return LoggerServiceResult(True, "finished", {"logger_id": self.entry.entry_id, "entity_id": self.meta_entity_id, "program_key": normalized_program, "avg_total_kwh": self.get_profile_summary(normalized_program)["avg_total_kwh"]})

    async def async_abort(self, reason: str | None = None, program_key: str | None = None) -> LoggerServiceResult:
        async with self._lock:
            active = self._data.get("active_run")
            if not active:
                return self._error(ERROR_NOT_RUNNING)
            if program_key is not None:
                normalized_program = normalize_program_key(program_key)
                if normalized_program and normalized_program != active["program_key"]:
                    reason = "program_mismatch"
            normalized_reason = reason if reason in ALLOWED_ABORT_REASONS else ERROR_ABORTED
            await self._async_rollback_locked(normalized_reason)
            return LoggerServiceResult(True, "aborted", {"logger_id": self.entry.entry_id, "entity_id": self.meta_entity_id, "reason": normalized_reason})

    async def async_reset_profile(self, program_key: str | None) -> LoggerServiceResult:
        async with self._lock:
            normalized_program = normalize_program_key(program_key)
            if not normalized_program:
                return self._error(ERROR_PROGRAM_MISSING)
            active = self._data.get("active_run")
            if active and active["program_key"] == normalized_program:
                return self._error(ERROR_ALREADY_RUNNING)
            profile = self._data.get("profiles", {}).get(normalized_program)
            if profile is None:
                return self._error(ERROR_PROFILE_NOT_FOUND)
            profile["run_count"] = 0
            profile["slot_minutes"] = self.slot_minutes
            profile["slots_kwh"] = []
            profile["slot_missing_runs"] = []
            profile["last_updated"] = dt_util.utcnow().isoformat()
            await self._store.async_save(self._data)
            self._notify_state()
            return LoggerServiceResult(True, "reset", {"logger_id": self.entry.entry_id, "entity_id": self.meta_entity_id, "program_key": normalized_program})

    async def async_delete_profile(self, program_key: str | None) -> LoggerServiceResult:
        async with self._lock:
            normalized_program = normalize_program_key(program_key)
            if not normalized_program:
                return self._error(ERROR_PROGRAM_MISSING)
            active = self._data.get("active_run")
            if active and active["program_key"] == normalized_program:
                return self._error(ERROR_ALREADY_RUNNING)
            if normalized_program not in self._data.get("profiles", {}):
                return self._error(ERROR_PROFILE_NOT_FOUND)
            del self._data["profiles"][normalized_program]
            await self._store.async_save(self._data)
            self._notify_state()
            self._notify_program_removed(normalized_program)
            return LoggerServiceResult(True, "deleted", {"logger_id": self.entry.entry_id, "entity_id": self.meta_entity_id, "program_key": normalized_program})

    async def async_handle_scheduled_sample(self, *_: Any) -> None:
        async with self._lock:
            active = self._data.get("active_run")
            if not active:
                return
            due_at = dt_util.parse_datetime(active["next_sample_at"])
            if due_at is None:
                await self._async_rollback_locked("invalid_next_sample_at")
                return
            delay_sec = max((dt_util.utcnow() - due_at).total_seconds(), 0.0)
            if delay_sec > self._allowed_delay_seconds():
                await self._async_rollback_locked(ABORT_REASON_DELAY_EXCEEDED)
                return
            current_energy, energy_error = self._read_energy_kwh()
            if energy_error is not None or current_energy is None:
                await self._async_rollback_locked(energy_error or ERROR_ENERGY_ENTITY_INVALID)
                return
            program_key = active["program_key"]
            profile = self._data["profiles"][program_key]
            prev_runs = int(active["snapshot_profile"].get("run_count", 0))
            new_runs = prev_runs + 1
            sample_index = int(active["samples_taken"])
            raw_delta = current_energy - float(active["last_total_kwh"])
            if raw_delta < 0:
                await self._async_rollback_locked(ERROR_ENERGY_COUNTER_DECREASED)
                return
            if raw_delta > self.max_delta_kwh:
                await self._async_rollback_locked(f"{ERROR_MAX_DELTA_EXCEEDED}:{raw_delta:.5f}>{self.max_delta_kwh:.5f}")
                return
            delta = raw_delta
            self._mean_into(profile["slots_kwh"], sample_index, delta, prev_runs, new_runs)
            self._reset_slot_missing_runs(profile, sample_index)
            next_due = due_at + timedelta(minutes=self.slot_minutes)
            active["samples_taken"] = sample_index + 1
            active["last_total_kwh"] = current_energy
            active["last_sample_at"] = dt_util.utcnow().isoformat()
            active["next_sample_at"] = next_due.isoformat()
            self._set_meta(STATE_RUNNING, active_program=profile["program_name"], run_id=active["run_id"], slot_minutes=self.slot_minutes, started_at=active["started_at"], last_sample_at=active["last_sample_at"], next_sample_at=active["next_sample_at"], samples_taken=active["samples_taken"], reason=None)
            await self._store.async_save(self._data)
            self._schedule_next_sample(next_due)
            self._notify_state()

    def _default_data(self) -> dict[str, Any]:
        return {
            "meta": {
                "state": STATE_IDLE,
                "active_program": None,
                "run_id": None,
                "slot_minutes": self.entry.data.get(CONF_SLOT_MINUTES, DEFAULT_SLOT_MINUTES),
                "started_at": None,
                "last_sample_at": None,
                "next_sample_at": None,
                "samples_taken": None,
                "reason": None,
            },
            "profiles": {},
            "estimated_runtimes": {},
            "active_run": None,
        }

    def _new_profile(self, program_key: str) -> dict[str, Any]:
        return {
            "program_key": program_key,
            "program_name": display_program_name(program_key),
            "run_count": 0,
            "slot_minutes": self.slot_minutes,
            "slots_kwh": [],
            "slot_missing_runs": [],
            "last_updated": None,
        }

    def _is_program_allowed(self, program_key: str) -> bool:
        blocked = {normalize_program_key(item) for item in self.config.get(CONF_BLOCKED_PROGRAMS, [])}
        allowed = {normalize_program_key(item) for item in self.config.get(CONF_ALLOWED_PROGRAMS, [])}
        blocked.discard(None)
        allowed.discard(None)
        if program_key in blocked:
            return False
        if allowed and program_key not in allowed:
            return False
        return True

    def _read_energy_kwh(self) -> tuple[float | None, str | None]:
        state = self.hass.states.get(self.energy_entity)
        if state is None:
            return None, ERROR_ENERGY_ENTITY_INVALID
        state_text = str(state.state).strip().lower()
        if state_text in {"unknown", "unavailable"}:
            return None, ERROR_ENERGY_UNAVAILABLE
        try:
            raw_value = float(state.state)
        except (TypeError, ValueError):
            return None, ERROR_ENERGY_ENTITY_INVALID
        unit = state.attributes.get("unit_of_measurement")
        if unit == UnitOfEnergy.KILO_WATT_HOUR:
            normalized = raw_value
        elif unit == UnitOfEnergy.WATT_HOUR:
            normalized = raw_value / 1000.0
        else:
            return None, ERROR_ENERGY_ENTITY_INVALID
        if state.attributes.get("state_class") != "total_increasing":
            return None, ERROR_ENERGY_STATE_CLASS_INVALID
        return normalized, None

    def _mean_into(self, values: list[float], index: int, value: float, prev_runs: int, new_runs: int) -> None:
        while len(values) <= index:
            values.append(0.0)
        previous = float(values[index])
        values[index] = ((previous * float(prev_runs)) + float(value)) / float(new_runs)

    def _reset_slot_missing_runs(self, profile: dict[str, Any], index: int) -> None:
        missing_runs = profile.setdefault("slot_missing_runs", [])
        values = profile.setdefault("slots_kwh", [])
        while len(missing_runs) < len(values):
            missing_runs.append(0)
        while len(missing_runs) <= index:
            missing_runs.append(0)
        missing_runs[index] = 0

    def _decay_trailing(self, profile: dict[str, Any], start_index: int, prev_runs: int, new_runs: int) -> None:
        values = profile.get("slots_kwh", [])
        missing_runs = profile.setdefault("slot_missing_runs", [])
        while len(missing_runs) < len(values):
            missing_runs.append(0)
        if start_index >= len(values) or new_runs <= 0:
            return
        factor = float(prev_runs) / float(new_runs)
        for index in range(start_index, len(values)):
            values[index] = float(values[index]) * factor
            missing_runs[index] = int(missing_runs[index]) + 1

    def _trim_trailing_slots(self, profile: dict[str, Any]) -> None:
        values = profile.get("slots_kwh", [])
        missing_runs = profile.setdefault("slot_missing_runs", [])
        while len(missing_runs) < len(values):
            missing_runs.append(0)
        while values and missing_runs and int(missing_runs[-1]) >= SLOT_UNUSED_TRIM_RUNS:
            values.pop()
            missing_runs.pop()

    def _allowed_delay_seconds(self) -> float:
        slot_seconds = float(self.slot_minutes * 60)
        return max(MIN_DELAY_FLOOR_SEC, slot_seconds * TOLERANCE_RATIO)

    async def _async_rollback_locked(self, reason: str) -> None:
        active = self._data.get("active_run")
        if active:
            self._data["profiles"][active["program_key"]] = deepcopy(active["snapshot_profile"])
        self._data["active_run"] = None
        self._cancel_next_sample()
        self._set_meta(STATE_ERROR, active_program=None, run_id=None, slot_minutes=self.slot_minutes, started_at=None, last_sample_at=dt_util.utcnow().isoformat(), next_sample_at=None, samples_taken=None, reason=reason)
        await self._store.async_save(self._data)
        self._notify_state()

    async def _async_fail_start(self, reason: str) -> LoggerServiceResult:
        self._set_meta(STATE_ERROR, active_program=None, run_id=None, slot_minutes=self.slot_minutes, started_at=None, last_sample_at=dt_util.utcnow().isoformat(), next_sample_at=None, samples_taken=None, reason=reason)
        await self._store.async_save(self._data)
        self._notify_state()
        return self._error(reason)

    def _set_meta(self, state: str, **attrs: Any) -> None:
        self._data["meta"] = {"state": state, **attrs}

    def _schedule_next_sample(self, due_at) -> None:
        self._cancel_next_sample()
        self._unsub_sample = async_track_point_in_utc_time(self.hass, self.async_handle_scheduled_sample, due_at)

    def _cancel_next_sample(self) -> None:
        if self._unsub_sample is not None:
            self._unsub_sample()
            self._unsub_sample = None

    @callback
    def _notify_state(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def _notify_program(self, program_key: str) -> None:
        for listener in list(self._program_listeners):
            listener(program_key)

    @callback
    def _notify_program_removed(self, program_key: str) -> None:
        for listener in list(self._program_removed_listeners):
            listener(program_key)

    def _error(self, reason: str) -> LoggerServiceResult:
        return LoggerServiceResult(ok=False, reason=reason, data={"entity_id": self.meta_entity_id})
