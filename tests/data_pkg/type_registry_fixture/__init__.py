# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TEST-ONLY fixture: one new deterministic handler and one new alert kind,
added through the type registry exactly as a contributor would add them.

Three files here (the handler's code, its declaration, the alert kind's
declaration) plus the test that proves them
(``tests/data_pkg/test_type_registry_fixture_proof.py``). Nothing else in the
tree is edited to add them; the test builds a registry that includes these
modules the way an installed package's ``legba.types`` entry point would.
"""
