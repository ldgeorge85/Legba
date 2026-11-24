#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render docs/API_ROLES.md from the registry route-role tables (X2-B2, A-1).

The tables are ``ROUTE_ROLES`` in ``src/legba/data/registry/route_roles.py``
(the answer with the sign-in switch off) and ``ROUTE_MIN_ROLES`` in
``src/legba/data/registry/route_policy.py`` (every route's minimum role with
``LEGBA_AUTH_SIGNIN=1``); the doc is never edited by hand.
``tests/data_pkg/test_route_roles.py`` fails when the committed doc and the
tables drift.

    python3 scripts/gen_api_roles_doc.py          # (re)write the doc
    python3 scripts/gen_api_roles_doc.py --check  # exit 1 on drift OR on a
                                                  # mounted route with no
                                                  # declared minimum role

``--check`` builds the registry app (``server.create_app``; nothing is
connected — the lifespan never runs) and compares every mounted route with
``ROUTE_MIN_ROLES``: an undeclared route, or an entry the app no longer
mounts, fails the check. ``--skip-app`` checks the doc only (an environment
that cannot import the full registry).
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "API_ROLES.md"
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.registry import route_policy  # noqa: E402
from legba.data.registry.route_roles import (  # noqa: E402
    iter_mounted_routes,
    render_api_roles_md,
)


def mounted_routes() -> set[tuple[str, str]]:
    """Every ``(METHOD, template)`` the real registry app mounts — the route
    walk plus the OpenAPI spec (the same two readings the route-role test
    uses), with ``OPTIONS`` left out (the framework's)."""
    logging.disable(logging.WARNING)
    try:
        from legba.data.registry.server import create_app

        app = create_app(enable_vocabulary_subscription=False)
    finally:
        logging.disable(logging.NOTSET)
    out = {(m.upper(), p) for m, p in iter_mounted_routes(app)}
    out |= _websocket_routes(app.routes)
    out |= {
        (m.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for m in ops
        if m.upper() in route_policy.DECLARABLE_METHODS | {"HEAD"}
    }
    return {(m, p) for m, p in out if m != "OPTIONS"}


def _websocket_routes(routes, prefix: str = "") -> set[tuple[str, str]]:
    """``("WEBSOCKET", path)`` for every WebSocket route, including those
    inside an included router that FastAPI >= 0.140 keeps nested (its route
    walk does not yield them)."""
    out: set[tuple[str, str]] = set()
    for route in routes:
        if "websocket" in type(route).__name__.lower():
            out.add(("WEBSOCKET", prefix + getattr(route, "path", "")))
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            out |= _websocket_routes(
                inner.routes, prefix + (getattr(ctx, "prefix", "") or ""),
            )
    return out


def check_declared(mounted: set[tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    for method, path in route_policy.undeclared_routes(mounted):
        problems.append(f"undeclared route (no minimum role): {method} {path}")
    for method, path in route_policy.stale_entries(mounted):
        problems.append(f"ROUTE_MIN_ROLES entry the app does not mount: {method} {path}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when the committed doc differs or a "
                             "mounted route has no declared minimum role")
    parser.add_argument("--skip-app", action="store_true",
                        help="with --check: compare the doc only")
    args = parser.parse_args()
    rendered = render_api_roles_md()
    if args.check:
        rc = 0
        current = DOC_PATH.read_text() if DOC_PATH.exists() else ""
        if current != rendered:
            print(f"{DOC_PATH} is out of date — run scripts/gen_api_roles_doc.py",
                  file=sys.stderr)
            rc = 1
        else:
            print("docs/API_ROLES.md is current")
        if not args.skip_app:
            mounted = mounted_routes()
            problems = check_declared(mounted)
            for line in problems:
                print(line, file=sys.stderr)
            if problems:
                rc = 1
            else:
                # Count what the doc counts: a HEAD answered by a GET route
                # (the framework's openapi.json route mounts both) is that
                # GET's row, not a second route.
                declared = {route_policy.declared_key(m, p) for m, p in mounted}
                extra = len(mounted) - len(declared)
                print(f"every mounted route ({len(declared)}) has a declared "
                      "minimum role"
                      + (f" ({extra} HEAD read as its GET)" if extra else ""))
        return rc
    DOC_PATH.write_text(rendered)
    print(f"wrote {DOC_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
