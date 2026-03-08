"""Source providers for timeline refresh."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .models import SlotRecord, SourceAttempt, utc_now_iso
from .time_utils import format_iso, parse_iso_aware

_LOGGER = logging.getLogger(__name__)


def _extract_path(data: Any, path: str | None) -> Any:
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if idx < 0 or idx >= len(current):
                return None
            current = current[idx]
        else:
            return None
    return current


def _parse_slot_time(raw: Any) -> str | None:
    dt = parse_iso_aware(raw)
    if dt is None:
        return None
    return format_iso(dt)


def normalize_slots(raw_slots: Any, source: dict) -> list[SlotRecord]:
    if not isinstance(raw_slots, list):
        return []

    mapping = source.get("slot_mapping") or {}
    time_key = mapping.get("time_key", "start_time")
    price_key = mapping.get("price_key", "price_per_kwh")

    out: list[SlotRecord] = []
    source_priority = int(source.get("priority", 9999))
    is_primary_source = source_priority == 0
    for item in raw_slots:
        if not isinstance(item, dict):
            continue
        parsed_time = _parse_slot_time(item.get(time_key))
        if not parsed_time:
            continue
        try:
            price = float(item.get(price_key))
        except (TypeError, ValueError):
            continue

        out.append(
            SlotRecord(
                start_time=parsed_time,
                price_per_kwh=price,
                source_id=str(source["id"]),
                source_priority=source_priority,
                is_primary_source=is_primary_source,
                observed_at=utc_now_iso(),
            )
        )

    return out


async def _fetch_entity_attribute(hass: HomeAssistant, source: dict) -> Any:
    entity_id = source.get("entity_id")
    attribute = source.get("attribute")
    if not entity_id:
        raise ValueError("missing_entity_id")
    if attribute is None:
        raise ValueError("missing_attribute")
    state = hass.states.get(entity_id)
    if state is None:
        raise ValueError("entity_not_found")
    return state.attributes.get(attribute)


async def _fetch_entity_action(hass: HomeAssistant, source: dict) -> Any:
    action = source.get("action")
    if not action:
        raise ValueError("invalid_action")

    if "/" in action:
        domain, service = action.split("/", 1)
    elif "." in action:
        domain, service = action.split(".", 1)
    else:
        raise ValueError("invalid_action")

    payload = dict(source.get("request_payload") or source.get("data") or {})
    inject_time_window = bool(source.get("inject_time_window", False))
    if not payload and not inject_time_window:
        raise ValueError("missing_request_payload")

    if inject_time_window:
        tz_name = source.get("timezone") or hass.config.time_zone
        start_key = source.get("start_key", "start")
        end_key = source.get("end_key", "end")
        time_format = source.get("time_format", "%Y-%m-%d %H:%M:%S")

        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_end = (today_start + timedelta(days=2)) - timedelta(seconds=1)
        payload[start_key] = today_start.strftime(time_format)
        payload[end_key] = tomorrow_end.strftime(time_format)
    entity_id = source.get("entity_id")
    if entity_id:
        payload.setdefault("entity_id", entity_id)

    response = await hass.services.async_call(
        domain,
        service,
        payload,
        blocking=True,
        return_response=True,
    )
    response_path = source.get("response_path")
    if response_path is None:
        raise ValueError("missing_response_path")
    return _extract_path(response, response_path)


async def _fetch_tibber_api(hass: HomeAssistant, source: dict) -> Any:
    token = source.get("token")
    if not token:
        raise ValueError("missing_token")

    home_index = int(source.get("home_index", 0))
    use_today_tomorrow = bool(source.get("use_today_tomorrow", True))
    use_consumption_history = bool(source.get("use_consumption_history", False))
    history_hours = int(source.get("history_hours", 196))

    if use_today_tomorrow:
        price_info_fragment = """
          currentSubscription {
            priceInfo {
              today { startsAt total }
              tomorrow { startsAt total }
            }
          }
        """
    else:
        price_info_fragment = ""

    consumption_fragment = (
        f"""
          consumption(resolution: HOURLY, last: {history_hours}) {{
            nodes {{ from unitPrice }}
          }}
        """
        if use_consumption_history
        else ""
    )

    query = f"""
    query PriceData {{
      viewer {{
        homes {{
          {price_info_fragment}
          {consumption_fragment}
        }}
      }}
    }}
    """

    auth = token if str(token).lower().startswith("bearer ") else f"Bearer {token}"
    session = async_get_clientsession(hass)
    async with session.post(
        "https://api.tibber.com/v1-beta/gql",
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json={"query": query},
        timeout=30,
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    homes = (((payload or {}).get("data") or {}).get("viewer") or {}).get("homes") or []
    if not homes:
        return []
    if home_index < 0 or home_index >= len(homes):
        home_index = 0
    home = homes[home_index]

    out: list[dict[str, Any]] = []
    if use_today_tomorrow:
        price_info = (((home or {}).get("currentSubscription") or {}).get("priceInfo") or {})
        for row in (price_info.get("today") or []) + (price_info.get("tomorrow") or []):
            out.append({"start_time": row.get("startsAt"), "price_per_kwh": row.get("total")})

    if use_consumption_history:
        nodes = ((home or {}).get("consumption") or {}).get("nodes") or []
        for row in nodes:
            out.append({"start_time": row.get("from"), "price_per_kwh": row.get("unitPrice")})

    return out


async def fetch_from_source(
    hass: HomeAssistant,
    source: dict,
) -> tuple[list[SlotRecord], SourceAttempt]:
    source_id = str(source.get("id", "unknown"))
    source_type = str(source.get("type", "unknown"))

    try:
        if source_type == "entity_attribute":
            raw = await _fetch_entity_attribute(hass, source)
        elif source_type == "entity_action":
            raw = await _fetch_entity_action(hass, source)
        elif source_type == "inject_only":
            raw = []
        elif source_type == "tibber_api":
            raw = await _fetch_tibber_api(hass, source)
        else:
            attempt = SourceAttempt(source_id, source_type, False, 0, "unsupported_source_type")
            return [], attempt

        slots = normalize_slots(raw, source)
        if not slots:
            return [], SourceAttempt(source_id, source_type, False, 0, "no_slots")
        return slots, SourceAttempt(source_id, source_type, True, len(slots), None)
    except Exception as err:  # pragma: no cover - defensive
        _LOGGER.debug("source fetch failed for %s: %s", source_id, err, exc_info=True)
        return [], SourceAttempt(source_id, source_type, False, 0, str(err))
