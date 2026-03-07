# Changelog

All notable changes to this project will be documented in this file.

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
