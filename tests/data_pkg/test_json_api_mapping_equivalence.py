# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The json_api record-mapping additions are byte-neutral.

``title_template`` and ``geometry`` (lat/lon paths) are new, OPTIONAL json_api
config. A source that declares neither must emit exactly the signals it
emitted before the lane. This test pins that: for every json_api descriptor
shipped in ``descriptors/`` that does not use the new keys, a synthetic item
is built at each configured path, pulled through the real handler (httpx
mocked), and the emitted signals — every field except the per-run
``signal_id`` / ``fetched_at`` — are compared with a golden captured from the
handler at the lane's base commit (fea54616).

Regenerate ONLY when a deliberate, reviewed change to the mapping lands:
``LEGBA_REGEN_JSON_API_GOLDEN=1`` (and say why in the commit).
"""

from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import yaml

from legba.data.sources._contract import InMemoryStateStore, SourceContext
from legba.data.sources.json_api import (
    JsonApiConfig,
    JsonApiSourceHandler,
    parse_path,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "fixtures" / "ingestion" / "json_api_mapping_golden.json"
FROZEN_NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
_NEW_KEYS = {"title_template", "geometry"}
#: UKMTO adopts the new mapping in this lane (its change is the point).
_ADOPTERS = {"source_ukmto_incidents"}


def _descriptor_configs() -> dict[str, tuple[dict[str, Any], list[str], list[str]]]:
    out = {}
    for path in sorted((REPO / "descriptors").glob("source_*.yaml")):
        body = yaml.safe_load(path.read_text())
        if body.get("identity", {}).get("kind") != "json_api":
            continue
        cfg = {k: (v.get("raw") if isinstance(v, dict) and "raw" in v else v)
               for k, v in (body.get("config") or {}).items()}
        if _NEW_KEYS & set(cfg) or path.stem in _ADOPTERS:
            continue
        scope = body.get("scope") or {}
        out[path.stem] = (cfg, list(scope.get("geo") or []), list(scope.get("languages") or []))
    return out


def _put(item: dict[str, Any], dotted: str | None, value: Any) -> None:
    if not dotted:
        return
    segs = parse_path(dotted)
    node = item
    for seg in segs[:-1]:
        node = node.setdefault(seg, {})
    node[segs[-1]] = value


def _items(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for n, ts in enumerate(("2026-11-13T11:30:00Z", "2026-11-13T10:00:00+00:00",
                             "20261113T091500Z")):
        item: dict[str, Any] = {"extra": {"untouched": n, "place": "Strait of Hormuz"},
                                "latitude": 26.4, "longitude": 56.5}
        _put(item, cfg.get("id_path") or "id", f"id-{n}" if n != 1 else 1000 + n)
        _put(item, cfg.get("title_path"), f"  Title {n} — Attack  ")
        _put(item, cfg.get("url_path"), f"https://upstream.invalid/item/{n}")
        _put(item, cfg.get("timestamp_path"), ts)
        _put(item, cfg.get("body_path"), f"Body {n}: vessel reported an incident.")
        _put(item, cfg.get("geo_path"), "om")
        out.append(item)
    # One item with every mapped field missing (id falls back to a hash).
    out.append({"other": "only"})
    return out


def _doc(cfg: dict[str, Any], items: list[dict[str, Any]]) -> Any:
    items_path = cfg.get("items_path") or ""
    if not items_path:
        return items
    doc: dict[str, Any] = {}
    _put(doc, items_path, items)
    return doc


async def _pull(name: str, cfg: dict[str, Any], geo: list[str], langs: list[str]) -> list[dict]:
    config = JsonApiConfig(**cfg)
    body = json.dumps(_doc(cfg, _items(cfg)))

    def transport(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, request=req)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    handler = JsonApiSourceHandler(config, http_client=client)
    ctx = SourceContext(
        target_id="target.eq", target_version="v", source_id=f"source.{name}",
        config=config, state_store=InMemoryStateStore(), now_fn=lambda: FROZEN_NOW,
        scope_geo=geo, scope_languages=langs,
    )
    out = []
    async for sig in handler.pull(ctx, since=datetime(2026, 11, 1, tzinfo=timezone.utc)):
        dumped = sig.model_dump(mode="json", exclude={"signal_id", "fetched_at"})
        out.append(dumped)
    await handler.aclose()
    return out


@pytest.mark.asyncio
async def test_existing_json_api_sources_emit_byte_identical_signals() -> None:
    configs = _descriptor_configs()
    assert len(configs) >= 5, sorted(configs)
    got = {name: await _pull(name, *spec) for name, spec in configs.items()}
    rendered = json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False)
    if os.environ.get("LEGBA_REGEN_JSON_API_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(rendered + "\n", encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(got) == sorted(golden)
    for name in golden:
        assert got[name] == golden[name], name
    assert rendered + "\n" == GOLDEN.read_text(encoding="utf-8")
