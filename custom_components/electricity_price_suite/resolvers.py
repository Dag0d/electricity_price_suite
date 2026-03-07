"""Entity target resolution helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import TimelineRuntime


def resolve_timeline_runtime(
    runtimes: dict[str, TimelineRuntime],
    target_entity_id: str,
) -> TimelineRuntime | None:
    """Resolve one timeline runtime from a timeline entity id."""

    for runtime in runtimes.values():
        if target_entity_id == runtime.timeline_entity_id:
            return runtime
    return None


def resolve_plan_target(
    runtimes: dict[str, TimelineRuntime],
    target_entity_id: str,
) -> tuple[TimelineRuntime, str] | None:
    """Resolve one plan target to its owning runtime and device slug."""

    for runtime in runtimes.values():
        for device_slug in runtime.store.get_plans():
            if runtime.plan_entity_id(device_slug) == target_entity_id:
                return runtime, device_slug
    return None
