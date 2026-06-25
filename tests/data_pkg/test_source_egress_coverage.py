# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Egress-coverage lint: every HTTP source fetcher MUST route through the
SSRF guard (``legba.data.sources._egress.guarded_async_client``), never a bare
``httpx.AsyncClient(...)``.

The egress guard (``_egress.py``) is only effective if it is the *single* way
a source handler constructs an HTTP client. A reviewer-found bypass class —
``scraper.py``'s BFS crawler following arbitrary discovered URLs with a raw
``httpx.AsyncClient`` — is exactly the guard's own threat model. This static
AST scan FAILS LOUD if any module under ``src/legba/data/sources/`` constructs
a bare ``httpx.AsyncClient(...)``; the sole exemption is ``_egress.py`` itself,
which legitimately wraps the bare client with the guarded transport.

AST (not a text grep) so that the guarded-client *docstring* in ``_egress.py``
and any ``httpx.AsyncClient`` used only as a type annotation do not register as
client construction — only an actual call node counts.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOURCES_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "legba"
    / "data"
    / "sources"
)

# Only ``_egress.py`` may construct the bare client (it installs the guarded
# transport on it). Everything else must go through ``guarded_async_client``.
_EXEMPT_FILES = {"_egress.py"}


def _bare_async_client_calls(path: Path) -> list[int]:
    """Line numbers of bare ``httpx.AsyncClient(...)`` *call* nodes in ``path``.

    Recognises both ``httpx.AsyncClient(...)`` (attribute access) and a
    directly-imported ``AsyncClient(...)``. Annotation-only references and the
    docstring mention are not call nodes, so they don't count.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # httpx.AsyncClient(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "AsyncClient"
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
        ):
            hits.append(node.lineno)
        # AsyncClient(...)  (from httpx import AsyncClient)
        elif isinstance(func, ast.Name) and func.id == "AsyncClient":
            hits.append(node.lineno)
    return hits


def _source_modules() -> list[Path]:
    return sorted(
        p
        for p in _SOURCES_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def test_sources_root_exists() -> None:
    # Guard against a silent no-op if the tree moves.
    assert _SOURCES_ROOT.is_dir(), f"sources package not found at {_SOURCES_ROOT}"
    assert _source_modules(), "no source modules discovered to scan"


def test_no_bare_httpx_async_client_in_sources() -> None:
    """No source handler may construct a bare ``httpx.AsyncClient`` — they
    must use ``guarded_async_client`` so the SSRF egress guard is in force."""
    offenders: dict[str, list[int]] = {}
    for path in _source_modules():
        if path.name in _EXEMPT_FILES:
            continue
        lines = _bare_async_client_calls(path)
        if lines:
            offenders[str(path.relative_to(_SOURCES_ROOT))] = lines
    assert not offenders, (
        "bare httpx.AsyncClient(...) construction bypasses the SSRF egress "
        "guard — route these through legba.data.sources._egress."
        f"guarded_async_client instead:\n{offenders}"
    )


def test_lint_catches_a_planted_bare_client(tmp_path: Path) -> None:
    """The scan must FLAG a planted bare client (so a real bypass can't slip
    past as a false negative)."""
    planted = tmp_path / "planted_source.py"
    planted.write_text(
        "import httpx\n"
        "async def fetch():\n"
        "    return httpx.AsyncClient(timeout=5.0)\n",
        encoding="utf-8",
    )
    assert _bare_async_client_calls(planted) == [3]


def test_lint_ignores_annotation_and_guarded_client(tmp_path: Path) -> None:
    """A type annotation and a guarded_async_client() call must NOT register
    as a bare-client construction (else the lint over-fires)."""
    clean = tmp_path / "clean_source.py"
    clean.write_text(
        "import httpx\n"
        "from ._egress import guarded_async_client\n"
        "async def get_client() -> httpx.AsyncClient:\n"
        "    return guarded_async_client(timeout=5.0)\n",
        encoding="utf-8",
    )
    assert _bare_async_client_calls(clean) == []


def test_guarded_client_installs_ssrf_transport() -> None:
    """Behavioural backstop to the static scan: the helper every handler is
    routed through installs the SSRF-guarded transport on its client."""
    from legba.data.sources._egress import SsrfGuardedTransport, guarded_async_client

    client = guarded_async_client(timeout=5.0)
    # ``httpx.AsyncClient`` keeps the mounted transport on ``_transport``; the
    # guard helper must have set the SSRF-guarded one.
    assert isinstance(client._transport, SsrfGuardedTransport), (  # noqa: SLF001
        "guarded_async_client did not install the SSRF-guarded transport"
    )
