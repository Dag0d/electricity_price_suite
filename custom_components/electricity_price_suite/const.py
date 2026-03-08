"""Constants for electricity_price_suite."""

from __future__ import annotations

DOMAIN = "electricity_price_suite"
PLATFORMS = ["sensor"]

CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_TIMELINE = "timeline"
ENTRY_TYPE_PROFILE_LOGGER = "profile_logger"

CONF_TIMELINE_NAME = "timeline_name"
CONF_CURRENCY = "currency"
CONF_CACHE_RETENTION_DAYS = "cache_retention_days"
CONF_ROUND_DECIMALS = "round_decimals"
CONF_ENABLE_CURRENT_PRICE_SENSOR = "enable_current_price_sensor"
CONF_SOURCE_CHAIN = "source_chain"
CONF_NAME = "name"
CONF_SLUG = "slug"
CONF_ENERGY_ENTITY = "energy_entity"
CONF_SLOT_MINUTES = "slot_minutes"
CONF_MAX_POWER_KW = "max_power_kw"
CONF_AUTO_CREATE_PROGRAMS = "auto_create_programs"
CONF_ALLOWED_PROGRAMS = "allowed_programs"
CONF_BLOCKED_PROGRAMS = "blocked_programs"

DEFAULT_CURRENCY = "EUR"
DEFAULT_BILLING_SLOT_MINUTES = 15
DEFAULT_CACHE_RETENTION_DAYS = 7
DEFAULT_ROUND_DECIMALS = 4
DEFAULT_ENABLE_CURRENT_PRICE_SENSOR = True
DEFAULT_MAX_EXTRA_COST_PERCENT = 1.0
DEFAULT_PREFER_EARLIEST = True
DEFAULT_SOURCE_CHAIN = []
DEFAULT_SLOT_MINUTES = 15
DEFAULT_MAX_POWER_KW = 3.6
DEFAULT_AUTO_CREATE_PROGRAMS = True

SERVICE_REFRESH_TIMELINE = "refresh_timeline"
SERVICE_INJECT_SLOTS = "inject_slots"
SERVICE_OPTIMIZE_DEVICE = "optimize_device"
SERVICE_MANAGE_PLAN = "manage_plan"
SERVICE_REOPTIMIZE_PLAN = "reoptimize_plan"
SERVICE_START_PROFILE_LOGGING = "start_profile_logging"
SERVICE_FINISH_PROFILE_LOGGING = "finish_profile_logging"
SERVICE_ABORT_PROFILE_LOGGING = "abort_profile_logging"
SERVICE_GET_CONSUMPTION_PROFILE = "get_consumption_profile"
SERVICE_RESET_CONSUMPTION_PROFILE = "reset_consumption_profile"
SERVICE_DELETE_CONSUMPTION_PROFILE = "delete_consumption_profile"
SERVICE_ADD_SOURCE = "add_source"
SERVICE_LIST_SOURCES = "list_sources"
SERVICE_DELETE_SOURCE = "delete_source"

ATTR_SLOTS = "slots"
ATTR_START_TIME = "start_time"
ATTR_PRICE_PER_KWH = "price_per_kwh"

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}_timeline_"
LOGGER_STORAGE_VERSION = 1
LOGGER_STORAGE_KEY_PREFIX = f"{DOMAIN}_profile_logger_"

ABORT_REASON_PROGRAM_ABORTED = "program_aborted"
ABORT_REASON_PROGRAM_MISMATCH = "program_mismatch"
ABORT_REASON_MANUAL = "manual_abort"
ABORT_REASON_RESTART_RECOVERY = "restart_recovery"
ABORT_REASON_DELAY_EXCEEDED = "sampling_delay_exceeded"

ALLOWED_ABORT_REASONS = {
    ABORT_REASON_PROGRAM_ABORTED,
    ABORT_REASON_PROGRAM_MISMATCH,
    ABORT_REASON_MANUAL,
    ABORT_REASON_RESTART_RECOVERY,
    ABORT_REASON_DELAY_EXCEEDED,
}

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_ERROR = "error"

ERROR_ABORTED = "aborted"
ERROR_ALREADY_RUNNING = "start_while_running"
ERROR_DELAY_EXCEEDED = "sampling_delay_exceeded"
ERROR_ENERGY_COUNTER_DECREASED = "energy_counter_decreased"
ERROR_ENERGY_ENTITY_INVALID = "energy_entity_invalid"
ERROR_ENERGY_STATE_CLASS_INVALID = "energy_state_class_invalid"
ERROR_ENERGY_UNAVAILABLE = "energy_unavailable"
ERROR_MAX_DELTA_EXCEEDED = "max_delta_exceeded"
ERROR_PROGRAM_BLOCKED = "program_blocked"
ERROR_PROGRAM_MISMATCH_FINISH = "program_mismatch_finish"
ERROR_PROGRAM_MISSING = "program_missing"
ERROR_PROFILE_NOT_FOUND = "profile_not_found"
ERROR_NOT_RUNNING = "not_running"

TOLERANCE_RATIO = 0.03
MIN_DELAY_FLOOR_SEC = 3.0
SLOT_UNUSED_TRIM_RUNS = 10

LOGGER_NAME = DOMAIN
