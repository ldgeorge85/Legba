# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-B1/W-B2 — the BEARING PIPELINE: a semantic gate + a batched confirm over
the edges ``claim_watch`` has already decided to write.

WHY THIS EXISTS (and why it is a separate module)
-------------------------------------------------
``claim_watch``'s matcher is deterministic and stays that way. Two rounds of
deterministic levers (v3.1 the measured vector floor; v3.2 meta-question
exclusion, global hub damping, the omnibus/duplicate dampers) took it from
"desk co-membership wearing three names" to a real fusion model — and then hit
their ceiling at ~0.21 write precision on the K-4 gold population. The residual
failures are not weight-tuning failures: they are pairs where every plane is
honestly positive (the names are shared, the desk geo overlaps, the cosine
clears the floor) and the signal still does not BEAR on the thesis. No
arithmetic over those planes can separate them, because the distinguishing fact
is semantic and lives in neither the entity set nor the embedding neighbourhood.

Measured on 242 gold-labeled (signal, open-question) pairs, the idle
self-hosted Llama-3.1-8B (``llm.verify.slm_8b``) answers exactly that question
at specificity 0.900 / yes-precision 0.620 with a NAIVE prompt — i.e. it is
already a strong NEGATIVE filter (it rarely says yes to a bad pair) even before
any prompt work. That asymmetry is the whole design: the gate is used to
REFUSE, and refusal is the operation it is measured to be good at.

Why a module and not more lines in ``claim_watch.py``:

  * ``claim_watch.py`` is 2k lines whose contract is "NO alerts, no LLM calls,
    no writes to analysis outputs". Inlining two LLM clients, two prompt
    constants and two parsers there would make that contract unreadable, and
    the prompt-lab lane would be tuning a string buried in a matcher.
  * The seam is genuinely clean: the matcher produces CANDIDATES, this module
    stamps/filters them, the matcher writes what survives. Nothing in the
    fusion model, the cursor policy, the caps or the guards is touched.
  * The X-1 catalog drift guard reads ``options.get("...")`` call sites out of
    the SUB-HANDLER's own module file. Every option this pipeline honours is
    therefore read in ``claim_watch.handle`` and passed here as an explicit
    argument — the catalog stays the honest operator contract with no
    delegation exception to maintain.

THE TWO LEGS
------------
**W-B1 — the gate (per-edge, self-hosted 8B).** For each would-be edge, ask
the ``bearing_gate_ref`` component (default ``llm.verify.slm_8b``) the bearing
question over the question thesis + a signal digest.

  * YES  → the edge is written as before, plus ``data.bearing_gate='yes'``.
  * NO   → the edge is NOT written; the run receipt counts it
           (``bearing_gated_out``). This is the whole point of the leg.
  * unreachable / timeout / unparseable → **STAMP AND WRITE**. The edge is
    written with ``data.bearing_gate='unavailable'`` and the receipt counts
    ``bearing_gate_errors``. An idle 8B falling over must NEVER silence the
    deterministic matcher — a gate that fails CLOSED would turn one host
    outage into a silent hole in the bearing plane, which is exactly the class
    of failure this project refuses. Consumers filter on the stamp.
  * over the per-run call cap → stamp ``'deferred'`` and write, counted. Same
    reasoning: the budget is ours, the loss must not be the matcher's.

**W-B2 — the confirm (batched, $0 core plane).** For gate-YES edges ONLY, a
second opinion from the PRIMARY core-plane model (gpt-oss-120b — never
Anthropic; the deterministic plane is self-hosted-only and
``_wire_deterministic_llm`` hard-refuses an Anthropic component). Batched and
echo-bound by id, the ``signal_salience`` discipline: an item whose id matches
no pair in the batch is DROPPED rather than positionally guessed, because
binding a verdict to the wrong edge is worse than no verdict. Writes
``data.bearing_confirm`` = 'yes'/'no'/'unavailable' plus a one-line
``data.bearing_confirm_reason``.

The confirm NEVER blocks an edge. By the time it runs the gate has already
decided the edge is written; the confirm is a second, richer reading recorded
ON the edge for the consumers and for the measurement loop. A core-plane
outage stamps ``'unavailable'`` and moves on; an over-cap pair is simply not
stamped (``bearing_confirm_deferred``) rather than being given a value from a
vocabulary it never earned.

DEFAULT OFF
-----------
``bearing_gate`` defaults to ``'off'`` **in code**. A descriptor with no
``method.options`` block therefore contributes nothing, the pipeline never
constructs a client, and every edge is written with ``data = '{}'`` — which is
the 0116 column default, i.e. byte-identical to what 3.2.0 wrote. Turning the
pipeline on is a descriptor PUT at deploy time (``method.options.bearing_gate:
"on"``), not a code change and not a rebuild. That is the X-1 contract, held.

PROMPTS
-------
:data:`GATE_PROMPT` and :data:`CONFIRM_PROMPT` are module-level constants
carrying the NAIVE benchmark prompts as their initial values — the shapes the
0.900/0.620 measurement was taken with. They are the prompt-lab lane's tuning
surface; :data:`GATE_PROMPT_VERSION` / :data:`CONFIRM_PROMPT_VERSION` ride onto
every stamped edge so a precision shift is always attributable to the prompt
that produced it. Bump the version with the prompt, in the same commit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIRM_BATCH_SIZE",
    "CONFIRM_LLM_DEPS_EXTRA_KEY",
    "CONFIRM_PROMPT",
    "CONFIRM_PROMPT_VERSION",
    "DEFAULT_BEARING_CONFIRM_CAP",
    "DEFAULT_BEARING_GATE",
    "DEFAULT_BEARING_GATE_CAP",
    "DEFAULT_BEARING_GATE_REF",
    "GATE_MODES",
    "GATE_PROMPT",
    "GATE_PROMPT_VERSION",
    "SLM_DEPS_EXTRA_KEY",
    "EdgeCandidate",
    "bearing_counter_defaults",
    "gate_enabled",
    "parse_confirm_batch",
    "parse_gate_verdict",
    "run_bearing_pipeline",
    "signal_digest",
]


# ---------------------------------------------------------------------------
# Options — DEFAULTS LIVE HERE (the handler's own `options.get(key, DEFAULT)`
# is the single source of truth per the X-1 contract; the catalog declares
# only type + range).
# ---------------------------------------------------------------------------

#: ``bearing_gate`` — the pipeline switch. OFF in code so a descriptor with no
#: ``method.options`` is byte-identical to 3.2.0; the operator flips it on with
#: a descriptor PUT at deploy.
DEFAULT_BEARING_GATE = "off"

#: The admissible values of that switch (choice-locked in the X-1 catalog).
GATE_MODES: tuple[str, ...] = ("on", "off")

#: ``bearing_gate_ref`` — the stack component the gate asks. The idle
#: self-hosted Llama-3.1-8B behind Caddy Basic Auth; NOT the core plane (the
#: gate is per-edge and wants a small, always-warm model).
DEFAULT_BEARING_GATE_REF = "llm.verify.slm_8b"

#: ``bearing_gate_cap`` — gate calls per run. Sized against the sibling
#: ``edge_cap`` (200) so a healthy run gates its whole batch; the overflow
#: stamps 'deferred' and is written, never dropped. 0 disables the calls while
#: leaving the leg on (every edge stamps 'deferred') — a deliberate
#: measure-the-shape mode, not a mistake.
DEFAULT_BEARING_GATE_CAP = 200

#: ``bearing_confirm_cap`` — core-plane confirm judgments per run, over
#: gate-YES edges only. Lower than the gate cap on purpose: the confirm is a
#: richer, batched read for the measurement loop, not a filter. 0 disables the
#: leg entirely (nothing is stamped, nothing is counted as an error).
DEFAULT_BEARING_CONFIRM_CAP = 60

#: ``deps.extras`` key the GATE client rides. Not wired by the deps builder
#: (the gate resolves its own component lazily from the registry, because the
#: component id is a run OPTION the builder cannot see); present only when a
#: caller injects one — the test seam, and an escape hatch for a future
#: pre-resolved wiring.
SLM_DEPS_EXTRA_KEY = "claim_watch_bearing_gate_llm"

#: ``deps.extras`` key the CONFIRM client rides. Wired by
#: ``analyst_deps_builder._wire_deterministic_llm`` from the descriptor's
#: ``method.llm.primary`` — the same $0 core-plane path ``signal_summarizer``
#: and ``signal_salience`` use, with the same Anthropic hard-refuse.
CONFIRM_LLM_DEPS_EXTRA_KEY = "claim_watch_bearing_confirm_llm"


# ---------------------------------------------------------------------------
# Bounds (module constants — deliberately NOT operator knobs; see the X-1
# catalog note: every declared knob must be one an operator has a reason to
# move, and these are wire-shape bounds, not policy)
# ---------------------------------------------------------------------------

#: Per-gate-call wall clock. Generous for an idle 8B on a warm host, short
#: enough that a hung endpoint costs one run's budget rather than the tick.
GATE_TIMEOUT_SECONDS = 30.0
#: Per-confirm-batch wall clock (a batch carries several pairs).
CONFIRM_TIMEOUT_SECONDS = 90.0
#: Pairs per confirm call. Small: each pair carries a thesis AND a signal
#: digest, and the echo-bound parse degrades a WHOLE batch on a failed call.
CONFIRM_BATCH_SIZE = 8
#: Bounded prompt inputs (the signal_embedder / signal_salience rule).
MAX_THESIS_CHARS = 600
MAX_SIGNAL_CHARS = 600
#: Stored reason length — one line, not an essay.
MAX_REASON_CHARS = 240
#: Output budget. vLLM ignores this unless explicitly opted in (see
#: ``vllm.py``), so it bounds non-vLLM providers without changing the core
#: plane's behaviour.
GATE_MAX_TOKENS = 8
CONFIRM_MAX_TOKENS = 1200


# ---------------------------------------------------------------------------
# THE PROMPTS — the prompt-lab lane's tuning surface
# ---------------------------------------------------------------------------

#: Bump WITH the prompt, in the same commit: this string is stamped onto every
#: edge the gate passes, and it is the only thing that tells an edge judged by
#: one prompt apart from an edge judged by another.
GATE_PROMPT_VERSION = "fewshot/1"

#: The prompt-lab's chosen prompt (planning-doc measurement 2026-07-30,
#: seed 20260730, 50/50 stratified split): the NAIVE criterion as the system
#: message plus SEVEN few-shot exemplar turns (4 NO drawn from the measured
#: failure taxonomy — hub fan-out, same-desk-off-topic, wrong-decision-locus,
#: vocabulary collision — and 3 YES), all drawn from the TRAIN half only.
#: Held-out validation: yes-precision 0.842 (naive 0.696), specificity 0.969,
#: recall 0.615 unchanged. Instruction rewrites all failed the recall gate —
#: the 8B pattern-matches worked failures better than it executes stricter
#: definitions. A better prompt is a new version with a new measurement,
#: never an edit to this one.
GATE_SYSTEM_PROMPT = (
    "Does this news item provide genuine evidence bearing on the thesis - "
    "supporting, refuting, or materially updating it? Being about the same "
    "country or actors is NOT enough; it must speak to what the thesis "
    "asserts. Answer with exactly one word: YES or NO."
)

#: (user, assistant) exemplar turns, verbatim from the measured prompt. The
#: exemplar rows come from the gold worksheet's TRAIN half (never validation).
GATE_FEWSHOT_TURNS: tuple[tuple[str, str], ...] = (
    (
        'Question thesis: "Collection gap: the leadership_transition dimension '
        "for desk country_g20_au is starved (low-faithfulness; persistent 2/2 "
        'cards). What sources would close it? Plausible source classes: '
        'official, reporting, analysis."\n'
        'News item: "\'It was all about money\': UK court convicts Norwegian '
        "teen 'hitman' in murder conspiracy linked to Iran-linked group - "
        "Natland was arrested in March last year from a hotel room in "
        'Huddersfield in northern England."',
        "NO",
    ),
    (
        'Question thesis: "How will continued monsoon flooding affect '
        "Pakistan's domestic power generation and fuel logistics?\"\n"
        'News item: "Govt aims to provide citizens access to all public '
        "facilities under single digital ID: PM Shehbaz - Prime Minister "
        "Shehbaz Sharif said on Wednesday that the government's vision for "
        "national digitalisation aims to provide citizens access to all "
        'public facilities under a single digital identity."',
        "NO",
    ),
    (
        'Question thesis: "Will the US proceed with airstrikes against JNIM '
        'in Mali, and how might that affect the insurgent dynamics?"\n'
        'News item: "Trump Says USA Will Strike Iran Hard - President Donald '
        "Trump said the U.S. would hit Iran hard after a recent attack that "
        'targeted a military base in Jordan."',
        "NO",
    ),
    (
        'Question thesis: "Will Iran enact the announced gasoline price '
        "increase within the next month, and how will it affect domestic fuel "
        'consumption?"\n'
        'News item: "2/3 of US adults say Iran war hasn\'t been worthwhile; '
        "28% back Trump's handling of it - AP-NORC poll shows 61% of "
        "Republicans approve of conflict, down from 71% last month; 72% of US "
        "adults say it's extremely or very important to keep gas prices in "
        'check."',
        "NO",
    ),
    (
        'Question thesis: "Will Iran proceed with closing the Strait of '
        'Hormuz if diplomatic proposals fail?"\n'
        'News item: "**Iran has rejected an Omani proposal for joint '
        "management of the Strait of Hormuz**, Reuters reported, citing "
        "sources. The initiative, supported by the Gulf countries, was "
        "modeled after the transit framework through the Strait of Malacca. "
        'Tehran disagreed with an equal share of control."',
        "YES",
    ),
    (
        'Question thesis: "Will continued elite reshuffles undermine cohesion '
        "within Ukraine's military leadership?\"\n"
        'News item: "The battlefield Ukraine\'s new commander-in-chief '
        "inherits as Russian troops push toward Ukraine's main defensive "
        "stronghold in Donbas - Ukraine's new commander-in-chief, Mykhailo "
        "Drapatyi, inherited a difficult situation on the front lines from "
        'his predecessor, Oleksandr Syrskyi."',
        "YES",
    ),
    (
        'Question thesis: "How long will the Jizan and Abqaiq facilities '
        "remain offline, and what impact will prolonged outages have on Saudi "
        'export capacity?"\n'
        'News item: "Middle East war: US and Saudi Arabia take unprecedented '
        "step with joint strikes on Iraq - After accusing pro-Iranian armed "
        "groups of targeting its energy infrastructure with drones, Riyadh "
        "carried out a military operation alongside Washington against "
        'positions in Iraq."',
        "YES",
    ),
)

#: The final user turn — the row under test, in the exemplars' exact shape
#: (the lab measured with this format; the truncation limits mirror it too).
GATE_PROMPT = (
    'Question thesis: "{thesis}"\n'
    'News item: "{signal}"'
)

#: Bump WITH the prompt (same rule as the gate).
CONFIRM_PROMPT_VERSION = "naive/1"

#: The confirm prompt. Batched + ECHO-BOUND (the ``signal_salience`` /
#: ``entity_researcher`` precedent): the model must repeat each pair's ``id``
#: verbatim so a verdict binds to the pair it was asked about. There is
#: DELIBERATELY no positional fallback in the parser — see
#: :func:`parse_confirm_batch`.
CONFIRM_PROMPT = """\
TASK — for each numbered PAIR below, judge whether the SIGNAL bears on the \
THESIS: would an analyst tracking that thesis want to see that signal as new \
evidence for or against it?

Output ONE JSON array and nothing else — one object per pair. ECHO the `id` \
VERBATIM so each verdict binds to the right pair:
[{{"id": "<id verbatim>", "bears": "yes"|"no", "reason": "<one short sentence>"}}]

Judge only what the texts say. Shared proper nouns, a shared country, or the \
same news day are NOT bearing on their own — the signal must speak to what \
the thesis actually claims. When the signal is too thin to tell, answer "no".

PAIRS:
{pairs}"""


# ---------------------------------------------------------------------------
# The candidate the matcher hands over
# ---------------------------------------------------------------------------


@dataclass
class EdgeCandidate:
    """One would-be ``bearing_edges`` row, plus whatever the pipeline stamped.

    Everything above ``gate`` is produced by the DETERMINISTIC matcher and is
    never modified here; the pipeline only ever fills the three stamp fields
    (or drops the candidate outright on a gate NO).
    """

    signal_id: Any
    signal_as_of: datetime
    signal_text: str
    question_id: Any
    question_as_of: datetime
    question_thesis: str
    weight: float
    planes: list[str] = field(default_factory=list)

    #: 'yes' | 'unavailable' | 'deferred', or None when the gate was OFF.
    #: A gate verdict of NO drops the candidate, so 'no' never lands here.
    gate: str | None = None
    #: The component that judged it — stamped onto the row for provenance.
    gate_ref: str = ""
    #: 'yes' | 'no' | 'unavailable', or None when the confirm did not run.
    confirm: str | None = None
    confirm_reason: str | None = None

    def data_payload(self) -> dict[str, Any]:
        """The ``bearing_edges.data`` jsonb for this row.

        EMPTY when the gate was off — which is the 0116 column default, so a
        gate-off run stores exactly the bytes 3.2.0 stored."""
        if self.gate is None:
            return {}
        out: dict[str, Any] = {
            "bearing_gate": self.gate,
            "bearing_gate_ref": self.gate_ref or "",
            "bearing_gate_prompt": GATE_PROMPT_VERSION,
        }
        if self.confirm is not None:
            out["bearing_confirm"] = self.confirm
            out["bearing_confirm_prompt"] = CONFIRM_PROMPT_VERSION
            if self.confirm_reason:
                out["bearing_confirm_reason"] = self.confirm_reason[:MAX_REASON_CHARS]
        return out


def bearing_counter_defaults() -> dict[str, Any]:
    """The pipeline's receipt counters at their inert values.

    Seeded into EVERY run's counters, gate on or off, so a receipt always
    carries the full set and "the gate wrote nothing" is distinguishable from
    "this build has no gate" by reading one receipt."""
    return {
        "bearing_gate_mode": DEFAULT_BEARING_GATE,
        "bearing_gate_ref": "",
        "bearing_gate_prompt": GATE_PROMPT_VERSION,
        "bearing_gate_calls": 0,
        "bearing_gate_yes": 0,
        "bearing_gated_out": 0,
        "bearing_gate_errors": 0,
        "bearing_gate_deferred": 0,
        "bearing_confirm_calls": 0,
        "bearing_confirm_yes": 0,
        "bearing_confirm_no": 0,
        "bearing_confirm_unavailable": 0,
        "bearing_confirm_deferred": 0,
    }


def gate_enabled(mode: Any) -> bool:
    """Is the pipeline on? Anything that is not exactly ``'on'`` is OFF —
    an unreadable value must never silently ENABLE a leg that drops edges."""
    return str(mode or "").strip().lower() == "on"


# ---------------------------------------------------------------------------
# Text shaping
# ---------------------------------------------------------------------------


def signal_digest(payload: Any, *, max_chars: int = MAX_SIGNAL_CHARS) -> str:
    """Title + short body out of a ``signals.payload`` jsonb.

    Signals have no ``title`` column — the human-readable text lives in the
    payload, in one of several shapes depending on the source handler. Mirrors
    ``signal_salience._signal_text`` rather than importing it: that module is
    an ANALYST (not a deterministic sub-handler) with its own taxonomy
    imports, and a one-way text helper is not worth the coupling."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    if not isinstance(payload, Mapping):
        return ""
    title = ""
    for k in ("title", "headline", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            title = v.strip()
            break
    body = ""
    for k in ("distilled_body", "summary", "body", "text", "content", "description"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            body = v.strip()
            break
    text = (title + " — " + body).strip(" —") if body else title
    return re.sub(r"\s+", " ", text)[:max_chars]


def _thesis_text(raw: Any) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip())[:MAX_THESIS_CHARS]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

#: Standalone YES / NO anywhere in the reply. Word-bounded so "NOTHING" is not
#: a NO and "YESTERDAY" is not a YES — the exact class of mis-parse that would
#: turn a chatty reply into a silent edge drop.
_VERDICT_RE = re.compile(r"\b(YES|NO)\b", re.IGNORECASE)


def parse_gate_verdict(content: Any) -> str | None:
    """``'yes'`` / ``'no'`` / ``None`` (unparseable) from one gate reply.

    FIRST standalone YES/NO wins: the naive prompt asks for exactly one word,
    and a model that answers "NO — the signal concerns ..." has answered. A
    reply carrying neither token is UNPARSEABLE and returns ``None``, which the
    caller treats as an outage (stamp-and-write), never as a refusal — an
    unreadable answer is the model failing, not the edge failing."""
    if not isinstance(content, str) or not content.strip():
        return None
    m = _VERDICT_RE.search(content)
    if m is None:
        return None
    return m.group(1).lower()


def _extract_json_array(content: str) -> list[Any]:
    """First well-formed JSON array in a possibly-fenced, possibly-prosed
    reply (the ``signal_salience._extract_json_array`` precedent)."""
    if not content:
        return []
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def parse_confirm_batch(
    content: Any, pair_ids: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """``{pair_id: (verdict, reason)}`` for the pairs the model actually bound.

    Verdict is ``'yes'`` or ``'no'``. A pair the model omitted, or bound with
    an unreadable verdict, is simply ABSENT from the result — the caller stamps
    those ``'unavailable'``.

    There is DELIBERATELY NO POSITIONAL FALLBACK. A verdict bound to the wrong
    edge is worse than no verdict at all: it would record "a 120B model says
    this signal bears on that thesis" about a pair the model never saw, and
    that stamp is exactly what the measurement loop and the consumers trust.
    The same reasoning ``signal_salience._parse_salience_batch`` documents at
    length, for the same reason."""
    known = {str(p) for p in pair_ids}
    out: dict[str, tuple[str, str]] = {}
    for item in _extract_json_array(content if isinstance(content, str) else ""):
        if not isinstance(item, Mapping):
            continue
        rid = item.get("id")
        if not isinstance(rid, str) or rid.strip() not in known:
            continue  # no id match → DROP the item, never a positional guess
        key = rid.strip()
        if key in out:
            continue  # first binding wins; a duplicate id is not new evidence
        verdict = str(item.get("bears") or "").strip().lower()
        if verdict in ("true", "y"):
            verdict = "yes"
        elif verdict in ("false", "n"):
            verdict = "no"
        if verdict not in ("yes", "no"):
            continue
        reason = item.get("reason")
        reason = reason.strip() if isinstance(reason, str) else ""
        out[key] = (verdict, reason[:MAX_REASON_CHARS])
    return out


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------

#: Process-lifetime cache of built gate handlers, keyed by component id.
#: Building one costs a registry round-trip plus two vault decrypts; the
#: matcher runs every 30 minutes forever. A FAILED build is never cached —
#: the next tick retries, so a registry that was not up yet at first fire
#: heals itself (the LazyNlpClient lesson).
_GATE_CLIENT_CACHE: dict[str, Any] = {}


def _extras(deps: Any) -> Mapping[str, Any]:
    extras = getattr(deps, "extras", None) if deps is not None else None
    return extras if isinstance(extras, Mapping) else {}


async def _resolve_gate_client(deps: Any, ref: str) -> Any:
    """The gate's LLM handler for ``ref``, or ``None`` when it cannot be built.

    Resolution order:

      1. ``deps.extras[SLM_DEPS_EXTRA_KEY]`` — an injected client (tests, and
         any future pre-resolved wiring).
      2. The process cache.
      3. A fresh build through the PRODUCTION path: the registry stack
         component row → :class:`LLMProviderConfig` → the subprovider handler
         → ``on_configure`` resolving ``api_user``/``api_pass`` out of the
         :class:`CredentialVault`. Identical to what
         ``scripts/reverify_composition_heads.py`` and the runtime's own
         verify-judge wiring do; the endpoint and the basic-auth pair are NEVER
         hardcoded here, they come from the component the operator registered.

    Returns ``None`` (never raises) on any failure — the caller's contract is
    stamp-and-write, so a build failure must degrade the RUN, not kill it."""
    injected = _extras(deps).get(SLM_DEPS_EXTRA_KEY)
    if injected is not None:
        return injected
    cached = _GATE_CLIENT_CACHE.get(ref)
    if cached is not None:
        return cached
    try:
        from ...config import PostgresConfig
        from ...postgres import PostgresStore
        from ...registry.credentials import CredentialVault
        from ....runtime.analyst_deps_builder import (
            build_llm_handler_from_stack_component,
        )
        from ....runtime.registry_client import RegistryHTTPClient

        store = PostgresStore(PostgresConfig.from_env())
        # The store must be CONNECTED before the vault can resolve — an
        # unconnected store raises on first use and every candidate degrades
        # to 'unavailable' (bit the first armed run, 2026-07-30 18:37Z).
        await store.connect()
        try:
            vault = CredentialVault(store)
            handler = await build_llm_handler_from_stack_component(
                ref,
                registry_client=RegistryHTTPClient(),
                secrets_resolve=vault.resolve,
            )
        finally:
            await store.close()
    except Exception as exc:  # noqa: BLE001 — degrade the RUN, never kill it
        logger.warning(
            "claim_watch.bearing_gate.client_build_failed ref=%s err=%s — every "
            "candidate this run stamps bearing_gate='unavailable' and is "
            "WRITTEN; the matcher is never silenced by an 8B outage. Check the "
            "stack component's endpoint + its api_user/api_pass vault entries",
            ref,
            exc,
        )
        return None
    _GATE_CLIENT_CACHE[ref] = handler
    logger.info("claim_watch.bearing_gate.client_built ref=%s", ref)
    return handler


# ---------------------------------------------------------------------------
# The legs
# ---------------------------------------------------------------------------


async def _gate_one(client: Any, cand: EdgeCandidate) -> str | None:
    """One gate verdict, or ``None`` on any failure (transport, timeout,
    unparseable reply) — the three collapse deliberately: from the edge's point
    of view they are one condition, "the gate could not answer"."""
    prompt = GATE_PROMPT.format(
        thesis=_thesis_text(cand.question_thesis),
        signal=cand.signal_text or "(no text)",
    )
    messages = [{"role": "system", "content": GATE_SYSTEM_PROMPT}]
    for ex_user, ex_answer in GATE_FEWSHOT_TURNS:
        messages.append({"role": "user", "content": ex_user})
        messages.append({"role": "assistant", "content": ex_answer})
    messages.append({"role": "user", "content": prompt})
    try:
        response = await asyncio.wait_for(
            client.chat_complete(
                messages,
                max_tokens=GATE_MAX_TOKENS,
                temperature=0.0,
            ),
            timeout=GATE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — one edge degrades, the run does not
        logger.warning(
            "claim_watch.bearing_gate.call_failed signal=%s question=%s err=%s",
            cand.signal_id,
            cand.question_id,
            exc,
        )
        return None
    return parse_gate_verdict(getattr(response, "content", "") or "")


async def _run_gate(
    candidates: list[EdgeCandidate],
    *,
    deps: Any,
    gate_ref: str,
    gate_cap: int,
    counters: dict[str, Any],
) -> list[EdgeCandidate]:
    """W-B1. Returns the SURVIVING candidates (gate-NO dropped), stamped."""
    client = await _resolve_gate_client(deps, gate_ref)
    kept: list[EdgeCandidate] = []
    calls = 0
    for cand in candidates:
        cand.gate_ref = gate_ref
        if client is None:
            # The gate could not be built at all — every candidate is
            # 'unavailable' AND WRITTEN. Counted once per candidate so the
            # receipt shows the true blast radius of the outage.
            cand.gate = "unavailable"
            counters["bearing_gate_errors"] += 1
            kept.append(cand)
            continue
        if calls >= gate_cap:
            cand.gate = "deferred"
            counters["bearing_gate_deferred"] += 1
            kept.append(cand)
            continue
        calls += 1
        verdict = await _gate_one(client, cand)
        if verdict == "yes":
            cand.gate = "yes"
            counters["bearing_gate_yes"] += 1
            kept.append(cand)
        elif verdict == "no":
            # THE POINT OF THE LEG: no row is written for this pair.
            counters["bearing_gated_out"] += 1
        else:
            cand.gate = "unavailable"
            counters["bearing_gate_errors"] += 1
            kept.append(cand)
    counters["bearing_gate_calls"] = calls
    return kept


def _confirm_prompt(batch: list[tuple[str, EdgeCandidate]]) -> str:
    lines: list[str] = []
    for i, (pid, cand) in enumerate(batch, 1):
        lines.append(
            f"{i}. id={pid}\n"
            f"   THESIS: {_thesis_text(cand.question_thesis) or '(no thesis)'}\n"
            f"   SIGNAL: {cand.signal_text or '(no text)'}"
        )
    return CONFIRM_PROMPT.format(pairs="\n".join(lines))


async def _run_confirm(
    candidates: list[EdgeCandidate],
    *,
    deps: Any,
    confirm_cap: int,
    counters: dict[str, Any],
) -> None:
    """W-B2. Stamps gate-YES candidates IN PLACE. Never drops one."""
    targets = [c for c in candidates if c.gate == "yes"]
    if not targets:
        return
    client = _extras(deps).get(CONFIRM_LLM_DEPS_EXTRA_KEY)
    if client is None:
        # Not wired (the descriptor declares no method.llm.primary, or the
        # builder refused an Anthropic component). The leg did not RUN, which
        # is not the same as it having failed — stamping 'unavailable' here
        # would fabricate an outage. Left unstamped, and silent by design:
        # this is the shipped default, not a fault.
        return
    if confirm_cap <= 0:
        return

    budgeted = targets[:confirm_cap]
    counters["bearing_confirm_deferred"] += len(targets) - len(budgeted)

    calls = 0
    for start in range(0, len(budgeted), CONFIRM_BATCH_SIZE):
        chunk = budgeted[start : start + CONFIRM_BATCH_SIZE]
        batch = [(f"e{start + i}", c) for i, c in enumerate(chunk)]
        calls += 1
        try:
            response = await asyncio.wait_for(
                client.chat_complete(
                    [{"role": "user", "content": _confirm_prompt(batch)}],
                    max_tokens=CONFIRM_MAX_TOKENS,
                    temperature=0.0,
                ),
                timeout=CONFIRM_TIMEOUT_SECONDS,
            )
            verdicts = parse_confirm_batch(
                getattr(response, "content", "") or "", [pid for pid, _ in batch]
            )
        except Exception as exc:  # noqa: BLE001 — degrade the BATCH, not the run
            logger.warning(
                "claim_watch.bearing_confirm.batch_failed pairs=%d err=%s — "
                "these edges keep their gate stamp and are marked "
                "bearing_confirm='unavailable'; the edges are already written",
                len(batch),
                exc,
            )
            verdicts = {}
        for pid, cand in batch:
            got = verdicts.get(pid)
            if got is None:
                cand.confirm = "unavailable"
                counters["bearing_confirm_unavailable"] += 1
                continue
            cand.confirm, cand.confirm_reason = got
            counters[f"bearing_confirm_{cand.confirm}"] += 1
    counters["bearing_confirm_calls"] = calls


async def run_bearing_pipeline(
    candidates: list[EdgeCandidate],
    *,
    deps: Any,
    mode: Any,
    gate_ref: str,
    gate_cap: int,
    confirm_cap: int,
    counters: dict[str, Any],
) -> list[EdgeCandidate]:
    """Both legs over one run's would-be edges. Returns what should be WRITTEN.

    OFF (the shipped default) returns the input list UNTOUCHED and makes no
    call, resolves no component and constructs no client — so a gate-off run is
    the 3.2.0 run, byte for byte, plus the receipt's inert counters."""
    counters.update(bearing_counter_defaults())
    if not gate_enabled(mode):
        return candidates
    counters["bearing_gate_mode"] = "on"
    counters["bearing_gate_ref"] = gate_ref
    if not candidates:
        return candidates

    kept = await _run_gate(
        candidates,
        deps=deps,
        gate_ref=gate_ref,
        gate_cap=max(0, int(gate_cap)),
        counters=counters,
    )
    await _run_confirm(
        kept, deps=deps, confirm_cap=max(0, int(confirm_cap)), counters=counters
    )
    logger.info(
        "claim_watch.bearing_gate.done candidates=%d kept=%d yes=%d gated_out=%d "
        "errors=%d deferred=%d confirm(yes=%d no=%d unavailable=%d deferred=%d) "
        "ref=%s prompt=%s",
        len(candidates),
        len(kept),
        counters["bearing_gate_yes"],
        counters["bearing_gated_out"],
        counters["bearing_gate_errors"],
        counters["bearing_gate_deferred"],
        counters["bearing_confirm_yes"],
        counters["bearing_confirm_no"],
        counters["bearing_confirm_unavailable"],
        counters["bearing_confirm_deferred"],
        gate_ref,
        GATE_PROMPT_VERSION,
    )
    return kept
