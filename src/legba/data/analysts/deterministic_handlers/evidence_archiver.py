# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``evidence_archiver`` sub-handler — NATIVE cited-evidence archival (P2-1).

Program §A3 (planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md): the receipt
chain today terminates in a ``canonical_url`` — a link that rots. This sweep
archives the ORIGINAL evidence behind CITED signals (not the firehose) so the
chain terminates in OUR verifiable copy:

  * **Selection — cited-only.** Signals referenced by the ``derived_from`` of
    VERIFIED findings (``kind='finding'``, not superseded, latest faithfulness
    critique exists AND ``LEAST(confidence, overall_score) >= verify_floor`` —
    the alert_trigger_scan / since_api verified bar) whose ``object_ref IS
    NULL`` and ``canonical_url`` is non-empty. Uncited signals are NEVER
    archived by this handler (bounded, moat-serving; the per-source depth
    lane is a separate concern).
  * **Fetch.** Original bytes over the SSRF egress guard
    (:func:`legba.data.sources._egress.fetch_client` — same guard as
    every source fetcher; an egress-blocked URL is terminal, it can never
    become fetchable). Streaming with a hard size cap; per-host politeness
    delay; bounded fetch budget per run; honest counters for every skip class.
  * **Store — content-addressed.** Bytes land at
    ``{LEGBA_ARCHIVE_ROOT}/{sha256[:2]}/{sha256}`` (write-to-temp + atomic
    rename; an existing object is a dedup hit, never rewritten). The recorded
    ``object_ref`` is the RELATIVE content address ``cas:sha256/<hex>`` — the
    root can move (or become MinIO/SeaweedFS later) without rewriting rows,
    and every read surface can derive ``archived`` + ``archive_sha256`` from
    the existing ``signals.object_ref`` column alone. The sha256 of the
    original bytes IS the receipt anchor: anyone holding our copy can re-hash
    it against the stamped value.
  * **Record.** Upsert into the ``evidence_archive`` sidecar (migration 0104
    — status / attempts / license verdict / media leg; see the migration
    header for the sidecar-vs-columns rationale) AND stamp the signal row in
    ONE update: ``object_ref`` (the cas address), ``retention_class`` upgraded
    to ``evidence_hold`` (the signals_retention purge already exempts it — an
    archived citation must never be TTL-purged out from under its archive),
    plus the corpus dirty-marker (below) when text extraction succeeded.
  * **Text extraction (bonus, S-10 rider).** Trafilatura main-text extraction
    over HTML/text objects → ``payload.archived_text``. The BYTES are the
    archive; extraction failure only means no corpus upgrade. On success the
    signal is re-queued for the OpenSearch corpus via the corpus_indexer
    DIRTY-MARKER CONTRACT (``SET indexed_at = NULL`` AND ``updated_at =
    now()`` in the SAME UPDATE — both load-bearing, see
    :mod:`.corpus_indexer`), so the archived FULL text replaces the thin
    teaser doc in ``legba_signals_corpus`` within a tick. This handler never
    touches OpenSearch directly — the index plane stays owned by
    corpus_indexer.
  * **Media leg.** ``media_ref`` bytes (when present and http/https) are
    fetched + stored the same way — NO processing (the process_media plane
    backfills extraction later). A media failure never fails the row; the
    canonical_url bytes are the primary archive.

**V-E1 — JS-WALL / BOT-CHECK / REDIRECT-INTERSTITIAL REJECTION.**
(planning/VERIFY_PATH_STRUCTURAL_FIXES_SPEC_2026-07-31.md §V-E, JUDGE_READOUT
§5: a stored ``archived_text`` reading "JavaScript is disabled in your
browser" — a Le Monde citation — grounded a judged claim.) A successful
Trafilatura extraction is not necessarily ARTICLE text — a no-JS fallback
page, a bot-check interstitial, or a link-shortener redirect notice all
"extract" cleanly; they are extraction FAILURES wearing the shape of success.
:func:`_match_wall_pattern` gates every extraction against a curated,
live-DB-seeded deny list (:data:`_WALL_DENY_PATTERNS`) — see its docstring for
the corpus audit that produced both the pattern list and the length cutoff
(:data:`_WALL_MAX_CHARS`). A match REJECTS the text exactly like a Trafilatura
failure: ``payload.archived_text`` is NOT written (the bytes archive is
untouched — only the derived-text upgrade is withheld, same discipline as the
R6b Telegram-widget skip below), counted separately
(``text_extract_rejected_boilerplate``, distinct from ``text_extract_failed``
so "Trafilatura found nothing" and "Trafilatura found a wall" stay
distinguishable) and logged at WARNING with the URL host (never the body —
hosts are low-cardinality and safe to log, article text is not).

**V-E2 — substance-floor marker (extraction-side half only).** Every
successful (non-rejected) extraction also stamps
``payload.archived_text_chars`` — the ``len()`` of the stored
``archived_text``, in the SAME update as the text itself — a free-to-compute
receipt of how much was actually extracted. This handler does not consume the
field (that is the judge-context "substance: thin" labeling — spec item V-E2
proper — which is explicitly OUT OF SCOPE here: it belongs to the verify-path
pass). Stamping it now means that pass never has to re-fetch or re-parse a
body just to learn its length.

**P2-2 LICENSE GATE (posture documented here, on purpose).** Each candidate's
``license_class`` is resolved from the signal (``payload.license_class`` — the
LIC-2 SourceScope→signal stamp — falling back to the manual-batch
``raw_provenance`` license, via :func:`legba.data.opensearch.
signal_license_class`). Sources whose class FORBIDS retention
(:data:`FORBID_RETENTION_CLASSES`: ``anti_ai_walled`` — never fetch, per the
LIC-1 ledger §E.4 enforcement hooks; ``tos_restrictive`` — reviewed-
restrictive terms; ``personal_use_only``) are SKIPPED with an honest
``skipped_license`` counter and a recorded ``skipped_license`` sidecar row —
never silently. The DEFAULT posture for unknown/unset license is **ARCHIVE**:
this is an open-web news quotation-for-evidence product, the LIC-1 ledger
found zero active sources with an anti-LLM EULA, and the evaluated class is
recorded on every row precisely so a future policy flip (e.g. fail-closed on
``unknown``) can re-evaluate mechanically instead of by memory.

**R-3b — THE FAIL-OPEN DEFAULT INVERTS FOR WEB-RETRIEVED ROWS (and ONLY those).**
The fail-open posture above was calibrated against a FINITE, operator-reviewed
set of ~48 active sources. A search provider returns arbitrary open-web
domains — unbounded, unreviewed, and certainly including anti-AI-walled
publishers. Under the old rule such a hit would arrive with
``license_class = None``, take the fail-open path, and be archived. So:

  * a row whose :func:`legba.data.retrieval_origin.resolve_retrieval_origin`
    says ``web_search:<component_id>`` AND whose ``license_class`` is unset or
    the reviewed-but-indeterminate ``"unknown"`` does **NOT** get its bytes
    archived. It is recorded with the DISTINCT status
    ``skipped_license_unreviewed`` (migration 0112) and its own
    ``skipped_license_unreviewed`` counter — counted and visible, never silent,
    and never conflated with ``skipped_license`` (which means "a REVIEWED class
    forbade retention"). Keeping them separate is what lets an operator ask
    "how much did fail-closed cost us?", the question that decides whether to
    move to ledger-on-first-sight.
  * METADATA retention is unaffected: the sidecar row still records the URL,
    the licence class and the origin. Only the BYTES are withheld, and the
    fetch is skipped entirely — nothing is downloaded and then discarded.
  * **Registered sources are byte-for-byte unaffected.** The gate keys on a web
    origin, which no existing row carries (``retrieval_origin`` is NULL for
    every row written before 0112, and NULL is not web).
  * Policy is per-run overridable without a code change, exactly like
    ``forbid_license_classes``: ``options.web_origin_license_gate`` =
    ``"fail_closed"`` (default) | ``"inherit"`` (fall back to the curated
    fail-open posture).

**R-4 — THE CURATED FAIL-OPEN POSTURE IS NOW AN OPTION, NOT A LAW.**
Everything above leaves one asymmetry standing: a REGISTERED source whose
licence was never classified still archives, because the LIC-1 review found no
anti-LLM EULA among the ~48 feeds active at the time. That was a finding about
a specific catalog on a specific date, not a property of the world, and an
operator who registers their own feeds inherits a posture that was never
measured for them. So the posture is now governed rather than assumed:

  * ``options.unknown_license_gate`` = ``"archive"`` (**DEFAULT — today's
    behaviour, byte for byte**) | ``"fail_closed"``.
  * Under ``"fail_closed"`` a candidate whose ``license_class`` is unset or the
    reviewed-but-indeterminate ``"unknown"`` does NOT get its bytes archived,
    whatever its origin. It is recorded with the SAME terminal status the R-3b
    skip uses (``skipped_license_unreviewed`` — the constraint vocabulary is
    closed, and that status already means exactly "we never reviewed this
    source, so we are not keeping its bytes") and its OWN run counter,
    ``skipped_license_unknown``, so the two policies stay separately priceable
    in the receipt. At ROW level they separate on ``retrieval_origin``: web
    rows are the R-3b population, NULL/curated rows the R-4 one.
  * The gate runs AFTER the R-3b gate, so a web-origin row keeps being counted
    as ``skipped_license_unreviewed``; turning R-4 on never re-attributes a
    skip that R-3b was already making.
  * An UNRECOGNISED option value keeps the DEFAULT and logs a WARNING — the
    descriptor channel also drops it with a ``handler_options`` trace note
    (choices are declared in ``legba.data.option_catalog.handler_options``). Neither
    direction of typo changes policy silently.

**THE RECOMMENDATION: run ``fail_closed`` unless your catalog is allowlisted.**
Fail-open is defensible only while every source that can reach this handler has
been licence-reviewed — which is true of the reference deployment and is not
true by default of anyone else's. The honest sequence for an operator is:
classify the feeds you registered (``payload.license_class`` via the LIC-2
``SourceScope`` stamp), then flip the gate; the ``skipped_license_unknown``
counter tells you what the flip costs before and after. Flipping it is ONE
registry PUT against the ``evidence_archiver`` descriptor —
``method.options.unknown_license_gate: fail_closed`` — with no code change, no
migration, and no redeploy. The default stays ``archive`` here because changing
a shipped default's behaviour under an operator who did not ask is the failure
mode this whole module is written against.

**RETENTION HONESTY.** Archived objects are evidence — this handler NEVER
deletes them, and nothing else does either (the sidecar has no TTL; archived
signals are upgraded to ``evidence_hold``, which signals_retention exempts).
The future retention interplay — ``media_ref_expires_at`` sweeps, object-store
GC, operator-gated erasure on a license policy flip — is a declared seam
(docs/SEAMS.md), not built here. C2 "one janitor" (migration 0109) surveyed
this seam when building the shared ``retention_policies`` config table /
``_retention_sweep`` engine (now backing ``signals_retention`` +
``analyst_traces_retention``) and deliberately did NOT build an archive-GC
policy now — but the schema needs no shape change to add one later (a new
``policy_name='evidence_archive_retention'`` row + a small Python adapter).

Idempotency: a successful archive stamps ``signals.object_ref`` → the row
leaves the selection predicate forever. Failures are retried up to
``max_attempts`` (attempt-counted in the sidecar); ``skipped_license`` /
``skipped_size`` rows are recorded once and excluded from re-selection so the
budget is never re-burned (a policy/cap change re-evaluates them explicitly).
An already-present CAS object (same bytes archived via another signal) is a
dedup hit: counted, stamped, not rewritten.

Output ``data`` keys (the cadence receipt the operator reads):
    examined            int — candidate rows pulled this run
    archived            int — signals whose original bytes are now archived
    already_present     int — of ``archived``, CAS dedup hits (bytes existed)
    media_archived      int — media_ref objects stored (the media leg)
    media_failed        int — media_ref fetches that failed (row still archived)
    text_extracted      int — archived objects whose main text reached payload
                              (payload.archived_text_chars stamped alongside —
                              V-E2, the substance-floor marker)
    text_extract_failed int — HTML/text objects Trafilatura could not extract
    text_extract_rejected_boilerplate
                        int — V-E1: Trafilatura DID return text, but it matched
                              the JS-wall/bot-check/redirect-interstitial deny
                              list (:func:`_match_wall_pattern`) — a no-JS
                              fallback page, a bot challenge, a link-shortener
                              redirect notice. Treated exactly like a failure:
                              no payload.archived_text write. Logged at WARNING
                              with the URL host.
    text_extract_skipped
                        int — R6b: objects whose URL is a known no-content-region
                              page (t.me / Telegram embed-widget previews) —
                              Trafilatura is never invoked for these, because it
                              has no article to find and instead extracts the
                              page CHROME ("Download\nContext\nEmbed\n…
                              telegram-widget.js…"), which previously polluted
                              ``payload.archived_text`` for ~30.7% of telegram
                              rows. The BYTES are still archived; only the
                              derived-text upgrade is withheld.
    skipped_license     int — P2-2 gate refusals on a REVIEWED forbidding class
    skipped_license_unreviewed
                        int — R-3b fail-closed refusals: web-retrieved origin +
                              unset/unknown licence. Bytes withheld, metadata
                              kept, counted separately from skipped_license so
                              the cost of fail-closed is measurable.
    skipped_license_unknown
                        int — R-4 fail-closed refusals: ANY row (curated
                              included) with unset/unknown licence, when
                              ``options.unknown_license_gate='fail_closed'``.
                              Always 0 at the shipped default. Shares the
                              sidecar STATUS with the R-3b skip (closed CHECK
                              vocabulary) but never its counter.
    blocked_by_challenge
                        int — (a′) pages an anti-bot CHALLENGE stood in front
                              of: a Cloudflare / DataDome interstitial body
                              (:func:`_challenge_detect.detect_challenge_page`,
                              recorded as ``blocked_challenge`` — the bytes are
                              still archived), or an edge 401/403/429 (recorded
                              as ``failed`` so it stays retryable). Before this
                              counter existed a 5–13 kB interstitial landed as
                              a thin extraction and read as *the web had
                              nothing*; that false absence is the defect.
    web_origin_examined int — candidates carrying a web retrieval origin
    skipped_size        int — objects over the size cap (recorded)
    fetch_failed        int — fetch/store failures this run (attempt-counted)
    egress_blocked      int — of ``fetch_failed``, SSRF-guard refusals (terminal)
    bytes_stored        int — NEW bytes written to the archive this run
    deadline_stop       int — 1 when the run stopped at the soft deadline
    skipped_no_root     int — 1 when the archive root was absent/unwritable
    per_source          {source_id: {fetched, challenge_blocked,
                          boilerplate_rejected, body_stored}} — W11-F138,
                          the per-feed outcomes the source-health surface
                          computes off the evidence_archive sidecar with the
                          same vocabulary (a feed's body-extraction rate is
                          body_stored / fetched).

**W11-F138 — PER-HOST FETCH POLICY.** Times of Israel and the Jerusalem Post
answer every fetch with an anti-bot challenge (http_403, several times a
day), so their signals carry a summary and no body. The fetch now consults
:mod:`legba.data.sources._fetch_policy`: an edge block (401/403/429) on a
host registered ``impersonate: on`` is counted under
``blocked_by_challenge`` and re-issued through the impersonated leg
(curl_cffi browser fingerprint — opened lazily, so an unblocked run never
pays for it). The global ``LEGBA_FETCH_IMPERSONATE`` keeps its meaning: a
non-off global already impersonates the primary client and the registry can
never weaken it, and an explicit ``off`` is the fleet-wide kill switch (no
escalation on any host). A block that an escalation then wins still counts as a
block — the refusal happened — so ``blocked_by_challenge`` can exceed
``fetch_failed`` on a run the impersonated leg saved.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from ... import critic_fold
from ...archive import (
    ARCHIVE_ROOT_ENV,
    CAS_PREFIX,
    DEFAULT_ARCHIVE_ROOT,
    cas_object_ref,
    cas_path,
    sha256_from_object_ref,
)
from ...archive_store import EvidenceStore
from ._ingest_copy import load_ingest_copy, observe_refetch
from ._intake_route import intake_copy_or_defer
from ...opensearch import signal_license_class
from ...provenance.models import FindingPayload
from ...retrieval_origin import is_web_retrieved, resolve_retrieval_origin
from ...sources._egress import (
    EgressBlockedError,
    fetch_client,
    guarded_async_client,
)
from ._challenge_detect import (
    BLOCKED_BY_CHALLENGE,
    challenge_from_status,
    detect_challenge_page,
    detect_challenge_text,
)
from ...sources.page_text import (  # noqa: F401 — the archiver's historical names
    _NO_CONTENT_REGION_HOSTS,
    _TEXTUAL_CONTENT_TYPES,
    _WALL_DENY_PATTERNS,
    _WALL_MAX_CHARS,
    _extract_text,
    _is_textual,
    _match_wall_pattern,
)
from ...sources.page_text import _skip_text_extraction as _skip_text_extraction_in
from ._fetch_leg import (
    _ObjectTooLargeError,
    _feed,
    _fetch_bytes,
    _fetch_canonical,
)
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "evidence_archiver"

#: P2-2 — license classes that FORBID retention of the original bytes (the
#: LIC-1 ledger §E.4 vocabulary). ``anti_ai_walled`` = never fetch (hard
#: denial); ``tos_restrictive`` = reviewed-restrictive publisher terms;
#: ``personal_use_only`` = personal-use grants. Everything else — including
#: unknown/unset (recorded as NULL) — ARCHIVES under the open-web
#: quotation-for-evidence posture (module docstring). Overridable per-run via
#: ``options.forbid_license_classes`` for a policy flip without a code change.
FORBID_RETENTION_CLASSES: frozenset[str] = frozenset(
    {"anti_ai_walled", "tos_restrictive", "personal_use_only"}
)

#: R-3b — for a WEB-RETRIEVED row, does an unreviewed licence still archive?
#: ``False`` = fail CLOSED. The LIC-1 fail-open default was calibrated against
#: ~48 operator-reviewed sources; search makes the domain set unbounded and
#: unaudited, so the default inverts — but ONLY for rows whose retrieval origin
#: says web. Registered sources keep the fail-open posture unchanged.
WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES: bool = False

#: Licence values that are NOT an affirmative permission to retain bytes.
#: ``None`` = never stamped; ``"unknown"`` = reviewed and indeterminate. For a
#: curated source both archive (the open-web quotation-for-evidence posture);
#: for a web-retrieved row neither does.
UNREVIEWED_LICENSE_CLASSES: frozenset[str] = frozenset({"unknown"})

#: The sidecar status for an R-3b fail-closed skip (migration 0112). Distinct
#: from ``skipped_license`` on purpose: that one means "a REVIEWED class forbade
#: retention", this one means "we never reviewed this domain".
STATUS_SKIPPED_LICENSE_UNREVIEWED = "skipped_license_unreviewed"

#: (a′) The sidecar status for a page that an anti-bot CHALLENGE stood in
#: front of (migration 0196). Distinct from ``failed`` on purpose: ``failed``
#: is "we could not fetch it", this one is "a publisher's edge refused us and
#: said so in an interstitial". Before this existed, a 5–13 kB Cloudflare /
#: DataDome page landed as a thin extraction and read downstream as *the web
#: had nothing* — the false-absence defect FETCH_REVIEW §2 names as bigger
#: than the 403 itself. The BYTES are still archived (they are the evidence of
#: the block); only the derived-text upgrade is withheld.
STATUS_BLOCKED_CHALLENGE = "blocked_challenge"

#: ``options.web_origin_license_gate`` values.
WEB_ORIGIN_GATE_FAIL_CLOSED = "fail_closed"
WEB_ORIGIN_GATE_INHERIT = "inherit"

#: R-4 — the CODE-level posture for an unreviewed licence on ANY row, curated
#: included. ``True`` = archive (the LIC-1 fail-OPEN default), which is what
#: ships. Kept as a module constant for the same reason
#: ``WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES`` is: a future decision to invert the
#: SHIPPED default is one line here, and the option below keeps working either
#: way round. Recommendation for a self-registered catalog is fail-closed —
#: see the R-4 section of the module docstring.
UNKNOWN_LICENSE_ARCHIVES: bool = True

#: ``options.unknown_license_gate`` values.
UNKNOWN_LICENSE_GATE_ARCHIVE = "archive"
UNKNOWN_LICENSE_GATE_FAIL_CLOSED = "fail_closed"

_USER_AGENT = "legba-evidence-archiver/0.1"

# Defaults (all overridable via descriptor options).
_DEFAULT_FETCH_BUDGET = 200          # candidate signals per run
_DEFAULT_WINDOW_HOURS = 720          # finding-recency window for the citation join
_DEFAULT_VERIFY_FLOOR = 0.50         # the house verified bar (floor=0.50)
_DEFAULT_MAX_OBJECT_BYTES = 20 * 1024 * 1024   # 20MB size cap per object
_DEFAULT_PER_HOST_DELAY_S = 2.0      # politeness delay between same-host fetches
_DEFAULT_MAX_ATTEMPTS = 3            # failed-fetch retry cap (across runs)
_DEFAULT_MAX_TEXT_CHARS = 200_000    # extracted-text cap stored into payload
_DEFAULT_TIMEOUT_S = 30.0            # per-request timeout
_DEFAULT_RUN_DEADLINE_S = 1200.0     # soft per-run wall-clock stop (20 min)


# ---------------------------------------------------------------------------
# Selection — signals cited by VERIFIED findings, not yet archived
# ---------------------------------------------------------------------------
#
# The verified bar mirrors alert_trigger_scan._VERIFIED_FINDINGS_SQL / the
# since_api: the LATEST faithfulness critique is pinned in a lateral, the
# floor applied over LEAST(confidence, overall_score). derived_from is the
# universal lineage array — the unnest joined against signals(id) naturally
# drops non-signal lineage members. The evidence_archive anti-join excludes
# terminal rows (archived / skipped_* / attempt-exhausted failures) so every
# run's budget goes to NEW work; signals.object_ref IS NULL is the primary
# idempotency gate (stamped on success → the row leaves this scan forever).
_SELECT_CANDIDATES_SQL = f"""
    WITH f AS MATERIALIZED (
        SELECT f.id, f.confidence, f.derived_from
          FROM analyst_outputs f
         WHERE f.kind = 'finding'
           AND f.superseded_by IS NULL
           AND f.produced_at > now() - make_interval(hours => $1)
    ), {critic_fold.faithfulness_score_cte()}, cited AS (
        SELECT DISTINCT d.sid
          FROM f
          JOIN v ON v.fid = f.id::text
          CROSS JOIN LATERAL unnest(f.derived_from) AS d(sid)
         WHERE LEAST(f.confidence, v.faithfulness_score) >= $2
    )
    SELECT s.id, s.source_id, s.canonical_url, s.media_ref, s.payload,
           s.raw_provenance, s.retention_class,
           -- R-3b (mig 0112): the retrieval-origin axis the fail-closed gate
           -- reads. NULL for every row written before the concept existed,
           -- which is exactly "a curated registered source".
           s.retrieval_origin,
           COALESCE(ea.attempts, 0) AS prior_attempts
      FROM signals s
      JOIN cited c ON c.sid = s.id
      LEFT JOIN evidence_archive ea ON ea.signal_id = s.id
     WHERE s.object_ref IS NULL
       AND s.canonical_url IS NOT NULL
       AND s.canonical_url <> ''
       -- C-1: a CLAIM row is text only — never archive-fetched (its page
       -- carries the leak-post captures a claim source must not keep).
       AND COALESCE(s.payload->'claim'->>'source_class', '') <> 'claim'
       AND (ea.signal_id IS NULL
            OR (ea.status = 'failed' AND ea.attempts < $3))
     ORDER BY s.fetched_at DESC
     LIMIT $4
"""

# Sidecar upsert — ONE statement for every terminal/attempt outcome. jsonb-free
# and additive; `attempts` accumulates across runs, `archived_at` only set on
# success, `created_at` preserved on conflict.
_UPSERT_ARCHIVE_SQL = """
    INSERT INTO evidence_archive (
        signal_id, status, object_ref, sha256, size_bytes, content_type,
        fetched_url, media_object_ref, media_sha256, media_size_bytes,
        license_class, text_extracted, attempts, last_error, retrieval_origin,
        archived_at, updated_at
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
        CASE WHEN $2 = 'archived' THEN now() ELSE NULL END, now()
    )
    ON CONFLICT (signal_id) DO UPDATE SET
        status           = EXCLUDED.status,
        object_ref       = EXCLUDED.object_ref,
        sha256           = EXCLUDED.sha256,
        size_bytes       = EXCLUDED.size_bytes,
        content_type     = EXCLUDED.content_type,
        fetched_url      = EXCLUDED.fetched_url,
        media_object_ref = EXCLUDED.media_object_ref,
        media_sha256     = EXCLUDED.media_sha256,
        media_size_bytes = EXCLUDED.media_size_bytes,
        license_class    = EXCLUDED.license_class,
        retrieval_origin = EXCLUDED.retrieval_origin,
        text_extracted   = EXCLUDED.text_extracted,
        attempts         = EXCLUDED.attempts,
        last_error       = EXCLUDED.last_error,
        archived_at      = COALESCE(evidence_archive.archived_at, EXCLUDED.archived_at),
        updated_at       = now()
"""

# The signal stamp — ONE update. object_ref = the cas address (the read
# surfaces derive archived/archive_sha256 from this existing column alone);
# retention_class upgraded to evidence_hold unless the row already sits in a
# keep-class (retain_always stays retain_always); and, WHEN text was extracted
# ($3 non-null), payload.archived_text AND payload.archived_text_chars (V-E2 —
# the substance-floor marker: a free len() of the stored text, so a later
# verify-side pass can label thin evidence without re-reading bodies) are BOTH
# set, and the corpus dirty-marker contract is honored: indexed_at = NULL
# re-queues the doc, updated_at = now() protects the re-null from the
# indexer's version-guarded stamp (BOTH in this same UPDATE — see
# corpus_indexer's DIRTY-MARKER CONTRACT). With no text (either Trafilatura
# failure OR a V-E1 wall-pattern rejection), updated_at still bumps
# (harmless) and indexed_at is left alone.
_STAMP_SIGNAL_SQL = """
    UPDATE signals
       SET object_ref = $2,
           retention_class = CASE
               WHEN retention_class IN ('retain_always', 'evidence_hold')
               THEN retention_class ELSE 'evidence_hold' END,
           payload = CASE WHEN $3::text IS NULL THEN payload
                          ELSE jsonb_set(
                                 jsonb_set(payload, '{archived_text}', to_jsonb($3::text)),
                                 '{archived_text_chars}', to_jsonb($4::int)
                               ) END,
           indexed_at = CASE WHEN $3::text IS NULL THEN indexed_at ELSE NULL END,
           updated_at = now()
     WHERE id = $1
"""


# ---------------------------------------------------------------------------
# Small pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------
# The CAS address format (cas_object_ref / cas_path / sha256_from_object_ref)
# lives in :mod:`legba.data.archive` — ONE owner shared with the read
# projections; re-exported here for the handler's callers/tests.


def resolve_license_class(row: Mapping[str, Any]) -> str | None:
    """The signal's license class as the P2-2 gate sees it.

    ``payload.license_class`` (the LIC-2 SourceScope→signal ingest stamp)
    first, then the manual-batch ``raw_provenance`` license — the same
    resolution the corpus doc projection uses (one truth, one helper)."""
    return signal_license_class(row)


def license_forbids_retention(
    license_class: str | None, forbid: frozenset[str] | set[str],
) -> bool:
    """True when the class forbids archiving the original bytes.

    Unknown/unset (``None``) ARCHIVES — the open-web quotation-for-evidence
    default posture (module docstring); the class is recorded either way so a
    policy flip can re-evaluate.

    This is the CURATED-source rule. Web-retrieved rows additionally pass
    :func:`web_origin_license_unreviewed` (R-3b), which inverts the unknown
    default for them alone."""
    return license_class is not None and license_class in forbid


def resolve_signal_retrieval_origin(row: Mapping[str, Any]) -> str | None:
    """The signal's retrieval origin as the R-3b gate sees it.

    The migration-0112 column first, then the payload stamp — resolved by the
    ONE owner (:mod:`legba.data.retrieval_origin`) that the OpenSearch facet
    also uses, so the gate and the facet can never disagree."""
    return resolve_retrieval_origin(row)


def web_origin_license_unreviewed(
    license_class: str | None,
    retrieval_origin: str | None,
    *,
    fail_closed: bool = True,
) -> bool:
    """True when this row is web-retrieved with NO affirmative licence verdict.

    The R-3b inversion, stated as a single predicate so its scope is
    auditable at a glance:

      * not a web origin → ``False`` (curated sources keep the fail-open
        posture EXACTLY as before — this is why the change is a no-op for every
        row that exists today, all of which have a NULL origin);
      * ``fail_closed=False`` (``options.web_origin_license_gate='inherit'``) →
        ``False`` (the documented policy escape hatch);
      * web origin AND ``license_class`` unset or ``"unknown"`` → ``True``:
        withhold the bytes.

    An AFFIRMATIVE class on a web-origin row (an operator classified the
    domain — the ledger-on-first-sight path) archives normally; a REVIEWED
    forbidding class was already refused upstream by
    :func:`license_forbids_retention`.
    """
    if not fail_closed:
        return False
    if not is_web_retrieved(retrieval_origin):
        return False
    return license_class is None or license_class in UNREVIEWED_LICENSE_CLASSES


def license_unreviewed(
    license_class: str | None, *, fail_closed: bool = False,
) -> bool:
    """True when this row has NO affirmative licence verdict AND policy says no.

    The R-4 gate, origin-blind on purpose — it is the CURATED-source rule
    :func:`license_forbids_retention` deliberately leaves fail-open, made
    governable:

      * ``fail_closed=False`` (``options.unknown_license_gate='archive'``, the
        SHIPPED DEFAULT) → always ``False``: nothing changes, for any row;
      * ``fail_closed=True`` and ``license_class`` unset or ``"unknown"`` →
        ``True``: withhold the bytes, keep the metadata;
      * an AFFIRMATIVE class → ``False`` (archives normally). A REVIEWED
        FORBIDDING class was already refused upstream by
        :func:`license_forbids_retention`, so this predicate never sees one as
        an archive decision.

    Deliberately NOT collapsed with :func:`web_origin_license_unreviewed`: that
    one is scoped to web origins and defaults ON, this one spans every row and
    defaults OFF. Two policies, two dials, two counters.
    """
    if not fail_closed:
        return False
    return license_class is None or license_class in UNREVIEWED_LICENSE_CLASSES


# The page-text helpers (``_is_textual``, ``_skip_text_extraction``, the V-E1
# wall list ``_match_wall_pattern`` and the Trafilatura pass ``_extract_text``)
# live in :mod:`legba.data.sources.page_text` since lane I-2, so the archiver,
# the fetch-receipt writer and the ``page`` source kind run ONE extraction.
# They are imported at the top of this module under their historical names.


def _skip_text_extraction(url: str) -> bool:
    """The shared rule over THIS module's :data:`_NO_CONTENT_REGION_HOSTS`
    (kept the archiver's own name, so a test or an operator patch of the set
    here is honoured)."""
    return _skip_text_extraction_in(url, _NO_CONTENT_REGION_HOSTS)


def _store_bytes(root: Path, body: bytes) -> tuple[str, str, bool]:
    """Content-address ``body`` under ``root``.

    Returns ``(sha256_hex, object_ref, was_already_present)``. The sha256 is
    of the ORIGINAL bytes; the object rests in the configured codec (zstd by
    default — :mod:`legba.data.archive_codec`; readers detect it by frame
    magic). Write is temp-file + fsync + atomic rename; an existing object is
    NEVER rewritten (CAS — same hash IS same bytes, store once, cite many)."""
    res = EvidenceStore(root).put(body)
    return res.sha256, res.object_ref, res.existed


class _HostPoliteness:
    """Per-host minimum spacing between fetches (monotonic-clock based)."""

    def __init__(self, delay_s: float) -> None:
        self._delay = max(0.0, float(delay_s))
        self._last: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if not host or self._delay <= 0:
            return
        now = time.monotonic()
        last = self._last.get(host)
        if last is not None:
            remaining = self._delay - (now - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last[host] = time.monotonic()


# ---------------------------------------------------------------------------
# W11-F138 — the per-host fetch policy (sources/_fetch_policy.py) reaches the
# archiver as a TWO-LEG client bundle:
#
#   * ``primary`` — the ONE plain guarded client every host shares, opened
#     lazily with byte-identical kwargs to the single client this sweep has
#     always built (the 2026-09-29 ``http2=False`` pin included — the h1 pin
#     is the archiver's own, fleet-wide, and the per-host policy's ``http2``
#     field deliberately does NOT reopen it: that pin exists because
#     Cloudflare keys on the h2 fingerprint, and a per-host h2 exception is
#     the defect it closed).
#   * ``impersonated`` — the curl_cffi leg, opened on FIRST use only. A run
#     whose fetches are never challenge-blocked on a policy host never pays
#     the impersonated session (nor the curl_cffi import — the off path stays
#     import-free exactly like _egress.fetch_client's own).
#
# The block itself is still COUNTED before the escalation re-asks — a
# publisher's refusal happened, and the counter is its receipt.
# ---------------------------------------------------------------------------


class _FetchClients:
    """The run's fetch clients, opened lazily on one exit stack."""

    def __init__(self, stack: AsyncExitStack, *, timeout: float) -> None:
        self._stack = stack
        self._timeout = timeout
        self._primary: Any = None
        self._impersonated: Any = None

    async def primary(self) -> Any:
        """The plain guarded client — identical kwargs for EVERY host."""
        if self._primary is None:
            self._primary = await self._stack.enter_async_context(
                fetch_client(
                    # ``guarded=`` hands ``fetch_client`` THIS module's own
                    # ``guarded_async_client``, so the flag-off path is the
                    # exact call this site made before, through the exact
                    # name the existing e2e suites monkeypatch.
                    guarded=guarded_async_client,
                    timeout=self._timeout,
                    follow_redirects=True,
                    headers={"User-Agent": _USER_AGENT},
                    # 2026-09-29 — HTTP/1.1 for the ARCHIVER ONLY. The 09-26
                    # transport change (pinned ciphers + ALPN h2) put httpx's
                    # HTTP/2 fingerprint in front of Cloudflare's managed
                    # challenge: dawn.com went to 0 archived pages/day, scmp
                    # to 0 by 09-29, 326 challenges/day; h2 off alone returned
                    # full pages. Fleet-wide, by design — a per-host policy's
                    # http2 field does NOT move this pin.
                    http2=False,
                )
            )
        return self._primary

    async def impersonated(self) -> Any:
        """The impersonated leg — opened only when a policy host is blocked."""
        if self._impersonated is None:
            self._impersonated = await self._stack.enter_async_context(
                fetch_client(
                    guarded=guarded_async_client,
                    # The per-host policy's resolution has ALREADY been done
                    # by the caller (impersonate_kwarg); an explicit True is
                    # the escalation itself.
                    impersonate=True,
                    timeout=self._timeout,
                    follow_redirects=True,
                    headers={"User-Agent": _USER_AGENT},
                    # Ignored by the impersonated adapter (curl carries its
                    # own fingerprint); pinned literally so the archiver's
                    # every fetch_client call keeps naming h1.
                    http2=False,
                )
            )
        return self._impersonated


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _zero_counters() -> dict[str, Any]:
    return {
        "examined": 0,
        "archived": 0,
        "already_present": 0,
        "media_archived": 0,
        "media_failed": 0,
        "text_extracted": 0,
        "text_extract_failed": 0,
        # (a′) The page WAS an anti-bot challenge/interstitial (or the edge
        # answered 401/403/429). Counted separately from every "failure" above
        # so a receipt can distinguish "a publisher blocked us" from "the web
        # had nothing" — the whole point of the detector.
        BLOCKED_BY_CHALLENGE: 0,
        # V-E1 — distinct from text_extract_failed: Trafilatura DID return
        # text, but it matched the JS-wall/bot-check/redirect deny list.
        "text_extract_rejected_boilerplate": 0,
        "text_extract_skipped": 0,
        "skipped_license": 0,
        # R-3b — kept DISTINCT from skipped_license so an operator can measure
        # what fail-closed costs, which is the question that decides whether to
        # move to ledger-on-first-sight.
        "skipped_license_unreviewed": 0,
        # R-4 — the curated fail-closed policy's own counter. Always 0 at the
        # shipped default; distinct from the R-3b counter so an operator can
        # price the two policies separately even though the sidecar rows share
        # a status (the CHECK vocabulary is closed; retrieval_origin separates
        # them at row level).
        "skipped_license_unknown": 0,
        "web_origin_examined": 0,
        "skipped_size": 0,
        "fetch_failed": 0,
        "egress_blocked": 0,
        "bytes_stored": 0,
        "deadline_stop": 0,
        "skipped_no_root": 0,
        # W11-F138 — per-feed outcomes: {source_id: {fetched,
        # challenge_blocked, boilerplate_rejected, body_stored}}. The same
        # four names the source-health surface computes per feed off the
        # evidence_archive sidecar, so a run receipt and the health read
        # speak one vocabulary. Nested, never flattened — the flat keys
        # above keep their exact per-run meaning.
        "per_source": {},
    }


def _archive_root() -> Path | None:
    """The archive root, created if needed; ``None`` when unusable (the run
    then no-ops LOUDLY via ``skipped_no_root`` — never a silent drop)."""
    raw = os.environ.get(ARCHIVE_ROOT_ENV, DEFAULT_ARCHIVE_ROOT)
    root = Path(raw)
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise PermissionError(f"{root} not writable")
        return root
    except Exception as exc:
        logger.warning(
            "evidence_archiver.no_root — archive root %s unusable (%s); "
            "set %s / mount the legba_archive volume. Skipping this tick.",
            raw, exc, ARCHIVE_ROOT_ENV,
        )
        return None


async def _record(
    pool: Any,
    *,
    signal_id: Any,
    status: str,
    attempts: int,
    object_ref: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    content_type: str | None = None,
    fetched_url: str | None = None,
    media_object_ref: str | None = None,
    media_sha256: str | None = None,
    media_size_bytes: int | None = None,
    license_class: str | None = None,
    retrieval_origin: str | None = None,
    text_extracted: bool = False,
    last_error: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _UPSERT_ARCHIVE_SQL,
            signal_id, status, object_ref, sha256, size_bytes, content_type,
            fetched_url, media_object_ref, media_sha256, media_size_bytes,
            license_class, text_extracted, attempts,
            str(last_error)[:2048] if last_error else None,
            retrieval_origin,
        )


async def _archive_one(
    pool: Any,
    clients: _FetchClients,
    row: Mapping[str, Any],
    *,
    root: Path,
    politeness: _HostPoliteness,
    counters: dict[str, Any],
    forbid: frozenset[str],
    max_bytes: int,
    max_text_chars: int,
    max_attempts: int,
    web_origin_fail_closed: bool = True,
    unknown_fail_closed: bool = False,
    prefer_ingest_copy: bool = True,
    intake_route: Mapping[str, Any] | None = None,
) -> None:
    """Fetch + store + record ONE candidate signal (all outcomes recorded)."""
    signal_id = row["id"]
    url = str(row["canonical_url"])
    attempts = int(row.get("prior_attempts") or 0) + 1
    license_class = resolve_license_class(row)
    retrieval_origin = resolve_signal_retrieval_origin(row)
    if is_web_retrieved(retrieval_origin):
        counters["web_origin_examined"] += 1

    # ---- P2-2 license gate — a REVIEWED forbidding class (never silent) ----
    if license_forbids_retention(license_class, forbid):
        counters["skipped_license"] += 1
        await _record(
            pool, signal_id=signal_id, status="skipped_license",
            attempts=attempts - 1, fetched_url=url, license_class=license_class,
            retrieval_origin=retrieval_origin,
            last_error=f"license_class {license_class!r} forbids retention",
        )
        return

    # ---- R-3b fail-closed gate — web origin + NO affirmative licence ----
    # Runs BEFORE the fetch: the bytes are never downloaded, so nothing has to
    # be discarded afterwards. Metadata (URL, class, origin) is still recorded.
    if web_origin_license_unreviewed(
        license_class, retrieval_origin, fail_closed=web_origin_fail_closed,
    ):
        counters["skipped_license_unreviewed"] += 1
        await _record(
            pool, signal_id=signal_id,
            status=STATUS_SKIPPED_LICENSE_UNREVIEWED,
            attempts=attempts - 1, fetched_url=url, license_class=license_class,
            retrieval_origin=retrieval_origin,
            last_error=(
                f"retrieval_origin {retrieval_origin!r} is web-retrieved and "
                f"license_class is {license_class!r} — bytes NOT archived "
                "(fail-closed for unreviewed open-web domains); metadata kept"
            ),
        )
        return

    # ---- R-4 fail-closed gate — ANY row with no affirmative licence ----
    # OFF at the shipped default, so this branch is unreachable unless the
    # operator PUT `unknown_license_gate: fail_closed` on the descriptor. It
    # runs AFTER the R-3b gate on purpose: a web row is already accounted for
    # above, so turning this on never re-attributes an existing skip. Same
    # pre-fetch position — nothing is downloaded and then discarded.
    if license_unreviewed(license_class, fail_closed=unknown_fail_closed):
        counters["skipped_license_unknown"] += 1
        await _record(
            pool, signal_id=signal_id,
            status=STATUS_SKIPPED_LICENSE_UNREVIEWED,
            attempts=attempts - 1, fetched_url=url, license_class=license_class,
            retrieval_origin=retrieval_origin,
            last_error=(
                f"license_class is {license_class!r} and "
                "unknown_license_gate='fail_closed' — bytes NOT archived "
                "(no affirmative permission to retain); metadata kept"
            ),
        )
        return

    # ---- fetch the original bytes (egress-guarded, size-capped, polite) ----
    # W11-F138 — the fetch consults the per-host policy: an edge block on a
    # host registered impersonate=on is counted and re-issued through the
    # impersonated leg (``_fetch_canonical``); every other host takes the
    # plain client it always took.
    feed = _feed(counters, row.get("source_id"))
    # N-L: the bytes the POLL kept win over a later re-fetch (_ingest_copy.py);
    # the re-fetch is the fallback, and it is recorded as a page version so a
    # page that changed or went away since leaves one drift row.
    copy = await load_ingest_copy(pool, signal_id, url, root) if prefer_ingest_copy else None
    if copy is None and intake_route:  # I-1: the re-fetch as an intake request (_intake_route.py)
        verdict, got = await intake_copy_or_defer(pool, signal_id, url, root=root, **intake_route)
        if verdict == "defer":
            counters["intake_deferred"] = counters.get("intake_deferred", 0) + 1
            return
        if verdict == "copy":
            counters["intake_copy_used"] = counters.get("intake_copy_used", 0) + 1
            copy = got
    if copy is not None:
        counters["ingest_copy_used"] = counters.get("ingest_copy_used", 0) + 1
        (body, content_type, encoding), fetch_error, escalated_tell = copy, None, None
    else:
        body, content_type, encoding, fetch_error, escalated_tell = (
            await _fetch_canonical(
                clients, politeness, url,
                max_bytes=max_bytes, counters=counters, feed=feed,
            )
        )
        await observe_refetch(
            pool, url, root=root, body=body if fetch_error is None else None,
            content_type=content_type, source_id=row.get("source_id"),
            error_status=getattr(getattr(fetch_error, "response", None), "status_code", None),
        )
    if isinstance(fetch_error, _ObjectTooLargeError):
        counters["skipped_size"] += 1
        await _record(
            pool, signal_id=signal_id, status="skipped_size",
            attempts=attempts, fetched_url=url, license_class=license_class,
            retrieval_origin=retrieval_origin, last_error=str(fetch_error),
        )
        return
    if isinstance(fetch_error, EgressBlockedError):
        # Terminal — a non-public target never becomes fetchable. Cap the
        # attempts immediately so the budget is never re-burned on it.
        counters["fetch_failed"] += 1
        counters["egress_blocked"] += 1
        await _record(
            pool, signal_id=signal_id, status="failed",
            attempts=max(attempts, max_attempts), fetched_url=url,
            license_class=license_class, retrieval_origin=retrieval_origin,
            last_error=f"egress blocked: {fetch_error}",
        )
        return
    if isinstance(fetch_error, httpx.HTTPStatusError):
        # (a′) A publisher's edge answered. 401/403/429 IS a block — say so.
        # The status stays ``failed`` (retryable, attempt-capped) rather than
        # ``blocked_challenge``: the refusal may well clear if an operator
        # turns LEGBA_FETCH_IMPERSONATE on, and a terminal status would freeze
        # the row out of that re-attempt.
        status_code = getattr(
            getattr(fetch_error, "response", None), "status_code", None,
        )
        tell = challenge_from_status(status_code) or escalated_tell
        counters["fetch_failed"] += 1
        if tell is not None:
            # ``escalated_tell`` means the block was already counted inside
            # ``_fetch_canonical`` (a counted block that the impersonated leg
            # then also failed); counting it again would double a single
            # refusal.
            if escalated_tell is None:
                counters[BLOCKED_BY_CHALLENGE] += 1
                feed["challenge_blocked"] += 1
            logger.warning(
                "evidence_archiver.%s host=%s tell=%s",
                BLOCKED_BY_CHALLENGE, urlsplit(url).hostname, tell,
            )
        await _record(
            pool, signal_id=signal_id, status="failed",
            attempts=attempts, fetched_url=url, license_class=license_class,
            retrieval_origin=retrieval_origin,
            last_error=(
                f"{BLOCKED_BY_CHALLENGE}: {tell} — {fetch_error}"
                if tell is not None else repr(fetch_error)
            ),
        )
        return
    if fetch_error is not None:
        counters["fetch_failed"] += 1
        await _record(
            pool, signal_id=signal_id, status="failed",
            attempts=attempts, fetched_url=url, license_class=license_class,
            retrieval_origin=retrieval_origin,
            last_error=(
                f"{BLOCKED_BY_CHALLENGE}: {escalated_tell} — escalated leg "
                f"failed: {fetch_error!r}"
                if escalated_tell is not None else repr(fetch_error)
            ),
        )
        return

    # ---- store (content-addressed, atomic, dedup-aware) ----
    # N-V: hashing + zstd + fsync of up to max_bytes, and Trafilatura over the
    # page below, are blocking work — off the event loop, which every other
    # actor turn, poll and /healthz shares (fetch_receipts already does this).
    try:
        digest, object_ref, existed = await asyncio.to_thread(
            _store_bytes, root, body
        )
    except OSError as exc:
        counters["fetch_failed"] += 1
        await _record(
            pool, signal_id=signal_id, status="failed",
            attempts=attempts, fetched_url=url, license_class=license_class,
            retrieval_origin=retrieval_origin,
            last_error=f"store failed: {exc}",
        )
        return
    if existed:
        counters["already_present"] += 1
    else:
        counters["bytes_stored"] += len(body)

    # ---- (a′) was this 200 actually a CHALLENGE PAGE? -------------------
    # Runs on the RAW body, because the sharpest tells (DataDome's
    # ``var dd={'rt'…``, Cloudflare's ``/cdn-cgi/challenge-platform``) never
    # survive main-text extraction. The bytes stay archived — they ARE the
    # evidence that we were blocked — but the row says ``blocked_challenge``
    # instead of ``archived``, and no derived text is written.
    challenge_tell = detect_challenge_page(body, content_type, encoding)
    if challenge_tell is not None:
        counters[BLOCKED_BY_CHALLENGE] += 1
        feed["challenge_blocked"] += 1
        logger.warning(
            "evidence_archiver.%s host=%s tell=%s bytes=%d",
            BLOCKED_BY_CHALLENGE, urlsplit(url).hostname, challenge_tell, len(body),
        )
        await _record(
            pool, signal_id=signal_id, status=STATUS_BLOCKED_CHALLENGE,
            attempts=attempts, object_ref=object_ref, sha256=digest,
            size_bytes=len(body), content_type=content_type, fetched_url=url,
            license_class=license_class, retrieval_origin=retrieval_origin,
            text_extracted=False,
            last_error=f"{BLOCKED_BY_CHALLENGE}: {challenge_tell}",
        )
        async with pool.acquire() as conn:
            await conn.execute(_STAMP_SIGNAL_SQL, signal_id, object_ref, None, None)
        return

    # ---- bonus text extraction (bytes are the archive either way) ----
    archived_text: str | None = None
    rejected_pattern: str | None = None
    if _skip_text_extraction(url):
        # R6b — a known no-content-region page (t.me widget preview): never
        # invoke Trafilatura, it would only harvest page chrome. Bytes are
        # already archived above; only the derived-text upgrade is withheld.
        counters["text_extract_skipped"] += 1
    elif _is_textual(content_type, body):
        archived_text = await asyncio.to_thread(
            _extract_text, body, encoding, max_chars=max_text_chars
        )
        if archived_text is not None:
            wall_pattern = _match_wall_pattern(archived_text)
            if wall_pattern is not None:
                # V-E1 — a JS-wall/bot-check/redirect-interstitial body wearing
                # the shape of a successful extraction. Reject it exactly like
                # a Trafilatura failure: no payload.archived_text write, no
                # corpus dirty-marker requeue. The BYTES stay archived either
                # way — only the derived-text upgrade is withheld.
                counters["text_extract_rejected_boilerplate"] += 1
                feed["boilerplate_rejected"] += 1
                # W11-F138 — stamped on the sidecar row (not a status: the
                # closed CHECK vocabulary has none for it) so the health
                # surface's per-feed boilerplate_rejected can read the SAME
                # event the counter just tallied.
                rejected_pattern = wall_pattern
                logger.warning(
                    "evidence_archiver.rejected_boilerplate host=%s pattern=%r",
                    urlsplit(url).hostname, wall_pattern,
                )
                archived_text = None
            else:
                counters["text_extracted"] += 1
        else:
            counters["text_extract_failed"] += 1

    # ---- media leg (best-effort; never fails the row; no processing) ----
    media_object_ref: str | None = None
    media_sha256: str | None = None
    media_size: int | None = None
    media_ref = row.get("media_ref")
    if isinstance(media_ref, str) and media_ref.startswith(("http://", "https://")):
        try:
            await politeness.wait(media_ref)
            media_body, _, _ = await _fetch_bytes(
                await clients.primary(), media_ref, max_bytes=max_bytes,
            )
            media_sha256, media_object_ref, media_existed = (
                await asyncio.to_thread(_store_bytes, root, media_body)
            )
            media_size = len(media_body)
            if not media_existed:
                counters["bytes_stored"] += media_size
            counters["media_archived"] += 1
        except Exception as exc:
            counters["media_failed"] += 1
            logger.debug(
                "evidence_archiver.media_failed signal=%s err=%s", signal_id, exc,
            )

    # ---- record + stamp (sidecar row, then the signal's object_ref) ----
    await _record(
        pool, signal_id=signal_id, status="archived", attempts=attempts,
        object_ref=object_ref, sha256=digest, size_bytes=len(body),
        content_type=content_type, fetched_url=url,
        media_object_ref=media_object_ref, media_sha256=media_sha256,
        media_size_bytes=media_size, license_class=license_class,
        retrieval_origin=retrieval_origin,
        text_extracted=archived_text is not None,
        last_error=(
            f"rejected_boilerplate: {rejected_pattern}"
            if rejected_pattern is not None else None
        ),
    )
    async with pool.acquire() as conn:
        # V-E2 — the substance-floor marker: a free len() alongside the text
        # itself, so a later verify-side pass never has to re-read the body
        # to know how much was actually extracted.
        archived_text_chars = len(archived_text) if archived_text is not None else None
        await conn.execute(
            _STAMP_SIGNAL_SQL, signal_id, object_ref, archived_text, archived_text_chars,
        )
    counters["archived"] += 1
    # W11-F138 — a real stored body for the feed's extraction rate (a
    # challenge page never reaches this line: it returns above with the
    # blocked_challenge record).
    feed["body_stored"] += 1


async def _sweep(pool: Any, options: Mapping[str, Any]) -> dict[str, Any]:
    counters = _zero_counters()

    root = _archive_root()
    if root is None:
        counters["skipped_no_root"] = 1
        return counters

    window_hours = int(options.get("window_hours", _DEFAULT_WINDOW_HOURS))
    verify_floor = float(options.get("verify_floor", _DEFAULT_VERIFY_FLOOR))
    fetch_budget = int(options.get("fetch_budget", _DEFAULT_FETCH_BUDGET))
    max_attempts = int(options.get("max_attempts", _DEFAULT_MAX_ATTEMPTS))
    max_bytes = int(options.get("max_object_bytes", _DEFAULT_MAX_OBJECT_BYTES))
    max_text_chars = int(options.get("max_text_chars", _DEFAULT_MAX_TEXT_CHARS))
    per_host_delay = float(
        options.get("per_host_delay_seconds", _DEFAULT_PER_HOST_DELAY_S)
    )
    timeout_s = float(options.get("timeout_seconds", _DEFAULT_TIMEOUT_S))
    deadline_s = float(options.get("run_deadline_seconds", _DEFAULT_RUN_DEADLINE_S))
    forbid_raw = options.get("forbid_license_classes")
    forbid = (
        frozenset(str(c) for c in forbid_raw)
        if isinstance(forbid_raw, (list, tuple, set))
        else FORBID_RETENTION_CLASSES
    )
    # R-3b — the web-origin posture, overridable per run without a code change
    # (same discipline as forbid_license_classes). Anything other than the
    # explicit "inherit" keeps the fail-closed default: a typo must not silently
    # re-open the gate.
    if WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES:
        web_origin_fail_closed = False
    else:
        gate = str(
            options.get("web_origin_license_gate", WEB_ORIGIN_GATE_FAIL_CLOSED)
        )
        web_origin_fail_closed = gate != WEB_ORIGIN_GATE_INHERIT

    # R-4 — the curated posture. DEFAULT "archive" = today's behaviour exactly.
    # Only the exact literal "fail_closed" engages it; an unrecognised value
    # keeps the DEFAULT and says so at WARNING, because a typo must not move a
    # licence policy in EITHER direction silently. (The descriptor channel
    # already rejects out-of-choices values with a handler_options trace note;
    # this covers the direct-call path too.)
    unknown_fail_closed = not UNKNOWN_LICENSE_ARCHIVES
    raw_gate = options.get("unknown_license_gate")
    if raw_gate is not None:
        gate_u = str(raw_gate)
        if gate_u == UNKNOWN_LICENSE_GATE_FAIL_CLOSED:
            unknown_fail_closed = True
        elif gate_u == UNKNOWN_LICENSE_GATE_ARCHIVE:
            unknown_fail_closed = False
        else:
            logger.warning(
                "evidence_archiver.unknown_license_gate.bad_value value=%r — "
                "keeping the default (%s)",
                raw_gate,
                UNKNOWN_LICENSE_GATE_ARCHIVE if UNKNOWN_LICENSE_ARCHIVES
                else UNKNOWN_LICENSE_GATE_FAIL_CLOSED,
            )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _SELECT_CANDIDATES_SQL,
            window_hours, verify_floor, max_attempts, fetch_budget,
        )
    if not rows:
        return counters
    counters["examined"] = len(rows)

    politeness = _HostPoliteness(per_host_delay)
    started = time.monotonic()
    async with AsyncExitStack() as stack:
        # W11-F138 — the plain client opens lazily with the exact kwargs this
        # sweep always passed (see ``_FetchClients``); a second impersonated
        # leg opens only if a challenge-blocked policy host ever calls for it.
        clients = _FetchClients(stack, timeout=timeout_s)
        for row in rows:
            if time.monotonic() - started > deadline_s:
                counters["deadline_stop"] = 1
                break
            await _archive_one(
                pool, clients, row,
                root=root, politeness=politeness, counters=counters,
                forbid=forbid, max_bytes=max_bytes,
                max_text_chars=max_text_chars, max_attempts=max_attempts,
                web_origin_fail_closed=web_origin_fail_closed,
                unknown_fail_closed=unknown_fail_closed,
                prefer_ingest_copy=bool(options.get("prefer_ingest_copy", True)),
                intake_route=({
                    "pipeline": str(options.get("intake_pipeline") or ""),
                    "wait_minutes": float(options.get("intake_wait_minutes") or 60),
                } if options.get("fetch_via_intake") is True else None),
            )
    return counters


def _build_finding(counters: Mapping[str, Any]) -> FindingPayload:
    title = (
        f"Evidence archiver: archived {counters.get('archived', 0)} of "
        f"{counters.get('examined', 0)} cited signal(s), "
        f"{counters.get('skipped_license', 0)} license-skipped, "
        # R-3b — surfaced in the TITLE, not buried in the body: a fail-closed
        # skip is a decision the operator took, and it should be visible in the
        # cadence receipt without opening the row.
        f"{counters.get('skipped_license_unreviewed', 0)} web-unreviewed-skipped, "
        f"{counters.get('fetch_failed', 0)} failed"
    )
    # R-4 — appended ONLY when the policy actually refused something, so the
    # shipped-default title is unchanged character for character.
    if counters.get("skipped_license_unknown", 0):
        title += (
            f", {counters['skipped_license_unknown']} unknown-licence-skipped"
        )
    # (a′) Same discipline: appended ONLY when a block actually happened, so a
    # clean run's title is unchanged character for character.
    if counters.get(BLOCKED_BY_CHALLENGE, 0):
        title += f", {counters[BLOCKED_BY_CHALLENGE]} challenge-blocked"
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "evidence_archiver"]
    if counters.get("archived", 0):
        tags.append("archived")
    if counters.get("skipped_license_unreviewed", 0):
        tags.append("web_origin_license_unreviewed")
    if counters.get("skipped_license_unknown", 0):
        tags.append("unknown_license_fail_closed")
    if counters.get(BLOCKED_BY_CHALLENGE, 0):
        tags.append(BLOCKED_BY_CHALLENGE)
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "the next budgeted batch of un-archived cited
    signals"). ``deps is None`` (unit-test path) yields a zeroed run. Usage is
    always zeroed (deterministic kind, no LLM)."""
    counters = _zero_counters()
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        try:
            counters = await _sweep(pool, options)
        except Exception as exc:
            logger.warning("evidence_archiver.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "SUB_HANDLER_NAME",
    "ARCHIVE_ROOT_ENV",
    "DEFAULT_ARCHIVE_ROOT",
    "FORBID_RETENTION_CLASSES",
    "STATUS_BLOCKED_CHALLENGE",
    "STATUS_SKIPPED_LICENSE_UNREVIEWED",
    "UNREVIEWED_LICENSE_CLASSES",
    "WEB_ORIGIN_GATE_FAIL_CLOSED",
    "WEB_ORIGIN_GATE_INHERIT",
    "WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES",
    "UNKNOWN_LICENSE_ARCHIVES",
    "UNKNOWN_LICENSE_GATE_ARCHIVE",
    "UNKNOWN_LICENSE_GATE_FAIL_CLOSED",
    "license_unreviewed",
    "CAS_PREFIX",
    "cas_object_ref",
    "cas_path",
    "sha256_from_object_ref",
    "resolve_license_class",
    "resolve_signal_retrieval_origin",
    "license_forbids_retention",
    "web_origin_license_unreviewed",
]
