# Electricity Price Suite

`electricity_price_suite` is a Home Assistant custom integration for:

- Building and maintaining a price timeline (today/tomorrow) from multiple sources
- Merging source data by strict priority (authoritative source wins)
- Learning consumption profiles from real device runs
- Optimizing device start times against stored timeline data
- Exposing automation-friendly entities and services

The integration is designed for setups where price data may come from different providers (attribute sensors, service/action responses, or manual injection), while planning logic should always run against one internal timeline store.

## Core Concepts

### 1) Entry Types

Each config entry is one of two types:

- `timeline`
- `profile_logger`

Timeline entries manage prices and plans.

Profile logger entries learn reusable device programs from one energy meter.

### 2) Timeline Instance

Each timeline entry creates one timeline instance (for example one meter/tariff/provider context).

Per timeline, the integration exposes:

- `sensor.<timeline_slug>_pricing_meta` (main timeline sensor)
- `sensor.<timeline_slug>_status` (high-level status state for automations)
- Optional: `sensor.<timeline_slug>_current_price`
- Dynamic plan entities: `sensor.<timeline_slug>_plan_<device_slug>`

### 3) Profile Logger Instance

Each profile logger entry exposes:

- `sensor.<logger_slug>_profile_logger_meta`
- `sensor.<logger_slug>_profile_<program_key>`

The meta sensor represents logger state and active run details.

Each program sensor represents one learned profile and exposes its average total energy and profile metadata.

### 4) Source Chain with Priority

Sources are ordered by priority:

- Lower numeric value = higher priority
- Priority `0` is typically the authoritative source
- Merge policy per slot start time:
  - Better priority replaces worse priority
  - Same priority replaces old value (refresh behavior)
  - Worse priority is ignored

### 5) Explicit Refresh, Deterministic Behavior

Timeline data updates on explicit calls (`refresh_timeline`, `inject_slots`) and optional scheduled checks implemented by the integration runtime. Source fallback behavior is transparent via response logs and sensor attributes.

### 6) Optimizer Works on Internal Store

The optimizer never needs price slots in its payload. It reads already stored timeline data and computes the best candidate start.

When a logger profile is used, the optimizer reads it directly from the internal logger runtime via `profile_logger_entity + program_key`. No external service hop is required.

## Features

- Multi-source timeline refresh (`entity_attribute`, `entity_action`, `inject_only`, optional API backup sources)
- Priority-based slot merge with replace/ignore logic
- Weighted timeline metrics (including mixed slot durations)
- Current price sensor (optional)
- Status sensor with fixed machine-readable states
- Device plan entity lifecycle: one persistent plan entity per device per timeline
- Consumption profile logger entries with per-program sensors
- Fine-grained optimizer (profile slot can be smaller than billing slot)
- Direct internal profile loading from suite logger entries
- Separate plan management service for reset/delete lifecycle actions
- Shared suite helpers for datetime parsing/formatting, profile resampling, logger program-key normalization, and input validation

## Internal Structure

The integration keeps the external feature set stable, but the runtime internals are split by responsibility:

- `runtime.py`
  - timeline orchestration, service-facing runtime behavior, scheduling, and entity lifecycle
- `logger_runtime.py`
  - profile logger orchestration, sampling, profile persistence, and logger-specific services
- `timeline_stats.py`
  - timeline state building, weighted metrics, current-price detection, and high-level status evaluation
- `plan_manager.py`
  - plan payload creation, reset handling, profile loading, and plan re-optimization helpers
- `resolvers.py`
  - target-to-runtime and target-to-plan resolution helpers
- `time_utils.py`
  - shared timezone-aware ISO parsing and formatting helpers
- `profile_utils.py`
  - shared profile export normalization and slot resampling helpers
- `logger_utils.py`
  - shared `program_key` normalization and display-name helpers
- `validation.py`
  - shared validation helpers for logger config input

This split was introduced to reduce duplication in the original monolithic runtime and make future changes easier to validate.

## Installation

1. Copy this integration into your Home Assistant config:
   - `custom_components/electricity_price_suite`
2. Restart Home Assistant.
3. Add integration in UI:
   - **Settings -> Devices & Services -> Add Integration -> Electricity Price Suite**

## Configuration Flow

The config flow starts with an entry-type selection:

- `Price Timeline`
- `Consumption Profile Logger`

### Timeline flow

1. Base settings:
   - Timeline name
   - Currency
   - Cache retention days
   - Price rounding decimals
   - Enable/disable current price sensor
2. Primary source type:
   - `entity_attribute`
   - `entity_action`
   - `inject_only`
3. Source-specific fields for the selected primary source

Additional timeline sources can be added later through service calls.

### Profile logger flow

1. Logger name
2. Total-increasing energy entity
3. Slot minutes
4. Maximum allowed power delta
5. Auto-create programs on unknown runs
6. Optional allowed/block lists for program keys

## Entities

### `sensor.<timeline_slug>_pricing_meta`

Main timeline sensor with:

- State: average price today (rounded) or `unknown`
- Attributes: timeline metrics, day rows, source/fetch metadata, merge-relevant info

### `sensor.<timeline_slug>_status`

Automation-friendly status state:

- `no_data`
- `today_only`
- `tomorrow_only`
- `tomorrow_not_from_prio0`
- `today_and_tomorrow`

Includes attributes like `today_rows`, `tomorrow_rows`, and `last_source_chain_fetch_at`.

### `sensor.<timeline_slug>_current_price` (optional)

- State: current slot price (rounded)
- Minimal attributes for current price context

### `sensor.<timeline_slug>_plan_<device_slug>`

Per-device planning entity:

- State: planned start timestamp (or `unknown`)
- Attributes: optimization window, duration, profile details, cost result, run metadata

### `sensor.<logger_slug>_profile_logger_meta`

Logger meta sensor with:

- State: `idle | running | error`
- Attributes: active run details, known profiles, last error, sampling metadata

### `sensor.<logger_slug>_profile_<program_key>`

Per-program learned profile sensor with:

- State: average total energy in kWh
- Attributes: `program_key`, `program_name`, `run_count`, `slot_minutes`, `slot_count`, `runtime_minutes`, `last_updated`

## Services

All services are in domain `electricity_price_suite`.

- `refresh_timeline`, `inject_slots`, `optimize_device`, `add_source`, `list_sources`, `delete_source` use a timeline target.
- `manage_plan` and `reoptimize_plan` use one or more plan entity targets.
- `start_profile_logging`, `finish_profile_logging`, `abort_profile_logging`, `get_consumption_profile`, `reset_consumption_profile`, `delete_consumption_profile` use a profile logger target.

---

### `refresh_timeline`

Refreshes timeline slots from configured sources and merges them by priority.

#### Inputs

- `target` (required): timeline entity target (`sensor.<timeline_slug>_pricing_meta`).
  - Expected: exactly one sensor entity in `target.entity_id`.
  - Effect: selects which timeline instance is refreshed.
- `sources` (optional): temporary source override for this call.
  - Expected: list of source objects with the same shape as stored pull sources.
  - Effect: only this refresh call uses these sources; stored source chain is unchanged.
- `overwrite` (optional, default `false`): explicit fresh re-fetch mode.
  - Expected: boolean.
  - Effect: deletes currently stored rows for today and tomorrow before fetching again from the source chain.

#### Response (typical)

- `status`: `ok | no_data`
- `timeline_entity`: resolved timeline entity id.
- `timeline_status`: high-level timeline status (`no_data`, `today_only`, ...).
- `used_source`: first source that produced usable data in this run.
- `used_sources`: all sources that contributed rows in this run.
- `attempt_log`: list of attempts (`source_id`, `source_type`, `success`, `rows`, `reason`).
- `rows_today`: number of stored rows for today after merge.
- `rows_tomorrow`: number of stored rows for tomorrow after merge.
- `has_primary_data_for_tomorrow`: whether tomorrow is currently covered by priority-0 rows.
- `pending_primary`: whether fallback rows still exist where primary is expected.
- `merge_debug`: counters (`inserted`, `replaced`, `ignored`) for this run.
- `cleared_rows`: number of today/tomorrow rows removed before fetch when `overwrite=true`.
- `last_source_chain_fetch_at`: timestamp of latest source-chain fetch.

---

### `inject_slots`

Directly injects slots into timeline storage.

#### Inputs

- `target` (required).
  - Expected: exactly one timeline target entity.
  - Effect: chooses which timeline store gets injected data.
- `slots` (required): list of slot objects.
  - Expected per item: `start_time` (ISO datetime with timezone), `price_per_kwh` (number).
  - Effect: slots are normalized and merged by priority rules.
- `source_name` (optional, default `manual_inject`).
  - Expected: string identifier.
  - Effect: stored as slot source id for traceability.
- `source_priority` (optional, default `9999`).
  - Expected: integer, lower = stronger source.
  - Effect: controls whether injected rows replace existing rows.
- `is_primary` (optional, default `false`).
  - Expected: boolean.
  - Effect: marks injected rows as primary-source rows.
- `overwrite` (optional, default `false`).
  - Expected: boolean.
  - Effect: deletes stored rows for the same local dates before injecting the new rows.

#### Response (typical)

- `status`: `ok | no_data`
- `timeline_entity`: resolved timeline entity id.
- `rows_received`: number of normalized rows accepted from payload.
- `merge_debug`: counters (`inserted`, `replaced`, `ignored`).
- `pending_primary`: whether fallback rows remain in active window.
- `cleared_rows`: number of stored rows removed before injection when `overwrite=true`.

---

### `optimize_device`

Computes best start for one device using timeline data.

#### Inputs

- `target` (required).
  - Expected: exactly one timeline target entity.
  - Effect: optimization uses that timeline's stored slots.
- `device_name` (required).
  - Expected: string.
  - Effect: identifies plan entity (`sensor.<timeline_slug>_plan_<device_slug>`).
- `duration_minutes` (optional unless profile source provides duration).
  - Expected: positive number.
  - Effect: runtime length used for cost window.
- `energy_profile` (optional).
  - Expected: numeric list of weights/energy segments.
  - Effect: weighted optimization profile; if shorter/longer than required it is normalized internally.
- `profile_slot_minutes` (optional).
  - Expected: positive integer.
  - Effect: slot resolution of `energy_profile`; also candidate grid base when not aligned to billing.
- `billing_slot_minutes` (optional).
  - Expected: positive integer.
  - Effect: override billing price raster; by default detected from timeline slots.
- `profile_logger_entity` (optional).
  - Expected: entity id of a suite profile logger meta sensor.
  - Effect: loads a profile directly from the internal logger runtime.
- `program_key` (required when `profile_logger_entity` is used).
  - Expected: stable program key, for example `auto_2`.
  - Effect: chooses which learned logger profile is used for optimization.
- `align_start_to_billing_slot` (optional, default `false`).
  - Expected: boolean.
  - Effect: candidate starts are forced to billing boundaries.
- `max_extra_cost_percent` (optional, default `1`).
  - Expected: float >= 0.
  - Effect: maximum additional cost in percent that is still acceptable when `prefer_earliest=true`.
- `prefer_earliest` (optional, default `true`).
  - Expected: boolean.
  - Effect: pick the earliest candidate within the allowed extra-cost threshold instead of the strict absolute minimum.
- `start_mode` (optional, default `now`).
  - Expected: `now | in`.
  - Effect: defines start anchor (`now` or `now + start_in_minutes`).
- `start_in_minutes` (optional, default `0`).
  - Expected: number >= 0.
  - Effect: used only for `start_mode=in`.
- `deadline_mode` (optional, default `none`).
  - Expected: `none | start_within | finish_within`.
  - Effect: applies relative deadline constraint.
- `deadline_minutes` (optional).
  - Expected: number >= 0.
  - Effect: relative limit for selected `deadline_mode`.
- `latest_start` (optional).
  - Expected: ISO datetime string.
  - Effect: expert override for absolute latest allowed start. Internally normalized to the optimizer's `latest_start` boundary.
- `latest_finish` (optional).
  - Expected: ISO datetime string.
  - Effect: expert override for absolute latest allowed finish. Internally converted to a derived `latest_start`.
#### Response (typical)

- `status`: `ok | no-candidate`
- `plan_entity_id`: per-device plan entity id.
- `best_start`: planned start datetime (ISO) or `null`.
- `best_end`: planned finish datetime (ISO) or `null`.
- `best_cost`: computed optimization cost or `null`.
- `reason`: explanatory reason for `no-candidate`.
- `requested_latest_start`: the originally requested latest-start boundary before any truncation by missing price data.

#### Common `reason` values

- `no_valid_slots_after_parse`: no usable price slots were available after parsing.
- `no_duration_or_profile`: neither duration nor usable energy profile was provided.
- `invalid_energy_profile`: the supplied profile could not be parsed as numbers.
- `invalid_duration_minutes`: duration was missing, zero, negative, or not finite.
- `invalid_deadline_minutes`: deadline offset was negative or not finite.
- `invalid_latest_start`: `latest_start` was provided but not parseable as ISO datetime.
- `invalid_latest_finish`: `latest_finish` was provided but not parseable as ISO datetime.
- `invalid_max_extra_cost_percent`: extra-cost threshold was negative or not finite.
- `window_too_short_for_duration`: the allowed search window is shorter than the runtime.
- `all_candidates_in_past`: all candidate starts fell at or before the current time.
- `incomplete_price_coverage_for_candidates`: price data did not fully cover any candidate run.
- `candidates_blocked_by_time_and_price_coverage`: some candidates were already in the past and the remaining ones had incomplete price coverage.
- `no_candidate_after_constraints`: constraints left no valid candidate, but no more specific optimizer reason applied.

---

### `manage_plan`

Resets or deletes existing plan entities.

#### Inputs

- `target` (required).
  - Expected: one or more existing plan entities (`sensor.<timeline_slug>_plan_<device_slug>`).
  - Effect: selected plan entities are managed.
- `reset` (optional).
  - Expected: boolean; exactly one of `reset`/`delete` must be true.
  - Effect: keeps entity, clears plan payload to a reset state (`status=reset`, no start timestamp).
- `delete` (optional).
  - Expected: boolean; exactly one of `reset`/`delete` must be true.
  - Effect: removes plan payload and plan entity from registry.

#### Response (typical)

- `results`: list of per-target results:
  - `status`: `reset | deleted | not_found`
  - `plan_entity_id`
  - `reason`

---

### `reoptimize_plan`

Recomputes one or more existing plan entities using the constraints already stored on the plan itself.

This is useful when:

- new price data has arrived,
- the original plan still exists,
- and you want to recompute the same plan without rebuilding the full `optimize_device` payload.

#### Inputs

- `target` (required).
  - Expected: one or more existing plan entities (`sensor.<timeline_slug>_plan_<device_slug>`).
  - Effect: each selected plan is re-optimized against the current timeline data using its stored profile, duration, cost tolerance, and requested latest-start boundary.

#### Response (typical)

- `results`: list of per-target results:

---

### `start_profile_logging`

Starts a new logger run for the selected profile logger.

#### Inputs

- `target` (required).
  - Expected: one profile logger meta sensor or one program profile sensor.
- `program_key` (optional when target is already a profile sensor).
  - Expected: program key string.

### `finish_profile_logging`

Finishes the active logger run and persists the updated profile.

#### Inputs

- `target` (required).
- `program_key` (optional when target is already a profile sensor).

### `abort_profile_logging`

Aborts the active logger run and restores the previous profile snapshot.

#### Inputs

- `target` (required).
- `reason` (optional).
  - Expected: one of `manual_abort`, `program_mismatch`, `restart_recovery`, `sampling_delay_exceeded`.
- `program_key` (optional).

### `get_consumption_profile`

Returns either the list of known programs, or one profile payload.

#### Inputs

- `target` (required).
- `program_key` (optional).
  - If omitted on the meta sensor, the response returns the known program list.
- `desired_slot_minutes` (optional).
  - Resamples the profile when the requested slot length is an integer multiple or divisor of the stored slot length.
- `debug` (optional).

### `reset_consumption_profile`

Clears one stored profile but keeps the entity.

#### Inputs

- `target` (required).
- `program_key` (optional when target is already a profile sensor).

### `delete_consumption_profile`

Deletes one stored profile and removes its entity.

#### Inputs

- `target` (required).
- `program_key` (optional when target is already a profile sensor).
  - `status`: `ok | no-candidate | not_found | not_reoptimized`
  - `plan_entity_id`
  - `best_start`
  - `best_end`
  - `best_cost`
  - `reason`

---

### `add_source`

Adds or updates a source definition in the timeline source chain.

`add_source` currently supports pull sources only:

- `entity_attribute`
- `entity_action`

`inject_only` is available for the primary source during config flow and for direct data injection via `inject_slots`, but it is not added through `add_source`.

#### Inputs

- `target` (required).
  - Expected: exactly one timeline target entity.
  - Effect: source is added to that timeline source chain.
- `id` (required).
  - Expected: unique source identifier string within timeline.
  - Effect: creates or updates this source entry.
- `source_type` (required).
  - Expected: `entity_attribute | entity_action`.
  - Effect: chooses provider path.
- `priority` (optional).
  - Expected: integer (lower = stronger).
  - Effect: merge rank for rows from this source.
- `source_entity_id` (optional).
  - Expected: entity id.
  - Effect: used by source type where entity context is required.
- `attribute` (optional for `entity_attribute`, required there).
  - Expected: attribute name string.
  - Effect: defines where slot list is read from state attributes.
- `action` (optional for `entity_action`, required there).
  - Expected: `domain.service` or `domain/service`.
  - Effect: action invoked to fetch source data.
- `response_path` (optional for `entity_action`, required there).
  - Expected: dotted path into service response payload.
  - Effect: points to list that should contain slot rows.
- `request_payload` (optional for `entity_action`).
  - Expected: object.
  - Effect: forwarded as action payload.
- `time_key` (optional, default `start_time`).
  - Expected: string.
  - Effect: source row field name used as slot start timestamp.
- `price_key` (optional, default `price_per_kwh`).
  - Expected: string.
  - Effect: source row field name used as slot price.
- `enabled` (optional, default `true`).
  - Expected: boolean.
  - Effect: enables/disables source participation in refresh.
- `inject_time_window` (optional for `entity_action`, default `true`).
  - Expected: boolean.
  - Effect: auto-injects today/tomorrow time window into request payload.
- `start_key` (optional, default `start`).
  - Expected: string.
  - Effect: payload key used for injected window start.
- `end_key` (optional, default `end`).
  - Expected: string.
  - Effect: payload key used for injected window end.
- `time_format` (optional, default `%Y-%m-%d %H:%M:%S`).
  - Expected: datetime format string.
  - Effect: format used for injected window start/end values.

#### Response (typical)

- `status`: `ok`
- `timeline_entity`
- `source`: normalized source object as stored
- `source_count`: number of sources in chain after upsert

---

### `list_sources`

Lists source IDs or one source configuration.

#### Inputs

- `target` (required)
- `id` (optional): if provided, returns full config for that source

#### Response (typical)

- If `id` provided:
  - `status`: `ok | not_found`
  - `timeline_entity`
  - `source`: source object or `null`
- If `id` omitted:
  - `status`: `ok`
  - `timeline_entity`
  - `source_ids`: list of configured source ids
  - `count`: source count

---

### `delete_source`

Deletes one source from the chain.

#### Inputs

- `target` (required)
- `id` (required)

#### Response (typical)

- `status`: `ok | not_found`
- `timeline_entity`
- `deleted_source_id`: deleted id or `null`
- `source_count`: remaining source count

## Optimizer Model Notes

- Billing slot and profile slot can differ
- Candidate start grid:
  - profile slot grid by default
  - billing slot grid if `align_start_to_billing_slot=true`
- Costs are overlap-weighted across price segments
- Deadlines can be constrained by currently available price coverage
- If a previous plan was data-truncated and new price coverage arrives before planned start, the integration can re-optimize and update the plan

## Cache and Persistence

- Timeline slots are stored in integration-managed storage per timeline entry
- Source metadata and plan payloads are persisted
- Cache retention controls historical cleanup behavior

## Branding

This integration includes local brand assets:

- `custom_components/electricity_price_suite/brand/icon.png`
- `custom_components/electricity_price_suite/brand/logo.png`

## Testing

Repository includes unit tests in `tests/` for key logic:

- slot normalization
- priority merge behavior
- optimizer candidate behavior and edge cases

These tests are recommended to keep, because they protect core algorithm behavior during refactors.

## Development Notes

- Requires Home Assistant with support for this integration version (`manifest.json`)
- Use Home Assistant service developer tools to test source and optimizer flows
- For production usage, configure at least one reliable priority-0 source

## Acknowledgements

Thanks to the Home Assistant ecosystem and maintainers of related integrations that make flexible price workflows possible, especially:

- [EPEX Spot for Home Assistant](https://github.com/mampfes/ha_epex_spot)
- The official Home Assistant Tibber integration
