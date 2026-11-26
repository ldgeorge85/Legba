# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-out from the phone app answers ``Clear-Site-Data: "storage"``.

The phone app (``/m/``) sends ``X-Legba-Clear-Device: phone`` on its sign-out;
only then does the registry ask the browser to clear the origin's storage
(which drops the phone app's shell cache and its service worker). A desktop
sign-out, which sends no such header, is unchanged: storage is per origin and
the desktop's saved layouts live there.

No database: the route is mounted on its own with a store that refuses to be
touched, and the requests carry no session cookie (the revoke path is the
X3-B3 suite's, ``test_users_signin_b3.py``).
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from legba.data.registry import users_api


class _NoPg:
    def acquire(self):  # pragma: no cover - reaching it is the failure
        raise AssertionError("sign-out without a cookie must not touch the store")


def _app() -> FastAPI:
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=_NoPg()))
    app = FastAPI()
    app.include_router(users_api.build_users_router(deps), prefix="/api/v1")
    return app


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw})


def test_clear_site_data_only_when_the_phone_asks() -> None:
    assert users_api.clear_site_data_for(_request({"X-Legba-Clear-Device": "phone"})) == '"storage"'
    assert users_api.clear_site_data_for(_request({"X-Legba-Clear-Device": " Phone "})) == '"storage"'
    assert users_api.clear_site_data_for(_request({})) is None
    assert users_api.clear_site_data_for(_request({"X-Legba-Clear-Device": "desktop"})) is None
    assert users_api.clear_site_data_for(_request({"X-Legba-Clear-Device": ""})) is None


def test_the_value_is_storage_only() -> None:
    # Not "cookies" (reaches every subdomain; the session cookie is already
    # expired by Set-Cookie) and not "cache" (partial in Chrome, hangs).
    assert users_api.CLEAR_SITE_DATA_VALUE == '"storage"'


@pytest.mark.asyncio
async def test_sign_out_route_sends_the_header_for_the_phone_only() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
        r = await c.post("/api/v1/auth/sign-out", headers={"X-Legba-Clear-Device": "phone"})
        assert r.status_code == 200, r.text
        assert r.headers.get("clear-site-data") == '"storage"'
        assert r.json() == {"signed_out": True, "session_revoked": False}
        assert r.headers.get("set-cookie", "").startswith("legba_session=")  # still cleared

        r = await c.post("/api/v1/auth/sign-out")
        assert r.status_code == 200, r.text
        assert "clear-site-data" not in r.headers
