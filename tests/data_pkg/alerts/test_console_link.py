# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Roll 84 — a page's tap target is the console's record address, never JSON."""
from __future__ import annotations

import pathlib
import re

from legba.data.alerts.console_link import (
    RECORD_SHEET_KINDS,
    console_address,
    console_link,
)

REPO = pathlib.Path(__file__).resolve().parents[3]
RECORD_SHEET_TS = REPO / "legba-ui-v3" / "src" / "lib" / "recordSheet.ts"


def test_record_kinds_mirror_the_console_chain_kinds() -> None:
    src = RECORD_SHEET_TS.read_text(encoding="utf-8")
    block = re.search(r"export const CHAIN_KINDS[^\[]*\[(.*?)\]", src, re.S)
    assert block, "CHAIN_KINDS not found in recordSheet.ts"
    ts_kinds = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert ts_kinds == set(RECORD_SHEET_KINDS)


def test_a_chain_kind_opens_the_record_sheet() -> None:
    assert console_link("ab-1", row_kind="finding", base_url="https://c.example.org/") == (
        "https://c.example.org/#rec=finding:ab-1"
    )
    assert console_link("42", row_kind="alert", base_url="https://c.example.org") == (
        "https://c.example.org/#rec=alert:42"
    )


def test_another_kind_gets_the_selection_address_not_json() -> None:
    url = console_link("x/y", row_kind="target", base_url="https://c.example.org")
    assert url == "https://c.example.org/#sel=target:x%2Fy"
    assert "/api/" not in url


def test_no_base_no_link_and_no_id_no_link(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_PUBLIC_BASE_URL", raising=False)
    assert console_link("ab-1", row_kind="finding") is None
    monkeypatch.setenv("LEGBA_PUBLIC_BASE_URL", "https://c.example.org")
    assert console_link("ab-1", row_kind="finding") == "https://c.example.org/#rec=finding:ab-1"
    assert console_link(None, row_kind="finding") is None
    assert console_address("finding", "  ") is None
