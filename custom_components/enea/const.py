"""Constants for the Enea Energy Meter integration."""
from datetime import timedelta
from enum import IntEnum

from homeassistant.const import Platform
from homeassistant.util import dt as dt_util

# ---------------------------------------------------------------------------
# Integration identity
# ---------------------------------------------------------------------------

DOMAIN = "enea"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DATE]
DEFAULT_NAME = "Enea"

# ---------------------------------------------------------------------------
# API URLs
# ---------------------------------------------------------------------------

ISSUE_TRACKER_URL = "https://github.com/PanSzelescik/home-assistant-enea/issues"
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
# The same endpoint with the data source of a prosumer's meter as one more segment —
# Portal Odbiorcy Enea joins the query's values with "/" in this order.
URL_CONSUMPTION_RANGE_DATA_SOURCE = URL_CONSUMPTION_RANGE + "/{data_source}"

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
# Set once a prosumer's energy history has been imported from balanced data.
CONF_BALANCED_HISTORY = "balanced_history"

# ---------------------------------------------------------------------------
# Sensor keys (must match translation files)
# ---------------------------------------------------------------------------

SENSOR_KEY_TARIFF = "tariff"
SENSOR_KEY_CAPACITY = "capacity"
SENSOR_KEY_STATUS = "status"
SENSOR_KEY_ADDRESS = "address"
SENSOR_KEY_READING_DATE = "reading_date"
SENSOR_KEY_METER_MODEL = "meter_model"
SENSOR_KEY_HAN_WMBUS = "han_wmbus"
SENSOR_KEY_HAN_P1 = "han_p1"
SENSOR_KEY_SWITCH_STATE = "switch_state"
SENSOR_KEY_BILLING_PERIOD_START = "billing_period_start"
SENSOR_KEY_PHASES = "phases"
SENSOR_KEY_STATISTICS_UNTIL = "statistics_until"

BINARY_SENSOR_KEY_TRANSMISSION = "transmission"
BINARY_SENSOR_KEY_HAN_AVAILABLE = "han_available"

# Stany portu HAN (pola wmbusStatus / p1Status z dashboardu PPE) — mapowanie jak w
# ikonkach Portalu Odbiorcy Enea: null/0 = nieaktywny, 1/2/3 jak niżej; gdy licznik
# nie obsługuje portu HAN (hanAvailable = false), Portal Odbiorcy Enea pokazuje osobny
# komunikat.
HAN_STATE_INACTIVE = "inactive"
HAN_STATE_NOT_SUPPORTED = "not_supported"
HAN_STATE_BY_CODE: dict[int, str] = {
    1: "active",
    2: "in_progress",
    3: "waiting_for_meter",
}
HAN_STATES: list[str] = [
    HAN_STATE_INACTIVE,
    *HAN_STATE_BY_CODE.values(),
    HAN_STATE_NOT_SUPPORTED,
]

# Stan członu wykonawczego (przekaźnika zdalnego odłączenia) — pole switchState z
# dashboardu PPE.  Kody odpowiadają klasom CSS ikonki w Portalu Odbiorcy Enea
# (switch-state--off/removed/warning/on); znaczenie słowne jest wywnioskowane z nich.
SWITCH_STATE_BY_CODE: dict[int, str] = {
    0: "off",
    1: "removed",
    2: "warning",
    3: "on",
}

# Liczba faz instalacji — Portal Odbiorcy Enea jej nie podaje, więc jest wnioskowana.
# Najpierw z modelu aktywnego licznika (tylko modele o pewnej liczbie faz), a gdy model
# jest nieznany — z mocy umownej: przyłącze jednofazowe kończy się w praktyce na ok.
# 9,2 kW (40 A × 230 V), więc próg ma zapas.  Niska moc niczego nie przesądza.
PHASES_SINGLE = "single_phase"
PHASES_THREE = "three_phase"
PHASES_BY_METER_MODEL: dict[str, str] = {
    "OTUS1": PHASES_SINGLE,
    "OTUS3": PHASES_THREE,
    "MT174": PHASES_THREE,
}
PHASES_THREE_MIN_CAPACITY_KW = 12
PHASES_SOURCE_METER_MODEL = "meter_model"
PHASES_SOURCE_CAPACITY = "contractual_capacity"
PHASES_COUNT: dict[str, int] = {PHASES_SINGLE: 1, PHASES_THREE: 3}

# Repairs — klucze zgłoszeń (muszą pasować do sekcji "issues" w tłumaczeniach)
ISSUE_PHASES_MISMATCH = "phases_mismatch"
ISSUE_BILLING_MONTHS_MISMATCH = "billing_months_mismatch"
ISSUE_ANNUAL_KWH_MISMATCH = "annual_kwh_mismatch"
# enea_prices jest zainstalowana i zna grupę taryfową licznika, ale nie ma dla niej wpisu.
ISSUE_PRICES_NOT_CONFIGURED = "prices_not_configured"
# Klucze wpisu enea_prices, które poprawia przycisk „Napraw” zgłoszeń o niezgodnej instalacji.
ENEA_PRICES_CONF_TARIFF = "tariff"
ENEA_PRICES_CONF_PHASES = "phases"
ENEA_PRICES_CONF_BILLING_MONTHS = "billing_months"
ENEA_PRICES_CONF_ANNUAL_KWH = "annual_kwh"
ISSUE_UNKNOWN_METER_MODEL = "unknown_meter_model"
# Formularz GitHub (.github/ISSUE_TEMPLATE) do zgłoszenia nowego modelu; {lang} = pl / en
ISSUE_TEMPLATE_NEW_METER_MODEL = "new_meter_model_{lang}.yml"

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


class DataSource(IntEnum):
    """Data source of a prosumer's meter — "Dane przed / po bilansowaniu" in the portal."""

    BEFORE_BALANCING = 1
    AFTER_BALANCING = 2


# PPE type (field "type" of /user/ppes and of the dashboard) of a prosumer, whose
# invoice is settled from the balanced data.  Portal Odbiorcy Enea offers that data
# only for energy; for power it falls back to the data before balancing.
PPE_TYPE_PROSUMER = 2
BALANCED_MEASUREMENT_TYPES = frozenset(
    {MeasurementType.ENERGY_CONSUMED, MeasurementType.ENERGY_RETURNED}
)

BACKFILL_MAX_CONSECUTIVE_EMPTY = 7  # stop after this many consecutive days with no data
RANGE_FETCH_CHUNK_DAYS = 180  # max days per single range request (~6 months)
MISSING_DAY_GRACE_DAYS = 3  # days to keep waiting for a late day before zero-filling it
# The same for balanced data, whose publication delay is not known yet — kept long so
# that a slow balancing is not stored as zero consumption.
MISSING_DAY_GRACE_DAYS_BALANCED = 14

EPOCH = dt_util.utc_from_timestamp(0)
"""Lower bound for a statistics lookup that must not miss anything, however old."""

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

VAT_RATE = 0.23

# Strefa z enea_prices → nazwa strefy.  Służy do nazw statystyk kosztów, a w billing.py
# także do odnalezienia statystyki „Energia pobrana – {nazwa}”, której nazwę nadaje
# portal Enei – dlatego musi się z nią zgadzać co do znaku.
COST_ZONE_DISPLAY: dict[str, str] = {
    "day": "Dzień",
    "night": "Noc",
    "peak": "Szczyt",
    "off_peak": "Poza szczytem",
    # G12sezON i G13active (od 2026).  Nazwy TYMCZASOWE – nie widzieliśmy jeszcze, jak
    # portal nazywa te strefy; do potwierdzenia z danymi licznika w tej grupie (issue #6
    # w enea_prices).  Niezgodność zgłasza ostrzeżenie w billing.py.
    "recommended_use": "Zalecany pobór",
    "remaining": "Pozostałe godziny",
    "recommended_limit": "Zalecane ograniczanie",
}

# Bill estimate entity keys
BILL_KEY_PREV_READING = "bill_prev_reading"
BILL_KEY_LAST_READING = "bill_last_reading"
BILL_KEY_PREVIOUS = "bill_previous"
BILL_KEY_CURRENT = "bill_current"

# billingWeekData z dashboardu PPE przeplata segmenty dzienne z segmentami obejmującymi
# cały okres rozliczeniowy; początek tych drugich to granica okresu na fakturze.  Próg
# odróżnia je od segmentów dziennych także w dniu zmiany czasu (doba 25 h).
BILLING_PERIOD_MIN_SEGMENT = timedelta(days=2)

# ---------------------------------------------------------------------------
# Installation — the enea_prices settings worked out from the meter data
# ---------------------------------------------------------------------------

# Długości okresu rozliczeniowego, dla których taryfa ma stawkę abonamentową (miesiące).
BILLING_PERIOD_MONTHS = (1, 2, 6, 12)
AVERAGE_MONTH_DAYS = 30.44
# Przedziały rocznego zużycia opłaty mocowej (pkt 3.1.29 taryfy Enea Operator, art. 89b
# ust. 3 ustawy o rynku mocy): poniżej 500, od 500 do 1200, powyżej 1200 do 2800,
# powyżej 2800 kWh.  Tu górne granice trzech pierwszych (druga i trzecia włącznie).
CAPACITY_BRACKET_LIMITS_KWH = (500, 1200, 2800)
CAPACITY_BRACKET_LABELS = ("< 500 kWh", "500–1200 kWh", "1200–2800 kWh", "> 2800 kWh")
# Skąd wzięto wartość — raport diagnostyczny i (dalej) teksty podpowiedzi.
INSTALLATION_SOURCE_BILLING_PERIODS = "billing_periods"
INSTALLATION_SOURCE_READING_DATES = "reading_dates"
INSTALLATION_SOURCE_LAST_365_DAYS = "last_365_days"
# Odczyt doliczony od ostatniej granicy z billingWeekData co długość okresu — okno
# portalu kończy się wcześniej niż dziś, więc ostatniego odczytu zwykle nie pokazuje.
INSTALLATION_SOURCE_BILLING_CYCLE = "billing_cycle"
# Nowe przyłącze: statystyki zaczynają się razem z licznikiem — najwyżej tyle dni po
# montażu (pierwsze dni portal bywa publikuje z opóźnieniem).
NEW_CONNECTION_STATISTICS_SLACK = timedelta(days=7)
