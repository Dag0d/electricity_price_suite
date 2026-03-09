# Manual Test Setup

These files provide two separate manual test harnesses for `electricity_price_suite`:

- one for the timeline and optimizer part
- one for the consumption profile logger part

The dashboard is shared, but the helpers and automations are separated so both subsystems can be tested independently.

## Timeline Entry to Create

Create one timeline via the config flow with this name:

- `Test Timeline`

Expected entity IDs:

- `sensor.test_timeline_pricing_meta`
- `sensor.test_timeline_status`
- `sensor.test_timeline_current_price`
- `sensor.test_timeline_plan_test_device`

## Profile Logger Entry to Create

Create one profile logger via the config flow with this name:

- `Test Logger`

Use this energy entity:

- `sensor.electricity_price_suite_logger_total_kwh`

Suggested logger settings:

- `slot_minutes`: `1`
- `max_power_kw`: `20`
- `auto_create_programs`: `true`

Expected entity IDs:

- `sensor.test_logger_profile_logger_meta`
- `sensor.test_logger_profile_auto_1` (appears after the first successful run)

## Helpers to Create

### Timeline helpers

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

### Logger helpers

- `input_select.electricity_price_suite_logger_test_case`
- `input_button.electricity_price_suite_run_logger_test`
- `input_number.electricity_price_suite_logger_total_kwh`

Create `input_number.electricity_price_suite_logger_total_kwh` with roughly these settings:

- minimum: `0`
- maximum: `50`
- step: `0.001`
- mode: `box`
- unit: `kWh`

`input_select.electricity_price_suite_logger_test_case` options:

- `1_reset_energy_counter`
- `2_start_auto_1`
- `3_add_0_120_kwh`
- `4_finish_auto_1`
- `5_get_profile_auto_1`
- `6_get_profile_auto_1_resampled_5`
- `7_start_missing_program_error`
- `8_start_auto_1_and_abort`
- `9_finish_wrong_program_rolls_back`
- `10_reset_profile_auto_1`
- `11_delete_profile_auto_1`

## Template Sensor to Create

Add this template sensor so the logger can read a deterministic `total_increasing` energy counter from the helper:

```yaml
template:
  - sensor:
      - name: Electricity Price Suite Logger Total kWh
        unique_id: electricity_price_suite_logger_total_kwh
        unit_of_measurement: kWh
        device_class: energy
        state_class: total_increasing
        state: "{{ states('input_number.electricity_price_suite_logger_total_kwh') }}"
```

This will create:

- `sensor.electricity_price_suite_logger_total_kwh`

## Files

- `automation_test_runner.yaml`
  - timeline/optimizer test automation
- `automation_logger_test_runner.yaml`
  - logger test automation
- `dashboard_eps_test.yaml`
  - shared dashboard in sections layout for both harnesses

## Timeline Notes

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

## Logger Notes

- The logger tests use the helper-backed template sensor, so every change to `input_number.electricity_price_suite_logger_total_kwh` becomes a deterministic energy reading.
- After writing to the helper, the automation waits `5` seconds before any read-dependent logger action. That delay is intentional.
- `7_start_missing_program_error` should move the logger into an error state because the runtime receives no `program_key`.
- `8_start_auto_1_and_abort` should leave no committed profile update behind.
- `9_finish_wrong_program_rolls_back` should trigger a rollback and set an error state on the logger.
- Logger run lifecycle tests now use `manage_profile_run` with `mode=start|finish|abort`.
- `5_get_profile_auto_1` and `6_get_profile_auto_1_resampled_5` create a persistent notification containing the returned service payload so you can inspect it directly.
- These two tests now use `manage_profile` with `mode=get`.
- `10_reset_profile_auto_1` uses `manage_profile` with `mode=reset`.
- `11_delete_profile_auto_1` uses `manage_profile` with `mode=delete`.
