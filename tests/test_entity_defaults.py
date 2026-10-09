"""Which entities a newly added meter gets enabled."""
from __future__ import annotations

from custom_components.enea.binary_sensor import BINARY_SENSOR_DESCRIPTIONS
from custom_components.enea.sensor import SENSOR_DESCRIPTIONS

DISABLED_BY_DEFAULT = {"han_wmbus", "han_p1", "han_available", "switch_state"}


def test_only_niche_diagnostics_start_disabled() -> None:
    """Most meters have no HAN port, and the relay state rarely changes."""
    disabled = {
        description.key
        for description in (*SENSOR_DESCRIPTIONS, *BINARY_SENSOR_DESCRIPTIONS)
        if not description.entity_registry_enabled_default
    }

    assert disabled == DISABLED_BY_DEFAULT
