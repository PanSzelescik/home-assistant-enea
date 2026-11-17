"""What Home Assistant expects of the platforms and the manifest."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.enea import binary_sensor, sensor
from custom_components.enea import date as date_platform
from custom_components.enea.date import EneaBillDateEntity
from ha_instance import (  # noqa: F401 - clock, portal, database and hass are fixtures
    clock,
    database,
    hass,
    make_entry,
    make_prices_entry,
    portal,
    register_prices_entry,
)

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "enea"


@pytest.mark.parametrize("platform", [sensor, binary_sensor, date_platform], ids=lambda m: m.__name__)
def test_platforms_declare_parallel_updates(platform):
    """Entities fed by the coordinator are not throttled: PARALLEL_UPDATES is 0."""
    assert platform.PARALLEL_UPDATES == 0


def test_bill_dates_are_settings_that_are_never_polled():
    """A date the user sets is a configuration entity Home Assistant has no reason to poll."""
    entity = EneaBillDateEntity(Mock(data=None), "590310600000001234", "bill_last_reading")

    assert entity.should_poll is False
    assert entity.entity_category is EntityCategory.CONFIG


async def test_no_entity_of_a_meter_is_polled(hass):
    """Set up with prices, every entity of the meter gets its state pushed, not polled.

    The dates of the bill are the user's settings, shown with the configuration.
    """
    register_prices_entry(hass, make_prices_entry())
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done(wait_background_tasks=True)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    domains = {reg.domain for reg in entries}
    assert domains == {"sensor", "binary_sensor", "date"}
    polled = []
    for platform in hass.data["entity_platform"]["enea"]:
        polled += [entity.entity_id for entity in platform.entities.values() if entity.should_poll]
        assert platform._async_polling_timer is None
    assert polled == []
    dates = [reg for reg in entries if reg.domain == "date"]
    assert len(dates) == 2
    assert {reg.entity_category for reg in dates} == {EntityCategory.CONFIG}


def test_manifest_orders_the_setup_after_enea_prices():
    """enea_prices is set up first when installed, without being required.

    Set up after the meter, enea_prices reloads the meter's entry so the bill
    entities appear — a second login and dashboard request on every start.
    """
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["after_dependencies"] == ["enea_prices"]
    assert "enea_prices" not in manifest["dependencies"]
    assert manifest["dependencies"] == ["recorder"]


def test_package_does_not_shadow_its_date_platform():
    """__init__ must not import the name "date": it would replace the date submodule.

    Home Assistant imports custom_components.enea.date as the platform; a
    module-level "date" in the package (from datetime import date) takes the
    place of that attribute until the platform is imported.
    """
    tree = ast.parse((INTEGRATION / "__init__.py").read_text(encoding="utf-8"))
    platforms = {"sensor", "binary_sensor", "date"}
    bound = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom | ast.Import):
            bound |= {alias.asname or alias.name for alias in node.names}
        elif isinstance(node, ast.Assign):
            bound |= {target.id for target in node.targets if isinstance(target, ast.Name)}

    assert not bound & platforms


def test_actions_are_registered_by_the_integration_not_by_an_entry():
    """async_setup registers the actions; no config entry adds or removes them."""
    source = (INTEGRATION / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    by_function = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }

    assert by_function["async_setup"].count("services.async_register") == 2
    for name in ("async_setup_entry", "async_unload_entry", "async_remove_entry"):
        assert "services." not in by_function[name]


@pytest.mark.parametrize("language", ["en", "pl"])
def test_new_errors_are_translated(language):
    """The action's error and the flow's error have a text in both languages."""
    strings = json.loads(
        (INTEGRATION / "translations" / f"{language}.json").read_text(encoding="utf-8")
    )

    message = strings["exceptions"]["end_before_start"]["message"]
    assert "{start_date}" in message and "{end_date}" in message
    assert strings["config"]["error"]["meter_not_on_account"]
