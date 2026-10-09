"""Downward corrections and long gaps preserve cumulative energy and cost history."""
from datetime import datetime, timezone

import pytest

from custom_components.enea import costs, statistics


@pytest.mark.parametrize("running_state", [False, True], ids=["energy", "cost"])
@pytest.mark.parametrize("replacement,expected", [(2.0, [100.0, 102.0, 104.0, 109.0, 116.0]),
                                                 (0.0, [100.0, 100.0, 100.0, 105.0, 112.0])],
                         ids=["lower-reading", "zero-reading"])
async def test_downward_correction_across_gap_moves_tail_once(wire_recorder, running_state, replacement, expected):
    """Correcting to less or zero shifts later sums, preserves readings and is repeatable."""
    hours = [datetime(2026, month, day, tzinfo=timezone.utc) for month, day in [
        (1, 1), (6, 1), (6, 2), (6, 3), (6, 4),
    ]]
    totals = [100.0, 104.0, 110.0, 115.0, 122.0]
    states = totals if running_state else [1.0, 4.0, 6.0, 5.0, 7.0]
    store = wire_recorder(statistics, list(zip(hours, totals, states)))
    inject = costs._inject_cost_series if running_state else statistics._inject_energy_series
    series = [(hours[1], replacement), (hours[2], replacement)]

    await inject(object(), "590310600000001234", "Test", series)

    assert store.totals == expected
    assert [row[2] for row in store.recorder.stored] == (
        expected if running_state else [1.0, replacement, replacement, 5.0, 7.0]
    )
    assert len(store.injected) == 2
    await inject(object(), "590310600000001234", "Test", series)
    assert store.totals == expected
    assert len(store.injected) == 3, "the unchanged second import must not shift the tail again"
