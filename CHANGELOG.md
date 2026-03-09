# Changelog

All notable changes to this project will be documented in this file.

## 2.0.0 - 2026-03-09

### Changed
- Consolidated the public service surface to reduce fragmentation:
  - `manage_sources` now replaces `add_source`, `list_sources`, and `delete_source`
  - `manage_plan` now uses `mode=reset|delete|reoptimize` and replaces the standalone `reoptimize_plan` path
  - `manage_profile_run` now replaces `start_profile_logging`, `finish_profile_logging`, and `abort_profile_logging`
  - `manage_profile` now replaces `get_consumption_profile`, `reset_consumption_profile`, `delete_consumption_profile`, and `manage_estimated_runtime`
- Updated service metadata, translations, and README documentation to describe the new mode-based service model consistently.
- Updated the tracked manual test automations and shared test dashboard to use the consolidated service API.

### Notes
- This release intentionally changes the service API and removes the previous split service names.

## 1.3.0 - 2026-03-09

### Added
- Added `manage_estimated_runtime` for profile logger entries so estimated fallback runtimes can be added, deleted, listed, or cleared per `program_key`.
- Added persisted plan fields `program_key_used` and `program_display_name_used` so automations can execute the exact planned program variant later.

### Changed
- `optimize_device` now accepts optional `program_display_name` for a compact user-facing variant label such as `Auto 2 [I,D,S]`.
- When `profile_logger_entity + program_key` is used, the optimizer now falls back from a missing learned profile to a configured estimated runtime for the same program key before returning `no-candidate`.
- Updated public documentation and service metadata for the new logger runtime management workflow.

## 1.2.0 - 2026-03-08

### Added
- Merged the former standalone consumption profile logger into `electricity_price_suite` as a first-class `profile_logger` entry type.
- Added explicit logger services in the suite domain: `start_profile_logging`, `finish_profile_logging`, `abort_profile_logging`, `get_consumption_profile`, `reset_consumption_profile`, and `delete_consumption_profile`.
- Added shared helper modules for suite-wide datetime handling, profile loading/resampling, logger key normalization, and validation.

### Changed
- The suite now supports two clear config entry types: `timeline` and `profile_logger`.
- `optimize_device` now reads logger profiles directly from internal suite logger runtimes via `profile_logger_entity + program_key` instead of using an external service hop.
- Refactored repeated logger/timeline internals onto shared helper paths so profile export, resampling, datetime formatting, ISO parsing, and logger validation are handled consistently across the suite.
- Standardized logger profile sensor payload building through the runtime instead of rebuilding metadata separately in the sensor layer.

### Fixed
- Removed duplicate profile-response logic from the service layer so `get_consumption_profile` and internal optimizer profile loading now share the same normalized handling for profile existence and invalid resampling requests.
- Reduced repeated UTC/ISO timestamp formatting and parsing paths in storage and runtime code to lower the chance of diverging behavior between logger and timeline subsystems.

## 1.1.1 - 2026-03-07

### Added
- Added `reoptimize_plan` so existing plan entities can be recomputed directly from their stored constraints and current timeline data.

### Changed
- Renamed the persisted plan/debug attribute from `requested_window_end` to `requested_latest_start` so it matches its actual meaning.

### Fixed
- Fixed a regression in the optimizer where `requested_latest_start` was converted to a string too early, causing `datetime` vs `str` comparison failures on normal optimize runs.

## 1.1.0 - 2026-03-07

### Changed
- Reworked the optimizer internals so all runtime window handling is normalized to `earliest_start` and `latest_start` before candidate evaluation.
- Cleaned up the optimizer/runtime split by moving plan persistence and plan re-optimization responsibilities into dedicated helpers while keeping timeline behavior unchanged.
- Renamed the optimizer tolerance input from `epsilon_rel` to `max_extra_cost_percent` for clearer service and UI semantics.
- Removed the unused `dry_run` path from the optimizer service and persisted plan payload.
- Clarified `latest_start` and `latest_finish` as expert overrides in the service documentation and translations.

### Fixed
- Fixed a refactor regression where the optimizer still referenced the removed `ws` variable on successful runs.
- Added a defensive candidate guard so starts at or before `now` are always rejected even if a future refactor touches grid rounding again.
- Improved optimizer validation and failure handling for invalid duration/profile input, invalid absolute deadline timestamps, invalid deadline values, and invalid extra-cost thresholds.
- Improved no-candidate reasoning so plan entities now expose more specific debug reasons such as `all_candidates_in_past` or incomplete price coverage.

### Added
- Expanded the manual test harness with dedicated optimizer validation and failure scenarios in addition to the existing success-path and boundary tests.

## 1.0.2 - 2026-03-07

### Fixed
- Fixed optimizer grid rounding so a calculation performed a few seconds after any valid start-grid boundary no longer returns a start time that is already in the past.
- Fixed the main pricing meta sensor to return a proper numeric-empty state (`None`) instead of the string `unknown` when no numeric value is available.

### Changed
- Added `overwrite` support to `inject_slots` so same-day test data or manual injections can explicitly replace stored day rows.

### Added
- Extended the manual test harness with a dedicated optimizer boundary-edge-case scenario for quarter-hour start validation.

## 1.0.1 - 2026-03-07

### Changed
- Cleaned up runtime structure and internal typing without changing the feature set.
- Split the former monolithic `runtime.py` responsibilities into dedicated runtime, timeline stats, plan manager, and resolver modules.
- Added `overwrite` support to `refresh_timeline` for explicit re-fetches of today and tomorrow.
- Made service responses optional so automations no longer require `response_variable`.

### Added
- Manual test automation, dashboard, and helper documentation under `tests/manual/`.

## 1.0.0 - 2026-03-06

### Added
- Initial release of `electricity_price_suite`.
- Multi-source timeline refresh with priority-based merge behavior.
- Runtime optimizer with per-device persistent plan entities.
- Config flow, options flow, service API, translations (EN/DE), tests, and branding assets.
