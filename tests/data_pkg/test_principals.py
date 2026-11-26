# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Principal handles (data model): the kinds are the type registry's,
handles are validated before SQL, and every server-stamped audit actor maps
to exactly one principal — never a guessed one. Pure: no database."""

from __future__ import annotations

import pytest

from legba.data import principals as P
from legba.data.type_registry import default_registry

_UID = "0b6c2f0e-1111-4c2a-9d3e-000000000001"


def test_kinds_are_the_type_registry_family():
    assert set(P.declared_kinds()) == {"user", "service", "agent", "rule", "shared"}
    for decl in default_registry().types("principal_kind"):
        P.validate_handle(decl.attr("example"))


@pytest.mark.parametrize("actor, handle", [
    (f"user:{_UID}", f"user:{_UID}"),
    (f"user:{_UID.upper()}", f"user:{_UID}"),
    ("service:runtime", "service:runtime"),
    ("service:worker", "service:worker"),
    ("service:bearer:0123456789ab", P.SERVICE_FLEET),
    ("service", P.SERVICE_FLEET),
    ("operator:bearer:0123456789ab", P.SHARED_ADMIN_TOKEN),
    ("admin-token:bearer:0123456789ab", P.SHARED_ADMIN_TOKEN),
    ("operator", P.SHARED_ADMIN_TOKEN),
    ("anonymous", P.SHARED_DEV),
    ("bearer:0123456789ab", P.SHARED_DEV),
])
def test_server_stamped_actors_map_to_one_principal(actor, handle):
    assert P.handle_for_actor(actor) == handle


@pytest.mark.parametrize("actor", [
    None, "", "  ", "unresolved", "Lewis", "someone@example.org", "user:not-a-uuid",
    "consult_write:6f1c", "system:health-loop", "analyst::desk_x", "service:Bad Name",
])
def test_anything_else_names_no_writer(actor):
    """A by-hand name a request body claimed, or an internal label, is never
    turned into a principal."""
    assert P.handle_for_actor(actor) is None


def test_with_sign_in_off_every_person_is_the_shared_credential_never_a_person():
    # The two labels the gates stamp with the switch off.
    assert P.handle_for_actor("operator:bearer:aaaaaaaaaaaa") == "shared:admin-token"
    assert P.parse_handle(P.SHARED_ADMIN_TOKEN)[0] == "shared"


def test_validate_refuses_undeclared_kinds_and_malformed_names():
    for bad in ("operator:me", "person:x", "user:abc", "agent:no_version", "rule:", "rule: x",
                "norule", "rule:" + "x" * 400):
        with pytest.raises(P.PrincipalError):
            P.validate_handle(bad)


def test_builders():
    assert P.user_handle(_UID) == f"user:{_UID}"
    assert P.service_handle("registry") == "service:registry"
    assert P.rule_handle("collection_gap") == "rule:collection_gap"
    assert P.seat_handle("desk_northaven", "3f9a0c") == "agent:desk_northaven@3f9a0c"
    # A hand-built context without a version names no writer.
    assert P.seat_handle("desk_northaven", "") is None
    assert P.seat_handle(None, "3f9a0c") is None


def test_an_output_names_its_rule_or_its_seat():
    # A declared deterministic handler (its id is its analyst id) is a rule;
    # any other analyst is a seat at its version.
    assert P.output_writer_handle("collection_gap", "3f9a0c") == "rule:collection_gap"
    assert P.output_writer_handle("desk_northaven", "3f9a0c") == "agent:desk_northaven@3f9a0c"
    assert P.output_writer_handle("desk_northaven", None) is None
    assert P.output_writer_handle(None, "3f9a0c") is None


def test_the_module_is_a_leaf():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(P.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            assert node.module in (None, "type_registry"), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in {"re", "typing", "uuid", "__future__"}
