# Manual Test Setup

These files provide a simple manual test harness for `electricity_price_suite`.

## Timeline to Create

Create one timeline via the config flow with this name:

- `Test Timeline`

Expected entity IDs:

- `sensor.test_timeline_pricing_meta`
- `sensor.test_timeline_status`
- `sensor.test_timeline_current_price`
- `sensor.test_timeline_plan_test_device`

## Helpers to Create

Create these Home Assistant helpers:

- `input_select.electricity_price_suite_test_case`
- `input_button.electricity_price_suite_run_test`
- `timer.electricity_price_suite_edge_case_wait`

`input_select.electricity_price_suite_test_case` options:

- `1_refresh_primary`
- `2_refresh_inject_only_override`
- `3_inject_fallback_today_tomorrow`
- `4_inject_primary_tomorrow_override`
- `5_optimize_basic`
- `6_optimize_service_validation_missing_runtime`
- `7_optimize_invalid_latest_start`
- `8_optimize_invalid_deadline_minutes`
- `9_optimize_all_candidates_in_past`
- `10_plan_reset`
- `11_plan_delete`
- `12_refresh_primary_overwrite`
- `13_optimizer_boundary_edge_case`

## Files

- `automation_test_runner.yaml`
  - test automation for refresh, inject, successful optimizer runs, optimizer error cases, reset, delete, and overwrite refresh
- `dashboard_eps_test.yaml`
  - dashboard view that puts all relevant entities and attributes on one page

## Notes

- The inject tests build dates dynamically from `now()`, so they always target today and tomorrow.
- `6_optimize_service_validation_missing_runtime` should be rejected directly by the service validator with an error that runtime input is missing.
- `7_optimize_invalid_latest_start` should return a no-candidate result with `reason=invalid_latest_start`.
- `8_optimize_invalid_deadline_minutes` should return a no-candidate result with `reason=invalid_deadline_minutes`.
- `9_optimize_all_candidates_in_past` uses a deliberately past-biased start anchor plus an absolute latest start at the current minute, so every candidate falls at or before `now`.
  - Expected result: `status=no-candidate`, `reason=all_candidates_in_past`.
- `12_refresh_primary_overwrite` is the cleanup path after inject tests. It deletes stored rows for today and tomorrow and then refreshes from the real source chain.
- `13_optimizer_boundary_edge_case` is a timed regression test for the optimizer:
  - it clears today/tomorrow,
  - injects synthetic quarter-hour prices,
  - waits until the next realistic quarter boundary plus 15 seconds,
  - and then runs the optimizer.
- The helper `timer.electricity_price_suite_edge_case_wait` shows the remaining wait time until the optimization fires.
- No response variables are required because the integration services now support optional responses.
