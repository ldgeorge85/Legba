"""
Prediction / Hypothesis Tracking Tools

Lets the agent create predictions during analysis or introspection cycles.
Predictions are tracked and later confirmed or refuted as new evidence arrives.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...tools.registry import ToolRegistry
    from ....shared.schemas.cycle import CycleState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

PREDICTION_CREATE_DEF = ToolDefinition(
    name="prediction_create",
    description="Create a prediction (falsifiable hypothesis) for future verification. "
                "Use this when analysis reveals a pattern that may develop into a "
                "significant event. Later cycles will evaluate predictions against "
                "incoming evidence.",
    parameters=[
        ToolParameter(name="hypothesis", type="string",
                      description="A specific, falsifiable hypothesis "
                                  "(e.g. 'Iran will conduct a nuclear test within 6 months')"),
        ToolParameter(name="category", type="string",
                      description="Category: conflict, political, economic, technology, "
                                  "health, environment, social, disaster, other",
                      required=False),
        ToolParameter(name="region", type="string",
                      description="Primary region or country this prediction concerns",
                      required=False),
        ToolParameter(name="confidence", type="number",
                      description="Your confidence level 0.0-1.0 (default: 0.5)",
                      required=False),
    ],
)

PREDICTION_UPDATE_DEF = ToolDefinition(
    name="prediction_update",
    description="Update a prediction with new evidence or change its status. "
                "Use this to add supporting/contradicting evidence, adjust confidence, "
                "or resolve a prediction as confirmed/refuted/expired.",
    parameters=[
        ToolParameter(name="prediction_id", type="string",
                      description="UUID of the prediction to update"),
        ToolParameter(name="status", type="string",
                      description="New status: open, confirmed, refuted, expired",
                      required=False),
        ToolParameter(name="evidence_for", type="string",
                      description="Evidence supporting the hypothesis",
                      required=False),
        ToolParameter(name="evidence_against", type="string",
                      description="Evidence contradicting the hypothesis",
                      required=False),
        ToolParameter(name="confidence", type="number",
                      description="Updated confidence level 0.0-1.0",
                      required=False),
        ToolParameter(name="resolution_note", type="string",
                      description="Explanation of why the prediction was confirmed/refuted",
                      required=False),
    ],
)

PREDICTION_LIST_DEF = ToolDefinition(
    name="prediction_list",
    description="List predictions/hypotheses, optionally filtered by status. "
                "Use this to review open predictions and check them against new evidence.",
    parameters=[
        ToolParameter(name="status", type="string",
                      description="Filter by status: open, confirmed, refuted, expired (default: all)",
                      required=False),
        ToolParameter(name="limit", type="number",
                      description="Max predictions to return (default: 50)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_VALID_STATUSES = {"open", "confirmed", "refuted", "expired"}


def register(registry: ToolRegistry, *, structured: StructuredStore, state: CycleState) -> None:
    """Register prediction tracking tools with the given registry."""

    def _check_available() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        return None

    async def prediction_create_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        hypothesis = args.get("hypothesis", "").strip()
        if not hypothesis:
            return "Error: hypothesis is required"

        category = args.get("category", "")
        region = args.get("region", "")
        confidence = float(args.get("confidence", 0.5))

        # Clamp confidence to valid range
        confidence = max(0.0, min(1.0, confidence))

        # Get source_cycle from agent state
        source_cycle = state.cycle_number if state else 0

        pid = await structured.create_prediction(
            hypothesis=hypothesis,
            source_cycle=source_cycle,
            category=category,
            region=region,
            confidence=confidence,
            source_type="agent",
        )

        if not pid:
            return "Error: Failed to create prediction"

        return json.dumps({
            "status": "created",
            "prediction_id": pid,
            "hypothesis": hypothesis,
            "confidence": confidence,
        }, indent=2)

    async def prediction_update_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        prediction_id = args.get("prediction_id", "").strip()
        if not prediction_id:
            return "Error: prediction_id is required"

        status = args.get("status")
        if status and status not in _VALID_STATUSES:
            return f"Error: Invalid status '{status}'. Use: {', '.join(sorted(_VALID_STATUSES))}"

        ok = await structured.update_prediction(
            prediction_id=prediction_id,
            status=status,
            evidence_for=args.get("evidence_for"),
            evidence_against=args.get("evidence_against"),
            confidence=float(args["confidence"]) if args.get("confidence") is not None else None,
            resolution_note=args.get("resolution_note"),
        )

        if not ok:
            return f"Error: Prediction {prediction_id} not found or update failed"

        return json.dumps({
            "status": "updated",
            "prediction_id": prediction_id,
        }, indent=2)

    async def prediction_list_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        status = args.get("status")
        if status and status not in _VALID_STATUSES:
            return f"Error: Invalid status '{status}'. Use: {', '.join(sorted(_VALID_STATUSES))}"

        limit = int(args.get("limit", 50))
        items = await structured.list_predictions(status=status, limit=limit)

        if not items:
            return "No predictions found"

        return json.dumps({"count": len(items), "predictions": items}, indent=2, default=str)

    registry.register(PREDICTION_CREATE_DEF, prediction_create_handler)
    registry.register(PREDICTION_UPDATE_DEF, prediction_update_handler)
    registry.register(PREDICTION_LIST_DEF, prediction_list_handler)
