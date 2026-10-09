"""Repair issues raised from the meter data of the Portal Odbiorcy Enea.

Two issues are kept in sync with every coordinator refresh:

- the installation phases inferred from the meter disagree with the phases set
  in the enea_prices integration, which skews the fixed network fee of the
  bill estimate;
- the active meter model is not in PHASES_BY_METER_MODEL, so the user is asked
  to report it on GitHub and get it added.

Each issue is created while its condition holds and deleted as soon as it does
not, so fixing the setting (enea_prices reloads Enea entries) clears it.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .billing import find_prices_config
from .connector import get_active_meter, infer_phases
from .const import (
    DOMAIN,
    ISSUE_PHASES_MISMATCH,
    ISSUE_TEMPLATE_NEW_METER_MODEL,
    ISSUE_TRACKER_URL,
    ISSUE_UNKNOWN_METER_MODEL,
    PHASES_BY_METER_MODEL,
    PHASES_COUNT,
)


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
    tariff_name: str | None,
    data: dict[str, Any],
) -> None:
    """Create or delete the repair issues for one meter from fresh dashboard data."""
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

    phases, _ = infer_phases(data)
    cfg = find_prices_config(hass, tariff_name)
    mismatch_id = _issue_id(ISSUE_PHASES_MISMATCH, entry_id)
    if phases is not None and cfg is not None and PHASES_COUNT[phases] != cfg.phases:
        ir.async_create_issue(
            hass,
            DOMAIN,
            mismatch_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PHASES_MISMATCH,
            translation_placeholders={
                "meter_code": meter_code,
                "model": model or "?",
                "capacity": str(data.get("agreementPower") or "?"),
                "detected": str(PHASES_COUNT[phases]),
                "configured": str(cfg.phases),
            },
            learn_more_url=ISSUE_TRACKER_URL,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, mismatch_id)


def async_delete_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every repair issue of a meter whose config entry is removed."""
    for key in (ISSUE_PHASES_MISMATCH, ISSUE_UNKNOWN_METER_MODEL):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(key, entry_id))
