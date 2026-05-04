"""Config flow for electricity_price_suite."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_ALLOWED_PROGRAMS,
    CONF_AVG_PRICE_INCLUDE_BASIC_FEE,
    CONF_AUTO_CREATE_PROGRAMS,
    CONF_BILLING_SLOT_MINUTES,
    CONF_BLOCKED_PROGRAMS,
    CONF_CACHE_RETENTION_DAYS,
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_CURRENT_MONTH_FIXED_FEE_MODE,
    CONF_ENABLE_CONSUMPTION_TRACKING,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_SURCHARGE_ABSOLUTE,
    CONF_ENERGY_SURCHARGE_PERCENT,
    CONF_ENERGY_TAX_PERCENT,
    CONF_USE_TIBBER_PROVIDER_FINAL_PRICE,
    CONF_ENTRY_TYPE,
    CONF_FIXED_FEE_DAILY_AMOUNT,
    CONF_FIXED_FEE_MONTHLY_AMOUNT,
    CONF_FIXED_FEE_TAX_PERCENT,
    CONF_FIXED_FEE_VALUES_INCLUDE_TAX,
    CONF_MAX_POWER_KW,
    CONF_NAME,
    CONF_PLANNER_DEVICES,
    CONF_ROUND_DECIMALS,
    CONF_SLUG,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_CHAIN,
    CONF_TIMELINE_NAME,
    DEFAULT_AUTO_CREATE_PROGRAMS,
    DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE,
    DEFAULT_ENABLE_CONSUMPTION_TRACKING,
    DEFAULT_ENERGY_SURCHARGE_ABSOLUTE,
    DEFAULT_ENERGY_SURCHARGE_PERCENT,
    DEFAULT_ENERGY_TAX_PERCENT,
    DEFAULT_USE_TIBBER_PROVIDER_FINAL_PRICE,
    DEFAULT_CURRENT_MONTH_FIXED_FEE_MODE,
    DEFAULT_FIXED_FEE_DAILY_AMOUNT,
    DEFAULT_FIXED_FEE_MONTHLY_AMOUNT,
    DEFAULT_FIXED_FEE_TAX_PERCENT,
    DEFAULT_FIXED_FEE_VALUES_INCLUDE_TAX,
    DEFAULT_BILLING_SLOT_MINUTES,
    DEFAULT_CACHE_RETENTION_DAYS,
    DEFAULT_MAX_POWER_KW,
    DEFAULT_PLANNER_DEVICES,
    DEFAULT_ROUND_DECIMALS,
    DEFAULT_SLOT_MINUTES,
    DOMAIN,
    ENTRY_TYPE_PROFILE_LOGGER,
    ENTRY_TYPE_TIMELINE,
)
from .providers import PROVIDER_LABELS, provider_market_areas, provider_supports_billing
from .validation import parse_program_list, validate_energy_entity

_LOGGER = logging.getLogger(__name__)

CONF_PROVIDER_COUNT = "provider_count"
CONF_PROVIDER_TYPE = "provider_type"
CONF_MULTIPLE_HOMES = "multiple_homes"

PROVIDER_ORDER = ["tibber", "smard", "energy_charts", "entsoe"]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_selector(*, min_value: int = 0, max_value: int | None = None) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=min_value, max=max_value, mode=selector.NumberSelectorMode.BOX, step=1)
    )


def _float_selector(*, min_value: float = 0, max_value: float | None = None, step: float = 0.1) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=min_value,
            max=max_value,
            mode=selector.NumberSelectorMode.BOX,
            step="any",
        )
    )


def _program_list_selector() -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(options=[], multiple=True, custom_value=True))


def _planner_list_selector() -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(options=[], multiple=True, custom_value=True))


def _text_selector() -> selector.TextSelector:
    return selector.TextSelector(selector.TextSelectorConfig(multiline=False))


def _energy_entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig())


def _current_month_fixed_fee_mode_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="prorated", label="Anteilig"),
                selector.SelectOptionDict(value="full", label="Voll"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _provider_count_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="1", label="1"),
                selector.SelectOptionDict(value="2", label="2"),
                selector.SelectOptionDict(value="3", label="3"),
                selector.SelectOptionDict(value="4", label="4"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _billing_slot_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="15", label="15 minutes"),
                selector.SelectOptionDict(value="60", label="60 minutes"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _provider_type_selector(billing_slot_minutes: int) -> selector.SelectSelector:
    options = [
        selector.SelectOptionDict(value=provider_type, label=PROVIDER_LABELS[provider_type])
        for provider_type in PROVIDER_ORDER
        if provider_supports_billing(provider_type, billing_slot_minutes)
    ]
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _market_area_selector(provider_type: str) -> selector.SelectSelector:
    options = [selector.SelectOptionDict(value=item, label=item) for item in provider_market_areas(provider_type)]
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def parse_name_list(values: list[str] | str | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    items: list[str]
    if isinstance(values, str):
        items = [part.strip() for part in values.replace("\n", ",").split(",")]
    else:
        items = list(values or [])
    for value in items:
        name = str(value).strip()
        if not name:
            continue
        slug = slugify(name)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        names.append(name)
    return names

class _TimelineProviderFlowMixin:
    def _init_provider_state(self) -> None:
        self._provider_count = 1
        self._provider_index = 0
        self._provider_chain: list[dict[str, Any]] = []
        self._pending_provider: dict[str, Any] = {}
        self._selected_provider_type: str | None = None

    def _billing_slot_minutes(self) -> int:
        try:
            return int(self._draft.get(CONF_BILLING_SLOT_MINUTES, DEFAULT_BILLING_SLOT_MINUTES))
        except (TypeError, ValueError):
            return DEFAULT_BILLING_SLOT_MINUTES

    def _provider_label(self) -> str:
        return str(self._provider_index + 1)

    def _source_id(self, provider_type: str) -> str:
        return f"source_{self._provider_index + 1}_{provider_type}"

    def _current_provider_source(self, provider_type: str | None = None) -> dict[str, Any] | None:
        if self._provider_index < len(getattr(self, "_existing_sources", [])):
            source = self._existing_sources[self._provider_index]
            if provider_type is None or source.get("type") == provider_type:
                return source
        return None

    def _append_provider(self, provider_type: str, extra: dict[str, Any] | None = None) -> None:
        source = {
            "id": self._source_id(provider_type),
            "type": provider_type,
            "priority": self._provider_index,
            "enabled": True,
            "duration_minutes": self._billing_slot_minutes(),
        }
        if extra:
            source.update(extra)
        self._provider_chain.append(source)

    def _provider_defaults(self, provider_type: str) -> dict[str, Any]:
        source = self._current_provider_source(provider_type) or {}
        return {
            "market_area": source.get("market_area", "DE-LU"),
            "token": source.get("token", ""),
            "home_index": int(source.get("home_index", 0)),
            CONF_MULTIPLE_HOMES: int(source.get("home_index", 0)) > 0,
            CONF_USE_TIBBER_PROVIDER_FINAL_PRICE: _as_bool(
                self._draft.get(
                    CONF_USE_TIBBER_PROVIDER_FINAL_PRICE,
                    DEFAULT_USE_TIBBER_PROVIDER_FINAL_PRICE,
                )
            ),
        }

    def _provider_count_default(self) -> str:
        if getattr(self, "_existing_sources", None):
            return str(max(1, min(4, len(self._existing_sources))))
        return "1"

    def _available_provider_types(self) -> list[str]:
        billing = self._billing_slot_minutes()
        return [provider_type for provider_type in PROVIDER_ORDER if provider_supports_billing(provider_type, billing)]

    def _next_provider_step(self):
        self._provider_index += 1
        self._selected_provider_type = None
        self._pending_provider = {}
        if self._provider_index < self._provider_count:
            _LOGGER.debug(
                "provider flow continuing to provider_type: index=%s count=%s chain=%s",
                self._provider_index,
                self._provider_count,
                self._provider_chain,
            )
            return self.async_step_provider_type()
        _LOGGER.debug(
            "provider flow continuing to timeline_energy_pricing: count=%s chain=%s",
            self._provider_count,
            self._provider_chain,
        )
        return self.async_step_timeline_energy_pricing()

    async def async_step_provider_count(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._provider_count = int(user_input[CONF_PROVIDER_COUNT])
            self._provider_index = 0
            self._provider_chain = []
            self._pending_provider = {}
            self._selected_provider_type = None
            return await self.async_step_provider_type()
        schema = vol.Schema({
            vol.Required(CONF_PROVIDER_COUNT, default=self._provider_count_default()): _provider_count_selector(),
        })
        return self.async_show_form(step_id="provider_count", data_schema=schema, errors={})

    async def async_step_provider_type(self, user_input: dict[str, Any] | None = None):
        available = self._available_provider_types()
        if user_input is not None:
            provider_type = str(user_input[CONF_PROVIDER_TYPE])
            self._selected_provider_type = provider_type
            if provider_type == "tibber":
                return await self.async_step_provider_tibber()
            if provider_type == "smard":
                return await self.async_step_provider_smard()
            if provider_type == "energy_charts":
                return await self.async_step_provider_energy_charts()
            return await self.async_step_provider_entsoe()
        default_type = None
        existing = self._current_provider_source()
        if existing and existing.get("type") in available:
            default_type = existing["type"]
        if default_type is None:
            default_type = available[0]
        schema = vol.Schema({
            vol.Required(CONF_PROVIDER_TYPE, default=default_type): _provider_type_selector(self._billing_slot_minutes()),
        })
        return self.async_show_form(step_id="provider_type", data_schema=schema, errors={})

    async def async_step_provider_tibber(self, user_input: dict[str, Any] | None = None):
        try:
            errors: dict[str, str] = {}
            defaults = self._provider_defaults("tibber")
            if user_input is not None:
                token = str(user_input.get("token", "") or "").strip()
                self._draft[CONF_USE_TIBBER_PROVIDER_FINAL_PRICE] = _as_bool(
                    user_input.get(
                        CONF_USE_TIBBER_PROVIDER_FINAL_PRICE,
                        defaults[CONF_USE_TIBBER_PROVIDER_FINAL_PRICE],
                    )
                )
                if not token:
                    errors["token"] = "required"
                else:
                    if _as_bool(user_input.get(CONF_MULTIPLE_HOMES, False)):
                        self._pending_provider = {"token": token}
                        return await self.async_step_provider_tibber_home()
                    self._append_provider("tibber", {"token": token, "home_index": 0})
                    return await self._next_provider_step()
            schema = vol.Schema({
                vol.Required("token", default=defaults["token"]): str,
                vol.Required(CONF_MULTIPLE_HOMES, default=defaults[CONF_MULTIPLE_HOMES]): bool,
                vol.Required(
                    CONF_USE_TIBBER_PROVIDER_FINAL_PRICE,
                    default=defaults[CONF_USE_TIBBER_PROVIDER_FINAL_PRICE],
                ): bool,
            })
            return self.async_show_form(step_id="provider_tibber", data_schema=schema, errors=errors)
        except Exception:
            _LOGGER.exception(
                "provider_tibber step failed: user_input=%s chain=%s index=%s count=%s",
                user_input,
                self._provider_chain,
                self._provider_index,
                self._provider_count,
            )
            raise

    async def async_step_provider_tibber_home(self, user_input: dict[str, Any] | None = None):
        defaults = self._provider_defaults("tibber")
        if user_input is not None:
            self._append_provider(
                "tibber",
                {
                    "token": self._pending_provider["token"],
                    "home_index": int(user_input.get("home_index", 0)),
                },
            )
            return await self._next_provider_step()
        schema = vol.Schema({
            vol.Required("home_index", default=defaults["home_index"]): _int_selector(min_value=0, max_value=20),
        })
        return self.async_show_form(step_id="provider_tibber_home", data_schema=schema, errors={})

    async def async_step_provider_smard(self, user_input: dict[str, Any] | None = None):
        defaults = self._provider_defaults("smard")
        if user_input is not None:
            self._append_provider("smard", {"market_area": user_input["market_area"]})
            return await self._next_provider_step()
        schema = vol.Schema({
            vol.Required("market_area", default=defaults["market_area"]): _market_area_selector("smard"),
        })
        return self.async_show_form(step_id="provider_smard", data_schema=schema, errors={})

    async def async_step_provider_energy_charts(self, user_input: dict[str, Any] | None = None):
        defaults = self._provider_defaults("energy_charts")
        if user_input is not None:
            self._append_provider("energy_charts", {"market_area": user_input["market_area"]})
            return await self._next_provider_step()
        schema = vol.Schema({
            vol.Required("market_area", default=defaults["market_area"]): _market_area_selector("energy_charts"),
        })
        return self.async_show_form(step_id="provider_energy_charts", data_schema=schema, errors={})

    async def async_step_provider_entsoe(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        defaults = self._provider_defaults("entsoe")
        if user_input is not None:
            token = str(user_input.get("token", "") or "").strip()
            if not token:
                errors["token"] = "required"
            else:
                self._append_provider(
                    "entsoe",
                    {
                        "market_area": user_input["market_area"],
                        "token": token,
                    },
                )
                return await self._next_provider_step()
        schema = vol.Schema({
            vol.Required("market_area", default=defaults["market_area"]): _market_area_selector("entsoe"),
            vol.Required("token", default=defaults["token"]): str,
        })
        return self.async_show_form(step_id="provider_entsoe", data_schema=schema, errors=errors)

    async def async_step_timeline_energy_pricing(self, user_input: dict[str, Any] | None = None):
        try:
            errors: dict[str, str] = {}
            defaults = self._energy_pricing_defaults()
            if user_input is not None:
                self._draft.update(
                    {
                        CONF_ENERGY_SURCHARGE_PERCENT: float(user_input[CONF_ENERGY_SURCHARGE_PERCENT]),
                        CONF_ENERGY_SURCHARGE_ABSOLUTE: float(user_input[CONF_ENERGY_SURCHARGE_ABSOLUTE]),
                        CONF_ENERGY_TAX_PERCENT: float(user_input[CONF_ENERGY_TAX_PERCENT]),
                    }
                )
                return await self.async_step_timeline_consumption()
            schema_dict: dict[Any, Any] = {
                vol.Required(CONF_ENERGY_SURCHARGE_PERCENT, default=defaults[CONF_ENERGY_SURCHARGE_PERCENT]): _float_selector(min_value=0, max_value=500, step=0.001),
                vol.Required(CONF_ENERGY_SURCHARGE_ABSOLUTE, default=defaults[CONF_ENERGY_SURCHARGE_ABSOLUTE]): _float_selector(min_value=0, max_value=5, step=0.0001),
                vol.Required(CONF_ENERGY_TAX_PERCENT, default=defaults[CONF_ENERGY_TAX_PERCENT]): _float_selector(min_value=0, max_value=100, step=0.001),
            }
            schema = vol.Schema(schema_dict)
            return self.async_show_form(step_id="timeline_energy_pricing", data_schema=schema, errors=errors)
        except Exception:
            _LOGGER.exception(
                "timeline_energy_pricing step failed: user_input=%s draft=%s provider_chain=%s",
                user_input,
                self._draft,
                self._provider_chain,
            )
            raise

    async def async_step_timeline_consumption(self, user_input: dict[str, Any] | None = None):
        try:
            defaults = self._consumption_defaults()
            if user_input is not None:
                enabled = _as_bool(user_input.get(CONF_ENABLE_CONSUMPTION_TRACKING, False))
                self._draft[CONF_ENABLE_CONSUMPTION_TRACKING] = enabled
                if enabled:
                    return await self.async_step_timeline_consumption_details()
                self._draft.update(
                    {
                        CONF_CONSUMPTION_ENERGY_ENTITY: "",
                        CONF_FIXED_FEE_MONTHLY_AMOUNT: DEFAULT_FIXED_FEE_MONTHLY_AMOUNT,
                        CONF_FIXED_FEE_DAILY_AMOUNT: DEFAULT_FIXED_FEE_DAILY_AMOUNT,
                        CONF_FIXED_FEE_TAX_PERCENT: DEFAULT_FIXED_FEE_TAX_PERCENT,
                        CONF_FIXED_FEE_VALUES_INCLUDE_TAX: DEFAULT_FIXED_FEE_VALUES_INCLUDE_TAX,
                        CONF_CURRENT_MONTH_FIXED_FEE_MODE: DEFAULT_CURRENT_MONTH_FIXED_FEE_MODE,
                        CONF_AVG_PRICE_INCLUDE_BASIC_FEE: DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE,
                    }
                )
                return await self.async_step_timeline_planner()
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLE_CONSUMPTION_TRACKING,
                        default=defaults[CONF_ENABLE_CONSUMPTION_TRACKING],
                    ): bool,
                }
            )
            return self.async_show_form(step_id="timeline_consumption", data_schema=schema, errors={})
        except Exception:
            _LOGGER.exception(
                "timeline_consumption step failed: user_input=%s draft=%s provider_chain=%s",
                user_input,
                self._draft,
                self._provider_chain,
            )
            raise

    async def async_step_timeline_consumption_details(self, user_input: dict[str, Any] | None = None):
        try:
            errors: dict[str, str] = {}
            defaults = self._consumption_defaults()
            if user_input is not None:
                consumption_energy_entity = str(user_input.get(CONF_CONSUMPTION_ENERGY_ENTITY, "") or "").strip()
                if not consumption_energy_entity:
                    errors[CONF_CONSUMPTION_ENERGY_ENTITY] = "required"
                elif (energy_error := validate_energy_entity(self.hass, consumption_energy_entity)) is not None:
                    errors[CONF_CONSUMPTION_ENERGY_ENTITY] = energy_error
                else:
                    self._draft.update(
                        {
                            CONF_ENABLE_CONSUMPTION_TRACKING: True,
                            CONF_CONSUMPTION_ENERGY_ENTITY: consumption_energy_entity,
                            CONF_FIXED_FEE_MONTHLY_AMOUNT: float(
                                user_input.get(CONF_FIXED_FEE_MONTHLY_AMOUNT, DEFAULT_FIXED_FEE_MONTHLY_AMOUNT)
                            ),
                            CONF_FIXED_FEE_DAILY_AMOUNT: float(
                                user_input.get(CONF_FIXED_FEE_DAILY_AMOUNT, DEFAULT_FIXED_FEE_DAILY_AMOUNT)
                            ),
                            CONF_FIXED_FEE_TAX_PERCENT: float(
                                user_input.get(CONF_FIXED_FEE_TAX_PERCENT, DEFAULT_FIXED_FEE_TAX_PERCENT)
                            ),
                            CONF_FIXED_FEE_VALUES_INCLUDE_TAX: _as_bool(
                                user_input.get(CONF_FIXED_FEE_VALUES_INCLUDE_TAX, DEFAULT_FIXED_FEE_VALUES_INCLUDE_TAX)
                            ),
                            CONF_CURRENT_MONTH_FIXED_FEE_MODE: str(
                                user_input.get(
                                    CONF_CURRENT_MONTH_FIXED_FEE_MODE,
                                    DEFAULT_CURRENT_MONTH_FIXED_FEE_MODE,
                                )
                            ),
                            CONF_AVG_PRICE_INCLUDE_BASIC_FEE: _as_bool(
                                user_input.get(CONF_AVG_PRICE_INCLUDE_BASIC_FEE, DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE)
                            ),
                        }
                    )
                    return await self.async_step_timeline_planner()
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_CONSUMPTION_ENERGY_ENTITY,
                        default=defaults[CONF_CONSUMPTION_ENERGY_ENTITY] or "",
                    ): _energy_entity_selector(),
                    vol.Required(
                        CONF_FIXED_FEE_MONTHLY_AMOUNT,
                        default=defaults[CONF_FIXED_FEE_MONTHLY_AMOUNT],
                    ): _float_selector(min_value=0, max_value=1000, step=0.01),
                    vol.Required(
                        CONF_FIXED_FEE_DAILY_AMOUNT,
                        default=defaults[CONF_FIXED_FEE_DAILY_AMOUNT],
                    ): _float_selector(min_value=0, max_value=100, step=0.0001),
                    vol.Required(
                        CONF_FIXED_FEE_TAX_PERCENT,
                        default=defaults[CONF_FIXED_FEE_TAX_PERCENT],
                    ): _float_selector(min_value=0, max_value=100, step=0.001),
                    vol.Required(
                        CONF_FIXED_FEE_VALUES_INCLUDE_TAX,
                        default=defaults[CONF_FIXED_FEE_VALUES_INCLUDE_TAX],
                    ): bool,
                    vol.Required(
                        CONF_CURRENT_MONTH_FIXED_FEE_MODE,
                        default=defaults[CONF_CURRENT_MONTH_FIXED_FEE_MODE],
                    ): _current_month_fixed_fee_mode_selector(),
                    vol.Required(CONF_AVG_PRICE_INCLUDE_BASIC_FEE, default=defaults[CONF_AVG_PRICE_INCLUDE_BASIC_FEE]): bool,
                }
            )
            return self.async_show_form(step_id="timeline_consumption_details", data_schema=schema, errors=errors)
        except Exception:
            _LOGGER.exception(
                "timeline_consumption_details step failed: user_input=%s draft=%s provider_chain=%s",
                user_input,
                self._draft,
                self._provider_chain,
            )
            raise

    async def async_step_timeline_planner(self, user_input: dict[str, Any] | None = None):
        defaults = self._planner_defaults()
        if user_input is not None:
            try:
                self._draft[CONF_PLANNER_DEVICES] = parse_name_list(user_input.get(CONF_PLANNER_DEVICES, []))
                return self._finish_timeline_entry()
            except Exception:
                _LOGGER.exception(
                    "timeline_planner step failed: user_input=%s draft=%s provider_chain=%s",
                    user_input,
                    self._draft,
                    self._provider_chain,
                )
                raise
        schema = vol.Schema({
            vol.Optional(CONF_PLANNER_DEVICES, default=defaults): _planner_list_selector(),
        })
        return self.async_show_form(step_id="timeline_planner", data_schema=schema, errors={})


class ElectricityPriceSuiteConfigFlow(_TimelineProviderFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._draft: dict[str, Any] = {}
        self._existing_sources: list[dict[str, Any]] = []
        self._init_provider_state()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._draft = {CONF_ENTRY_TYPE: user_input[CONF_ENTRY_TYPE]}
            if user_input[CONF_ENTRY_TYPE] == ENTRY_TYPE_PROFILE_LOGGER:
                return await self.async_step_profile_logger()
            return await self.async_step_timeline_core()
        schema = vol.Schema({
            vol.Required(CONF_ENTRY_TYPE, default=ENTRY_TYPE_TIMELINE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=ENTRY_TYPE_TIMELINE, label="Price Timeline"),
                        selector.SelectOptionDict(value=ENTRY_TYPE_PROFILE_LOGGER, label="Consumption Profile Logger"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors={})

    async def async_step_timeline_core(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            title = str(user_input[CONF_TIMELINE_NAME]).strip()
            if not title:
                errors[CONF_TIMELINE_NAME] = "required"
            else:
                await self.async_set_unique_id(f"{ENTRY_TYPE_TIMELINE}_{slugify(title)}")
                self._abort_if_unique_id_configured()
                self._draft.update(
                    {
                        CONF_TIMELINE_NAME: title,
                        CONF_BILLING_SLOT_MINUTES: int(user_input[CONF_BILLING_SLOT_MINUTES]),
                        CONF_CACHE_RETENTION_DAYS: int(user_input[CONF_CACHE_RETENTION_DAYS]),
                        CONF_ROUND_DECIMALS: int(user_input[CONF_ROUND_DECIMALS]),
                    }
                )
                self._init_provider_state()
                return await self.async_step_provider_count()
        schema = vol.Schema({
            vol.Required(CONF_TIMELINE_NAME): str,
            vol.Required(CONF_BILLING_SLOT_MINUTES, default=str(DEFAULT_BILLING_SLOT_MINUTES)): _billing_slot_selector(),
            vol.Required(CONF_CACHE_RETENTION_DAYS, default=DEFAULT_CACHE_RETENTION_DAYS): _int_selector(min_value=1, max_value=365),
            vol.Required(CONF_ROUND_DECIMALS, default=DEFAULT_ROUND_DECIMALS): _int_selector(min_value=0, max_value=8),
        })
        return self.async_show_form(step_id="timeline_core", data_schema=schema, errors=errors)

    def _energy_pricing_defaults(self) -> dict[str, Any]:
        return {
            CONF_ENERGY_SURCHARGE_PERCENT: DEFAULT_ENERGY_SURCHARGE_PERCENT,
            CONF_ENERGY_SURCHARGE_ABSOLUTE: DEFAULT_ENERGY_SURCHARGE_ABSOLUTE,
            CONF_ENERGY_TAX_PERCENT: DEFAULT_ENERGY_TAX_PERCENT,
            CONF_USE_TIBBER_PROVIDER_FINAL_PRICE: DEFAULT_USE_TIBBER_PROVIDER_FINAL_PRICE,
        }

    def _consumption_defaults(self) -> dict[str, Any]:
        return {
            CONF_ENABLE_CONSUMPTION_TRACKING: _as_bool(
                self._draft.get(CONF_ENABLE_CONSUMPTION_TRACKING, DEFAULT_ENABLE_CONSUMPTION_TRACKING)
            ),
            CONF_CONSUMPTION_ENERGY_ENTITY: self._draft.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""),
            CONF_FIXED_FEE_MONTHLY_AMOUNT: float(
                self._draft.get(CONF_FIXED_FEE_MONTHLY_AMOUNT, DEFAULT_FIXED_FEE_MONTHLY_AMOUNT)
            ),
            CONF_FIXED_FEE_DAILY_AMOUNT: float(
                self._draft.get(CONF_FIXED_FEE_DAILY_AMOUNT, DEFAULT_FIXED_FEE_DAILY_AMOUNT)
            ),
            CONF_FIXED_FEE_TAX_PERCENT: float(
                self._draft.get(CONF_FIXED_FEE_TAX_PERCENT, DEFAULT_FIXED_FEE_TAX_PERCENT)
            ),
            CONF_FIXED_FEE_VALUES_INCLUDE_TAX: _as_bool(
                self._draft.get(CONF_FIXED_FEE_VALUES_INCLUDE_TAX, DEFAULT_FIXED_FEE_VALUES_INCLUDE_TAX)
            ),
            CONF_CURRENT_MONTH_FIXED_FEE_MODE: str(
                self._draft.get(CONF_CURRENT_MONTH_FIXED_FEE_MODE, DEFAULT_CURRENT_MONTH_FIXED_FEE_MODE)
            ),
            CONF_AVG_PRICE_INCLUDE_BASIC_FEE: _as_bool(
                self._draft.get(CONF_AVG_PRICE_INCLUDE_BASIC_FEE, DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE)
            ),
        }

    def _planner_defaults(self) -> list[str]:
        return DEFAULT_PLANNER_DEVICES

    def _finish_timeline_entry(self):
        title = str(self._draft[CONF_TIMELINE_NAME])
        data = {
            CONF_ENTRY_TYPE: ENTRY_TYPE_TIMELINE,
            CONF_TIMELINE_NAME: title,
            CONF_BILLING_SLOT_MINUTES: int(self._draft[CONF_BILLING_SLOT_MINUTES]),
            CONF_CACHE_RETENTION_DAYS: int(self._draft[CONF_CACHE_RETENTION_DAYS]),
            CONF_ROUND_DECIMALS: int(self._draft[CONF_ROUND_DECIMALS]),
            CONF_SOURCE_CHAIN: self._provider_chain,
            CONF_ENABLE_CONSUMPTION_TRACKING: bool(
                self._draft.get(CONF_ENABLE_CONSUMPTION_TRACKING, DEFAULT_ENABLE_CONSUMPTION_TRACKING)
            ),
            CONF_CONSUMPTION_ENERGY_ENTITY: self._draft.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""),
            CONF_ENERGY_SURCHARGE_PERCENT: float(
                self._draft.get(CONF_ENERGY_SURCHARGE_PERCENT, DEFAULT_ENERGY_SURCHARGE_PERCENT)
            ),
            CONF_ENERGY_SURCHARGE_ABSOLUTE: float(
                self._draft.get(CONF_ENERGY_SURCHARGE_ABSOLUTE, DEFAULT_ENERGY_SURCHARGE_ABSOLUTE)
            ),
            CONF_ENERGY_TAX_PERCENT: float(
                self._draft.get(CONF_ENERGY_TAX_PERCENT, DEFAULT_ENERGY_TAX_PERCENT)
            ),
            CONF_FIXED_FEE_MONTHLY_AMOUNT: float(
                self._draft.get(CONF_FIXED_FEE_MONTHLY_AMOUNT, DEFAULT_FIXED_FEE_MONTHLY_AMOUNT)
            ),
            CONF_FIXED_FEE_DAILY_AMOUNT: float(
                self._draft.get(CONF_FIXED_FEE_DAILY_AMOUNT, DEFAULT_FIXED_FEE_DAILY_AMOUNT)
            ),
            CONF_FIXED_FEE_TAX_PERCENT: float(
                self._draft.get(CONF_FIXED_FEE_TAX_PERCENT, DEFAULT_FIXED_FEE_TAX_PERCENT)
            ),
            CONF_FIXED_FEE_VALUES_INCLUDE_TAX: bool(
                _as_bool(
                    self._draft.get(CONF_FIXED_FEE_VALUES_INCLUDE_TAX, DEFAULT_FIXED_FEE_VALUES_INCLUDE_TAX)
                )
            ),
            CONF_CURRENT_MONTH_FIXED_FEE_MODE: str(
                self._draft.get(CONF_CURRENT_MONTH_FIXED_FEE_MODE, DEFAULT_CURRENT_MONTH_FIXED_FEE_MODE)
            ),
            CONF_AVG_PRICE_INCLUDE_BASIC_FEE: bool(
                _as_bool(self._draft.get(CONF_AVG_PRICE_INCLUDE_BASIC_FEE, DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE))
            ),
            CONF_PLANNER_DEVICES: self._draft.get(CONF_PLANNER_DEVICES, DEFAULT_PLANNER_DEVICES),
        }
        _LOGGER.debug(
            "creating timeline entry: title=%s data=%s provider_chain=%s",
            title,
            data,
            self._provider_chain,
        )
        return self.async_create_entry(title=title, data=data)

    async def async_step_profile_logger(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            title = user_input[CONF_NAME].strip()
            energy_entity = user_input[CONF_ENERGY_ENTITY].strip()
            slot_minutes = int(user_input[CONF_SLOT_MINUTES])
            max_power_kw = float(user_input[CONF_MAX_POWER_KW])
            if not title:
                errors[CONF_NAME] = "required"
            elif not energy_entity:
                errors[CONF_ENERGY_ENTITY] = "required"
            elif slot_minutes <= 0:
                errors[CONF_SLOT_MINUTES] = "invalid"
            elif max_power_kw <= 0:
                errors[CONF_MAX_POWER_KW] = "invalid"
            elif (energy_error := validate_energy_entity(self.hass, energy_entity)) is not None:
                errors[CONF_ENERGY_ENTITY] = energy_error
            else:
                slug = slugify(title)
                await self.async_set_unique_id(f"{ENTRY_TYPE_PROFILE_LOGGER}_{slug}")
                self._abort_if_unique_id_configured()
                data = {
                    CONF_ENTRY_TYPE: ENTRY_TYPE_PROFILE_LOGGER,
                    CONF_NAME: title,
                    CONF_ENERGY_ENTITY: energy_entity,
                    CONF_SLOT_MINUTES: slot_minutes,
                    CONF_MAX_POWER_KW: max_power_kw,
                    CONF_AUTO_CREATE_PROGRAMS: _as_bool(user_input[CONF_AUTO_CREATE_PROGRAMS]),
                    CONF_ALLOWED_PROGRAMS: parse_program_list(user_input.get(CONF_ALLOWED_PROGRAMS, [])),
                    CONF_BLOCKED_PROGRAMS: parse_program_list(user_input.get(CONF_BLOCKED_PROGRAMS, [])),
                    CONF_SLUG: slug,
                }
                return self.async_create_entry(title=title, data=data)
        schema = vol.Schema({
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_ENERGY_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig()),
            vol.Required(CONF_SLOT_MINUTES, default=DEFAULT_SLOT_MINUTES): _int_selector(min_value=1, max_value=120),
            vol.Required(CONF_MAX_POWER_KW, default=DEFAULT_MAX_POWER_KW): _float_selector(min_value=0.001, max_value=50, step=0.001),
            vol.Required(CONF_AUTO_CREATE_PROGRAMS, default=DEFAULT_AUTO_CREATE_PROGRAMS): bool,
            vol.Optional(CONF_ALLOWED_PROGRAMS, default=[]): _program_list_selector(),
            vol.Optional(CONF_BLOCKED_PROGRAMS, default=[]): _program_list_selector(),
        })
        return self.async_show_form(step_id="profile_logger", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ElectricityPriceSuiteOptionsFlow(config_entry)


class ElectricityPriceSuiteOptionsFlow(_TimelineProviderFlowMixin, config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self._config_entry = config_entry
        self._draft: dict[str, Any] = {}
        self._existing_sources: list[dict[str, Any]] = []
        self._init_provider_state()

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        entry_type = self._config_entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_TIMELINE)
        if entry_type == ENTRY_TYPE_PROFILE_LOGGER:
            return await self.async_step_profile_logger(user_input)
        current = {**self._config_entry.data, **self._config_entry.options}
        self._draft = dict(current)
        self._existing_sources = list(current.get(CONF_SOURCE_CHAIN, []))
        self._init_provider_state()
        return await self.async_step_timeline_core(user_input)

    async def async_step_timeline_core(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._draft.update(
                {
                    CONF_BILLING_SLOT_MINUTES: int(user_input[CONF_BILLING_SLOT_MINUTES]),
                    CONF_CACHE_RETENTION_DAYS: int(user_input[CONF_CACHE_RETENTION_DAYS]),
                    CONF_ROUND_DECIMALS: int(user_input[CONF_ROUND_DECIMALS]),
                }
            )
            self._existing_sources = list(({**self._config_entry.data, **self._config_entry.options}).get(CONF_SOURCE_CHAIN, []))
            self._init_provider_state()
            return await self.async_step_provider_count()
        current = {**self._config_entry.data, **self._config_entry.options}
        schema = vol.Schema({
            vol.Required(
                CONF_BILLING_SLOT_MINUTES,
                default=str(current.get(CONF_BILLING_SLOT_MINUTES, DEFAULT_BILLING_SLOT_MINUTES)),
            ): _billing_slot_selector(),
            vol.Required(CONF_CACHE_RETENTION_DAYS, default=current.get(CONF_CACHE_RETENTION_DAYS, DEFAULT_CACHE_RETENTION_DAYS)): _int_selector(min_value=1, max_value=365),
            vol.Required(CONF_ROUND_DECIMALS, default=current.get(CONF_ROUND_DECIMALS, DEFAULT_ROUND_DECIMALS)): _int_selector(min_value=0, max_value=8),
        })
        return self.async_show_form(step_id="timeline_core", data_schema=schema, errors={})

    def _energy_pricing_defaults(self) -> dict[str, Any]:
        current = {**self._config_entry.data, **self._config_entry.options, **self._draft}
        return {
            CONF_ENERGY_SURCHARGE_PERCENT: current.get(CONF_ENERGY_SURCHARGE_PERCENT, DEFAULT_ENERGY_SURCHARGE_PERCENT),
            CONF_ENERGY_SURCHARGE_ABSOLUTE: current.get(CONF_ENERGY_SURCHARGE_ABSOLUTE, DEFAULT_ENERGY_SURCHARGE_ABSOLUTE),
            CONF_ENERGY_TAX_PERCENT: current.get(CONF_ENERGY_TAX_PERCENT, DEFAULT_ENERGY_TAX_PERCENT),
            CONF_USE_TIBBER_PROVIDER_FINAL_PRICE: current.get(
                CONF_USE_TIBBER_PROVIDER_FINAL_PRICE,
                DEFAULT_USE_TIBBER_PROVIDER_FINAL_PRICE,
            ),
        }

    def _consumption_defaults(self) -> dict[str, Any]:
        current = {**self._config_entry.data, **self._config_entry.options, **self._draft}
        enabled_default = current.get(
            CONF_ENABLE_CONSUMPTION_TRACKING,
            bool(current.get(CONF_CONSUMPTION_ENERGY_ENTITY)) or DEFAULT_ENABLE_CONSUMPTION_TRACKING,
        )
        return {
            CONF_ENABLE_CONSUMPTION_TRACKING: enabled_default,
            CONF_CONSUMPTION_ENERGY_ENTITY: current.get(CONF_CONSUMPTION_ENERGY_ENTITY),
            CONF_FIXED_FEE_MONTHLY_AMOUNT: current.get(CONF_FIXED_FEE_MONTHLY_AMOUNT, DEFAULT_FIXED_FEE_MONTHLY_AMOUNT),
            CONF_FIXED_FEE_DAILY_AMOUNT: current.get(CONF_FIXED_FEE_DAILY_AMOUNT, DEFAULT_FIXED_FEE_DAILY_AMOUNT),
            CONF_FIXED_FEE_TAX_PERCENT: current.get(CONF_FIXED_FEE_TAX_PERCENT, DEFAULT_FIXED_FEE_TAX_PERCENT),
            CONF_FIXED_FEE_VALUES_INCLUDE_TAX: current.get(
                CONF_FIXED_FEE_VALUES_INCLUDE_TAX,
                DEFAULT_FIXED_FEE_VALUES_INCLUDE_TAX,
            ),
            CONF_CURRENT_MONTH_FIXED_FEE_MODE: current.get(
                CONF_CURRENT_MONTH_FIXED_FEE_MODE,
                DEFAULT_CURRENT_MONTH_FIXED_FEE_MODE,
            ),
            CONF_AVG_PRICE_INCLUDE_BASIC_FEE: current.get(
                CONF_AVG_PRICE_INCLUDE_BASIC_FEE,
                DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE,
            ),
        }

    def _planner_defaults(self) -> list[str]:
        current = {**self._config_entry.data, **self._config_entry.options, **self._draft}
        return current.get(CONF_PLANNER_DEVICES, DEFAULT_PLANNER_DEVICES)

    def _finish_timeline_entry(self):
        return self.async_create_entry(
            title="",
            data={
                CONF_BILLING_SLOT_MINUTES: int(self._draft[CONF_BILLING_SLOT_MINUTES]),
                CONF_CACHE_RETENTION_DAYS: int(self._draft[CONF_CACHE_RETENTION_DAYS]),
                CONF_ROUND_DECIMALS: int(self._draft[CONF_ROUND_DECIMALS]),
                CONF_SOURCE_CHAIN: self._provider_chain,
                CONF_ENABLE_CONSUMPTION_TRACKING: bool(
                    _as_bool(self._draft.get(CONF_ENABLE_CONSUMPTION_TRACKING, DEFAULT_ENABLE_CONSUMPTION_TRACKING))
                ),
                CONF_CONSUMPTION_ENERGY_ENTITY: self._draft.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""),
                CONF_ENERGY_SURCHARGE_PERCENT: float(self._draft.get(CONF_ENERGY_SURCHARGE_PERCENT, DEFAULT_ENERGY_SURCHARGE_PERCENT)),
                CONF_ENERGY_SURCHARGE_ABSOLUTE: float(self._draft.get(CONF_ENERGY_SURCHARGE_ABSOLUTE, DEFAULT_ENERGY_SURCHARGE_ABSOLUTE)),
                CONF_ENERGY_TAX_PERCENT: float(self._draft.get(CONF_ENERGY_TAX_PERCENT, DEFAULT_ENERGY_TAX_PERCENT)),
                CONF_USE_TIBBER_PROVIDER_FINAL_PRICE: bool(
                    _as_bool(
                        self._draft.get(
                            CONF_USE_TIBBER_PROVIDER_FINAL_PRICE,
                            DEFAULT_USE_TIBBER_PROVIDER_FINAL_PRICE,
                        )
                    )
                ),
                CONF_FIXED_FEE_MONTHLY_AMOUNT: float(
                    self._draft.get(CONF_FIXED_FEE_MONTHLY_AMOUNT, DEFAULT_FIXED_FEE_MONTHLY_AMOUNT)
                ),
                CONF_FIXED_FEE_DAILY_AMOUNT: float(
                    self._draft.get(CONF_FIXED_FEE_DAILY_AMOUNT, DEFAULT_FIXED_FEE_DAILY_AMOUNT)
                ),
                CONF_FIXED_FEE_TAX_PERCENT: float(
                    self._draft.get(CONF_FIXED_FEE_TAX_PERCENT, DEFAULT_FIXED_FEE_TAX_PERCENT)
                ),
                CONF_FIXED_FEE_VALUES_INCLUDE_TAX: bool(
                    _as_bool(
                        self._draft.get(CONF_FIXED_FEE_VALUES_INCLUDE_TAX, DEFAULT_FIXED_FEE_VALUES_INCLUDE_TAX)
                    )
                ),
                CONF_CURRENT_MONTH_FIXED_FEE_MODE: str(
                    self._draft.get(CONF_CURRENT_MONTH_FIXED_FEE_MODE, DEFAULT_CURRENT_MONTH_FIXED_FEE_MODE)
                ),
                CONF_AVG_PRICE_INCLUDE_BASIC_FEE: bool(
                    _as_bool(self._draft.get(CONF_AVG_PRICE_INCLUDE_BASIC_FEE, DEFAULT_AVG_PRICE_INCLUDE_BASIC_FEE))
                ),
                CONF_PLANNER_DEVICES: self._draft.get(CONF_PLANNER_DEVICES, DEFAULT_PLANNER_DEVICES),
            },
        )

    async def async_step_profile_logger(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        current = {**self._config_entry.data, **self._config_entry.options}
        if user_input is not None:
            slot_minutes = int(user_input[CONF_SLOT_MINUTES])
            max_power_kw = float(user_input[CONF_MAX_POWER_KW])
            if slot_minutes <= 0:
                errors[CONF_SLOT_MINUTES] = "invalid"
            elif max_power_kw <= 0:
                errors[CONF_MAX_POWER_KW] = "invalid"
            elif (energy_error := validate_energy_entity(self.hass, user_input[CONF_ENERGY_ENTITY].strip())) is not None:
                errors[CONF_ENERGY_ENTITY] = energy_error
            else:
                return self.async_create_entry(title="", data={
                    CONF_ENERGY_ENTITY: user_input[CONF_ENERGY_ENTITY].strip(),
                    CONF_SLOT_MINUTES: slot_minutes,
                    CONF_MAX_POWER_KW: max_power_kw,
                    CONF_AUTO_CREATE_PROGRAMS: _as_bool(user_input[CONF_AUTO_CREATE_PROGRAMS]),
                    CONF_ALLOWED_PROGRAMS: parse_program_list(user_input.get(CONF_ALLOWED_PROGRAMS, [])),
                    CONF_BLOCKED_PROGRAMS: parse_program_list(user_input.get(CONF_BLOCKED_PROGRAMS, [])),
                })
        schema = vol.Schema({
            vol.Required(CONF_ENERGY_ENTITY, default=current[CONF_ENERGY_ENTITY]): selector.EntitySelector(selector.EntitySelectorConfig()),
            vol.Required(CONF_SLOT_MINUTES, default=current.get(CONF_SLOT_MINUTES, DEFAULT_SLOT_MINUTES)): _int_selector(min_value=1, max_value=120),
            vol.Required(CONF_MAX_POWER_KW, default=current.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW)): _float_selector(min_value=0.001, max_value=50, step=0.001),
            vol.Required(CONF_AUTO_CREATE_PROGRAMS, default=current.get(CONF_AUTO_CREATE_PROGRAMS, DEFAULT_AUTO_CREATE_PROGRAMS)): bool,
            vol.Optional(CONF_ALLOWED_PROGRAMS, default=current.get(CONF_ALLOWED_PROGRAMS, [])): _program_list_selector(),
            vol.Optional(CONF_BLOCKED_PROGRAMS, default=current.get(CONF_BLOCKED_PROGRAMS, [])): _program_list_selector(),
        })
        return self.async_show_form(step_id="profile_logger", data_schema=schema, errors=errors)
