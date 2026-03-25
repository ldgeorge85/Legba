"""Prompt-level LLM routing.

Routes individual prompts to different LLM providers based on:
1. Static overrides (config-driven, per prompt name)
2. Escalation flags (agent-requested or deterministic)
3. Default provider

This sits between the prompt assembler and the LLM client.
"""

import logging
from ...shared.config import AgentConfig

logger = logging.getLogger("legba.agent.llm.router")


class PromptRouter:
    def __init__(self, default_provider, escalation_provider=None, config=None):
        self.default_provider = default_provider
        self.escalation_provider = escalation_provider
        self._static_overrides: dict[str, str] = {}  # prompt_name -> provider
        self._escalated = False  # intra-cycle escalation flag
        self._config = config
        self._load_static_overrides()

    def _load_static_overrides(self):
        """Load static per-prompt provider overrides from config."""
        # Format: "journal_synthesis:anthropic,analysis_report:anthropic"
        if self._config and hasattr(self._config, 'llm_route_overrides'):
            raw = self._config.llm_route_overrides
            if not raw:
                return
            for entry in raw.split(','):
                entry = entry.strip()
                if ':' in entry:
                    prompt_name, provider_name = entry.rsplit(':', 1)
                    self._static_overrides[prompt_name.strip()] = provider_name.strip()

    def route(self, prompt_name: str) -> object:
        """Select provider for a given prompt.

        Priority: static override > escalation flag > default
        """
        # 1. Static override
        if prompt_name in self._static_overrides:
            if self.escalation_provider:
                logger.info("Routing %s to escalation provider (static override)", prompt_name)
                return self.escalation_provider
            else:
                logger.warning(
                    "Static override for %s but no escalation provider configured",
                    prompt_name,
                )

        # 2. Escalation flag (intra-cycle)
        if self._escalated and self.escalation_provider:
            logger.info("Routing %s to escalation provider (escalated)", prompt_name)
            return self.escalation_provider

        # 3. Default
        return self.default_provider

    def escalate(self):
        """Mark remaining prompts in this cycle for escalation."""
        self._escalated = True
        logger.info("Cycle escalated -- remaining prompts route to escalation provider")

    def reset(self):
        """Reset escalation state for new cycle."""
        self._escalated = False

    # ------------------------------------------------------------------
    # Deterministic complexity scoring
    # ------------------------------------------------------------------

    def should_escalate(self, orient_context: dict) -> bool:
        """Deterministic complexity check after ORIENT.

        Returns True if the current cycle warrants escalation to a stronger model.
        """
        score = 0.0

        # Contradiction count
        contradictions = orient_context.get('contradiction_count', 0)
        if contradictions > 3:
            score += 0.3
        elif contradictions > 0:
            score += 0.1

        # Active hypothesis count needing evaluation
        hypotheses = orient_context.get('active_hypothesis_count', 0)
        if hypotheses > 5:
            score += 0.2
        elif hypotheses > 2:
            score += 0.1

        # Priority stack top situation severity
        top_severity = orient_context.get('top_situation_severity', 'medium')
        if top_severity == 'critical':
            score += 0.3
        elif top_severity == 'high':
            score += 0.15

        # Operator priority goals active
        if orient_context.get('has_operator_priority_goals', False):
            score += 0.2

        threshold = float(getattr(self._config, 'escalation_threshold', '0.6'))

        logger.info(
            "Escalation score: %.2f (threshold %.2f) — "
            "contradictions=%d hypotheses=%d severity=%s operator_goals=%s",
            score, threshold, contradictions, hypotheses,
            top_severity, orient_context.get('has_operator_priority_goals', False),
        )

        return score >= threshold
