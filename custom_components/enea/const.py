"""Constants for the Enea Energy Meter integration."""
from datetime import timedelta
from enum import IntEnum

from homeassistant.const import Platform

# ---------------------------------------------------------------------------
# Integration identity
# ---------------------------------------------------------------------------

DOMAIN = "enea"
PLATFORMS = [Platform.SENSOR]
DEFAULT_NAME = "Enea"

# ---------------------------------------------------------------------------
# API URLs
# ---------------------------------------------------------------------------

PORTAL_URL = "https://portalodbiorcy.operator.enea.pl"
BASE_URL = f"{PORTAL_URL}/portalOdbiorcy/api"
URL_LOGIN = f"{BASE_URL}/auth/login"
URL_PPES = f"{BASE_URL}/user/ppes"
URL_PPE_DASHBOARD = f"{BASE_URL}/consumptionDashboard/ppe/{{meter_id}}"

# endpoint /consumption/{meter_id}/{start_date}/{end_date}/{measurement_type}/{resolution}
URL_CONSUMPTION_RANGE = (
    f"{BASE_URL}"
    "/consumption/{meter_id}/{start_date}/{end_date}/{measurement_type}/{resolution}"
)

# ---------------------------------------------------------------------------
# Config entry keys
# ---------------------------------------------------------------------------

CONF_METER_ID = "meter_id"
CONF_METER_NAME = "meter_name"
CONF_TARIFF = "tariff"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_FETCH_CONSUMPTION = "fetch_consumption"
CONF_FETCH_GENERATION = "fetch_generation"
CONF_FETCH_POWER_CONSUMPTION = "fetch_power_consumption"
CONF_FETCH_POWER_GENERATION = "fetch_power_generation"

# ---------------------------------------------------------------------------
# Sensor keys (must match translation files)
# ---------------------------------------------------------------------------

SENSOR_KEY_TARIFF = "tariff"
SENSOR_KEY_CAPACITY = "capacity"
SENSOR_KEY_STATUS = "status"
SENSOR_KEY_ADDRESS = "address"
SENSOR_KEY_READING_DATE = "reading_date"
SENSOR_KEY_METER_MODEL = "meter_model"

# ---------------------------------------------------------------------------
# Config flow — error and abort reason keys (must match translation files)
# ---------------------------------------------------------------------------

ERROR_INVALID_AUTH = "invalid_auth"
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_UNKNOWN = "unknown"
ERROR_AT_LEAST_ONE_FETCH_TYPE = "at_least_one_fetch_type"
ERROR_INTERVAL_TOO_SHORT = "interval_too_short"

ABORT_REAUTH_SUCCESSFUL = "reauth_successful"
ABORT_RECONFIGURE_SUCCESSFUL = "reconfigure_successful"

SERVICE_REFRESH = "refresh"
SERVICE_BACKFILL = "backfill"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_UPDATE_INTERVAL_DICT: dict[str, int] = {"hours": 3, "minutes": 30, "seconds": 0}
MIN_UPDATE_INTERVAL_MINUTES = 30
METERS_CACHE_TTL = timedelta(minutes=5)

# ---------------------------------------------------------------------------
# Statistics API — measurement types and resolution
# ---------------------------------------------------------------------------

MEASUREMENT_ID_CONSUMPTION = 1

class MeasurementType(IntEnum):
    """API measurement type identifiers."""

    ENERGY_CONSUMED = 1
    ENERGY_RETURNED = 5
    POWER_CONSUMED = 4
    POWER_RETURNED = 9


class Resolution(IntEnum):
    """API resolution codes (1 = 15-minute slots, 2 = 60-minute slots)."""

    MIN_15 = 1
    MIN_60 = 2

BACKFILL_MAX_CONSECUTIVE_EMPTY = 7  # stop after this many consecutive days with no data
RANGE_FETCH_CHUNK_DAYS = 180  # max days per single range request (~6 months)
RANGE_SLOTS_PER_DAY = 24  # hourly slots per day in resolution=2 responses

STAT_KEY_ENERGY_CONSUMED = "energy_consumed"
STAT_KEY_ENERGY_RETURNED = "energy_returned"
STAT_KEY_POWER_CONSUMED = "power_consumed"
STAT_KEY_POWER_RETURNED = "power_returned"

STAT_NAME_BY_KEY: dict[str, str] = {
    STAT_KEY_ENERGY_CONSUMED: "Energia pobrana",
    STAT_KEY_ENERGY_RETURNED: "Energia oddana",
    STAT_KEY_POWER_CONSUMED: "Moc pobrana",
    STAT_KEY_POWER_RETURNED: "Moc oddana",
}

# ---------------------------------------------------------------------------
# Costs (optional — requires enea_prices integration with matching tariff)
# ---------------------------------------------------------------------------

ENEA_PRICES_DOMAIN = "enea_prices"

UNIT_COST = "PLN"

COST_ZONE_DISPLAY: dict[str, str] = {
    "day": "Dzień",
    "night": "Noc",
    "peak": "Szczyt",
    "off_peak": "Poza szczytem",
}
