"""Today and now come from Home Assistant's clock, never from the process's.

Home Assistant keeps its own time zone in dt_util and never sets the process's;
a Container or Core installation without TZ runs the process in UTC.  So
date.today() and datetime.now() are a day behind for the first hours after
Polish midnight, while dt_util.now() is right.  The Enea Ceny integration (enea_prices)
follows the same rule — both read the same "today" — and has the same test.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCES = sorted((Path(__file__).resolve().parents[1] / "custom_components" / "enea").glob("*.py"))
PROCESS_CLOCK = {"today", "now", "utcnow"}
"""Methods of date and datetime that read the process's clock."""


def _process_clock_reads(path: Path) -> list[str]:
    """Return 'line: call' for every date/datetime clock read in a module."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", None)
        if node.func.attr in PROCESS_CLOCK and name in {"date", "datetime"}:
            found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_no_process_clock(path: Path) -> None:
    """Use dt_util.now() / dt_util.now().date() instead."""
    assert _process_clock_reads(path) == []


def test_the_check_finds_a_process_clock_read(tmp_path: Path) -> None:
    """The check itself catches the calls it is there for."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import datetime\nfrom datetime import date\n"
        "a = date.today()\nb = datetime.datetime.now()\nc = dt_util.now()\n",
        encoding="utf-8",
    )
    assert _process_clock_reads(sample) == ["3: date.today()", "4: datetime.datetime.now()"]
