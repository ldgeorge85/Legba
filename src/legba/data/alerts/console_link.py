# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The console address a page opens — never the JSON lineage route (roll 84).

A push's tap target (ntfy ``X-Click``) used to be the receipt,
``/api/v1/lineage/{kind}/{id}``: a JSON route, so tapping a page on a phone
showed raw JSON. The tap target is now the CONSOLE's address for the record:

* a kind the Record sheet's chain reads (``legba-ui-v3/src/lib/recordSheet.ts``
  ``CHAIN_KINDS``, mirrored by :data:`RECORD_SHEET_KINDS`) →
  ``<base>/#rec=<kind>:<id>`` — the sheet opens on that record
  (``readRecordAddress``); on a phone the redirect to ``/m/`` keeps the whole
  hash (``lib/phoneSurface.ts``);
* any other kind → ``<base>/#sel=<kind>:<id>``, the console's selection
  address (the record becomes the point), still a page, never JSON.

The base is ``LEGBA_PUBLIC_BASE_URL`` (the console's public origin). Unset ⇒
no absolute link (``None``): a relative console address is useless to a
phone, and the push then carries no tap target rather than a wrong one.

Stdlib-only leaf.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

#: Mirrors ``CHAIN_KINDS`` in ``legba-ui-v3/src/lib/recordSheet.ts`` (a test
#: holds the two equal).
RECORD_SHEET_KINDS: frozenset[str] = frozenset({
    "finding", "signal", "event", "situation", "fact", "hypothesis",
    "observation", "alert", "critique", "meta_finding",
})

ENV_PUBLIC_BASE_URL = "LEGBA_PUBLIC_BASE_URL"


def console_address(row_kind: str, row_id: Any) -> str | None:
    """The console hash for one record (``#rec=…`` or ``#sel=…``), or None."""
    if row_id is None or not str(row_id).strip() or not row_kind:
        return None
    key = "rec" if row_kind in RECORD_SHEET_KINDS else "sel"
    return f"#{key}={row_kind}:{quote(str(row_id), safe='')}"


def console_link(row_id: Any, *, row_kind: str, base_url: str | None = None) -> str | None:
    """The absolute console URL that opens one record, or None without a base."""
    address = console_address(row_kind, row_id)
    if address is None:
        return None
    base = (base_url if base_url is not None else os.environ.get(ENV_PUBLIC_BASE_URL, "")).strip()
    if not base:
        return None
    return base.rstrip("/") + "/" + address
