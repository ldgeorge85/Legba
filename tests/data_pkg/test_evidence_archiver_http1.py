# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The evidence archiver fetches over HTTP/1.1, by its own hand (2026-09-29).

Cloudflare's managed challenge keys on httpx's HTTP/2 fingerprint: after the
09-26 transport change (h2 on by default for every guarded fetch) dawn.com went
from 22-34 archived pages a day to zero and scmp.com followed. A six-variant
probe showed the User-Agent and the cipher pin change nothing and that h2 off
alone returns full pages. The archiver therefore names ``http2=False`` on its
own client; the module default (and the reference fetcher, which needs h2 for
its tarpitting CDN hosts) is untouched. Pinned on the call site's AST so a
refactor that drops the keyword fails here, not on the next Cloudflare host.
"""

from __future__ import annotations

import ast
import inspect

from legba.data.analysts.deterministic_handlers import evidence_archiver as A
from legba.data.sources import _egress as E


def _fetch_client_calls() -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(A))
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "fetch_client":
                out.append(node)
    return out


def test_the_archiver_names_http1_on_its_own_client():
    calls = _fetch_client_calls()
    assert calls, "the archiver no longer builds its client through fetch_client"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert "http2" in kw, "the archiver's fetch_client call must name http2"
        assert isinstance(kw["http2"], ast.Constant) and kw["http2"].value is False


def test_the_module_default_still_offers_h2(monkeypatch):
    """The archiver's choice is its own: a silent caller keeps ALPN h2."""
    monkeypatch.delenv(E.FETCH_HTTP2_ENV, raising=False)
    assert E.fetch_http2_default() is True

