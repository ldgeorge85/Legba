"""
Hypothesis Tools — Analysis of Competing Hypotheses (ACH)

Tools for creating and evaluating competing hypothesis pairs.
Hypotheses persist across cycles and accumulate evidence incrementally.
SYNTHESIZE creates them, SURVEY stress-tests them, ANALYSIS reviews them.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ....shared.schemas.tools import ToolDefinition, ToolParameter


# ---------------------------------------------------------------------------
# Duplicate detection helpers
# ---------------------------------------------------------------------------

_STOP = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or",
         "is", "was", "by", "from", "with", "that", "this", "are", "be", "as",
         "it", "its", "not", "but", "has", "had", "have", "been", "would",
         "will", "can", "may"}


def _thesis_words(text: str) -> set[str]:
    """Extract normalised significant words from a thesis."""
    return {w for w in text.lower().split() if w not in _STOP and len(w) > 2}


def _thesis_similarity(a: str, b: str) -> float:
    """Jaccard similarity of thesis words (fast fallback)."""
    wa, wb = _thesis_words(a), _thesis_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


async def _thesis_cosine(a: str, b: str, embed_fn) -> float:
    """Cosine similarity of thesis embeddings (semantic dedup)."""
    try:
        emb_a = await embed_fn(a)
        emb_b = await embed_fn(b)
        if not emb_a or not emb_b:
            return 0.0
        dot = sum(x * y for x, y in zip(emb_a, emb_b))
        norm_a = sum(x * x for x in emb_a) ** 0.5
        norm_b = sum(x * x for x in emb_b) ** 0.5
        return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0
    except Exception:
        return 0.0

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...tools.registry import ToolRegistry
    from ....shared.schemas.cycle import CycleState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

HYPOTHESIS_CREATE_DEF = ToolDefinition(
    name="hypothesis_create",
    description="Create a competing hypothesis pair (ACH). Provide a thesis and a "
                "counter-thesis — two competing explanations for an observed pattern. "
                "Optionally specify diagnostic evidence: specific observations that "
                "would prove one and disprove the other.",
    parameters=[
        ToolParameter(name="thesis", type="string",
                      description="Primary hypothesis (e.g. 'Iran is preparing for a naval exercise')"),
        ToolParameter(name="counter_thesis", type="string",
                      description="Competing explanation (e.g. 'Iran is conducting a bluff to mask land repositioning')"),
        ToolParameter(name="situation_id", type="string",
                      description="UUID of the situation this hypothesis belongs to",
                      required=False),
        ToolParameter(name="diagnostic_evidence", type="string",
                      description="JSON array of diagnostic evidence items. Each: "
                                  '{"description": "what to look for", "proves": "thesis|counter"}. '
                                  "These are specific observations that would prove one hypothesis "
                                  "and disprove the other.",
                      required=False),
    ],
)

HYPOTHESIS_EVALUATE_DEF = ToolDefinition(
    name="hypothesis_evaluate",
    description="Evaluate a hypothesis against new evidence. Link a signal that "
                "supports or refutes the thesis. Use this when you encounter evidence "
                "that bears on an active hypothesis during SURVEY or ANALYSIS cycles.",
    parameters=[
        ToolParameter(name="hypothesis_id", type="string",
                      description="UUID of the hypothesis to evaluate"),
        ToolParameter(name="supporting_signal", type="string",
                      description="UUID of a signal that supports the THESIS",
                      required=False),
        ToolParameter(name="refuting_signal", type="string",
                      description="UUID of a signal that supports the COUNTER-THESIS (refutes the thesis)",
                      required=False),
        ToolParameter(name="status", type="string",
                      description="Update status: active, confirmed, refuted, superseded, stale",
                      required=False),
    ],
)

HYPOTHESIS_LIST_DEF = ToolDefinition(
    name="hypothesis_list",
    description="List active hypotheses, optionally filtered by status or situation. "
                "Shows thesis vs counter-thesis, evidence balance, and diagnostic "
                "evidence status. Use this to find hypotheses to evaluate against "
                "new signals.",
    parameters=[
        ToolParameter(name="status", type="string",
                      description="Filter by status: active, confirmed, refuted, superseded, stale (default: active)",
                      required=False),
        ToolParameter(name="situation_id", type="string",
                      description="Filter by situation UUID",
                      required=False),
        ToolParameter(name="limit", type="number",
                      description="Max results (default: 20)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_VALID_STATUSES = {"active", "confirmed", "refuted", "superseded", "stale"}


def register(registry: ToolRegistry, *, structured: StructuredStore, state: CycleState, embed_fn=None) -> None:
    """Register hypothesis (ACH) tools. embed_fn: async (text) -> list[float] for semantic dedup."""

    def _check_available() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        return None

    async def hypothesis_create_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        thesis = args.get("thesis", "").strip()
        counter_thesis = args.get("counter_thesis", "").strip()
        if not thesis:
            return "Error: thesis is required"
        if not counter_thesis:
            return "Error: counter_thesis is required — every hypothesis needs a competing explanation"

        # --- Dedup: check for existing active hypotheses with similar thesis ---
        # Uses embedding cosine similarity (semantic) when available, Jaccard as fallback.
        try:
            async with structured._pool.acquire() as conn:
                active = await conn.fetch(
                    "SELECT id, thesis, counter_thesis FROM hypotheses "
                    "WHERE status = 'active' ORDER BY created_at DESC",
                )
                for row in active:
                    # Try semantic similarity first, fall back to Jaccard
                    if embed_fn:
                        sim = await _thesis_cosine(thesis, row["thesis"], embed_fn)
                        method = "cosine"
                        threshold = 0.80
                    else:
                        sim = _thesis_similarity(thesis, row["thesis"])
                        method = "jaccard"
                        threshold = 0.45
                    if sim >= threshold:
                        return json.dumps({
                            "status": "duplicate_detected",
                            "existing_hypothesis_id": str(row["id"]),
                            "existing_thesis": row["thesis"][:120],
                            "similarity": round(sim, 3),
                            "method": method,
                            "reason": (
                                f"An active hypothesis already covers this topic ({method} {sim:.2f}). "
                                "Use hypothesis_evaluate to add evidence to it, or update its status."
                            ),
                        }, indent=2)
        except Exception as e:
            logger.debug("Hypothesis dedup check failed: %s", e)

        situation_id = args.get("situation_id")
        diag_raw = args.get("diagnostic_evidence")
        diagnostic = None
        if diag_raw:
            try:
                diagnostic = json.loads(diag_raw) if isinstance(diag_raw, str) else diag_raw
            except json.JSONDecodeError:
                return "Error: diagnostic_evidence must be valid JSON array"

        cycle_number = state.cycle_number if state else 0

        hid = await structured.create_hypothesis(
            thesis=thesis,
            counter_thesis=counter_thesis,
            created_cycle=cycle_number,
            situation_id=situation_id,
            diagnostic_evidence=diagnostic,
        )

        if not hid:
            return "Error: Failed to create hypothesis"

        return json.dumps({
            "status": "created",
            "hypothesis_id": hid,
            "thesis": thesis,
            "counter_thesis": counter_thesis,
            "diagnostic_evidence_count": len(diagnostic) if diagnostic else 0,
        }, indent=2)

    async def hypothesis_evaluate_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        hypothesis_id = args.get("hypothesis_id", "").strip()
        if not hypothesis_id:
            return "Error: hypothesis_id is required"

        status = args.get("status")
        if status and status not in _VALID_STATUSES:
            return f"Error: Invalid status '{status}'. Use: {', '.join(sorted(_VALID_STATUSES))}"

        cycle_number = state.cycle_number if state else None

        ok = await structured.evaluate_hypothesis(
            hypothesis_id=hypothesis_id,
            supporting_signal=args.get("supporting_signal"),
            refuting_signal=args.get("refuting_signal"),
            status=status,
            evaluated_cycle=cycle_number,
        )

        if not ok:
            return f"Error: Hypothesis {hypothesis_id} not found or update failed"

        parts = {"status": "evaluated", "hypothesis_id": hypothesis_id}
        if args.get("supporting_signal"):
            parts["supporting_signal_linked"] = args["supporting_signal"]
        if args.get("refuting_signal"):
            parts["refuting_signal_linked"] = args["refuting_signal"]
        if status:
            parts["new_status"] = status

        return json.dumps(parts, indent=2)

    async def hypothesis_list_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        status = args.get("status", "active")
        if status and status not in _VALID_STATUSES:
            return f"Error: Invalid status '{status}'. Use: {', '.join(sorted(_VALID_STATUSES))}"

        situation_id = args.get("situation_id")
        limit = int(args.get("limit", 20))

        items = await structured.list_hypotheses(
            status=status, situation_id=situation_id, limit=limit,
        )

        if not items:
            return f"No hypotheses found (status={status})"

        # Format for readability
        formatted = []
        for h in items:
            entry = {
                "id": str(h["id"]),
                "thesis": h["thesis"],
                "counter_thesis": h["counter_thesis"],
                "evidence_balance": h["evidence_balance"],
                "supporting": h.get("support_count") or 0,
                "refuting": h.get("refute_count") or 0,
                "status": h["status"],
                "situation": h.get("situation_name") or "(unlinked)",
                "created_cycle": h["created_cycle"],
                "last_evaluated": h["last_evaluated_cycle"],
            }
            # Include diagnostic evidence if present
            diag = h.get("diagnostic_evidence")
            if diag and isinstance(diag, list):
                entry["diagnostic_evidence"] = diag
            formatted.append(entry)

        return json.dumps({"count": len(formatted), "hypotheses": formatted}, indent=2, default=str)

    registry.register(HYPOTHESIS_CREATE_DEF, hypothesis_create_handler)
    registry.register(HYPOTHESIS_EVALUATE_DEF, hypothesis_evaluate_handler)
    registry.register(HYPOTHESIS_LIST_DEF, hypothesis_list_handler)
