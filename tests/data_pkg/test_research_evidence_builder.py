# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-A — the research signal BUILDER, unit-tested with no DB and no provider.

Everything here is a pure function over the row a research hit becomes. The
DB-backed proofs (the write, the archive depths, the slice reachability, the
GATHER citation path) live in ``agency/test_research_web_evidence_e2e.py`` and
``test_research_slice_and_gather.py``; this file pins the invariants that must
hold BEFORE a row ever reaches Postgres:

  * the ceiling is ``MASS_FLOOR`` — not a number that happens to equal it;
  * the source id is synthetic, per-PROVIDER, and unregistered;
  * the licence gate's three depths, including the fail-safe on a class nobody
    taught it;
  * the row satisfies every NOT NULL the live ``signals`` DDL carries;
  * the exclusion predicate is BUILT from the origin prefix constant;
  * the flag defaults to ``off`` and an unrecognised value reads as ``off``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from legba.data import research_evidence as rev
from legba.data.research_flag import (
    RESEARCH_DEFAULT_MODE,
    RESEARCH_DESKS,
    RESEARCH_EVIDENCE_ENV,
    RESEARCH_OFF,
    RESEARCH_SLICE_EXCLUSION_SQL,
    RESEARCH_SUBSTRATE,
    research_evidence_mode,
    research_reaches_desks,
    research_writes_enabled,
)
from legba.data.retrieval_origin import WEB_SEARCH_PREFIX, is_web_retrieved


def _hit(**over):
    base = {
        "url": "https://example.org/story",
        "title": "Mali fuel embargo tightens",
        "snippet": "JNIM blockades the Bamako corridors.",
        "text": "JNIM blockades the Bamako corridors.",
        "published_at": "2026-09-04T06:00:00Z",
        "language": "en",
        "engine": "duckduckgo",
        "rank": 3,
        "query": "mali fuel embargo",
    }
    base.update(over)
    return rev.ResearchHit(**base)


def _provider(**over):
    base = {
        "component_id": "search.searxng.local",
        "subprovider": "searxng",
        "route_class": "configured",
        "version": "deadbeefdeadbeef",
    }
    base.update(over)
    return rev.ProviderContext(**base)


# ---------------------------------------------------------------------------
# 1) THE CEILING — asserted on the ARITHMETIC, never on the literal 0.50
# ---------------------------------------------------------------------------


def test_ceiling_is_mass_floor_itself_not_a_coincidence():
    """The cap must BE ``signal_salience.MASS_FLOOR``.

    The whole ceiling argument is that ``cited_mass = Σ max(0, m − MASS_FLOOR)``
    makes a capped signal contribute exactly zero. If MASS_FLOOR moves and the
    cap does not, the ceiling silently stops meaning anything — a research
    signal would start contributing mass to the assembly's ordering with no
    test failing. So they are pinned EQUAL, with the reason in the message.
    """
    from legba.data.analysts.signal_salience import MASS_FLOOR

    assert rev.RESEARCH_MAGNITUDE_CAP == MASS_FLOOR, (
        "the research ceiling is MASS_FLOOR by construction: cited_mass sums "
        "max(0, m - MASS_FLOOR), so a capped research signal contributes "
        "EXACTLY zero mass and can never crown the Morning Read. If MASS_FLOOR "
        "moved, move the ceiling with it — do not decouple them."
    )
    assert rev.RESEARCH_CREDIBILITY_CAP == MASS_FLOOR


def test_capped_salience_contributes_zero_cited_mass():
    """The property, through the REAL ``cited_mass.v1`` arithmetic."""
    from legba.data.analysts.signal_salience import salience_over_ids

    sid = str(uuid4())
    sal = rev.research_salience()
    block = salience_over_ids([sid], {sid: sal["magnitude"]})
    assert block["cited_mass"] == 0.0
    # …and it is still READ and COUNTED — the ceiling bounds influence, not
    # visibility. A desk may quote it; it just cannot be ordered up by it.
    assert block["n_cited_scored"] == 1
    assert block["cited_max"] == rev.RESEARCH_MAGNITUDE_CAP
    assert block["n_above"] == 0


def test_capped_salience_sorts_below_an_ordinary_signal():
    from legba.data.analysts.signal_salience import salience_sort_key

    research = salience_sort_key(rev.research_salience())
    ordinary = salience_sort_key(
        {"magnitude": 0.62, "authority": "reporting"}
    )
    assert research < ordinary
    # authority rank 0 — the honest floor the unregistered source yields.
    assert rev.research_salience()["authority"] == "unknown"


def test_credibility_cap_never_exceeds_the_floor():
    assert rev.capped_credibility(0.95) == rev.RESEARCH_CREDIBILITY_CAP
    assert rev.capped_credibility(0.20) == 0.20
    # An unscored host gets the CAP, not NULL: downstream, NULL reads as "no
    # opinion" rather than "unreviewed open web".
    assert rev.capped_credibility(None) == rev.RESEARCH_CREDIBILITY_CAP


def test_stamped_salience_is_invisible_to_the_llm_scorer():
    """The scorer selects ``WHERE s.salience IS NULL``. A stamped row is
    therefore structurally unreachable by it — which is what makes the ceiling
    un-liftable by a model rather than merely un-lifted."""
    from legba.data.analysts import signal_salience

    assert "s.salience IS NULL" in signal_salience._SELECT_BATCH_SQL
    assert rev.research_salience()["magnitude"] is not None


# ---------------------------------------------------------------------------
# 2) THE SOURCE — synthetic, per-provider, unregistered
# ---------------------------------------------------------------------------


def test_source_id_is_synthetic_per_provider_and_drops_the_family_prefix():
    assert rev.research_source_id("search.searxng.local") == (
        "source.research.searxng_local"
    )
    assert rev.research_source_id("search.brave.cloud") == "source.research.brave_cloud"
    # One id per PROVIDER — the same provider on two different queries is ONE
    # source, which is what preserves the free per-source dilution cap.
    a = rev.research_source_id("search.searxng.local")
    b = rev.research_source_id("search.searxng.local")
    assert a == b
    assert rev.is_research_source_id(a)
    assert not rev.is_research_source_id("source.bbc.world")


def test_unregistered_source_yields_authority_rank_zero():
    """The load-bearing claim behind NOT registering a descriptor.

    ``signal_salience`` LEFT JOINs ``source_descriptors`` for ``source_class``;
    no descriptor ⇒ NULL ⇒ ``unknown`` ⇒ rank 0. A REGISTERED descriptor would
    have to declare a class, whose schema default is ``reporting`` = rank 3 —
    a lie about an unreviewed domain set.
    """
    from legba.data.analysts.signal_salience import AUTHORITY_RANK, _authority_for
    from legba.data.schemas.source import SourceScope

    assert _authority_for(None) == "unknown"
    assert AUTHORITY_RANK["unknown"] == 0
    # The alternative, stated so the choice is legible: the schema default.
    assert SourceScope.model_fields["source_class"].default == "reporting"
    assert AUTHORITY_RANK["reporting"] == 3


def test_synthetic_source_ids_resolve_to_a_display_name():
    """The obligation the unregistered id creates: every read surface that
    renders a source_id must get a NAME, not a raw synthetic id that reads as
    a bug."""
    assert rev.research_source_display_name("source.research.searxng_local") == (
        "Web research (searxng.local)"
    )
    assert rev.research_source_display_name("source.bbc.world") is None
    assert rev.research_source_display_name(None) is None


# ---------------------------------------------------------------------------
# 3) THE ROW ID — deterministic, run-scoped, idempotent
# ---------------------------------------------------------------------------


def test_row_id_is_deterministic_per_provider_url_and_run():
    run = uuid4()
    a = rev.research_signal_id(
        provider_component_id="search.searxng.local",
        canonical_url="https://x/y", run_id=run,
    )
    b = rev.research_signal_id(
        provider_component_id="search.searxng.local",
        canonical_url="https://x/y", run_id=run,
    )
    assert a == b and isinstance(a, UUID)
    # A DIFFERENT run re-fetching the same URL is a genuinely new observation
    # with its own fetched_at — the dedup plane folds those, not this id.
    assert a != rev.research_signal_id(
        provider_component_id="search.searxng.local",
        canonical_url="https://x/y", run_id=uuid4(),
    )
    assert a != rev.research_signal_id(
        provider_component_id="search.searxng.local",
        canonical_url="https://x/z", run_id=run,
    )


# ---------------------------------------------------------------------------
# 4) THE LICENCE GATE — three depths, and the fail-safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "license_class,depth",
    [
        (None, rev.DEPTH_TEASER),
        ("unknown", rev.DEPTH_TEASER),
        ("permissive_feed_unreviewed", rev.DEPTH_TEASER),
        ("cc_by", rev.DEPTH_FULL_TEXT),
        ("public_domain", rev.DEPTH_FULL_TEXT),
        ("api_terms", rev.DEPTH_FULL_TEXT),
        ("anti_ai_walled", rev.DEPTH_FORBIDDEN),
        ("tos_restrictive", rev.DEPTH_FORBIDDEN),
        ("personal_use_only", rev.DEPTH_FORBIDDEN),
    ],
)
def test_depth_for_license(license_class, depth):
    assert rev.depth_for_license(license_class)[0] == depth


def test_unknown_license_value_fails_SAFE_to_teaser():
    """A licence class nobody taught the gate must never widen retention."""
    assert rev.depth_for_license("some_new_2027_licence")[0] == rev.DEPTH_TEASER


def test_forbid_set_is_the_archivers_own_never_forked():
    from legba.data.analysts.deterministic_handlers.evidence_archiver import (
        FORBID_RETENTION_CLASSES,
    )

    assert rev.forbidden_license_classes() is FORBID_RETENTION_CLASSES


def test_cleared_and_forbidden_sets_are_disjoint():
    assert not (rev.CLEARED_LICENSE_CLASSES & rev.forbidden_license_classes())


# ---------------------------------------------------------------------------
# 5) THE ROW — every live NOT NULL satisfied, and the honest fields
# ---------------------------------------------------------------------------

#: The 19 NOT NULL columns on the live ``signals`` DDL that a writer must
#: supply a value for (the rest carry a server default this row does not
#: override). Pinned here so a schema change that adds an obligation fails HERE
#: rather than as an IntegrityError inside a GATHER round.
_REQUIRED = (
    "id", "source_id", "source_version", "produced_by_kind", "fetched_at",
    "owner_tenant", "modality", "retention_class", "payload", "raw_provenance",
    "geo", "tags", "entity_classes", "content_hash", "derived_from",
    "schema_uri",
)


def _row(**over):
    dispatch = over.pop("dispatch", rev.ResearchDispatch())
    hit = over.pop("hit", _hit())
    return rev.build_research_row(
        hit, _provider(), dispatch,
        run_id=over.pop("run_id", uuid4()),
        requested_by=over.pop("requested_by", "analyst::corpus_researcher"),
        regime=over.pop("regime", RESEARCH_SUBSTRATE),
        fetched_at=over.pop("fetched_at", datetime(2026, 9, 5, tzinfo=timezone.utc)),
    )


def test_row_satisfies_every_not_null():
    row = _row()
    for col in _REQUIRED:
        assert row[col] is not None, f"{col} is NOT NULL on the live signals DDL"


def test_row_carries_the_retrieval_origin_label_the_program_exists_to_write():
    row = _row()
    assert row["retrieval_origin"] == f"{WEB_SEARCH_PREFIX}search.searxng.local"
    assert is_web_retrieved(row["retrieval_origin"])
    # …and the payload mirror, which ``resolve_retrieval_origin`` reads second.
    assert row["payload"]["retrieval_origin"] == row["retrieval_origin"]


def test_row_is_produced_by_research_and_starts_reference_only():
    row = _row()
    assert row["produced_by_kind"] == "research"
    # An unreviewed teaser must NEVER claim evidence_hold — only the archiver's
    # stamp raises it, and only when bytes actually landed.
    assert row["retention_class"] == "reference_only"
    assert row["object_ref"] is None
    assert row["derived_from"] == []


def test_geo_comes_from_the_dispatching_target_and_is_the_ONLY_desk_reach():
    with_target = _row(
        dispatch=rev.ResearchDispatch(
            kind="open_question", target_id="country_watch_il", geo=("IL",),
            tags=("news",), hypothesis_id=str(uuid4()),
        )
    )
    assert with_target["geo"] == ["IL"]
    assert with_target["tags"] == ["research", "news"]
    # A SELF-SELECTED run carries no geo, so it matches no desk's `geo &&`
    # predicate and reaches no desk at all. Stated, not papered over.
    assert _row()["geo"] == []


def test_teaser_payload_never_carries_full_extracted_text():
    """THE ANTI-LAUNDERING ASSERTION — the one a reviewer should check first.

    Storing the content while declaring the bytes withheld is worse than not
    archiving. At teaser depth ``payload.text`` IS the provider snippet, byte
    for byte, and nothing else.
    """
    snippet = "The engine's one-line snippet."
    row = _row(hit=_hit(
        snippet=snippet, text=snippet,
        depth=rev.DEPTH_TEASER, depth_reason=rev.REASON_LICENSE_UNREVIEWED,
    ))
    assert row["payload"]["text"] == snippet
    assert row["payload"]["summary"] == snippet
    assert row["payload"]["research"]["depth"] == rev.DEPTH_TEASER


def test_summary_is_always_the_provider_snippet_never_synthesized():
    row = _row(hit=_hit(snippet="ENGINE SNIPPET", text="FULL ARTICLE BODY"))
    assert row["payload"]["summary"] == "ENGINE SNIPPET"
    assert row["payload"]["text"] == "FULL ARTICLE BODY"


def test_published_at_is_never_guessed():
    row = _row(hit=_hit(published_at=None))
    assert row["payload"]["published_at"] is None


def test_provenance_names_the_question_and_the_run():
    run = uuid4()
    hyp = str(uuid4())
    row = _row(
        run_id=run,
        dispatch=rev.ResearchDispatch(
            kind="open_question", hypothesis_id=hyp,
            target_id="country_watch_il", bounded_question="How likely is X?",
            geo=("IL",),
        ),
    )
    prov = row["raw_provenance"]
    assert prov["kind"] == "research"
    assert prov["fetch_kind"] == "web_evidence"
    assert prov["run_id"] == str(run)
    assert prov["requested_by"] == "analyst::corpus_researcher"
    assert prov["provider_component_id"] == "search.searxng.local"
    assert prov["dispatch"]["hypothesis_id"] == hyp
    assert prov["dispatch"]["target_id"] == "country_watch_il"
    assert prov["dispatch"]["bounded_question"] == "How likely is X?"
    assert prov["query"] == "mali fuel embargo"


def test_payload_records_the_ceiling_so_a_later_lift_is_auditable():
    row = _row()
    ceiling = row["payload"]["research"]["ceiling"]
    assert ceiling["applied"] is True
    assert ceiling["magnitude_cap"] == rev.RESEARCH_MAGNITUDE_CAP
    assert ceiling["reason"] == rev.RESEARCH_CEILING_REASON
    assert row["payload"]["research"]["corroboration"]["state"] == "pending"
    assert row["payload"]["research"]["schema"] == rev.RESEARCH_PAYLOAD_SCHEMA


def test_payload_carries_the_regime_so_readers_can_resplit_across_cutover():
    assert _row(regime=RESEARCH_DESKS)["payload"]["research"]["regime"] == "desks"


def test_novelty_mirrors_into_raw_provenance_without_disagreeing():
    """R-D reads the payload block; the task's brief asks for the two booleans
    on ``raw_provenance``. Both are written by ONE function from ONE probe, so
    they cannot drift."""
    novelty = {
        "version": rev.RESEARCH_NOVELTY_VERSION,
        "scope": rev.NOVELTY_SCOPE_TARGET,
        "url_in_slice": False,
        "content_hash_in_slice": True,
        "novel": False,
        "slice_size": 118,
    }
    row = _row(hit=_hit(novelty=novelty))
    assert row["payload"]["research"]["novelty"] == novelty
    mirror = row["raw_provenance"]["novelty"]
    assert mirror["novel"] is False
    assert mirror["url_in_slice"] is False
    assert mirror["content_hash_in_slice"] is True
    assert mirror["scope"] == rev.NOVELTY_SCOPE_TARGET


def test_content_hash_is_over_the_stored_text_and_empty_is_never_a_key():
    row = _row(hit=_hit(text="body text"))
    assert row["content_hash"] == rev.content_hash_for("body text")
    assert len(row["content_hash"]) == 64
    # The schema default "no hash" — the S-4 dedup path refuses it as a key
    # (`content_hash <> ''`), and fabricating one over a title would make two
    # unrelated teasers look like the same content.
    assert _row(hit=_hit(text=""))["content_hash"] == ""


def test_row_id_matches_the_deterministic_helper():
    run = uuid4()
    row = _row(run_id=run)
    assert row["id"] == rev.research_signal_id(
        provider_component_id="search.searxng.local",
        canonical_url="https://example.org/story", run_id=run,
    )


# ---------------------------------------------------------------------------
# 6) THE FLAG + THE EXCLUSION PREDICATE
# ---------------------------------------------------------------------------


def test_flag_defaults_to_off_and_an_unknown_value_reads_as_off(monkeypatch):
    monkeypatch.delenv(RESEARCH_EVIDENCE_ENV, raising=False)
    assert research_evidence_mode() == RESEARCH_DEFAULT_MODE == RESEARCH_OFF
    assert research_writes_enabled() is False
    assert research_reaches_desks() is False
    # A TYPO'D flag must never silently open the write path.
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, "desk")
    assert research_evidence_mode() == RESEARCH_OFF
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, "DESKS")
    assert research_evidence_mode() == RESEARCH_DESKS


def test_flag_rungs(monkeypatch):
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_SUBSTRATE)
    assert research_writes_enabled() is True
    assert research_reaches_desks() is False   # the poisoning-rollback rung
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_DESKS)
    assert research_writes_enabled() is True
    assert research_reaches_desks() is True


def test_exclusion_sql_is_built_from_the_origin_prefix_constant():
    """Not an inlined literal: the clause, ``web_search_origin()``'s output and
    ``is_web_retrieved``'s test must be the same string or the flag silently
    stops excluding anything."""
    assert WEB_SEARCH_PREFIX in RESEARCH_SLICE_EXCLUSION_SQL
    assert RESEARCH_SLICE_EXCLUSION_SQL == (
        "(retrieval_origin IS NULL OR retrieval_origin NOT LIKE "
        f"'{WEB_SEARCH_PREFIX}%')"
    )


def test_slice_reader_appends_the_clause_from_the_constant(monkeypatch):
    """The slice module must USE the constant, not a copy of its text."""
    import inspect

    from legba.runtime import actor_substrate_slice

    src = inspect.getsource(actor_substrate_slice._read_substrate_slice)
    assert "RESEARCH_SLICE_EXCLUSION_SQL" in src
    # …and the literal appears nowhere in the CODE (the comment above the
    # clause quotes it, which is the point of stripping comments here).
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "web_search:%" not in code, "import the constant, never the literal"
