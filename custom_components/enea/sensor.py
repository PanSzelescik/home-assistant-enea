"""Sensor platform for the Enea Energy Meter integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import EneaConfigEntry
from .connector import format_address, get_active_meter, infer_phases
from .const import (
    BILL_KEY_CURRENT,
    BILL_KEY_PREVIOUS,
    BILLING_PERIOD_MIN_SEGMENT,
    CONF_FETCH_CONSUMPTION,
    CONF_FETCH_GENERATION,
    CONF_METER_NAME,
    PORTAL_URL,
    DEFAULT_NAME,
    DOMAIN,
    HAN_STATE_BY_CODE,
    HAN_STATE_INACTIVE,
    HAN_STATE_NOT_SUPPORTED,
    HAN_STATES,
    MEASUREMENT_ID_CONSUMPTION,
    PHASES_SINGLE,
    PHASES_THREE,
    SENSOR_KEY_ADDRESS,
    SENSOR_KEY_BILLING_PERIOD_START,
    SENSOR_KEY_CAPACITY,
    SENSOR_KEY_HAN_P1,
    SENSOR_KEY_HAN_WMBUS,
    SENSOR_KEY_METER_MODEL,
    SENSOR_KEY_PHASES,
    SENSOR_KEY_READING_DATE,
    SENSOR_KEY_STATISTICS_UNTIL,
    SENSOR_KEY_STATUS,
    SENSOR_KEY_SWITCH_STATE,
    SENSOR_KEY_TARIFF,
    SWITCH_STATE_BY_CODE,
    UNIT_COST,
)
from .coordinator import EneaUpdateCoordinator
from .costs import find_tariff_group


def _get_device_info(meter_code: str, data: dict[str, Any] | None) -> DeviceInfo:
    """Build DeviceInfo, enriched with physical meter details when data is available."""
    active = get_active_meter(data) if data else None
    return DeviceInfo(
        identifiers={(DOMAIN, meter_code)},
        name=f"{DEFAULT_NAME} {meter_code}",
        manufacturer="Enea",
        model=active["typeName"] if active else None,
        serial_number=active["serialNumber"] if active else None,
        configuration_url=PORTAL_URL,
    )


# ---------------------------------------------------------------------------
# Static diagnostic sensors (always created, data from dashboard endpoint)
# ---------------------------------------------------------------------------


def _address_attrs(data: dict[str, Any]) -> dict[str, Any]:
    """Return address fields as a flat dict, omitting null values."""
    addr = data.get("address")
    if not addr:
        return {}
    return {
        k: v
        for k, v in {
            "street": addr.get("street"),
            "house_number": addr.get("houseNum"),
            "apartment_number": addr.get("apartmentNum"),
            "post_code": addr.get("postCode"),
            "city": addr.get("city"),
            "district": addr.get("district"),
            "parcel_number": addr.get("parcelNum"),
        }.items()
        if v is not None
    }


def _meter_model_attrs(data: dict[str, Any]) -> dict[str, Any]:
    """Return assembly/disassembly timestamps of the active meter as ISO strings."""
    m = get_active_meter(data)
    if not m:
        return {}
    return {
        k: datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
        for k, ts in {
            "assembly_date": m.get("assemblyDate"),
            "disassembly_date": m.get("disassemblyDate"),
        }.items()
        if ts is not None
    }


def _get_reading_date(data: dict[str, Any]) -> datetime | None:
    """Return the last reading timestamp from dashboard data, or None if unavailable."""
    ts = next(
        (
            cv["readingDate"]
            for cv in data.get("currentValues", [])
            if cv.get("measurementId") == MEASUREMENT_ID_CONSUMPTION and cv.get("readingDate")
        ),
        None,
    )
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None


def _han_port_state(data: dict[str, Any], status_key: str) -> str | None:
    """Return the HAN port state the way the Portal Odbiorcy Enea icons show it.

    A meter without HAN support reports `hanAvailable = false` and its port
    status is meaningless.  Otherwise a missing or zero status means the port is
    inactive.  An unrecognised code yields None (unknown) rather than a state
    outside the ENUM options.
    """
    if not data.get("hanAvailable"):
        return HAN_STATE_NOT_SUPPORTED
    code = data.get(status_key)
    if not code:
        return HAN_STATE_INACTIVE
    return HAN_STATE_BY_CODE.get(code)


def _switch_state_attrs(data: dict[str, Any]) -> dict[str, Any]:
    """Return the relay description shown in the Portal Odbiorcy Enea tooltip, if any."""
    load_status = data.get("drvSwitchLoadStatus")
    return {"load_status": load_status} if load_status else {}


def _billing_period_starts(data: dict[str, Any]) -> list[date]:
    """Return billing period start dates found in the dashboard's billingWeekData.

    Segments spanning a whole billing period are interleaved with daily ones;
    each such segment starts on the first day of a period on the invoice.  The
    very first segment is skipped — it starts where the portal's data window
    begins, not on a reading date.  All measurements share the same segments,
    so the consumption one is enough.
    """
    measurement = next(
        (
            m
            for m in data.get("billingWeekData") or []
            if m.get("measurementId") == MEASUREMENT_ID_CONSUMPTION
        ),
        None,
    )
    if measurement is None:
        return []
    starts: list[date] = []
    for i, segment in enumerate(measurement.get("values") or []):
        time_from, time_to = segment.get("timeFrom"), segment.get("timeTo")
        if i == 0 or time_from is None or time_to is None:
            continue
        start = dt_util.utc_from_timestamp(time_from / 1000)
        if dt_util.utc_from_timestamp(time_to / 1000) - start >= BILLING_PERIOD_MIN_SEGMENT:
            starts.append(dt_util.as_local(start).date())
    return starts


@dataclass(frozen=True, kw_only=True)
class EneaSensorEntityDescription(SensorEntityDescription):
    """Extended sensor description for Enea diagnostic sensors."""

    value_fn: Callable[[dict[str, Any]], Any] | None = None
    attr_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


SENSOR_DESCRIPTIONS: tuple[EneaSensorEntityDescription, ...] = (
    EneaSensorEntityDescription(
        key=SENSOR_KEY_TARIFF,
        translation_key=SENSOR_KEY_TARIFF,
        icon="mdi:tag-text",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("tariffGroupName"),
        attr_fn=lambda data: {
            "zones": next(
                (
                    cv["ppeZones"]
                    for cv in data.get("currentValues", [])
                    if cv.get("measurementId") == MEASUREMENT_ID_CONSUMPTION
                ),
                [],
            ),
        },
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_CAPACITY,
        translation_key=SENSOR_KEY_CAPACITY,
        icon="mdi:flash-triangle",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("agreementPower"),
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_STATUS,
        translation_key=SENSOR_KEY_STATUS,
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("detailedStatus"),
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_ADDRESS,
        translation_key=SENSOR_KEY_ADDRESS,
        icon="mdi:map-marker",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: format_address(data.get("address")),
        attr_fn=_address_attrs,
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_READING_DATE,
        translation_key=SENSOR_KEY_READING_DATE,
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_get_reading_date,
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_METER_MODEL,
        translation_key=SENSOR_KEY_METER_MODEL,
        icon="mdi:meter-electric-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (get_active_meter(data) or {}).get("typeName"),
        attr_fn=_meter_model_attrs,
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_PHASES,
        translation_key=SENSOR_KEY_PHASES,
        icon="mdi:sine-wave",
        device_class=SensorDeviceClass.ENUM,
        options=[PHASES_SINGLE, PHASES_THREE],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: infer_phases(data)[0],
        attr_fn=lambda data: {"source": source} if (source := infer_phases(data)[1]) else {},
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_HAN_WMBUS,
        translation_key=SENSOR_KEY_HAN_WMBUS,
        icon="mdi:access-point",
        device_class=SensorDeviceClass.ENUM,
        options=HAN_STATES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _han_port_state(data, "wmbusStatus"),
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_HAN_P1,
        translation_key=SENSOR_KEY_HAN_P1,
        icon="mdi:serial-port",
        device_class=SensorDeviceClass.ENUM,
        options=HAN_STATES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _han_port_state(data, "p1Status"),
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_SWITCH_STATE,
        translation_key=SENSOR_KEY_SWITCH_STATE,
        icon="mdi:electric-switch-closed",
        device_class=SensorDeviceClass.ENUM,
        options=list(SWITCH_STATE_BY_CODE.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: SWITCH_STATE_BY_CODE.get(data.get("switchState")),  # pyright: ignore[reportArgumentType]
        attr_fn=_switch_state_attrs,
    ),
    EneaSensorEntityDescription(
        key=SENSOR_KEY_BILLING_PERIOD_START,
        translation_key=SENSOR_KEY_BILLING_PERIOD_START,
        icon="mdi:calendar-start",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: next(reversed(_billing_period_starts(data)), None),
        attr_fn=lambda data: {
            "period_starts": [d.isoformat() for d in _billing_period_starts(data)],
        },
    ),
)


# ---------------------------------------------------------------------------
# Energy sensors (static total + dynamic per-zone)
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EneaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enea sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    meter_code = entry.data[CONF_METER_NAME]
    data = coordinator.data or {}
    fetch_consumption = entry.options.get(CONF_FETCH_CONSUMPTION, True)
    fetch_generation = entry.options.get(CONF_FETCH_GENERATION, True)

    sensors: list[SensorEntity] = []

    # Diagnostic sensors
    sensors.extend(
        EneaSensor(coordinator, meter_code, description)
        for description in SENSOR_DESCRIPTIONS
    )
    sensors.append(EneaStatisticsDateSensor(coordinator, meter_code))

    # Energy sensors — total (always) + per-zone (dynamic)
    for cv in data.get("currentValues", []):
        measurement_id: int = cv["measurementId"]
        is_consumption = measurement_id == MEASUREMENT_ID_CONSUMPTION
        if is_consumption and not fetch_consumption:
            continue
        if not is_consumption and not fetch_generation:
            continue
        prefix = "consumption" if is_consumption else "generation"
        type_label = "pobrana" if is_consumption else "oddana"

        # Total (sum of all zones)
        sensors.append(
            EneaEnergySensor(
                coordinator=coordinator,
                meter_code=meter_code,
                measurement_id=measurement_id,
                zone_key="valueNoZones",
                unique_key=f"{prefix}_total",
                sensor_name=None,  # uses translation_key
                translation_key=f"{prefix}_total",
            )
        )

        # Per-zone — name includes type to distinguish consumption vs generation
        for i, zone_label in enumerate(cv.get("ppeZones", []), start=1):
            zone_key = f"valueZone{i}"
            if cv.get(zone_key) is not None:
                short_name = zone_label.split(" ")[0]  # "Dzień 1.8.1" → "Dzień"
                sensors.append(
                    EneaEnergySensor(
                        coordinator=coordinator,
                        meter_code=meter_code,
                        measurement_id=measurement_id,
                        zone_key=zone_key,
                        unique_key=f"{prefix}_zone{i}",
                        sensor_name=f"Energia {type_label} – {short_name}",
                        translation_key=None,
                    )
                )

    # Bill sensors — created only when enea_prices is configured with matching tariff
    tariff_name = data.get("tariffGroupName")
    if find_tariff_group(hass, tariff_name) is not None:
        sensors.append(EneaBillSensor(coordinator, meter_code, BILL_KEY_PREVIOUS))
        sensors.append(EneaBillSensor(coordinator, meter_code, BILL_KEY_CURRENT))

    async_add_entities(sensors)


class EneaSensor(CoordinatorEntity[EneaUpdateCoordinator], SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Diagnostic sensor entity for an Enea meter."""

    entity_description: EneaSensorEntityDescription  # pyright: ignore[reportIncompatibleVariableOverride]
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EneaUpdateCoordinator,
        meter_code: str,
        description: EneaSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description  # pyright: ignore[reportIncompatibleVariableOverride]
        self._meter_code = meter_code
        self._attr_unique_id = f"enea-{meter_code}-{description.key}"
        self._attr_device_info = _get_device_info(meter_code, coordinator.data)
        self._update_attrs()

    def _update_attrs(self) -> None:
        """Compute the state and attributes from the coordinator data."""
        data = self.coordinator.data
        description = self.entity_description
        self._attr_native_value = (
            description.value_fn(data)
            if data is not None and description.value_fn is not None
            else None
        )
        self._attr_extra_state_attributes = (
            description.attr_fn(data)
            if data is not None and description.attr_fn is not None
            else None
        ) or {}

    @callback
    def _handle_coordinator_update(self) -> None:
        """Recompute the state from the fresh coordinator data, then write it.

        Home Assistant only invalidates its cached entity properties when an
        _attr_ field is assigned, so the state must be assigned here rather
        than computed in a (cached) property — that would keep the first value
        until a restart.
        """
        self._update_attrs()
        super()._handle_coordinator_update()


class EneaStatisticsDateSensor(CoordinatorEntity[EneaUpdateCoordinator], SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Diagnostic sensor with the newest day the imported statistics cover.

    Shows at a glance when the Portal Odbiorcy Enea is late with data or has
    skipped a day, and lets automations alert on stale statistics.
    """

    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_KEY_STATISTICS_UNTIL
    _attr_icon = "mdi:database-clock"
    _attr_device_class = SensorDeviceClass.DATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EneaUpdateCoordinator, meter_code: str) -> None:
        """Initialize the statistics date sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"enea-{meter_code}-{SENSOR_KEY_STATISTICS_UNTIL}"
        self._attr_device_info = _get_device_info(meter_code, coordinator.data)
        self._update_attrs()

    def _update_attrs(self) -> None:
        """Take the newest statistics day, None before the first import."""
        self._attr_native_value = self.coordinator.statistics_until

    @callback
    def _handle_coordinator_update(self) -> None:
        """Recompute the state from the fresh coordinator data, then write it.

        Home Assistant only invalidates its cached entity properties when an
        _attr_ field is assigned, so the state must be assigned here rather
        than computed in a (cached) property — that would keep the first value
        until a restart.
        """
        self._update_attrs()
        super()._handle_coordinator_update()


class EneaEnergySensor(CoordinatorEntity[EneaUpdateCoordinator], SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Energy sensor for a specific measurement/zone of an Enea meter."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: EneaUpdateCoordinator,
        meter_code: str,
        measurement_id: int,
        zone_key: str,
        unique_key: str,
        sensor_name: str | None,
        translation_key: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._meter_code = meter_code
        self._measurement_id = measurement_id
        self._zone_key = zone_key
        self._attr_unique_id = f"enea-{meter_code}-{unique_key}"
        self._attr_device_info = _get_device_info(meter_code, coordinator.data)

        if translation_key:
            self._attr_translation_key = translation_key
        else:
            self._attr_name = sensor_name
        self._update_attrs()

    def _update_attrs(self) -> None:
        """Take the energy value in kWh for this measurement and zone."""
        data = self.coordinator.data or {}
        cv = next(
            (
                cv
                for cv in data.get("currentValues", [])
                if cv.get("measurementId") == self._measurement_id
            ),
            None,
        )
        zone_data = cv.get(self._zone_key) if cv is not None else None
        self._attr_native_value = zone_data.get("value") if zone_data is not None else None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Recompute the state from the fresh coordinator data, then write it.

        Home Assistant only invalidates its cached entity properties when an
        _attr_ field is assigned, so the state must be assigned here rather
        than computed in a (cached) property — that would keep the first value
        until a restart.
        """
        self._update_attrs()
        super()._handle_coordinator_update()



class EneaBillSensor(CoordinatorEntity[EneaUpdateCoordinator], SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Sensor showing an estimated electricity bill for a billing period.

    Two sensors are created per meter (previous closed period and current
    running period).  Values are recomputed by the coordinator whenever the
    user changes a reading date or new statistics arrive.

    No state_class is set intentionally — the value is an estimate, not a
    metered quantity, and setting state_class would cause the recorder to
    compile competing long-term statistics.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UNIT_COST
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: EneaUpdateCoordinator,
        meter_code: str,
        bill_key: str,
    ) -> None:
        """Initialize a bill estimate sensor."""
        super().__init__(coordinator)
        self._bill_key = bill_key
        self._attr_unique_id = f"enea-{meter_code}-{bill_key}"
        self._attr_translation_key = bill_key
        self._attr_device_info = _get_device_info(meter_code, coordinator.data)

    @property
    def native_value(self) -> float | None:
        """Return the estimated bill total in PLN, or None when unavailable."""
        est = self.coordinator.bill_estimates.get(self._bill_key)
        return est.total if est is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return cost breakdown mirroring the Enea invoice structure.

        Top-level keys: start, end, months, total_netto, total (brutto = state).
        Section 'Sprzedaż energii': energy_netto + per-zone kwh_<zone> and
        energy_<zone>_netto.
        Section 'Usługa dystrybucji': distribution_netto (sum) + fixed fees
        (fixed_network_netto, fixed_capacity_netto, fixed_subscription_netto) +
        per-zone components: variable_network_<zone>_netto, quality_<zone>_netto,
        oze_<zone>_netto, cogeneration_<zone>_netto.
        """
        est = self.coordinator.bill_estimates.get(self._bill_key)
        if est is None:
            return None
        _TRANSL = str.maketrans({
            "ó": "o", "ę": "e", "ą": "a", "ź": "z", "ż": "z",
            "ń": "n", "ł": "l", "ś": "s", "ć": "c",
            "Ó": "O", "Ę": "E", "Ą": "A", "Ź": "Z", "Ż": "Z",
            "Ń": "N", "Ł": "L", "Ś": "S", "Ć": "C",
        })
        attrs: dict[str, Any] = {
            "start": est.start.isoformat(),
            "end": est.end.isoformat(),
            "months": est.months,
        }
        # Sprzedaż energii — per strefa (kWh + koszt), potem suma
        for zone_display, kwh in est.kwh_by_zone.items():
            safe = zone_display.lower().translate(_TRANSL).replace(" ", "_")
            attrs[f"kwh_{safe}"] = kwh
            attrs[f"energy_{safe}_netto"] = est.energy_by_zone_netto.get(zone_display, 0.0)
        attrs["energy_netto"] = est.energy_netto
        # Usługa dystrybucji — kolejność jak na fakturze Enea
        attrs["fixed_network_netto"] = est.fixed_network_netto
        attrs["fixed_capacity_netto"] = est.fixed_capacity_netto
        for zone_display in est.kwh_by_zone:
            safe = zone_display.lower().translate(_TRANSL).replace(" ", "_")
            attrs[f"variable_network_{safe}_netto"] = est.variable_network_by_zone_netto.get(zone_display, 0.0)
        for zone_display in est.kwh_by_zone:
            safe = zone_display.lower().translate(_TRANSL).replace(" ", "_")
            attrs[f"quality_{safe}_netto"] = est.quality_by_zone_netto.get(zone_display, 0.0)
        for zone_display in est.kwh_by_zone:
            safe = zone_display.lower().translate(_TRANSL).replace(" ", "_")
            attrs[f"oze_{safe}_netto"] = est.oze_by_zone_netto.get(zone_display, 0.0)
        for zone_display in est.kwh_by_zone:
            safe = zone_display.lower().translate(_TRANSL).replace(" ", "_")
            attrs[f"cogeneration_{safe}_netto"] = est.cogeneration_by_zone_netto.get(zone_display, 0.0)
        attrs["fixed_subscription_netto"] = est.fixed_subscription_netto
        attrs["distribution_netto"] = est.distribution_netto
        # Podsumowanie
        attrs["total_netto"] = est.total_netto
        return attrs

