# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The nested citation group ``[[ref:a] [ref:b]]``.

A consolidation on 11-13 wrote 7 of its 18 refs as one outer bracket pair
around several single-bracket refs. REFLECT's ``[[ref:<uuid>]]`` regex matched
none of them, so they never reached ``claims`` / ``cited_substrate_refs``.
"""
from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from legba.data import journal_evidence as je
from legba.data.analysts.journal_reflect import _reflect_claims

A, B, C, D = (str(uuid4()) for _ in range(4))


class _Conn:
    def __init__(self, known=()) -> None:
        self.known = {str(k).lower() for k in known}

    async def fetch(self, sql, *args):
        if "FROM signals" in sql and "WHERE id = ANY" in sql:
            return [{"id": UUID(str(k))} for k in args[0] if str(k).lower() in self.known]
        return []


@pytest.mark.parametrize("group", [
    f"[[ref:{A}] [ref:{B}]]",
    f"[[ref:{A}], [ref:{B}]]",
    f"[[ref:{A}][ref:{B}]]",
    f"[[ref:{A}]; [ref:{B}]]",
    f"[[ref: {A} ] [ref: {B} ]]",
])
def test_marker_refs_reads_every_member_of_a_nested_group(group):
    assert je.marker_refs(f"The strait closed {group}.") == [A, B]


def test_marker_refs_keeps_text_order_across_both_forms():
    body = f"x [[ref:{C}]] y [[ref:{A}] [ref:{B}] [ref:{D}]] z"
    assert je.marker_refs(body) == [C, A, B, D]


def test_a_bare_single_bracket_ref_is_not_a_marker():
    # ``[ref:<uuid>]`` alone is an INLINE form the gate counts, not a REFLECT marker.
    assert je.marker_refs(f"see [ref:{A}] here") == []
    assert je.extract_inline_refs(f"see [ref:{A}] here") == [A]


def test_reflect_folds_nested_refs_into_claims_and_cited_refs():
    body = (f"Shipping through Hormuz halted [[ref:{A}] [ref:{B}]].\n\n"
            f"Insurers repriced war risk [[ref:{C}]].")
    claims, flat, flags = _reflect_claims(body)
    assert [str(u) for u in flat] == [A, B, C]
    assert [str(u) for u in claims[0].refs] == [A, B]
    assert claims[0].kind == "fact" and "uncited_factual_span" not in flags


def test_collect_refs_carries_nested_refs_and_the_gate_resolves_them():
    body = ("The consolidation reads plainly. " * 30
            + f"Tanker traffic resumed [[ref:{A}] [ref:{B}]] and rates fell [[ref:{C}]].")
    refs = je.collect_refs([], body)
    assert refs == [A, B, C]
    resolved = asyncio.run(je.resolve_ref_existence(_Conn([A, B, C]), refs))
    v = je.evaluate_publish_gate(body=body, refs=refs, resolved=resolved,
                                 entry_kind="consolidation")
    assert not v.withhold and (v.refs_resolved, v.refs_total) == (3, 3)


def test_invented_ids_inside_a_nested_group_still_fail():
    fake = [f"{i:08x}-9c30-3{i:03x}-8611-a5e7b8199260" for i in range(1, 6)]
    group = "[[" + " ".join(f"[ref:{u}]" for u in fake)[1:] + "]"
    body = "Prose. " * 100 + group
    refs = je.collect_refs([], body)
    assert set(fake) <= set(refs)
    resolved = asyncio.run(je.resolve_ref_existence(_Conn([]), refs))
    v = je.evaluate_publish_gate(body=body, refs=refs, resolved=resolved,
                                 entry_kind="consolidation")
    assert v.withhold and v.reason == "refs_unresolved"
