from custom_components.electricity_price_suite.models import SlotRecord
from custom_components.electricity_price_suite.providers import normalize_slots
from custom_components.electricity_price_suite.store import merge_slot_dicts


def test_normalize_slots_with_custom_mapping():
    source = {
        "id": "provider_a",
        "priority": 2,
        "slot_mapping": {"time_key": "startsAt", "price_key": "total"},
    }
    raw = [{"startsAt": "2026-03-06T00:00:00+01:00", "total": "0.315"}]

    out = normalize_slots(raw, source)

    assert len(out) == 1
    assert out[0].start_time == "2026-03-06T00:00:00+01:00"
    assert out[0].price_per_kwh == 0.315
    assert out[0].is_primary_source is False


def test_normalize_slots_marks_priority_zero_as_primary():
    source = {
        "id": "provider_primary",
        "priority": 0,
        "slot_mapping": {"time_key": "start_time", "price_key": "price_per_kwh"},
    }
    raw = [{"start_time": "2026-03-06T00:00:00+01:00", "price_per_kwh": 0.3}]

    out = normalize_slots(raw, source)

    assert len(out) == 1
    assert out[0].is_primary_source is True


def test_merge_rank_overwrite_policy():
    by_start = {
        "2026-03-06T00:00:00+01:00": {
            "start_time": "2026-03-06T00:00:00+01:00",
            "price_per_kwh": 0.30,
            "source_id": "fallback",
            "source_priority": 5,
            "is_primary_source": False,
            "observed_at": "2026-03-05T00:00:00Z",
        }
    }

    higher = SlotRecord(
        start_time="2026-03-06T00:00:00+01:00",
        price_per_kwh=0.22,
        source_id="primary",
        source_priority=0,
        is_primary_source=True,
        observed_at="2026-03-05T01:00:00Z",
    )
    lower = SlotRecord(
        start_time="2026-03-06T00:00:00+01:00",
        price_per_kwh=0.50,
        source_id="low",
        source_priority=9,
        is_primary_source=False,
        observed_at="2026-03-05T02:00:00Z",
    )

    result1 = merge_slot_dicts(by_start, [higher])
    result2 = merge_slot_dicts(by_start, [lower])

    assert result1 == {"inserted": 0, "replaced": 1, "ignored": 0}
    assert result2 == {"inserted": 0, "replaced": 0, "ignored": 1}
    assert by_start["2026-03-06T00:00:00+01:00"]["source_id"] == "primary"
