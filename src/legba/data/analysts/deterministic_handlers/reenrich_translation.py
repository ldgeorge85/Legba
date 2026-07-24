# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``reenrich_translation`` sub-handler — TRANSLATION backfill (M13/T-1c).

The M11/M12 NER lane translates a non-Latin signal to English transiently, NERs
the translation, and then DISCARDS it. T-1a (``filters/ner.py``) fixes the
FORWARD path — every NEW non-Latin signal now stamps ``payload.title_en`` +
``payload.text_en`` at ingest. This sub-handler is the BACKFILL leg for the
~1.9k non-EN signals ingested BEFORE T-1a landed (0% carry a stored translation),
the exact rows the journal/chronicle narrator read raw + a transliterated NER
surface and inverted (the Rubio-inversion class, j7 review).

It is an ASYNC SWEEP (never inline in ingest), mirroring ``reenrich_ner`` /
``signal_summarizer`` / ``signal_embedder`` exactly: every time the bound
``deterministic`` analyst fires (cadence), it translates the next throttled batch
of candidate signals, draining the backlog over ~1-2 days, then idles (the
forward fix stamps every new non-Latin signal at ingest, so no new candidates
arrive).

Candidate = a signal whose payload language is in the non-Latin translate set
(``ar/fa/he/ru/uk/zh/ja/ko/hi/th/ur`` — the SAME routing set the NER filter uses)
AND that carries NO ``payload.title_en``. The idempotency marker IS the field
itself (``payload->>'title_en' IS NULL`` is the forward-progress gate), so — unlike
``reenrich_ner`` — this sweep needs NO marker column / migration: a row drains out
of the candidate set the moment its title_en is stamped, and a row whose translate
call fails simply stays a candidate and is retried next tick.

Per examined signal:
  * SELECT the next batch ``WHERE lower(payload->>'language') = ANY(<set>) AND
    payload->>'title_en' IS NULL ORDER BY fetched_at DESC LIMIT max_translate``
    (newest-first, per the summarizer's recency rationale — the freshest non-EN
    rows are the ones a live entry is most likely to narrate).
  * Translate the TITLE via the hosted /translate (the short field every slice
    renders). Stamp ``payload.title_en`` on success.
  * If the title translation succeeded AND a body field is present, ALSO translate
    the combined body (cheap-enough; it backs read_document / corpus reads) and
    stamp ``payload.text_en``. A body-translate failure never blocks the title
    stamp — title_en is the load-bearing field.

Degrade-not-break (mirrors ``reenrich_ner`` exactly):
  * If the hosted ``nlp_client`` is absent from ``deps.extras`` (the NLP plane
    didn't wire), the tick NO-OPs with a LOUD warning and ``skipped_no_nlp = 1``
    (the SELECT never runs).
  * A per-signal translate failure (service 5xx / exception) leaves the row
    UNSTAMPED — it stays a candidate (the field-is-the-marker gate) and is retried
    next tick, so a transient models-host outage never burns the batch. If NOTHING
    translated this tick (a probable outage) the sweep goes LOUD.

Output ``data`` keys (the cadence receipt the operator reads):
    examined        int — rows pulled this run
    titles_stamped  int — rows that GAINED payload.title_en this run
    texts_stamped   int — rows that ALSO gained payload.text_en this run
    failures        int — rows whose translate call failed this run
    skipped_no_nlp  int — 1 when the hosted nlp_client was not wired (else 0)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "reenrich_translation"

#: The key under which the runtime stashes the hosted :class:`NlpServiceClient` on
#: ``StandardDeps.extras`` for this sweep (wired in analyst_deps_builder when the
#: bound sub-handler is ``reenrich_translation``). Absent → the sweep no-ops that
#: tick.
NLP_DEPS_EXTRA_KEY = "reenrich_translation_nlp"

#: Per-run cap on the number of signals translated (each is a bounded hosted
#: /translate round-trip — a title, and when present a body). This IS the SELECT
#: ``LIMIT``. ~300/tick over the 15-min cadence drains the ~1.9k backlog in ~1-2
#: days, then idles (the forward T-1a fix stamps every new non-Latin signal).
_DEFAULT_MAX_TRANSLATE = 300

#: Per-signal wall-clock timeout (seconds) around the translate calls. On expiry
#: the row DEGRADES to a failure (never a hang) — same shape as reenrich_ner. The
#: hosted client carries its own httpx timeout too; this is the belt.
TRANSLATE_TIMEOUT_SECONDS = 60.0

#: Max chars fed to /translate (mirrors NERMultilingualConfig.max_text_chars — the
#: NLLB model's input bound; over-long input is truncated, never rejected).
_MAX_TRANSLATE_CHARS = 4000

#: The non-Latin source-language set routed through translate, kept IN SYNC with
#: ``ner.py::_DEFAULT_TRANSLATE_LANGS`` / ``reenrich_ner`` (a literal here so this
#: backfill module stays self-contained and the SELECT never imports the
#: httpx-bearing ner module at query time). ``und`` is intentionally EXCLUDED —
#: language-detect emits ``und`` when it cannot decide a source lang, and NLLB
#: needs a concrete source code; those rows are handled by the forward NER lane's
#: script-inference fallback, not this language-keyed backfill.
_DEFAULT_TRANSLATE_LANGS: tuple[str, ...] = (
    "ar", "fa", "he", "ru", "uk", "zh", "ja", "ko", "hi", "th", "ur",
)

#: The payload text fields concatenated into the body translate input (title FIRST,
#: mirroring ``NERMultilingualConfig.text_fields`` so the body_en matches what NER
#: consumed). Kept a literal to avoid importing the ner config at query time.
_BODY_TEXT_FIELDS: tuple[str, ...] = ("title", "summary", "description")

#: NEWEST-first scan of the un-translated non-Latin candidate pool. The field IS
#: the marker: ``payload->>'title_en' IS NULL`` is the forward-progress gate, so a
#: row drains the instant its title_en is stamped (no marker column needed). $1 =
#: the per-run LIMIT; $2 = the translate-language set.
_SELECT_BATCH_SQL = """
    SELECT id, language, payload
      FROM signals
     WHERE (payload->>'title_en') IS NULL
       AND lower(payload->>'language') = ANY($2::text[])
     ORDER BY fetched_at DESC
     LIMIT $1
"""

#: Stamp the translated fields onto the payload in ONE atomic UPDATE. $1 = id; $2 =
#: title_en; $3 = text_en (may be NULL when the body translation was skipped/failed
#: — jsonb_set with a JSON null still records the attempt was made for title only,
#: so we branch in Python and only set text_en when non-NULL, keeping the field
#: ABSENT rather than JSON-null when there is no body_en).
_WRITE_TITLE_ONLY_SQL = """
    UPDATE signals
       SET payload = jsonb_set(
               COALESCE(payload, '{}'::jsonb), '{title_en}', $2::jsonb, true
           ),
           updated_at = now()
     WHERE id = $1
"""

_WRITE_TITLE_AND_TEXT_SQL = """
    UPDATE signals
       SET payload = jsonb_set(
               jsonb_set(
                   COALESCE(payload, '{}'::jsonb), '{title_en}', $2::jsonb, true
               ),
               '{text_en}', $3::jsonb, true
           ),
           updated_at = now()
     WHERE id = $1
"""


def _as_dict(v: Any) -> dict[str, Any]:
    """Coerce a payload column (jsonb dict or JSON string) to a dict; else ``{}``."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _source_lang(row_lang: Any, payload: Mapping[str, Any]) -> str | None:
    """Two-letter NLLB source-language code for a candidate row, or ``None``.

    Prefer the row ``language`` column, then ``payload.language`` (the SELECT
    already gated on payload.language being in the set, so this normally resolves).
    Returns a value only when it is in the translate set — never translate FROM an
    unknown/``und`` source (NLLB needs a concrete code)."""
    for src in (row_lang, payload.get("language")):
        if isinstance(src, str) and src:
            code = src.lower().split("-", 1)[0].split("_", 1)[0]
            if code in _DEFAULT_TRANSLATE_LANGS:
                return code
    return None


def _combined_body(payload: Mapping[str, Any]) -> str:
    """Concatenate the configured body text fields (title first), de-duplicated,
    mirroring ``NERMultilingualHandler._extract_text`` so text_en matches the NER
    input. Empty when no field carries text."""
    parts: list[str] = []
    seen: set[str] = set()
    for fld in _BODY_TEXT_FIELDS:
        val = payload.get(fld)
        if not val:
            continue
        if not isinstance(val, str):
            val = str(val)
        stripped = val.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        parts.append(stripped)
    return "\n".join(parts).strip()


async def _translate(
    nlp_client: Any, text: str, source_lang: str
) -> str | None:
    """One hosted /translate call, best-effort. Returns the stripped translation,
    or ``None`` on any failure (service error / empty result / exception) so the
    caller leaves the row a candidate for the next tick."""
    try:
        data = await asyncio.wait_for(
            nlp_client.translate(text, source_lang=source_lang, target_lang="en"),
            timeout=TRANSLATE_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    translated = data.get("translated") if isinstance(data, dict) else None
    if isinstance(translated, str) and translated.strip():
        return translated.strip()[:_MAX_TRANSLATE_CHARS]
    return None


async def _sweep_batch(
    pool: Any,
    *,
    nlp_client: Any,
    translate_languages: list[str],
    max_translate: int,
) -> dict[str, int]:
    """Translate the next throttled batch of non-Latin candidate signals.

    Sequential (no gather-fanout) so a tick never bursts the hosted plane. The
    pooled connection is NOT held across the slow translate calls — the batch is
    SELECTed once, then each stamped row briefly re-acquires a connection to write.
    All writes are idempotent + forward-progressing (the title_en-is-marker gate)."""
    counters = {
        "examined": 0,
        "titles_stamped": 0,
        "texts_stamped": 0,
        "failures": 0,
        "skipped_no_nlp": 0,
    }

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _SELECT_BATCH_SQL, max_translate, list(translate_languages)
        )
    if not rows:
        return counters
    counters["examined"] = len(rows)

    titles_stamped = 0
    texts_stamped = 0
    failures = 0

    for r in rows:
        payload = _as_dict(r["payload"])
        source_lang = _source_lang(r["language"], payload)
        raw_title = payload.get("title")
        if source_lang is None or not (
            isinstance(raw_title, str) and raw_title.strip()
        ):
            # No usable source lang or no title to translate — not a translate
            # FAILURE (the plane was never called); skip it. (The SELECT gates on a
            # set membership, so source_lang None here is a rare data edge.)
            continue

        title_src = raw_title.strip()[:_MAX_TRANSLATE_CHARS]
        title_en = await _translate(nlp_client, title_src, source_lang)
        if not title_en:
            # Translate hop failed → leave the row a candidate (retried next tick).
            failures += 1
            continue

        # Title stamped. ALSO translate the combined body when present — cheap and
        # backs read_document / corpus reads. A body failure never blocks the title.
        text_en: str | None = None
        body = _combined_body(payload)
        if body:
            text_en = await _translate(nlp_client, body[:_MAX_TRANSLATE_CHARS], source_lang)

        async with pool.acquire() as conn:
            if text_en:
                await conn.execute(
                    _WRITE_TITLE_AND_TEXT_SQL,
                    r["id"],
                    json.dumps(title_en),
                    json.dumps(text_en),
                )
                texts_stamped += 1
            else:
                await conn.execute(
                    _WRITE_TITLE_ONLY_SQL, r["id"], json.dumps(title_en)
                )
        titles_stamped += 1

    counters["titles_stamped"] = titles_stamped
    counters["texts_stamped"] = texts_stamped
    counters["failures"] = failures
    if titles_stamped == 0 and failures > 0:
        # Nothing stamped but rows failed → a probable models-host OUTAGE. The rows
        # stay candidates (title_en still NULL) so they retry next tick; go LOUD so
        # the misconfig/outage is visible (mirrors reenrich_ner's outage guard).
        logger.warning(
            "reenrich_translation.nlp_outage failures=%d — no signal translated "
            "this tick; rows left un-stamped for retry (likely a hosted NLP-plane "
            "outage, not poison rows)",
            failures,
        )
    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"Translation backfill: stamped title_en on "
        f"{counters.get('titles_stamped', 0)} signal(s) "
        f"(+{counters.get('texts_stamped', 0)} text_en), "
        f"examined {counters.get('examined', 0)}, "
        f"{counters.get('failures', 0)} failed"
    )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "reenrich_translation"]
    if counters.get("titles_stamped", 0):
        tags.append("translated")
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
    ignored — the unit of work is "the next batch of un-translated non-Latin
    signals"). ``deps is None`` (unit-test path) yields a zeroed run. Usage is
    always zeroed (deterministic kind, no LLM — the hosted translate plane is
    self-hosted / $0)."""
    counters: dict[str, int] = {
        "examined": 0,
        "titles_stamped": 0,
        "texts_stamped": 0,
        "failures": 0,
        "skipped_no_nlp": 0,
    }
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        nlp_client = _resolve_nlp(deps)
        if nlp_client is None:
            # The NLP plane isn't wired (dep missing / models-host unreachable at
            # deps-build). No-op this tick. Go LOUD so a mis-wire is observable.
            counters["skipped_no_nlp"] = 1
            logger.warning(
                "reenrich_translation.no_nlp — NlpServiceClient absent from "
                "deps.extras[%r]; the NLP plane did not wire (dep missing / "
                "models-host unreachable). Signals left un-translated this tick.",
                NLP_DEPS_EXTRA_KEY,
            )
        else:
            max_translate = int(options.get("max_translate", _DEFAULT_MAX_TRANSLATE))
            translate_languages = list(
                options.get("translate_languages", _DEFAULT_TRANSLATE_LANGS)
            )
            try:
                counters = await _sweep_batch(
                    pool,
                    nlp_client=nlp_client,
                    translate_languages=translate_languages,
                    max_translate=max_translate,
                )
            except Exception as exc:
                logger.warning("reenrich_translation.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


def _resolve_nlp(deps: Any | None) -> Any | None:
    """Pull the hosted :class:`NlpServiceClient` off ``deps.extras`` (or ``None``).

    Injected by
    :func:`legba.runtime.analyst_deps_builder._wire_reenrich_translation` when the
    bound sub-handler is ``reenrich_translation``. Absent → the sweep no-ops."""
    if deps is None:
        return None
    extras = getattr(deps, "extras", None)
    if not isinstance(extras, Mapping):
        return None
    return extras.get(NLP_DEPS_EXTRA_KEY)


__all__ = ["handle", "SUB_HANDLER_NAME", "NLP_DEPS_EXTRA_KEY"]
