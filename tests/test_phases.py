"""The installation phases are inferred from the meter model, then the capacity."""
from __future__ import annotations

from typing import Any

import pytest


def _dashboard(model: str | None, capacity: float | None) -> dict[str, Any]:
    """Return a dashboard response with one active meter and a capacity."""
    meters = [] if model is None else [{"typeName": model, "disassemblyDate": None}]
    return {"meters": meters, "agreementPower": capacity}


@pytest.mark.parametrize(
    ("model", "capacity", "state", "attrs"),
    [
        ("OTUS3", 14, "three_phase", {"source": "meter_model"}),
        ("OTUS1", 5, "single_phase", {"source": "meter_model"}),
        ("MT174", None, "three_phase", {"source": "meter_model"}),
        ("otus3 ", 3, "three_phase", {"source": "meter_model"}),
        ("OTUS1", 14, "single_phase", {"source": "meter_model"}),
        ("XYZ123", 12, "three_phase", {"source": "contractual_capacity"}),
        (None, 16.5, "three_phase", {"source": "contractual_capacity"}),
        ("XYZ123", 11, None, {}),
        ("XYZ123", None, None, {}),
        (None, None, None, {}),
    ],
)
def test_inferred_phases(
    model: str | None, capacity: float | None, state: str | None, attrs: dict[str, Any]
) -> None:
    """A known model wins; a capacity beyond single phase is the fallback; else unknown."""
    from custom_components.enea.sensor import SENSOR_DESCRIPTIONS

    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "phases")
    data = _dashboard(model, capacity)

    assert description.value_fn is not None and description.attr_fn is not None
    assert description.value_fn(data) == state
    assert state is None or state in (description.options or [])
    assert description.attr_fn(data) == attrs
