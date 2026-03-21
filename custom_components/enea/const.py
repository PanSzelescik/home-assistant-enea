"""Constants for the Enea Energy Meter integration."""
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "enea"
PLATFORMS = [Platform.SENSOR]

CONST_BASE_URL = "https://portalodbiorcy.operator.enea.pl/portalOdbiorcy/api"
CONST_URL_LOGIN = f"{CONST_BASE_URL}/auth/login"
CONST_URL_PPES = f"{CONST_BASE_URL}/user/ppes"
CONST_URL_PPE_DASHBOARD = f"{CONST_BASE_URL}/consumptionDashboard/ppe/{{meter_id}}"

CONF_METER_ID = "meter_id"
CONF_METER_NAME = "meter_name"
CONF_TARIFF = "tariff"

CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL_DICT: dict[str, int] = {"hours": 8, "minutes": 30, "seconds": 0}
DEFAULT_UPDATE_INTERVAL = timedelta(hours=8, minutes=30)
DEFAULT_NAME = "Enea"

MEASUREMENT_ID_CONSUMPTION = 1
MEASUREMENT_ID_GENERATION = 2

# Statistics API — endpoint /consumption/{meter_id}/1/{date}/{measurement_type}/{resolution}
CONST_URL_CONSUMPTION = (
    f"{CONST_BASE_URL}"
    "/consumption/{meter_id}/1/{date}/{measurement_type}/{resolution}"
)

STATS_ENERGY_CONSUMED = 1
STATS_ENERGY_RETURNED = 5
STATS_POWER_CONSUMED = 4
STATS_POWER_RETURNED = 9

STATS_RESOLUTION_15MIN = 1
STATS_RESOLUTION_60MIN = 2

CONF_BACKFILL_DAYS = "backfill_days"
DEFAULT_BACKFILL_DAYS = 30
BACKFILL_DAYS_MAX = 0  # sentinel: fetch as far back as data is available
