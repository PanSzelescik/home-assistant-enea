"""Repair issues raised from the meter data of the Portal Odbiorcy Enea.

Each issue is created while its condition holds and deleted as soon as it does
not.  Three groups are kept in sync:

- with every dashboard refresh: the active meter model is not in
  PHASES_BY_METER_MODEL, so the user is asked to report it on GitHub;
- with every statistics step, once the installation has been worked out (see
  installation.py): a setting of the enea_prices integration — phases, billing
  period length, yearly consumption bracket — disagrees with what the meter
  shows.  These are fixable: the repair flow (repairs.py) writes the meter's
  value into the enea_prices entry, whose reload reloads the Enea entries and
  so clears the issue;
- with every statistics step as well: enea_prices is installed and prices the
  meter's tariff group, but has no entry for it.  Its fix opens the enea_prices
  config flow.  Without enea_prices installed nothing is raised — Repairs is
  no place to advertise an integration the user never chose.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_custom_components

from .billing import PricesConfig, find_prices_config, find_prices_entry
from .connector import get_active_meter
from .const import (
    CAPACITY_BRACKET_LABELS,
    DOMAIN,
    ENEA_PRICES_CONF_ANNUAL_KWH,
    ENEA_PRICES_CONF_BILLING_MONTHS,
    ENEA_PRICES_CONF_PHASES,
    ENEA_PRICES_DOMAIN,
    ISSUE_ANNUAL_KWH_MISMATCH,
    ISSUE_BILLING_MONTHS_MISMATCH,
    ISSUE_PHASES_MISMATCH,
    ISSUE_PRICES_NOT_CONFIGURED,
    ISSUE_TEMPLATE_NEW_METER_MODEL,
    ISSUE_TRACKER_URL,
    ISSUE_UNKNOWN_METER_MODEL,
    PHASES_BY_METER_MODEL,
)
from .installation import DetectedInstallation, capacity_bracket

INSTALLATION_ISSUES = (
    ISSUE_PHASES_MISMATCH,
    ISSUE_BILLING_MONTHS_MISMATCH,
    ISSUE_ANNUAL_KWH_MISMATCH,
)


@dataclass(frozen=True)
class _Mismatch:
    """A setting the meter shows otherwise, and what fixing it writes."""

    issue: str
    setting: str
    value: int
    placeholders: dict[str, str]


def _issue_id(key: str, entry_id: str) -> str:
    """Return the per-meter issue id.

    Built from the config entry id, not the PPE number or the portal's meter
    id: issue ids land in the diagnostics report, which users paste into
    public GitHub issues.
    """
    return f"{key}_{entry_id}"


def _new_meter_model_url(hass: HomeAssistant, model: str, capacity: Any) -> str:
    """Return a GitHub link opening the new meter model form, pre-filled.

    The Polish form is used when Home Assistant runs in Polish, the English one
    otherwise.  Only the model and the contractual capacity are filled in —
    nothing that identifies the user's connection point.
    """
    polish = (hass.config.language or "").startswith("pl")
    params = {
        "template": ISSUE_TEMPLATE_NEW_METER_MODEL.format(lang="pl" if polish else "en"),
        "title": f"{'Nowy model licznika' if polish else 'New meter model'}: {model}",
        "model": model,
    }
    if capacity is not None:
        params["capacity"] = str(capacity)
    return f"{ISSUE_TRACKER_URL}/new?{urlencode(params)}"


def async_update_issues(
    hass: HomeAssistant,
    entry_id: str,
    meter_code: str,
    data: dict[str, Any],
) -> None:
    """Create or delete the unknown meter model issue from fresh dashboard data."""
    model = ((get_active_meter(data) or {}).get("typeName") or "").strip()
    unknown_id = _issue_id(ISSUE_UNKNOWN_METER_MODEL, entry_id)
    if model and model.upper() not in PHASES_BY_METER_MODEL:
        report_url = _new_meter_model_url(hass, model, data.get("agreementPower"))
        ir.async_create_issue(
            hass,
            DOMAIN,
            unknown_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UNKNOWN_METER_MODEL,
            translation_placeholders={
                "model": model,
                "meter_code": meter_code,
                "report_url": report_url,
            },
            learn_more_url=report_url,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, unknown_id)


def _meters_in_tariff(hass: HomeAssistant, tariff_name: str) -> int:
    """Return how many Enea meters report a tariff group, matched like enea_prices does."""
    wanted = tariff_name.casefold()
    return sum(
        1
        for entry in hass.config_entries.async_entries(DOMAIN)
        if (getattr(getattr(entry, "runtime_data", None), "coordinator", None) is not None)
        and (entry.runtime_data.coordinator.tariff_name or "").casefold() == wanted
    )


def _mismatches(
    meter_code: str,
    data: dict[str, Any],
    detected: DetectedInstallation,
    cfg: PricesConfig,
) -> list[_Mismatch]:
    """Return the enea_prices settings the meter shows otherwise.

    The yearly consumption is compared by capacity fee bracket, the only thing
    it prices by; a consumption within the configured bracket is no mismatch.
    """
    found = []
    if detected.phases is not None and detected.phases != cfg.phases:
        model = ((get_active_meter(data) or {}).get("typeName") or "").strip()
        found.append(
            _Mismatch(
                ISSUE_PHASES_MISMATCH,
                ENEA_PRICES_CONF_PHASES,
                detected.phases,
                {
                    "meter_code": meter_code,
                    "model": model or "?",
                    "capacity": str(data.get("agreementPower") or "?"),
                    "detected": str(detected.phases),
                    "configured": str(cfg.phases),
                },
            )
        )
    if detected.billing_months is not None and detected.billing_months != cfg.billing_months:
        found.append(
            _Mismatch(
                ISSUE_BILLING_MONTHS_MISMATCH,
                ENEA_PRICES_CONF_BILLING_MONTHS,
                detected.billing_months,
                {
                    "meter_code": meter_code,
                    "detected": str(detected.billing_months),
                    "configured": str(cfg.billing_months),
                },
            )
        )
    if (
        detected.annual_kwh is not None
        and detected.annual_kwh_until is not None
        and capacity_bracket(detected.annual_kwh) != capacity_bracket(cfg.annual_kwh)
    ):
        found.append(
            _Mismatch(
                ISSUE_ANNUAL_KWH_MISMATCH,
                ENEA_PRICES_CONF_ANNUAL_KWH,
                round(detected.annual_kwh),
                {
                    "meter_code": meter_code,
                    "detected": str(round(detected.annual_kwh)),
                    "until": detected.annual_kwh_until.isoformat(),
                    "detected_bracket": CAPACITY_BRACKET_LABELS[
                        capacity_bracket(detected.annual_kwh)
                    ],
                    "configured_bracket": CAPACITY_BRACKET_LABELS[
                        capacity_bracket(cfg.annual_kwh)
                    ],
                },
            )
        )
    return found


def async_update_installation_issues(
    hass: HomeAssistant,
    entry_id: str,
    meter_code: str,
    tariff_name: str | None,
    data: dict[str, Any],
    detected: DetectedInstallation,
) -> None:
    """Create or delete the issues for enea_prices settings the meter shows otherwise.

    Two meters in one tariff group share an enea_prices entry, and their facts
    may well differ — fixing it for one would break it for the other, and
    the two issues would undo each other.  Then nothing is raised.
    """
    cfg = find_prices_config(hass, tariff_name)
    found: dict[str, _Mismatch] = {}
    if cfg is not None and tariff_name and _meters_in_tariff(hass, tariff_name) <= 1:
        found = {m.issue: m for m in _mismatches(meter_code, data, detected, cfg)}

    for key in INSTALLATION_ISSUES:
        issue_id = _issue_id(key, entry_id)
        mismatch = found.get(key)
        if mismatch is None:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders=mismatch.placeholders,
            data={
                "tariff": tariff_name,
                "setting": mismatch.setting,
                "value": mismatch.value,
            },
            learn_more_url=ISSUE_TRACKER_URL,
        )


async def async_prices_supports(hass: HomeAssistant, tariff_name: str | None) -> bool:
    """Return True when enea_prices is installed and prices the tariff group.

    Installed means among Home Assistant's custom integrations, whether set up
    or not.  Its tariff table is read by duck typing, like AKCYZA elsewhere:
    the package is imported (once; it is already in memory whenever it has an
    entry) and TARIFFS looked up in its tariffs module.  Anything short of a
    table to look the group up in counts as not supported.
    """
    if not tariff_name:
        return False
    integration = (await async_get_custom_components(hass)).get(ENEA_PRICES_DOMAIN)
    if integration is None:
        return False
    try:
        await integration.async_get_component()
    except ImportError:
        return False
    tariffs = getattr(sys.modules.get(f"{integration.pkg_path}.tariffs"), "TARIFFS", None)
    if not isinstance(tariffs, dict):
        return False
    wanted = tariff_name.casefold()
    return any(str(name).casefold() == wanted for name in tariffs)


async def async_update_prices_setup_issue(
    hass: HomeAssistant, entry_id: str, meter_code: str, tariff_name: str | None
) -> None:
    """Create or delete the issue asking to set enea_prices up for the meter's group.

    Run in the statistics step, after Home Assistant has started: during
    startup enea_prices may simply not be set up yet, and the issue would flash
    up on every restart.  An entry that failed to load still counts as set up.
    """
    issue_id = _issue_id(ISSUE_PRICES_NOT_CONFIGURED, entry_id)
    if find_prices_entry(hass, tariff_name) is not None or not await async_prices_supports(
        hass, tariff_name
    ):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_PRICES_NOT_CONFIGURED,
        translation_placeholders={"meter_code": meter_code, "tariff": tariff_name or "?"},
    )


def async_delete_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every repair issue of a meter whose config entry is removed."""
    for key in (*INSTALLATION_ISSUES, ISSUE_PRICES_NOT_CONFIGURED, ISSUE_UNKNOWN_METER_MODEL):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(key, entry_id))
