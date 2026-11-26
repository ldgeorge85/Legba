# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The model gateway behind the hosted-NLP client contract.

The legba-models service (GLiREL triples over spaCy NER, DeBERTa zero-shot,
NLLB translation) is dead since 2026-10-22. The model gateway serves the
same jobs on OpenAI-style routes with Bearer auth:

  * ``POST /entities``  — ``{"model": "ner", "text", "entities": [...]}`` →
    ``{"results": [{"entities": {label: [{"text", "confidence"}]}}]}``
  * ``POST /relations`` — ``{"model": "ner", "text", "relations": [...]}`` →
    ``{"results": [{"relation_extraction": {label: [hit, ...]}}]}`` where a hit
    is ``{"head": {"text", "confidence"}, "tail": {...}}`` with
    ``include_confidence`` and a plain ``[head, tail]`` pair without it.
  * ``POST /classify``  — ``{"model": "classify", "input", "labels"}`` →
    zero-shot scores (shape unverified; read defensively below).
  * no translate route — but ``POST /chat/completions`` serves a multilingual
    chat model (alias ``fast``, thinking off), which :meth:`translate` asks
.

:class:`GatewayNlpClient` exposes the SAME public methods, return shapes and
exceptions as :class:`~legba.data.stack.nlp_service.client.NlpServiceClient`,
so ``ner_multilingual``, ``classify`` and ``fact_extractor`` run on it
unchanged:

  * :meth:`extract` → ONE ``/relations`` call, answered as the legacy
    ``{"triples": [{"subject", "predicate", "object", ...}], "ms"}``. The
    predicate is the CONFIGURED label (lowercase-spaced, e.g. ``"leader of"``),
    whatever separator the gateway keys its answer by, because the fact write
    path and ``ner.py``'s predicate cues key on that surface. The
    default labels are DERIVED from the graph's relationship types, the
    triple's ``confidence`` is the min of its endpoint scores, hits below
    ``relation_threshold`` (sent as ``threshold``) and junk pairs are dropped
    and counted — see :mod:`.gateway_relations`.
  * :meth:`translate` → one chat completion per chunk on the
    configured ``translate_model``, answered as the legacy
    ``{"translated", "source_lang", "target_lang", "ms"}``; an empty or
    oversized answer is :class:`NlpServiceUnavailable`, never a stamp. With no
    ``translate_model`` it raises :class:`NlpServiceUnsupported` without a
    network call and :attr:`supports_translate` is ``False``, so the backfill
    sweeps' wiring reports ``skipped_no_nlp``.
  * :meth:`summarize` raises :class:`NlpServiceUnsupported` (a
    :class:`NlpServiceUnavailable`) WITHOUT a network call, so every caller's
    existing degrade path takes it.

No request text is ever logged or put in an exception message.
"""

from __future__ import annotations
from legba.shared.sampling import HOUSE_TEMPERATURE

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence

import httpx

from .client import NlpServiceAuthError, NlpServiceUnavailable
from .gateway_relations import (
    DEFAULT_RELATION_LABELS,
    apply_relation_quality,
    coerce_confidence_mode,
    coerce_threshold,
)
from .gateway_translate import (
    DEFAULT_CHUNK_CHARS,
    chat_content,
    clean_translation,
    normalise_lang,
    split_for_translation,
    system_prompt,
    within_size_bound,
)

logger = logging.getLogger(__name__)


PROVIDER_LEGBA_MODELS = "legba_models"
PROVIDER_BEARER_GATEWAY = "bearer_gateway"
NLP_PROVIDERS: tuple[str, ...] = (PROVIDER_LEGBA_MODELS, PROVIDER_BEARER_GATEWAY)

#: ``DEFAULT_RELATION_LABELS`` (the labels ``extract`` asks for when the
#: component names none) is derived from the graph's relationship types in
#: :mod:`.gateway_relations` and re-exported here; legba-models' 20
#: generic GLiREL labels are kept there as ``LEGACY_GLIREL_RELATION_LABELS``.

#: Legba's closed entity taxonomy (``legba.data.vocabulary.ENTITY_CLASSES``)
#: less the ``entity`` catch-all, which names no span a span model can find.
DEFAULT_ENTITY_LABELS: tuple[str, ...] = (
    "person",
    "organization",
    "location",
    "country",
    "event",
    "corporation",
    "software",
    "concept",
)

#: legba-models' server-default classify categories and their short names
#: (``CATEGORIES`` / ``CATEGORY_MAP``) — sent when a caller passes no labels
#: (``ClassifyConfig.use_server_defaults``), mapped back the same way.
DEFAULT_CLASSIFY_CATEGORIES: dict[str, str] = {
    "armed conflict or military action": "conflict",
    "government legislation or diplomatic action": "political",
    "economic development or financial markets": "economic",
    "public health or disease outbreak": "health",
    "environmental or climate event": "environment",
    "technology or cybersecurity": "technology",
    "natural disaster or humanitarian crisis": "disaster",
    "social unrest or protest": "social",
}

#: Attempts per call (the first plus two retries) on 429 / 5xx / transport.
MAX_ATTEMPTS = 3
#: Backoff between attempts when the gateway names no ``Retry-After``.
_BACKOFF_S: tuple[float, ...] = (0.5, 1.0)
#: Longest ``Retry-After`` this client sleeps for. Enrichment runs under a
#: per-entry budget of seconds; a longer ask fails the call NOW (unavailable,
#: the callers degrade) instead of sleeping past the caller's own deadline.
MAX_RETRY_WAIT_S = 10.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})

#: The gateway chat alias translation asks when the component names none
#: (``translate_model`` absent; the client factory applies it): the core chat model,
#: thinking off — multilingual, and the cheapest lane on the gateway.
DEFAULT_TRANSLATE_MODEL = "fast"


class NlpServiceUnsupported(NlpServiceUnavailable):
    """The configured provider has no route for this method (translate,
    summarize on the gateway). A :class:`NlpServiceUnavailable` so every
    existing degrade path catches it; raised without a network call."""


class NlpServiceRejected(NlpServiceUnavailable):
    """The gateway refused the request body (a non-retryable 4xx such as 400 /
    422). Carries the ``status`` and the sanitised ``detail`` so a caller can
    tell WHICH field was refused (``extract`` drops an unknown ``threshold``
    and retries once) — still an :class:`NlpServiceUnavailable` everywhere
    else, as before."""

    def __init__(self, message: str, *, status: int, detail: str) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


def _norm_label(label: str) -> str:
    return " ".join(str(label).replace("_", " ").split()).casefold()


def _error_detail(resp: httpx.Response) -> str:
    """The gateway's ``error.type`` / ``error.message`` / ``error.limit``.

    Never the raw body: a validation error body may echo the request's own
    ``input`` (the signal text), which must not reach a log line.
    """
    try:
        body = resp.json()
    except Exception:
        return "(non-JSON error body)"
    err = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(err, Mapping):
        parts = [str(err.get(k)) for k in ("type", "message") if err.get(k)]
        if err.get("limit"):
            parts.append(f"limit={err['limit']}")
        return ": ".join(parts)[:300]
    detail = body.get("detail") if isinstance(body, Mapping) else None
    if isinstance(detail, str):
        return detail[:300]
    if isinstance(detail, list):  # FastAPI-style: field + message, never "input"
        msgs = [
            f"{'.'.join(str(x) for x in d.get('loc', []))}: {d.get('msg')}"
            for d in detail if isinstance(d, Mapping)
        ]
        return "; ".join(msgs)[:300]
    return "(unrecognised error body)"


def _names_field(exc: NlpServiceRejected, field: str) -> bool:
    """A 400/422 whose sanitised detail names ``field`` — the gateway refusing
    that body field (unknown, or out of range)."""
    return exc.status in (400, 422) and field in exc.detail.casefold()


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    # Import-local: the llm package __init__ pulls every provider handler, which
    # the NLP client has no business loading at import time.
    from ..llm.base import _parse_retry_after

    return _parse_retry_after(resp.headers.get("retry-after"))


def _endpoint_text(value: Any) -> tuple[str, float | None]:
    """A head/tail endpoint as (text, confidence|None) — dict or bare string."""
    if isinstance(value, Mapping):
        text = value.get("text")
        conf = value.get("confidence", value.get("score"))
        try:
            conf_f = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf_f = None
        return (str(text).strip() if text is not None else ""), conf_f
    if isinstance(value, str):
        return value.strip(), None
    return "", None


def _first_result(data: Any) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        return {}
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], Mapping):
        return results[0]
    return data


def relations_to_triples(
    data: Any, labels: Sequence[str],
) -> list[dict[str, Any]]:
    """Map a ``/relations`` answer onto the legacy ``/extract`` triple shape.

    ``{"subject", "predicate", "object", "subject_start", "subject_end",
    "object_start", "object_end"}`` (offsets ``None`` — the gateway gives none
    for relations; the callers re-locate spans themselves) plus the endpoint
    scores as ``subject_confidence`` / ``object_confidence`` when present.
    NO ``confidence`` key here: the gateway scores endpoints, not the
    relation. :func:`~.gateway_relations.apply_relation_quality` stamps one
    from the two endpoint scores (``relation_confidence``, W11-F113) — or, on
    mode ``none``, leaves it absent so the fact write path applies its
    documented floor.
    """
    canon = {_norm_label(lbl): lbl for lbl in labels}
    block = _first_result(data).get("relation_extraction")
    if not isinstance(block, Mapping):
        return []
    triples: list[dict[str, Any]] = []
    for key, hits in block.items():
        predicate = canon.get(_norm_label(key), _norm_label(key))
        if not predicate or not isinstance(hits, list):
            continue
        for hit in hits:
            if isinstance(hit, Mapping):
                head, tail = hit.get("head"), hit.get("tail")
            elif isinstance(hit, (list, tuple)) and len(hit) >= 2:
                head, tail = hit[0], hit[1]
            else:
                continue
            subj, subj_conf = _endpoint_text(head)
            obj, obj_conf = _endpoint_text(tail)
            if not subj or not obj:
                continue
            triple: dict[str, Any] = {
                "subject": subj,
                "predicate": predicate,
                "object": obj,
                "subject_start": None,
                "subject_end": None,
                "object_start": None,
                "object_end": None,
            }
            if subj_conf is not None:
                triple["subject_confidence"] = subj_conf
            if obj_conf is not None:
                triple["object_confidence"] = obj_conf
            triples.append(triple)
    return triples


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def entities_to_list(data: Any) -> list[dict[str, Any]]:
    """Map an ``/entities`` answer to ``[{"text", "label", "confidence",
    "start", "end"}]`` (offsets ``None`` unless the gateway sent spans)."""
    block = _first_result(data).get("entities")
    if not isinstance(block, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for label, hits in block.items():
        if not isinstance(hits, list):
            continue
        for hit in hits:
            text, conf = _endpoint_text(hit)
            if not text:
                continue
            start = end = None
            if isinstance(hit, Mapping):
                start, end = _as_int(hit.get("start")), _as_int(hit.get("end"))
                span = hit.get("span")
                if start is None and isinstance(span, (list, tuple)) and len(span) >= 2:
                    start, end = _as_int(span[0]), _as_int(span[1])
            out.append({
                "text": text, "label": str(label), "confidence": conf,
                "start": start, "end": end,
            })
    return out


def _score_map(node: Any) -> dict[str, float] | None:
    """A label→score mapping out of one classify result node, or ``None``."""
    if not isinstance(node, Mapping):
        return None
    labels, scores = node.get("labels"), node.get("scores")
    if isinstance(labels, list) and isinstance(scores, list) and len(labels) == len(scores):
        try:
            return {str(lbl): float(s) for lbl, s in zip(labels, scores)}
        except (TypeError, ValueError):
            return None
    for key in ("scores", "classification", "classifications", "label_scores"):
        val = node.get(key)
        if isinstance(val, Mapping) and val:
            try:
                return {str(k): float(v) for k, v in val.items()}
            except (TypeError, ValueError):
                return None
        if isinstance(val, list) and val and all(isinstance(v, Mapping) for v in val):
            pairs = _label_score_pairs(val)
            if pairs is not None:
                return pairs
    for key in ("label", "category"):
        if isinstance(node.get(key), str) and node.get(key):
            score = node.get("score", node.get("confidence", 1.0))
            try:
                return {node[key]: float(score)}
            except (TypeError, ValueError):
                return {node[key]: 1.0}
    return None


def _label_score_pairs(items: list[Any]) -> dict[str, float] | None:
    out: dict[str, float] = {}
    for item in items:
        if not isinstance(item, Mapping):
            return None
        label = item.get("label", item.get("category"))
        score = item.get("score", item.get("confidence"))
        if not isinstance(label, str):
            return None
        try:
            out[label] = float(score) if score is not None else 1.0
        except (TypeError, ValueError):
            return None
    return out or None


def classify_scores(data: Any) -> dict[str, float] | None:
    """Every shape a zero-shot answer plausibly takes, to label→score."""
    if not isinstance(data, Mapping):
        return None
    results = data.get("results", data.get("data"))
    if isinstance(results, list) and results:
        if all(isinstance(r, Mapping) for r in results):
            first = _score_map(results[0])
            if first is not None and len(results) == 1:
                return first
            pairs = _label_score_pairs(results)
            if pairs is not None:
                return pairs
            if first is not None:
                return first
    return _score_map(data)


class GatewayNlpClient:
    """Async client for the model gateway's NLP routes (Bearer auth).

    Same public surface as :class:`NlpServiceClient`: :meth:`health`,
    :meth:`classify`, :meth:`extract`, :meth:`translate`, :meth:`summarize`,
    :meth:`aclose` and the async context manager. Adds :meth:`entities`.
    """

    provider = PROVIDER_BEARER_GATEWAY

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        classify_path: str = "/classify",
        entities_path: str = "/entities",
        relations_path: str = "/relations",
        health_path: str = "/models",
        relation_labels: Sequence[str] | None = None,
        entity_labels: Sequence[str] | None = None,
        relation_confidence: str | None = None,
        relation_threshold: float | None = None,
        ner_model: str = "ner",
        classify_model: str = "classify",
        translate_model: str | None = None,
        chat_path: str = "/chat/completions",
        translate_chunk_chars: int = DEFAULT_CHUNK_CHARS,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is required")
        if not api_key:
            raise ValueError("api_key is required (the gateway is Bearer-only)")
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout_seconds
        self._classify_path = classify_path
        self._entities_path = entities_path
        self._relations_path = relations_path
        self._health_path = health_path
        self._relation_labels = list(relation_labels or DEFAULT_RELATION_LABELS)
        self._entity_labels = list(entity_labels or DEFAULT_ENTITY_LABELS)
        # None = the provider defaults ("min", 0.5); a threshold of
        # 0 is OFF. Bad values fail at construction, never at the first call.
        self._relation_confidence = coerce_confidence_mode(relation_confidence)
        self._relation_threshold = coerce_threshold(relation_threshold)
        # Flipped once the gateway refuses ``threshold`` on /relations: later
        # calls stop sending it and rely on the client-side filter alone.
        self._threshold_refused = False
        self._extract_counts = {"calls": 0, "hits": 0, "kept": 0, "dropped_junk": 0,
                                "dropped_below_threshold": 0, "threshold_refused": 0}
        self._ner_model = ner_model
        self._classify_model = classify_model
        self._translate_model = (translate_model or "").strip() or None
        self._chat_path = chat_path
        if translate_chunk_chars <= 0:
            raise ValueError("translate_chunk_chars must be positive")
        self._translate_chunk_chars = translate_chunk_chars
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client
        self._owned_client = client is None
        self._sleep = sleep or asyncio.sleep

    @property
    def supports_translate(self) -> bool:
        """``True`` when a ``translate_model`` is configured — read by
        ``analyst_deps_builder`` to wire (or skip) the backfill sweeps."""
        return self._translate_model is not None

    @property
    def translate_model(self) -> str | None:
        return self._translate_model

    @property
    def relation_labels(self) -> list[str]:
        return list(self._relation_labels)

    @property
    def entity_labels(self) -> list[str]:
        return list(self._entity_labels)

    @property
    def relation_confidence(self) -> str:
        return self._relation_confidence

    @property
    def relation_threshold(self) -> float | None:
        return self._relation_threshold

    @property
    def extract_counts(self) -> dict[str, int]:
        """Cumulative ``extract`` counters for this client (calls, hits, kept,
        dropped_junk, dropped_below_threshold, threshold_refused)."""
        return dict(self._extract_counts)

    # ------------------------------------------------------------------ plumbing

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._endpoint, timeout=httpx.Timeout(self._timeout),
            )
            self._owned_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "GatewayNlpClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None,
        *, meta: dict[str, str] | None = None,
    ) -> Any:
        """One gateway call with the bounded retry; JSON body or a typed error.
        ``meta``, when given, receives the answer's ``x-served-model`` header."""
        client = self._ensure_client()
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            wait: float | None = None
            try:
                resp = await client.request(
                    method, path, json=body, headers=self._headers,
                )
            except httpx.HTTPError as exc:
                last = f"{method} {path} network error: {type(exc).__name__}"
            else:
                status = resp.status_code
                if status in (401, 403):
                    raise NlpServiceAuthError(
                        f"{method} {path} -> {status}: {_error_detail(resp)}"
                    )
                if status < 400:
                    if meta is not None and resp.headers.get("x-served-model"):
                        meta["served_model"] = resp.headers["x-served-model"]
                    try:
                        return resp.json()
                    except Exception as exc:
                        raise NlpServiceUnavailable(
                            f"{method} {path} non-JSON response: {type(exc).__name__}"
                        ) from exc
                detail = _error_detail(resp)
                last = f"{method} {path} -> HTTP {status}: {detail}"
                if status not in _RETRY_STATUSES:
                    # A 400/422 names the bad field; retrying the same body is
                    # pointless. Unavailable, like the legacy client's 4xx.
                    raise NlpServiceRejected(last, status=status, detail=detail)
                wait = _retry_after_seconds(resp)
                if wait is not None and wait > MAX_RETRY_WAIT_S:
                    raise NlpServiceUnavailable(
                        f"{last} (Retry-After {wait:.0f}s exceeds the "
                        f"{MAX_RETRY_WAIT_S:.0f}s client ceiling)"
                    )
            if attempt + 1 >= MAX_ATTEMPTS:
                break
            if wait is None:
                wait = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
            logger.debug(
                "nlp_gateway.retry path=%s attempt=%d wait=%.1fs", path, attempt + 1, wait,
            )
            await self._sleep(wait)
        raise NlpServiceUnavailable(f"{last} (after {MAX_ATTEMPTS} attempts)")

    # ------------------------------------------------------------------ methods

    async def health(self) -> dict[str, Any]:
        """GET the model list; synthesised into the legacy health dict
        ``{"status": "ok", "models_loaded": True, "models": [...]}``."""
        data = await self._request("GET", self._health_path)
        models: list[str] = []
        listing = data.get("data") if isinstance(data, Mapping) else None
        if isinstance(listing, list):
            models = [
                str(m.get("id")) for m in listing
                if isinstance(m, Mapping) and m.get("id")
            ]
        return {
            "status": "ok",
            "models_loaded": True,
            "models": models,
            "provider": self.provider,
        }

    async def extract(self, text: str) -> dict[str, Any]:
        """ONE ``/relations`` call → ``{"triples": [...], "ms": float}``.

        ``threshold`` rides on the body when one is configured; a
        400/422 that names it is retried ONCE without it (and the client stops
        sending it), so an unknown field never costs the whole call. Whatever
        the server did with it, the threshold is applied again here to the
        aggregate score, the junk gate runs, and the triple's ``confidence`` is
        stamped per ``relation_confidence``. Counts (never text) go to one
        debug line and :attr:`extract_counts`.
        """
        t0 = time.perf_counter()
        body: dict[str, Any] = {
            "model": self._ner_model,
            "text": text,
            "relations": list(self._relation_labels),
            "include_confidence": True,
        }
        threshold = self._relation_threshold
        if threshold is not None and not self._threshold_refused:
            body["threshold"] = threshold
        refused_now = False
        try:
            data = await self._request("POST", self._relations_path, body)
        except NlpServiceRejected as exc:
            if "threshold" not in body or not _names_field(exc, "threshold"):
                raise
            self._threshold_refused = refused_now = True
            self._extract_counts["threshold_refused"] += 1
            logger.warning(
                "nlp_gateway.threshold_refused path=%s status=%d — retrying without "
                "it; filtering client-side at %.2f from now on",
                self._relations_path, exc.status, threshold,
            )
            body.pop("threshold")
            data = await self._request("POST", self._relations_path, body)
        triples, counts = apply_relation_quality(
            relations_to_triples(data, self._relation_labels),
            confidence_mode=self._relation_confidence,
            threshold=threshold,
        )
        self._extract_counts["calls"] += 1
        for key in ("hits", "kept", "dropped_junk", "dropped_below_threshold"):
            self._extract_counts[key] += counts[key]
        logger.debug(  # counts only — never the text
            "nlp_gateway.extract hits=%d kept=%d dropped_junk=%d "
            "dropped_below_threshold=%d threshold=%s threshold_sent=%s "
            "confidence=%s",
            counts["hits"], counts["kept"], counts["dropped_junk"],
            counts["dropped_below_threshold"],
            "off" if threshold is None else f"{threshold:g}",
            "threshold" in body and not refused_now, self._relation_confidence,
        )
        return {
            "triples": triples,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    async def entities(
        self, text: str, *, labels: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """``/entities`` → ``{"entities": [{"text", "label", "confidence",
        "start", "end"}], "ms": float}``. No caller needs it today (``ner.py``
        reads its entities off the ``/extract`` triples); kept for the
        entity-only consumers the gateway makes cheap."""
        t0 = time.perf_counter()
        data = await self._request("POST", self._entities_path, {
            "model": self._ner_model,
            "text": text,
            "entities": list(labels or self._entity_labels),
            "include_confidence": True,
            "include_spans": True,
        })
        return {
            "entities": entities_to_list(data),
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    async def classify(
        self, text: str, *, labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """``/classify`` → the legacy ``{"category", "confidence", "scores",
        "ms"}``. ``labels=None`` sends legba-models' default categories and maps
        them back to their short names, as that server did."""
        t0 = time.perf_counter()
        send = list(labels) if labels else list(DEFAULT_CLASSIFY_CATEGORIES)
        data = await self._request("POST", self._classify_path, {
            "model": self._classify_model, "input": text, "labels": send,
        })
        scores = classify_scores(data)
        if not scores:
            raise NlpServiceUnavailable(
                f"POST {self._classify_path}: unrecognised response shape "
                f"(keys={sorted(data)[:6] if isinstance(data, Mapping) else type(data).__name__})"
            )
        if not labels:
            scores = {DEFAULT_CLASSIFY_CATEGORIES.get(k, k): v for k, v in scores.items()}
        top = max(scores, key=lambda k: scores[k])
        return {
            "category": top,
            "confidence": round(scores[top], 4),
            "scores": {k: round(v, 4) for k, v in scores.items()},
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    async def translate(
        self, text: str, source_lang: str | None, target_lang: str = "en",
    ) -> dict[str, Any]:
        """The legacy ``/translate`` on the chat model → ``{"translated",
        "source_lang", "target_lang", "ms"}``.

        ONE completion per chunk (chunks only above ``translate_chunk_chars``,
        split at paragraph boundaries and re-joined with the original
        separators), temperature 1.0, no ``max_tokens``. ``source_lang`` names
        the source language in the prompt and is echoed back as the legacy
        server did (``None`` / ``und`` → ``"und"``; nothing is detected — the
        legacy shape echoes, it never detected). A ``source_lang`` equal to the
        target returns the text unchanged without a call. An empty answer, or
        one more than three times its input, is :class:`NlpServiceUnavailable`.
        """
        if self._translate_model is None:
            raise NlpServiceUnsupported(
                "translate: no translate_model configured on this gateway component"
            )
        t0 = time.perf_counter()
        src = normalise_lang(source_lang)
        tgt = normalise_lang(target_lang) or "en"
        if not text.strip() or src == tgt:
            translated = text
            chunks_sent = 0
        else:
            chunks, joins = split_for_translation(text, self._translate_chunk_chars)
            outputs: list[str] = []
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            served = ""
            for chunk in chunks:
                if not chunk.strip():
                    outputs.append(chunk)
                    continue
                out, chunk_usage, served = await self._translate_chunk(chunk, src, tgt)
                outputs.append(out)
                for key in usage:
                    usage[key] += _as_int(chunk_usage.get(key)) or 0
            translated = outputs[0] + "".join(
                join + out for join, out in zip(joins, outputs[1:])
            )
            chunks_sent = sum(1 for c in chunks if c.strip())
            logger.debug(  # counts only — never the text
                "nlp_gateway.translate src=%s chunks=%d chars_in=%d chars_out=%d "
                "prompt_tokens=%d completion_tokens=%d served_model=%s",
                src or "und", chunks_sent, len(text), len(translated),
                usage["prompt_tokens"], usage["completion_tokens"], served or "?",
            )
        return {
            "translated": translated,
            "source_lang": source_lang or "und",
            "target_lang": target_lang,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    async def _translate_chunk(
        self, chunk: str, src: str | None, tgt: str,
    ) -> tuple[str, Mapping[str, Any], str]:
        """One completion → (cleaned translation, usage, served model)."""
        meta: dict[str, str] = {}
        data = await self._request("POST", self._chat_path, {
            "model": self._translate_model,
            "temperature": HOUSE_TEMPERATURE,
            "messages": [
                {"role": "system", "content": system_prompt(src, tgt)},
                {"role": "user", "content": chunk},
            ],
        }, meta=meta)
        content = chat_content(data)
        out = clean_translation(content, chunk) if content is not None else ""
        if not out:
            raise NlpServiceUnavailable(
                f"POST {self._chat_path}: translate answer empty or unreadable"
            )
        if not within_size_bound(out, chunk):
            raise NlpServiceUnavailable(
                f"POST {self._chat_path}: translate answer refused — "
                f"{len(out.encode('utf-8'))} bytes for a "
                f"{len(chunk.encode('utf-8'))}-byte input (over 3x)"
            )
        usage = data.get("usage") if isinstance(data, Mapping) else None
        served = meta.get("served_model") or (
            data.get("model") if isinstance(data, Mapping) else None
        )
        return out, usage if isinstance(usage, Mapping) else {}, str(served or "")

    async def summarize(
        self, texts: list[str], max_length: int = 64,
    ) -> dict[str, Any]:
        raise NlpServiceUnsupported(
            "summarize: the model gateway has no summarization route"
        )


__all__ = [
    "DEFAULT_CLASSIFY_CATEGORIES",
    "DEFAULT_ENTITY_LABELS",
    "DEFAULT_RELATION_LABELS",
    "DEFAULT_TRANSLATE_MODEL",
    "GatewayNlpClient",
    "MAX_ATTEMPTS",
    "NLP_PROVIDERS",
    "NlpServiceRejected",
    "NlpServiceUnsupported",
    "PROVIDER_BEARER_GATEWAY",
    "PROVIDER_LEGBA_MODELS",
    "classify_scores",
    "entities_to_list",
    "relations_to_triples",
]
