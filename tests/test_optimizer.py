from custom_components.electricity_price_suite.optimizer import optimize_runtime


def test_optimizer_allows_fine_grained_profile_start():
    slots = [
        {"start_time": "2026-03-06T10:00:00+01:00", "price_per_kwh": 0.40},
        {"start_time": "2026-03-06T10:15:00+01:00", "price_per_kwh": 0.10},
        {"start_time": "2026-03-06T10:30:00+01:00", "price_per_kwh": 0.10},
        {"start_time": "2026-03-06T10:45:00+01:00", "price_per_kwh": 0.40},
    ]

    result = optimize_runtime(
        slots=slots,
        timezone_name="Europe/Berlin",
        billing_slot_minutes=15,
        duration_minutes=20,
        energy_profile=[1.0, 1.0, 1.0, 1.0],
        profile_slot_minutes=5,
        epsilon_rel=0.0,
        prefer_earliest=False,
        start_mode="now",
        start_in_minutes=0,
        deadline_mode="none",
        deadline_minutes=None,
        latest_start="2026-03-06T10:25:00+01:00",
        latest_finish=None,
        align_start_to_billing_slot=False,
        reference_time="2026-03-06T10:00:00+01:00",
    )

    assert result.status == "ok"
    assert result.best_start == "2026-03-06T10:10+01:00"
    assert result.best_end == "2026-03-06T10:30+01:00"


def test_optimizer_no_candidate_when_window_too_short():
    slots = [
        {"start_time": "2026-03-06T10:00:00+01:00", "price_per_kwh": 0.20},
        {"start_time": "2026-03-06T10:15:00+01:00", "price_per_kwh": 0.22},
    ]

    result = optimize_runtime(
        slots=slots,
        timezone_name="Europe/Berlin",
        billing_slot_minutes=15,
        duration_minutes=45,
        energy_profile=None,
        profile_slot_minutes=None,
        epsilon_rel=0.01,
        prefer_earliest=True,
        start_mode="now",
        start_in_minutes=0,
        deadline_mode="none",
        deadline_minutes=None,
        latest_start="2026-03-06T10:00:00+01:00",
        latest_finish="2026-03-06T10:30:00+01:00",
        align_start_to_billing_slot=False,
        reference_time="2026-03-06T10:00:00+01:00",
    )

    assert result.status == "no-candidate"
    assert result.reason == "window_too_short_for_duration"


def test_optimizer_ceil_does_not_return_already_passed_slot_start():
    slots = [
        {"start_time": "2026-03-07T15:45:00+01:00", "price_per_kwh": 0.20},
        {"start_time": "2026-03-07T16:00:00+01:00", "price_per_kwh": 0.25},
        {"start_time": "2026-03-07T16:15:00+01:00", "price_per_kwh": 0.30},
    ]

    result = optimize_runtime(
        slots=slots,
        timezone_name="Europe/Berlin",
        billing_slot_minutes=15,
        duration_minutes=15,
        energy_profile=None,
        profile_slot_minutes=None,
        epsilon_rel=0.0,
        prefer_earliest=True,
        start_mode="now",
        start_in_minutes=0,
        deadline_mode="start_within",
        deadline_minutes=60,
        latest_start=None,
        latest_finish=None,
        align_start_to_billing_slot=True,
        reference_time="2026-03-07T15:45:15+01:00",
    )

    assert result.status == "ok"
    assert result.best_start == "2026-03-07T16:00+01:00"
