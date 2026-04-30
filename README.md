# Electricity Price Suite

`electricity_price_suite` is a Home Assistant custom integration for:

- Building and maintaining a price timeline (today/tomorrow) from direct market providers
- Merging source data by strict priority (authoritative source wins)
- Learning consumption profiles from real device runs
- Optimizing device start times against stored timeline data
- Exposing automation-friendly entities and services

The integration keeps one internal timeline store per entry. Provider selection, fallback order, planning, and logger-based optimization all work against that internal timeline rather than against proxy entities.

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
- `sensor.<timeline_slug>_current_price`
- `sensor.<timeline_slug>_current_market_price`
- Optional consumption/cost sensors when a total-increasing consumption entity is configured:
  - `sensor.<timeline_slug>_consumption_today_kwh`
  - `sensor.<timeline_slug>_consumption_current_hour_kwh`
  - `sensor.<timeline_slug>_consumption_month_kwh`
  - `sensor.<timeline_slug>_consumption_yesterday_kwh`
  - `sensor.<timeline_slug>_cost_today`
  - `sensor.<timeline_slug>_cost_yesterday`
  - `sensor.<timeline_slug>_cost_month`
  - `sensor.<timeline_slug>_cost_last_month`
  - `sensor.<timeline_slug>_cost_today_incl_basic_fee`
  - `sensor.<timeline_slug>_cost_yesterday_incl_basic_fee`
  - `sensor.<timeline_slug>_cost_month_incl_basic_fee`
  - `sensor.<timeline_slug>_cost_last_month_incl_basic_fee`
  - `sensor.<timeline_slug>_avg_paid_price_today`
  - `sensor.<timeline_slug>_avg_paid_price_yesterday`
  - `sensor.<timeline_slug>_avg_paid_price_month`
  - `sensor.<timeline_slug>_avg_paid_price_last_month`
- Dynamic plan entities: `sensor.<timeline_slug>_<planner_slug>_<device_slug>`

### 3) Profile Logger Instance

Each profile logger entry exposes:

- `sensor.<logger_slug>_profile_logger_meta`
- `sensor.<logger_slug>_profile_<program_key>`

The meta sensor represents logger state and active run details.

Each program sensor represents one learned profile and exposes its average total energy and profile metadata.

### 4) Provider Chain with Priority

Providers are ordered by priority:

- Lower numeric value = higher priority
- Priority `0` is typically the authoritative source
- Merge policy per slot start time:
  - Better priority replaces worse priority
  - Same priority replaces old value (refresh behavior)
  - Worse priority is ignored

### 5) Explicit Refresh, Deterministic Behavior

Timeline data updates on explicit calls (`refresh_timeline`, `inject_slots`) and optional scheduled checks implemented by the integration runtime. Provider fallback behavior is transparent via response logs and sensor attributes.

### 6) Optimizer Works on Internal Store

The optimizer never needs price slots in its payload. It reads already stored timeline data and computes the best candidate start.

When a logger profile is used, the optimizer reads it directly from the internal logger runtime via `profile_logger_entity + program_key`. No external service hop is required.

## Features

- Direct provider timeline refresh for:
  - `Tibber`
  - `SMARD`
  - `Energy-Charts`
  - `ENTSO-E`
- Ordered provider fallback chain per timeline
- `15 -> 60` slot aggregation where needed
- Never `60 -> 15` slot expansion
- Priority-based slot merge with replace/ignore logic
- Weighted timeline metrics (including mixed slot durations)
- Current price sensor (always enabled)
- Separate current market price sensor (raw provider market price before EPS surcharges/tax)
- Optional consumption/cost tracking from one total-increasing energy entity
- Status sensor with fixed machine-readable states
- Device plan entity lifecycle: one persistent plan entity per device per planner within a timeline
- Consumption profile logger entries with per-program sensors
- Fine-grained optimizer (profile slot can be smaller than billing slot)
- Direct internal profile loading from suite logger entries
- Separate plan management service for reset/delete lifecycle actions
- Shared suite helpers for datetime parsing/formatting, profile resampling, logger program-key normalization, and input validation

## Notifications

The integration no longer creates logger error notifications on its own.

Instead, it exposes the relevant machine-readable state for Home Assistant automations:

- logger state via `sensor.<logger_slug>_profile_logger_meta`
- logger reason via the `reason` attribute
- plan status and `reason` via `sensor.<timeline_slug>_<planner_slug>_<device_slug>`

This keeps notification policy outside the integration:

- choose your own language
- choose your own channels
- decide which error cases should notify and which should stay silent

Generic automation examples are available in:

- `examples/logger_error_notification.yaml`
- `examples/plan_no_candidate_notification.yaml`

## Examples

The repository includes small generic examples that you can adapt to your own setup:

- `examples/logger_error_notification.yaml`
  - watches a logger meta sensor
  - reads `reason`, `active_program`, and `started_at`
  - shows how to build your own notification policy
- `examples/plan_no_candidate_notification.yaml`
  - watches a plan entity
  - reacts to `status=no-candidate`
  - reports `reason`, `timeline_entity`, and `requested_latest_start`

Replace the placeholder entity IDs and notify services in those examples with your own Home Assistant entities.

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

1. Timeline core:
   - Timeline name
   - Billing resolution (`15` or `60` minutes)
   - Cache retention days
   - Price rounding decimals
2. Provider chain:
   - Number of providers (`1` to `4`)
   - Ordered provider selection
   - Provider-specific settings per step
3. Consumption and cost tracking:
   - Optional total-increasing consumption energy entity
   - Percentage surcharge on market price
   - Absolute surcharge per kWh
   - Energy tax / VAT percent
   - Optional simplified basic-fee settings
   - Optional flag whether average paid price should include the configured basic fee
4. Planner devices:
   - One or more planner device names for this timeline

Provider chain notes:

- The integration is EUR-native for now.
- `15 -> 60` aggregation is allowed for providers that only expose 15-minute slots.
- `60 -> 15` expansion is never performed.
- Tibber defaults to the first home and only asks for `home_index` when multiple homes are enabled.

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
- Attributes: timeline metrics, day rows, source/fetch metadata, merge-relevant info, and the configured energy price formula values

### `sensor.<timeline_slug>_status`

Automation-friendly status state:

- `no_data`
- `today_only`
- `tomorrow_only`
- `tomorrow_not_from_provider_1`
- `today_and_tomorrow`

Includes attributes like `today_rows`, `tomorrow_rows`, and `last_source_chain_fetch_at`.

### `sensor.<timeline_slug>_current_price`

- State: current slot price (rounded)
- Minimal attributes for current price context

### `sensor.<timeline_slug>_current_market_price`

- State: current raw market price before EPS energy surcharges and tax
- Minimal attributes for current market price context

### Optional consumption/cost sensors

If a timeline is configured with a total-increasing consumption energy entity, the integration exposes dedicated consumption/cost sensors.

- Consumption sensors expose `kWh`
- Cost sensors expose `EUR`
- Average paid price sensors expose `EUR/kWh`
- The consumption path is re-sampled every 30 seconds, so `current hour` and running averages are near-live without keeping 30-second raw rows
- Recorder will store their history like normal Home Assistant sensors
- `last_month` values are preserved via monthly rollups and do not require retention beyond 31 days

Basic fee modes:

- `none`
- `monthly`
  - Internally prorated to a daily share (`monthly_fee / days_in_month`)
- `daily`
  - Treated as a fixed fee per elapsed day

For month-level `incl_basic_fee` sensors, the fee is shown as the current accumulated month-to-date share.

Average paid price sensors can optionally include the configured basic fee through the timeline option `avg_price_include_basic_fee`.

### `sensor.<timeline_slug>_<planner_slug>_<device_slug>`

Per-device planning entity:

- State: planned start timestamp (or `unknown`)
- Attributes: optimization window, duration, profile details, cost result, run metadata
- Variant-related attributes include:
  - `program_key_used`
  - `program_display_name_used`

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

- `refresh_timeline`, `inject_slots`, `optimize_device` use a timeline target.
- `manage_plan` uses one or more plan entity targets.
- `manage_profile_run`, `manage_profile` use a profile logger target.

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
- `planner_name` (required).
  - Expected: name of a configured planner device for that timeline, for example `Geräteplanung`.
  - Effect: selects which planner device should own the resulting plan entity.
- `device_name` (required).
  - Expected: string.
  - Effect: identifies the per-planner plan entity.
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
- `program_display_name` (optional).
  - Expected: user-facing compact display label, for example `Auto 2 [I,D,S]`.
  - Effect: persists a readable variant label on the plan without changing the technical `program_key`.
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
- `plan_entity_id`: per-device per-planner plan entity id.
- `best_start`: planned start datetime (ISO) or `null`.
- `best_end`: planned finish datetime (ISO) or `null`.
- `best_cost`: computed optimization cost or `null`.
- `reason`: explanatory reason for `no-candidate`.
- `requested_latest_start`: the originally requested latest-start boundary before any truncation by missing price data.

When `profile_logger_entity + program_key` is used, optimization tries sources in this order:

1. learned profile from the selected logger
2. estimated runtime configured on that logger for the same `program_key`

If neither exists, the optimizer returns `no-candidate` with a specific reason.

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

Resets, deletes, or re-optimizes existing plan entities.

#### Inputs

- `target` (required).
  - Expected: one or more existing plan entities (`sensor.<timeline_slug>_<planner_slug>_<device_slug>`).
  - Effect: selected plan entities are managed.
- `mode` (required).
  - Expected: `reset | delete | reoptimize`.
  - Effect: chooses which plan-management action is executed.

#### Response (typical)

- `results`: list of per-target results:
  - `status`: `reset | deleted | ok | no-candidate | not_found | not_reoptimized`
  - `plan_entity_id`
  - `reason`

---

### `manage_profile_run`

Starts, finishes, or aborts a logger run for the selected profile logger.

#### Inputs

- `target` (required).
  - Expected: one profile logger meta sensor or one program profile sensor.
- `mode` (required).
  - Expected: `start | finish | abort`.
  - Effect: chooses the run-lifecycle action.
- `program_key` (optional when target is already a profile sensor).
  - Expected: program key string.
  - Effect: program to start, finish, or guard during abort.
- `program_display_name` (optional).
  - Only used for mode=`start`.
  - Expected: human-readable display name such as `Auto 2 [I,D]`.
  - Effect: stored as the profile name when a new profile is created or an existing profile name should be refreshed.
- `reason` (optional).
  - Only used for mode=`abort`.
  - Expected: one of `manual_abort`, `program_mismatch`, `restart_recovery`, `sampling_delay_exceeded`.

### `manage_profile`

Returns, resets, deletes, or manages fallback estimated runtimes for a profile logger.

#### Inputs

- `target` (required).
- `mode` (required).
  - Expected: `get | reset | delete | add_estimated_runtimes | list_estimated_runtimes | delete_estimated_runtime | clear_estimated_runtimes`.
  - Effect: chooses which profile-management action is executed.
- `program_key` (optional).
  - Only used for mode=`get`, `reset`, `delete`, `list_estimated_runtimes`, `delete_estimated_runtime`.
  - If omitted for mode=`get` on the meta sensor, the response returns the known program list.
- `desired_slot_minutes` (optional).
  - Only used for mode=`get`.
  - Resamples the profile when the requested slot length is an integer multiple or divisor of the stored slot length.
- `debug` (optional).
  - Only used for mode=`get`.
- `items` (optional).
  - Only used for mode=`add_estimated_runtimes`.
  - Expected: mapping of `program_key -> duration_minutes`.

#### Typical use

```yaml
action: electricity_price_suite.manage_profile
target:
  entity_id: sensor.dishwasher_profile_logger_meta
data:
  mode: add_estimated_runtimes
  items:
    auto_2: 180
    auto_2_i_d_s: 140
```

#### Response (typical)

- mode=`get`
  - profile list or one profile payload
- mode=`reset`
  - `status`: `ok | not_found`
  - `program_key`
- mode=`delete`
  - `status`: `ok | not_found`
  - `program_key`
- mode=`add_estimated_runtimes`
  - `status`: `ok`
  - `count`
  - `estimated_runtimes`
- mode=`list_estimated_runtimes`
  - without `program_key`:
    - `ok`: `true`
    - `count`
    - `estimated_runtimes`
  - with `program_key`:
    - `ok`: `true | false`
    - `program_key`
    - `estimated_runtime_minutes`
    - `reason`: `estimated_runtime_not_found` when missing
- mode=`delete_estimated_runtime`
  - `status`: `ok | not_found`
  - `program_key`
- mode=`clear_estimated_runtimes`
  - `status`: `ok`
  - `count`

---

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
- Provider metadata and plan payloads are persisted
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
- Use Home Assistant service developer tools to test provider and optimizer flows
- For production usage, configure at least one reliable priority-0 provider

## Acknowledgements

Thanks to the Home Assistant ecosystem and maintainers of related integrations that make flexible price workflows possible, especially:

- [EPEX Spot for Home Assistant](https://github.com/mampfes/ha_epex_spot)
- The official Home Assistant Tibber integration
