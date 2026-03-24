"""Testy jednostkowe dla statistics.py (czyste funkcje, bez HA)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from custom_components.enea.statistics import (
    _collect_series,
    get_statistic_id,
    has_data,
    time_id_to_dt,
)


# ---------------------------------------------------------------------------
# get_statistic_id
# ---------------------------------------------------------------------------


def test_get_statistic_id_format():
    sid = get_statistic_id("590310600000000001", "Energia pobrana")
    assert sid == "enea:590310600000000001_energia_pobrana"


def test_get_statistic_id_ze_strefą():
    sid = get_statistic_id("590310600000000001", "Energia pobrana – Dzień")
    assert sid.startswith("enea:")
    assert "590310600000000001" in sid
    # slugify zamienia "–" i spacje; ważne że wynik jest deterministyczny
    assert sid == get_statistic_id("590310600000000001", "Energia pobrana – Dzień")


# ---------------------------------------------------------------------------
# has_data
# ---------------------------------------------------------------------------


def test_has_data_zwraca_true_gdy_jest_wartość():
    api = {
        "values": [
            {"timeId": 1, "items": [{"tarifZoneId": 1, "value": 0.5}]},
        ]
    }
    assert has_data(api) is True


def test_has_data_zwraca_false_gdy_same_null():
    api = {
        "values": [
            {"timeId": 1, "items": [{"tarifZoneId": 1, "value": None}]},
            {"timeId": 2, "items": [{"tarifZoneId": 1, "value": None}]},
        ]
    }
    assert has_data(api) is False


def test_has_data_zero_traktowane_jako_dane():
    """Zero zużycie to poprawna wartość — has_data powinno zwrócić True."""
    api = {
        "values": [
            {"timeId": 1, "items": [{"tarifZoneId": 1, "value": 0.0}]},
        ]
    }
    assert has_data(api) is True


def test_has_data_pusta_lista_values():
    assert has_data({"values": []}) is False


def test_has_data_brak_klucza_values():
    assert has_data({}) is False


def test_has_data_mix_null_i_wartości():
    api = {
        "values": [
            {"timeId": 1, "items": [{"tarifZoneId": 1, "value": None}]},
            {"timeId": 2, "items": [{"tarifZoneId": 1, "value": 1.23}]},
        ]
    }
    assert has_data(api) is True


# ---------------------------------------------------------------------------
# time_id_to_dt
# ---------------------------------------------------------------------------


def test_time_id_to_dt_timeId_1_to_północ():
    dt = time_id_to_dt(date(2024, 6, 15), 1)
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.date() == date(2024, 6, 15)
    assert dt.tzinfo is not None


def test_time_id_to_dt_timeId_24_to_23():
    dt = time_id_to_dt(date(2024, 6, 15), 24)
    assert dt.hour == 23


def test_time_id_to_dt_środek_dnia():
    dt = time_id_to_dt(date(2024, 1, 1), 13)
    assert dt.hour == 12


def test_time_id_to_dt_kolejne_godziny_są_rosnące():
    day = date(2024, 3, 20)
    times = [time_id_to_dt(day, t) for t in range(1, 25)]
    for i in range(1, len(times)):
        assert times[i] > times[i - 1]


# ---------------------------------------------------------------------------
# _collect_series
# ---------------------------------------------------------------------------


def _make_api_day(zone_ids: list[int], value: float = 0.1) -> dict:
    """Pomocnik: buduje odpowiedź API dla jednego dnia."""
    return {
        "zones": [{"id": zid, "name": f"Strefa{zid}"} for zid in zone_ids],
        "values": [
            {
                "timeId": t,
                "items": [{"tarifZoneId": zid, "value": value} for zid in zone_ids],
            }
            for t in range(1, 25)
        ],
    }


def test_collect_series_tworzy_total_i_strefy():
    api = _make_api_day([1, 2], value=0.1)
    series: dict = {}
    _collect_series(api, date(2024, 1, 1), "pobrana", "Energia", series)

    assert "Energia pobrana" in series              # total
    assert "Energia pobrana – Strefa1" in series    # strefa 1
    assert "Energia pobrana – Strefa2" in series    # strefa 2


def test_collect_series_total_suma_stref():
    """Wartość totalu powinna być sumą wartości wszystkich stref."""
    api = _make_api_day([1, 2], value=0.1)
    series: dict = {}
    _collect_series(api, date(2024, 1, 1), "pobrana", "Energia", series)

    # Dla każdej godziny: 0.1 (strefa1) + 0.1 (strefa2) = 0.2
    totals = [v for _, v in series["Energia pobrana"]]
    assert all(abs(v - 0.2) < 1e-9 for v in totals)


def test_collect_series_24_wpisy():
    """Każda seria powinna mieć 24 wpisy (godziny doby)."""
    api = _make_api_day([1], value=0.5)
    series: dict = {}
    _collect_series(api, date(2024, 1, 1), "pobrana", "Energia", series)

    assert len(series["Energia pobrana"]) == 24


def test_collect_series_null_traktowany_jako_zero():
    """Wartość None z API jest traktowana jako 0 przy liczeniu sumy."""
    api = {
        "zones": [{"id": 1, "name": "Dzień"}],
        "values": [
            {"timeId": 1, "items": [{"tarifZoneId": 1, "value": None}]},
        ],
    }
    series: dict = {}
    _collect_series(api, date(2024, 1, 1), "pobrana", "Energia", series)
    # Total istnieje i ma wartość 0.0
    assert series["Energia pobrana"][0][1] == 0.0


def test_collect_series_akumuluje_wiele_dni():
    """Wywołanie dla kilku dni powinno dołączać do tej samej serii."""
    series: dict = {}
    for day_offset in range(3):
        api = _make_api_day([1], value=1.0)
        _collect_series(api, date(2024, 1, 1 + day_offset), "pobrana", "Energia", series)

    # 3 dni × 24 godziny = 72 wpisy
    assert len(series["Energia pobrana"]) == 72
