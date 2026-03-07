"""Constants for electricity_price_suite."""

from __future__ import annotations

DOMAIN = "electricity_price_suite"
PLATFORMS = ["sensor"]

CONF_TIMELINE_NAME = "timeline_name"
CONF_CURRENCY = "currency"
CONF_CACHE_RETENTION_DAYS = "cache_retention_days"
CONF_ROUND_DECIMALS = "round_decimals"
CONF_ENABLE_CURRENT_PRICE_SENSOR = "enable_current_price_sensor"
CONF_SOURCE_CHAIN = "source_chain"

DEFAULT_CURRENCY = "EUR"
DEFAULT_BILLING_SLOT_MINUTES = 15
DEFAULT_CACHE_RETENTION_DAYS = 7
DEFAULT_ROUND_DECIMALS = 4
DEFAULT_ENABLE_CURRENT_PRICE_SENSOR = True
DEFAULT_MAX_EXTRA_COST_PERCENT = 1.0
DEFAULT_PREFER_EARLIEST = True
DEFAULT_SOURCE_CHAIN = []

SERVICE_REFRESH_TIMELINE = "refresh_timeline"
SERVICE_INJECT_SLOTS = "inject_slots"
SERVICE_OPTIMIZE_DEVICE = "optimize_device"
SERVICE_MANAGE_PLAN = "manage_plan"
SERVICE_ADD_SOURCE = "add_source"
SERVICE_LIST_SOURCES = "list_sources"
SERVICE_DELETE_SOURCE = "delete_source"

ATTR_SLOTS = "slots"
ATTR_START_TIME = "start_time"
ATTR_PRICE_PER_KWH = "price_per_kwh"

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}_timeline_"

LOGGER_NAME = DOMAIN
