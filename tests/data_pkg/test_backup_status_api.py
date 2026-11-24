# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/backup-status`` — X1-A2.

Pins, through the ASGI app (the router mounted the way server.py mounts it):
  * absence is null at HTTP 200 — no state dir, a missing file, a malformed
    file or a naive timestamp never becomes a fabricated time;
  * the two fields read the host scripts' status files, normalised to UTC;
  * the route is bearer-gated like its siblings;
  * server.py wires it; the module imports in the slim registry image.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.registry.api import API_TOKEN_ENV
from legba.data.registry.backup_status_api import (
    STATE_DIR_ENV,
    build_backup_status_router,
)

_ROUTE = "/api/v1/v3/system/backup-status"
_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def _app() -> FastAPI:
    app = FastAPI()
    # The route reads host status files only; it never touches the deps.
    app.include_router(build_backup_status_router(None), prefix="/api/v1/v3")  # type: ignore[arg-type]
    return app


async def _get(headers: dict | None = None):
    async with AsyncClient(transport=ASGITransport(app=_app()),
                           base_url="http://testserver") as c:
        return await c.get(_ROUTE, headers=headers or {})


@pytest.mark.asyncio
async def test_absent_state_dir_is_null_not_zero(tmp_path, monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "never-created"))
    r = await _get()
    assert r.status_code == 200, r.text
    assert r.json() == {"last_backup_at": None, "last_drill_at": None}


@pytest.mark.asyncio
async def test_reads_both_status_files(tmp_path, monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path))
    (tmp_path / "last_backup.json").write_text(json.dumps(
        {"last_backup_at": "2026-10-08T21:51:12+00:00", "set_id": "20261008T215109Z"}))
    (tmp_path / "last_drill.json").write_text(json.dumps(
        {"last_drill_at": "2026-10-08T16:00:00-07:00"}))
    r = await _get()
    assert r.status_code == 200
    assert r.json() == {"last_backup_at": "2026-10-08T21:51:12+00:00",
                        "last_drill_at": "2026-10-08T23:00:00+00:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "{not json",
    json.dumps({"last_backup_at": 1728424272}),
    json.dumps({"last_backup_at": "2026-10-08T21:51:12"}),  # naive: no zone
    json.dumps(["2026-10-08T21:51:12Z"]),
])
async def test_malformed_backup_file_reads_as_absent(tmp_path, monkeypatch, body):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path))
    (tmp_path / "last_backup.json").write_text(body)
    r = await _get()
    assert r.status_code == 200
    assert r.json()["last_backup_at"] is None


@pytest.mark.asyncio
async def test_bearer_gated(tmp_path, monkeypatch):
    monkeypatch.setenv(API_TOKEN_ENV, "x1-a2-test-token")
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path))
    assert (await _get()).status_code == 401
    ok = await _get({"Authorization": "Bearer x1-a2-test-token"})
    assert ok.status_code == 200


def test_server_wires_the_router():
    src = (_SRC / "legba" / "data" / "registry" / "server.py").read_text()
    assert "build_backup_status_router(deps), prefix=\"/api/v1/v3\"" in src


def test_backup_status_route_imports_without_the_runtime_stack() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.backup_status_api as api
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, leaked
        assert api.build_backup_status_router is not None
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(_SRC), os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
