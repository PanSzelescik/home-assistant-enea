"""Constants for the Enea Energy Meter integration."""
from datetime import timedelta

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

CONST_PORTAL_URL = "https://portalodbiorcy.operator.enea.pl"
CONST_BASE_URL = f"{CONST_PORTAL_URL}/portalOdbiorcy/api"
CONST_URL_LOGIN = f"{CONST_BASE_URL}/auth/login"
CONST_URL_PPES = f"{CONST_BASE_URL}/user/ppes"
CONST_URL_PPE_DASHBOARD = f"{CONST_BASE_URL}/consumptionDashboard/ppe/{{meter_id}}"

# endpoint /consumption/{meter_id}/1/{date}/{measurement_type}/{resolution}
CONST_URL_CONSUMPTION = (
    f"{CONST_BASE_URL}"
    "/consumption/{meter_id}/1/{date}/{measurement_type}/{resolution}"
)

# ---------------------------------------------------------------------------
# Config entry keys
# ---------------------------------------------------------------------------

CONF_METER_ID = "meter_id"
CONF_METER_NAME = "meter_name"
CONF_TARIFF = "tariff"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_BACKFILL_DAYS = "backfill_days"
CONF_FETCH_CONSUMPTION = "fetch_consumption"
CONF_FETCH_GENERATION = "fetch_generation"

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

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_UPDATE_INTERVAL_DICT: dict[str, int] = {"hours": 8, "minutes": 30, "seconds": 0}
DEFAULT_BACKFILL_DAYS = 30
MIN_UPDATE_INTERVAL_MINUTES = 30
METERS_CACHE_TTL = timedelta(minutes=5)

# ---------------------------------------------------------------------------
# Statistics API — measurement types and resolution
# ---------------------------------------------------------------------------

MEASUREMENT_ID_CONSUMPTION = 1
MEASUREMENT_ID_GENERATION = 2

STATS_ENERGY_CONSUMED = 1
STATS_ENERGY_RETURNED = 5
STATS_POWER_CONSUMED = 4
STATS_POWER_RETURNED = 9

STATS_RESOLUTION_15MIN = 1
STATS_RESOLUTION_60MIN = 2

BACKFILL_DAYS_MAX = 0  # sentinel: fetch as far back as data is available
BACKFILL_MAX_CONSECUTIVE_EMPTY = 7  # stop after this many consecutive days with no data

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
