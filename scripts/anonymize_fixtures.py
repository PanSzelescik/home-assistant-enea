"""Turn raw Portal Odbiorcy Enea responses into anonymised test fixtures.

Usage::

    uv run python scripts/anonymize_fixtures.py [data] [tests/fixtures/portal]

The input directory holds responses saved from a real account (``data/`` is
git-ignored for that reason), named after the request:

* ``user_ppes.json`` — ``/user/ppes``
* ``consumptionDashboard_ppe_id.json`` — ``/consumptionDashboard/ppe/{id}``
* ``consumption_id_{from}_{to}_{type}_{resolution}[_{source}].json`` —
  ``/consumption/{id}/{from}/{to}/{type}/{resolution}[/{source}]``
* ``notification.json`` — ``POST /notification/`` (the request body names the
  meter id, the response only the PPE numbers in ``ppeCodes``)

Every value identifying the customer — the meter id, the PPE number, the
address, the serial numbers of the meters and the ids and numbers of the
agreements — is replaced with a made-up one.  The fixtures land in a public
repository that belongs to the person whose account they come from, so the
consumption itself is attributable as well: every energy and power value is
multiplied by SCALE.  That keeps the shape of the data (gaps, nulls, zeroes,
zones, daylight saving days) and hides the household's real usage.

Before writing, the script checks that none of the original identifiers is
left anywhere in the output and refuses to write if one is.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

SCALE = 0.83
"""Factor every energy and power value is multiplied by."""

FAKE_METER_ID = 12345
FAKE_PPE = "590310600000001234"
FAKE_ADDRESS = {
    "city": "Poznań",
    "district": None,
    "street": "Przykładowa",
    "postCode": "60-001",
    "houseNum": "1",
    "apartmentNum": None,
    "parcelNum": None,
}

SCALED_KEYS = {"value", "consumption", "consumptionForReport", "sumConsumption"}
"""Keys holding an energy or power figure, wherever they appear."""

RANGE_FILE = re.compile(
    r"consumption_id_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})_(\d+)_(\d+)(?:_(\d+))?\.json"
)


def _scale(value: Any) -> Any:
    """Multiply one figure by SCALE, keeping integers integral.

    Floats keep six decimals rather than the portal's three, so hourly values
    still add up to the portal's own monthly sums within a thousandth.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return round(value * SCALE)
    if isinstance(value, float):
        return round(value * SCALE, 6)
    return value


def scale_figures(node: Any) -> Any:
    """Return a copy of node with every figure under SCALED_KEYS scaled."""
    if isinstance(node, dict):
        return {
            key: _scale(value) if key in SCALED_KEYS and not isinstance(value, (dict, list))
            else scale_figures(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [scale_figures(item) for item in node]
    return node


def _identifiers(ppes: list[dict[str, Any]], dashboard: dict[str, Any] | None) -> set[str]:
    """Collect the original identifying values, as they would appear in JSON text."""
    found: set[str] = set()
    for ppe in ppes:
        found.update(str(ppe[key]) for key in ("id", "name", "code") if ppe.get(key))
    if dashboard:
        found.update(str(dashboard[key]) for key in ("id", "name", "code") if dashboard.get(key))
        found.update(
            str(value) for value in (dashboard.get("address") or {}).values()
            if isinstance(value, str) and len(value) > 2
        )
        for meter in dashboard.get("meters") or []:
            found.update(str(meter[key]) for key in ("id", "serialNumber") if meter.get(key))
        for agreement in dashboard.get("agreements") or []:
            found.update(
                str(agreement[key]) for key in ("id", "agreementNumber") if agreement.get(key)
            )
    return found


def anonymize_ppes(ppes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the identifiers of /user/ppes; there is one meter per fixture account."""
    return [
        {**ppe, "id": FAKE_METER_ID + index, "name": FAKE_PPE, "code": FAKE_PPE}
        for index, ppe in enumerate(ppes)
    ]


def anonymize_dashboard(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Replace the identifiers of a dashboard response and scale its figures."""
    result = scale_figures(dashboard)
    result.update(id=FAKE_METER_ID, name=FAKE_PPE, code=FAKE_PPE, address=dict(FAKE_ADDRESS))
    result["meters"] = [
        {**meter, "id": 1001 + index, "serialNumber": f"{90000001 + index}"}
        for index, meter in enumerate(result.get("meters") or [])
    ]
    result["agreements"] = [
        {**agreement, "id": 2001 + index, "agreementNumber": f"UMOWA/TEST/{index + 1:04d}"}
        for index, agreement in enumerate(result.get("agreements") or [])
    ]
    return result


def anonymize_notifications(
    messages: list[dict[str, Any]], ppes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace the account's PPE numbers among the codes a message is addressed to."""
    codes = {str(ppe[key]) for ppe in ppes for key in ("name", "code") if ppe.get(key)}

    def fake(value: str | None) -> str | None:
        """Return the comma-separated PPE codes with the account's replaced."""
        if not value:
            return value
        return ",".join(
            FAKE_PPE if code.strip() in codes else code for code in value.split(",")
        )

    return [{**message, "ppeCodes": fake(message.get("ppeCodes"))} for message in messages]


def _leaks(text: str, identifiers: set[str]) -> list[str]:
    """Return the original identifiers still present in text."""
    leaked = []
    for identifier in identifiers:
        pattern = rf"(?<!\d){re.escape(identifier)}(?!\d)" if identifier.isdigit() else re.escape(identifier)
        if re.search(pattern, text):
            leaked.append(identifier)
    return leaked


def main(source: Path, target: Path) -> int:
    """Anonymise every recognised response in source into target."""
    def load(name: str) -> Any:
        return json.loads((source / name).read_text(encoding="utf-8"))

    ppes = load("user_ppes.json") if (source / "user_ppes.json").exists() else []
    dashboard_name = "consumptionDashboard_ppe_id.json"
    dashboard = load(dashboard_name) if (source / dashboard_name).exists() else None
    identifiers = _identifiers(ppes, dashboard)

    outputs: dict[str, tuple[Any, bool]] = {}
    if ppes:
        outputs["user_ppes.json"] = (anonymize_ppes(ppes), True)
    if dashboard:
        outputs["dashboard.json"] = (anonymize_dashboard(dashboard), True)
    if (source / "notification.json").exists():
        outputs["notifications.json"] = (
            anonymize_notifications(load("notification.json"), ppes), True
        )
    for path in sorted(source.glob("consumption_id_*.json")):
        match = RANGE_FILE.fullmatch(path.name)
        if not match:
            print(f"skipped {path.name}: unrecognised name", file=sys.stderr)
            continue
        first, last, mtype, _, data_source = match.groups()
        name = f"range_{first}_{last}_{mtype}" + (f"_{data_source}" if data_source else "")
        outputs[f"{name}.json"] = (scale_figures(load(path.name)), False)

    texts = {
        name: json.dumps(data, ensure_ascii=False, indent=2 if pretty else None) + "\n"
        for name, (data, pretty) in outputs.items()
    }
    leaks = {name: found for name, text in texts.items() if (found := _leaks(text, identifiers))}
    if leaks:
        for name, found in leaks.items():
            print(f"{name}: {len(found)} original identifier(s) left", file=sys.stderr)
        return 1

    target.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (target / name).write_text(text, encoding="utf-8")
        print(f"wrote {target / name}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(main(
        Path(args[0] if args else "data"),
        Path(args[1] if len(args) > 1 else "tests/fixtures/portal"),
    ))
