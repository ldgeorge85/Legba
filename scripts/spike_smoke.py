# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5a spike — registry HTTP smoke test (gate 3 acceptance helper).

Boots the FastAPI registry app in-process via the ASGI lifespan, hits
/api/v1/registry/openapi.json + /healthz, and prints PASS/FAIL.

Usage:
    python3 scripts/spike_smoke_test.py
"""

from __future__ import annotations

import asyncio
import sys

from httpx import ASGITransport, AsyncClient


async def main() -> int:
    from legba.data.registry.server import API_PREFIX, create_app

    app = create_app(enable_healthcheck_loop=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # The lifespan handler isn't automatically triggered by ASGITransport
        # in this minimal mode; we trigger it manually.
        async with app.router.lifespan_context(app):
            r1 = await client.get(f"{API_PREFIX}/healthz")
            r2 = await client.get(f"{API_PREFIX}/openapi.json")

    print(f"healthz: {r1.status_code} {r1.json() if r1.headers.get('content-type','').startswith('application/json') else r1.text}")
    print(f"openapi: {r2.status_code} (body size {len(r2.text)} bytes)")
    if r1.status_code == 200 and r2.status_code == 200:
        print("SPIKE GATE 3: PASS")
        return 0
    print("SPIKE GATE 3: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
