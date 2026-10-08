"""HAN port, transmission and relay states mirror the Portal Odbiorcy Enea icons."""
from __future__ import annotations

from typing import Any

import pytest


def _dashboard(**fields: Any) -> dict[str, Any]:
    """Return the dashboard fields behind the icons, as the API sends them."""
    return {
        "wmbusStatus": None,
        "p1Status": None,
        "transmissionStatus": True,
        "hanAvailable": True,
        **fields,
    }


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "inactive"),
        ({"wmbusStatus": 0}, "inactive"),
        ({"wmbusStatus": 1}, "active"),
        ({"wmbusStatus": 2}, "in_progress"),
        ({"wmbusStatus": 3}, "waiting_for_meter"),
        ({"wmbusStatus": 1, "hanAvailable": False}, "not_supported"),
        ({"hanAvailable": None}, "not_supported"),
        ({"wmbusStatus": 7}, None),
    ],
)
def test_han_port_state(fields: dict[str, Any], expected: str | None) -> None:
    """Each status code maps to the state the portal tooltip describes."""
    from custom_components.enea.const import HAN_STATES
    from custom_components.enea.sensor import _han_port_state

    state = _han_port_state(_dashboard(**fields), "wmbusStatus")

    assert state == expected
    assert state is None or state in HAN_STATES


def test_p1_reads_its_own_field() -> None:
    """The P1 sensor is not fooled by the Wireless M-Bus status."""
    from custom_components.enea.sensor import _han_port_state

    data = _dashboard(wmbusStatus=1, p1Status=2)

    assert _han_port_state(data, "p1Status") == "in_progress"


@pytest.mark.parametrize(
    ("fields", "expected_state", "expected_attrs"),
    [
        ({"switchState": 3, "drvSwitchLoadStatus": ""}, "on", {}),
        ({"switchState": 0, "drvSwitchLoadStatus": "Rozłączony"}, "off", {"load_status": "Rozłączony"}),
        ({"switchState": 1}, "removed", {}),
        ({"switchState": 2}, "warning", {}),
        ({"switchState": None}, None, {}),
        ({"switchState": 9}, None, {}),
    ],
)
def test_switch_state(
    fields: dict[str, Any], expected_state: str | None, expected_attrs: dict[str, Any]
) -> None:
    """The relay code maps to an ENUM option and its tooltip text becomes an attribute."""
    from custom_components.enea.sensor import SENSOR_DESCRIPTIONS

    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "switch_state")
    data = _dashboard(**fields)

    assert description.value_fn is not None and description.attr_fn is not None
    state = description.value_fn(data)
    assert state == expected_state
    assert state is None or state in (description.options or [])
    assert description.attr_fn(data) == expected_attrs


@pytest.mark.parametrize(
    ("fields", "transmission", "han_available"),
    [
        ({}, True, True),
        ({"transmissionStatus": False, "hanAvailable": False}, False, False),
        ({"transmissionStatus": None, "hanAvailable": None}, None, None),
    ],
)
def test_binary_sensor_values(
    fields: dict[str, Any], transmission: bool | None, han_available: bool | None
) -> None:
    """Missing flags stay unknown instead of reading as off."""
    from custom_components.enea.binary_sensor import BINARY_SENSOR_DESCRIPTIONS

    values = {d.key: d.value_fn(_dashboard(**fields)) for d in BINARY_SENSOR_DESCRIPTIONS}

    assert values == {"transmission": transmission, "han_available": han_available}
