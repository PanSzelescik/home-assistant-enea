"""Entities pick up every coordinator update, not only the first data.

Home Assistant invalidates its cached entity properties only when an _attr_
field is assigned.  A state computed in a cached property instead stays at the
first value until a restart — which is how "Statistics up to" stayed unknown.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any

from custom_components.enea.binary_sensor import BINARY_SENSOR_DESCRIPTIONS, EneaBinarySensor
from custom_components.enea.sensor import (
    SENSOR_DESCRIPTIONS,
    EneaEnergySensor,
    EneaSensor,
    EneaStatisticsDateSensor,
)


def _dashboard(tariff: str, total: float, transmission: bool) -> dict[str, Any]:
    """Return the dashboard fields the entities below read."""
    return {
        "tariffGroupName": tariff,
        "transmissionStatus": transmission,
        "meters": [],
        "currentValues": [
            {"measurementId": 1, "ppeZones": [], "valueNoZones": {"value": total}}
        ],
    }


def test_entities_follow_coordinator_updates() -> None:
    """After an update every entity reports the new data."""
    coordinator = SimpleNamespace(
        data=_dashboard("G11", 100.0, True),
        statistics_until=None,
        async_add_listener=lambda *args, **kwargs: None,
    )
    tariff = EneaSensor(
        coordinator, "PPE", next(d for d in SENSOR_DESCRIPTIONS if d.key == "tariff")  # type: ignore[arg-type]
    )
    energy = EneaEnergySensor(
        coordinator, "PPE", 1, "valueNoZones", "consumption_total", None, "consumption_total"  # type: ignore[arg-type]
    )
    until = EneaStatisticsDateSensor(coordinator, "PPE")  # type: ignore[arg-type]
    link = EneaBinarySensor(coordinator, "PPE", BINARY_SENSOR_DESCRIPTIONS[0])  # type: ignore[arg-type]
    entities = (tariff, energy, until, link)
    assert (tariff.native_value, energy.native_value, until.native_value, link.is_on) == (
        "G11", 100.0, None, True
    )

    coordinator.data = _dashboard("G12", 105.5, False)
    coordinator.statistics_until = datetime.date(2026, 10, 7)
    for entity in entities:
        entity.async_write_ha_state = lambda: None  # type: ignore[method-assign]
        entity._handle_coordinator_update()

    assert (tariff.native_value, energy.native_value, until.native_value, link.is_on) == (
        "G12", 105.5, datetime.date(2026, 10, 7), False
    )
