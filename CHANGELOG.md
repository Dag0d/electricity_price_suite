# Changelog

All notable changes to this project will be documented in this file.

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
