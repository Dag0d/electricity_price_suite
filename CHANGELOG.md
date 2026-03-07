# Changelog

All notable changes to this project will be documented in this file.

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
