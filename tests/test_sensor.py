"""Testy jednostkowe dla pomocniczych funkcji sensor.py."""
from __future__ import annotations

from custom_components.enea.sensor import (
    _address_attrs,
    _get_reading_date,
    _meter_model_attrs,
)


# ---------------------------------------------------------------------------
# _get_reading_date
# ---------------------------------------------------------------------------


def test_get_reading_date_zwraca_datetime():
    data = {
        "currentValues": [
            {"measurementId": 1, "readingDate": 1741000000000},
        ]
    }
    dt = _get_reading_date(data)
    assert dt is not None
    assert dt.tzinfo is not None


def test_get_reading_date_brak_danych():
    assert _get_reading_date({}) is None
    assert _get_reading_date({"currentValues": []}) is None


def test_get_reading_date_ignoruje_inny_measurementId():
    data = {
        "currentValues": [
            {"measurementId": 2, "readingDate": 1741000000000},  # id=2, nie 1
        ]
    }
    assert _get_reading_date(data) is None


def test_get_reading_date_brak_readingDate():
    data = {"currentValues": [{"measurementId": 1}]}
    assert _get_reading_date(data) is None


# ---------------------------------------------------------------------------
# _address_attrs
# ---------------------------------------------------------------------------


def test_address_attrs_pełny_adres():
    data = {
        "address": {
            "street": "Pastelowa",
            "houseNum": "8",
            "apartmentNum": "3",
            "postCode": "60-198",
            "city": "Poznań",
            "district": "Grunwald",
            "parcelNum": None,
        }
    }
    attrs = _address_attrs(data)
    assert attrs["street"] == "Pastelowa"
    assert attrs["house_number"] == "8"
    assert attrs["apartment_number"] == "3"
    assert attrs["post_code"] == "60-198"
    assert attrs["city"] == "Poznań"
    assert attrs["district"] == "Grunwald"
    assert "parcel_number" not in attrs  # None → pominięty


def test_address_attrs_brak_address():
    assert _address_attrs({}) == {}
    assert _address_attrs({"address": None}) == {}


def test_address_attrs_pomija_none_wartości():
    data = {
        "address": {
            "street": "Lipowa",
            "houseNum": None,
            "city": "Warszawa",
        }
    }
    attrs = _address_attrs(data)
    assert "house_number" not in attrs
    assert attrs["street"] == "Lipowa"
    assert attrs["city"] == "Warszawa"


# ---------------------------------------------------------------------------
# _meter_model_attrs
# ---------------------------------------------------------------------------


def test_meter_model_attrs_zwraca_daty_iso():
    data = {
        "meters": [
            {
                "serialNumber": "NEW",
                "typeName": "OTUS3",
                "assemblyDate": 1700000000000,
                "disassemblyDate": None,
            }
        ]
    }
    attrs = _meter_model_attrs(data)
    assert "assembly_date" in attrs
    assert attrs["assembly_date"].endswith("+00:00") or attrs["assembly_date"].endswith("Z")
    assert "disassembly_date" not in attrs  # None → pominięty


def test_meter_model_attrs_brak_aktywnego_licznika():
    data = {
        "meters": [
            {"serialNumber": "OLD", "disassemblyDate": 1700000000000}
        ]
    }
    assert _meter_model_attrs(data) == {}


def test_meter_model_attrs_pusty():
    assert _meter_model_attrs({}) == {}
    assert _meter_model_attrs({"meters": []}) == {}
