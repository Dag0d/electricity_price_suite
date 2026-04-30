"""Source providers for timeline refresh."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import enum
import logging
import xml.etree.ElementTree as ET
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .models import SlotRecord, SourceAttempt, utc_now_iso
from .time_utils import format_iso, parse_iso_aware

_LOGGER = logging.getLogger(__name__)

_SMARD_MARKET_AREA_MAP = {
    "DE-LU": 4169,
    "Anrainer DE-LU": 5078,
    "BE": 4996,
    "NO2": 4997,
    "AT": 4170,
    "DK1": 252,
    "DK2": 253,
    "FR": 254,
    "IT (North)": 255,
    "NL": 256,
    "PL": 257,
    "CH": 259,
    "SI": 260,
    "CZ": 261,
    "HU": 262,
}

_SMARD_MARKET_AREA_ALIASES = {
    "IT-North": "IT (North)",
}

_ENTSOE_MARKET_AREA_MAP = {
    "AT": "10YAT-APG------L",
    "BE": "10YBE----------2",
    "BG": "10YCA-BULGARIA-R",
    "CH": "10YCH-SWISSGRIDZ",
    "CZ": "10YCZ-CEPS-----N",
    "DE-LU": "10Y1001A1001A82H",
    "DK1": "10YDK-1--------W",
    "DK2": "10YDK-2--------M",
    "EE": "10Y1001A1001A39I",
    "ES": "10YES-REE------0",
    "FI": "10YFI-1--------U",
    "FR": "10YFR-RTE------C",
    "GR": "10YGR-HTSO-----Y",
    "HR": "10YHR-HEP------M",
    "HU": "10YHU-MAVIR----U",
    "IT-North": "10Y1001A1001A73I",
    "LT": "10YLT-1001A0008Q",
    "LV": "10YLV-1001A00074",
    "NL": "10YNL----------L",
    "NO1": "10YNO-1--------2",
    "NO2": "10YNO-2--------T",
    "NO3": "10YNO-3--------J",
    "NO4": "10YNO-4--------9",
    "NO5": "10Y1001A1001A48H",
    "PL": "10YPL-AREA-----S",
    "PT": "10YPT-REN------W",
    "RO": "10YRO-TEL------P",
    "SE1": "10Y1001A1001A44P",
    "SE2": "10Y1001A1001A45N",
    "SE3": "10Y1001A1001A46L",
    "SE4": "10Y1001A1001A47J",
    "SI": "10YSI-ELES-----O",
    "SK": "10YSK-SEPS-----K",
}

_ENTSOE_MARKET_AREA_ALIASES = {
    "IT (North)": "IT-North",
    "DE-AT-LU": "DE-LU",
}

_ENERGY_CHARTS_BIDDING_ZONES = {
    "AT", "BE", "BG", "CH", "CZ", "DE-LU", "DE-AT-LU", "DK1", "DK2", "EE", "ES", "FI", "FR", "GR",
    "HR", "HU", "IT-Calabria", "IT-Centre-North", "IT-Centre-South", "IT-North", "IT-SACOAC",
    "IT-SACODC", "IT-Sardinia", "IT-Sicily", "IT-South", "LT", "LV", "ME", "NL", "NO1", "NO2",
    "NO2NSL", "NO3", "NO4", "NO5", "PL", "PT", "RO", "RS", "SE1", "SE2", "SE3", "SE4", "SI", "SK",
}

_ENERGY_CHARTS_ALIASES = {
    "DE": "DE-LU",
    "IT (North)": "IT-North",
}

_ENTSOE_XML_NS = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}

PROVIDER_NATIVE_DURATIONS = {
    "tibber": {15, 60},
    "smard": {15, 60},
    "energy_charts": {15},
    "entsoe": {15, 60},
}

PROVIDER_ALLOW_AGGREGATE_15_TO_60 = {
    "tibber": False,
    "smard": False,
    "energy_charts": True,
    "entsoe": True,
}

PROVIDER_LABELS = {
    "tibber": "Tibber",
    "smard": "SMARD",
    "energy_charts": "Energy-Charts",
    "entsoe": "ENTSO-E",
}


class _EntsoeAgreementType(enum.Enum):
    DAY_AHEAD = "A01"


class _EntsoeDocumentType(enum.Enum):
    DAY_AHEAD_PRICES = "A44"


def provider_supports_billing(provider_type: str, billing_minutes: int) -> bool:
    native = PROVIDER_NATIVE_DURATIONS.get(provider_type, set())
    if billing_minutes in native:
        return True
    return (
        billing_minutes == 60
        and 15 in native
        and PROVIDER_ALLOW_AGGREGATE_15_TO_60.get(provider_type, False)
    )


def provider_market_areas(provider_type: str) -> list[str]:
    if provider_type == "smard":
        return sorted(_SMARD_MARKET_AREA_MAP.keys())
    if provider_type == "energy_charts":
        return sorted(_ENERGY_CHARTS_BIDDING_ZONES)
    if provider_type == "entsoe":
        return sorted(_ENTSOE_MARKET_AREA_MAP.keys())
    return []


def _parse_slot_time(raw: Any) -> str | None:
    dt = parse_iso_aware(raw)
    if dt is None:
        return None
    return format_iso(dt)


def _normalize_market_area(
    market_area: str | None,
    *,
    supported: dict[str, Any] | set[str],
    aliases: dict[str, str] | None = None,
) -> str:
    raw = str(market_area or "").strip()
    if not raw:
        raise ValueError("missing_market_area")
    if raw in supported:
        return raw
    if aliases and raw in aliases and aliases[raw] in supported:
        return aliases[raw]

    upper = raw.upper()
    if upper in supported:
        return upper
    if aliases and upper in aliases and aliases[upper] in supported:
        return aliases[upper]

    raise ValueError(f"unsupported_market_area:{raw}")


def _requested_duration_minutes(source: dict) -> int:
    try:
        duration = int(source.get("duration_minutes", 15))
    except (TypeError, ValueError):
        duration = 15
    return 60 if duration == 60 else 15


def _aggregate_15_to_60_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sorted_rows = sorted(
        rows,
        key=lambda item: parse_iso_aware(item.get("start_time")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    buckets: dict[datetime, dict[str, list[float]]] = {}
    for row in sorted_rows:
        start = parse_iso_aware(row.get("start_time"))
        if start is None:
            continue
        bucket = start.replace(minute=0, second=0, microsecond=0)
        values = buckets.setdefault(bucket, {"market": [], "final": []})
        values["market"].append(float(row.get("market_price_per_kwh", row["price_per_kwh"])))
        provider_final = row.get("provider_final_price_per_kwh")
        if provider_final is not None:
            values["final"].append(float(provider_final))

    aggregated: list[dict[str, Any]] = []
    for bucket_start in sorted(buckets.keys()):
        values = buckets[bucket_start]
        prices = values["market"]
        if len(prices) != 4:
            continue
        row: dict[str, Any] = {
            "start_time": format_iso(bucket_start),
            "market_price_per_kwh": sum(prices) / 4.0,
        }
        if len(values["final"]) == 4:
            row["provider_final_price_per_kwh"] = sum(values["final"]) / 4.0
        aggregated.append(row)
    return aggregated


def _slot_minutes_from_resolution(resolution: str) -> int:
    value = str(resolution or "").strip().upper()
    if value == "PT15M":
        return 15
    if value == "PT30M":
        return 30
    if value == "PT60M":
        return 60
    raise ValueError(f"unsupported_resolution:{resolution}")


def normalize_slots(raw_slots: Any, source: dict) -> list[SlotRecord]:
    if not isinstance(raw_slots, list):
        return []

    mapping = source.get("slot_mapping") or {}
    time_key = mapping.get("time_key", "start_time")
    price_key = mapping.get("price_key", "price_per_kwh")
    market_price_key = mapping.get("market_price_key", "market_price_per_kwh")
    provider_final_price_key = mapping.get("provider_final_price_key", "provider_final_price_per_kwh")

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
            market_price = item.get(market_price_key)
            if market_price is None:
                market_price = item.get(price_key)
            market_price = float(market_price)
        except (TypeError, ValueError):
            continue

        out.append(
            SlotRecord(
                start_time=parsed_time,
                market_price_per_kwh=market_price,
                price_per_kwh=market_price,
                provider_final_price_per_kwh=(
                    float(item.get(provider_final_price_key))
                    if item.get(provider_final_price_key) is not None
                    else None
                ),
                source_id=str(source["id"]),
                source_priority=source_priority,
                is_primary_source=is_primary_source,
                observed_at=utc_now_iso(),
            )
        )

    return out


async def _fetch_tibber_direct(hass: HomeAssistant, source: dict) -> list[dict[str, Any]]:
    token = str(source.get("token") or "").strip()
    if not token:
        raise ValueError("missing_token")

    home_index = int(source.get("home_index", 0))
    requested_duration = _requested_duration_minutes(source)
    if requested_duration not in PROVIDER_NATIVE_DURATIONS["tibber"]:
        raise ValueError(f"unsupported_duration:{requested_duration}")
    resolution = "QUARTER_HOURLY" if requested_duration == 15 else "HOURLY"
    query = """
    query PriceData {
      viewer {
        homes {
          currentSubscription {
            priceInfo(resolution: %s) {
              today { startsAt energy total currency }
              tomorrow { startsAt energy total currency }
            }
          }
        }
      }
    }
    """ % resolution
    auth = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    session = async_get_clientsession(hass)
    async with session.post(
        "https://api.tibber.com/v1-beta/gql",
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json={"query": query},
        timeout=30,
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    errors = payload.get("errors") or []
    if errors:
        message = "; ".join(str(item.get("message") or item) for item in errors)
        raise ValueError(f"tibber_api_error:{message}")

    homes = (((payload or {}).get("data") or {}).get("viewer") or {}).get("homes") or []
    if not homes:
        raise ValueError("no_tibber_homes")
    if home_index < 0 or home_index >= len(homes):
        home_index = 0
    price_info = ((((homes[home_index] or {}).get("currentSubscription") or {}).get("priceInfo")) or {})
    rows = [
        {
            "start_time": row.get("startsAt"),
            "market_price_per_kwh": row.get("energy"),
            "provider_final_price_per_kwh": row.get("total"),
        }
        for row in (price_info.get("today") or []) + (price_info.get("tomorrow") or [])
        if row.get("startsAt") is not None and row.get("energy") is not None
    ]
    return rows


async def _fetch_smard(hass: HomeAssistant, source: dict) -> list[dict[str, Any]]:
    market_area = _normalize_market_area(
        source.get("market_area"),
        supported=_SMARD_MARKET_AREA_MAP,
        aliases=_SMARD_MARKET_AREA_ALIASES,
    )
    duration_minutes = _requested_duration_minutes(source)
    if duration_minutes not in PROVIDER_NATIVE_DURATIONS["smard"]:
        raise ValueError(f"unsupported_duration:{duration_minutes}")
    resolution = "hour" if duration_minutes == 60 else "quarterhour"
    market_filter = _SMARD_MARKET_AREA_MAP[market_area]
    session = async_get_clientsession(hass)

    index_url = f"https://www.smard.de/app/chart_data/{market_filter}/{market_area}/index_{resolution}.json"
    async with session.get(index_url, timeout=30) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    timestamps = list((payload or {}).get("timestamps") or [])[-2:]
    rows: list[dict[str, Any]] = []
    for timestamp in timestamps:
        data_url = f"https://www.smard.de/app/chart_data/{market_filter}/{market_area}/{market_filter}_{market_area}_{resolution}_{timestamp}.json"
        async with session.get(data_url, timeout=30) as resp:
            resp.raise_for_status()
            data_payload = await resp.json()
        for item in data_payload.get("series") or []:
            if not isinstance(item, list) or len(item) < 2 or item[1] is None:
                continue
            start_time = datetime.fromtimestamp(float(item[0]) / 1000.0, tz=timezone.utc)
            rows.append({
                "start_time": format_iso(start_time),
                "market_price_per_kwh": float(item[1]) / 1000.0,
            })

    rows.sort(key=lambda item: item["start_time"])
    return rows


async def _fetch_energy_charts(hass: HomeAssistant, source: dict) -> list[dict[str, Any]]:
    market_area = _normalize_market_area(
        source.get("market_area"),
        supported=_ENERGY_CHARTS_BIDDING_ZONES,
        aliases=_ENERGY_CHARTS_ALIASES,
    )
    requested_duration = _requested_duration_minutes(source)
    if not provider_supports_billing("energy_charts", requested_duration):
        raise ValueError(f"unsupported_duration:{requested_duration}")
    tz = ZoneInfo(hass.config.time_zone)
    local_today = datetime.now(tz).date()
    session = async_get_clientsession(hass)
    params = {
        "bzn": market_area,
        "start": local_today.isoformat(),
        "end": (local_today + timedelta(days=1)).isoformat(),
    }
    async with session.get("https://api.energy-charts.info/price", params=params, timeout=30) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    timestamps = payload.get("unix_seconds") or []
    prices = payload.get("price") or []
    if len(timestamps) != len(prices):
        raise ValueError("invalid_energy_charts_payload")

    rows = []
    for ts, price in zip(timestamps, prices, strict=False):
        if price is None:
            continue
        start_time = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        rows.append({
            "start_time": format_iso(start_time),
            "market_price_per_kwh": float(price) / 1000.0,
        })

    if requested_duration == 60:
        return _aggregate_15_to_60_rows(rows)
    return rows


async def _fetch_entsoe(hass: HomeAssistant, source: dict) -> list[dict[str, Any]]:
    token = str(source.get("token") or "").strip()
    if not token:
        raise ValueError("missing_token")
    market_area = _normalize_market_area(
        source.get("market_area"),
        supported=_ENTSOE_MARKET_AREA_MAP,
        aliases=_ENTSOE_MARKET_AREA_ALIASES,
    )
    requested_duration = _requested_duration_minutes(source)
    if not provider_supports_billing("entsoe", requested_duration):
        raise ValueError(f"unsupported_duration:{requested_duration}")
    area_code = _ENTSOE_MARKET_AREA_MAP[market_area]
    tz = ZoneInfo(hass.config.time_zone)
    local_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=2)
    utc_start = local_start.astimezone(timezone.utc)
    utc_end = local_end.astimezone(timezone.utc)

    params = {
        "securityToken": token,
        "documentType": _EntsoeDocumentType.DAY_AHEAD_PRICES.value,
        "in_Domain": area_code,
        "out_Domain": area_code,
        "contract_MarketAgreement.Type": _EntsoeAgreementType.DAY_AHEAD.value,
        "periodStart": utc_start.strftime("%Y%m%d%H%M"),
        "periodEnd": utc_end.strftime("%Y%m%d%H%M"),
    }
    session = async_get_clientsession(hass)
    async with session.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=30) as resp:
        resp.raise_for_status()
        xml_text = await resp.text()

    root = ET.fromstring(xml_text)
    reason_text = root.findtext(".//Reason/text")
    if reason_text:
        raise ValueError(f"entsoe_api_error:{reason_text}")

    rows: list[dict[str, Any]] = []
    requested_resolution = "PT15M" if requested_duration == 15 else "PT60M"
    quarter_rows: list[dict[str, Any]] = []
    for period in root.findall(".//ns:TimeSeries/ns:Period", _ENTSOE_XML_NS):
        period_start_raw = period.findtext("ns:timeInterval/ns:start", namespaces=_ENTSOE_XML_NS)
        resolution_raw = period.findtext("ns:resolution", namespaces=_ENTSOE_XML_NS)
        period_start = parse_iso_aware(period_start_raw)
        if period_start is None or resolution_raw is None:
            continue
        if resolution_raw not in {"PT15M", "PT60M"}:
            continue
        source_minutes = _slot_minutes_from_resolution(resolution_raw)

        for point in period.findall("ns:Point", _ENTSOE_XML_NS):
            position_raw = point.findtext("ns:position", namespaces=_ENTSOE_XML_NS)
            amount_raw = point.findtext("ns:price.amount", namespaces=_ENTSOE_XML_NS)
            try:
                position = int(position_raw or "")
                amount = float(amount_raw or "")
            except (TypeError, ValueError):
                continue
            start_time = period_start + timedelta(minutes=source_minutes * (position - 1))
            entry = {
                "start_time": format_iso(start_time),
                "market_price_per_kwh": amount / 1000.0,
            }
            if resolution_raw == requested_resolution:
                rows.append(entry)
            elif requested_duration == 60 and resolution_raw == "PT15M":
                quarter_rows.append(entry)

    if not rows:
        if requested_duration == 60 and quarter_rows:
            rows = _aggregate_15_to_60_rows(quarter_rows)
        if not rows:
            raise ValueError("no_entsoe_rows")

    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        deduped[row["start_time"]] = row
    return [deduped[key] for key in sorted(deduped.keys())]


async def fetch_from_source(
    hass: HomeAssistant,
    source: dict,
) -> tuple[list[SlotRecord], SourceAttempt]:
    source_id = str(source.get("id", "unknown"))
    source_type = str(source.get("type", "unknown"))

    try:
        if source_type == "tibber":
            raw = await _fetch_tibber_direct(hass, source)
        elif source_type == "smard":
            raw = await _fetch_smard(hass, source)
        elif source_type == "energy_charts":
            raw = await _fetch_energy_charts(hass, source)
        elif source_type == "entsoe":
            raw = await _fetch_entsoe(hass, source)
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
