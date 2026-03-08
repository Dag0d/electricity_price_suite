"""Entity target resolution helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import ENTRY_TYPE_PROFILE_LOGGER, ENTRY_TYPE_TIMELINE

if TYPE_CHECKING:
    from .logger_runtime import ProfileLoggerRuntime
    from .runtime import TimelineRuntime


RuntimeMap = dict[str, Any]


def resolve_timeline_runtime(runtimes: RuntimeMap, target_entity_id: str) -> TimelineRuntime | None:
    """Resolve one timeline runtime from a timeline entity id."""

    for runtime in runtimes.values():
        if getattr(runtime, "entry", None) is None:
            continue
        if runtime.entry.data.get("entry_type", ENTRY_TYPE_TIMELINE) != ENTRY_TYPE_TIMELINE:
            continue
        if target_entity_id == runtime.timeline_entity_id:
            return runtime
    return None


def resolve_plan_target(runtimes: RuntimeMap, target_entity_id: str) -> tuple[TimelineRuntime, str] | None:
    """Resolve one plan target to its owning runtime and device slug."""

    for runtime in runtimes.values():
        if getattr(runtime, "entry", None) is None:
            continue
        if runtime.entry.data.get("entry_type", ENTRY_TYPE_TIMELINE) != ENTRY_TYPE_TIMELINE:
            continue
        for device_slug in runtime.store.get_plans():
            if runtime.plan_entity_id(device_slug) == target_entity_id:
                return runtime, device_slug
    return None


def resolve_logger_runtime(
    runtimes: RuntimeMap,
    target_entity_id: str,
) -> tuple[ProfileLoggerRuntime | None, str | None]:
    """Resolve a logger runtime from meta or program profile entities."""

    for runtime in runtimes.values():
        if getattr(runtime, "entry", None) is None:
            continue
        if runtime.entry.data.get("entry_type") != ENTRY_TYPE_PROFILE_LOGGER:
            continue
        if target_entity_id == runtime.meta_entity_id:
            return runtime, None
        for program_key in runtime.program_keys:
            if runtime.profile_entity_id(program_key) == target_entity_id:
                return runtime, program_key
    return None, None
