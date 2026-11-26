# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retired stack names that stored rows may still carry, keyed by sha256 digest.

The model-gateway stack components and the NLP gateway provider value were
renamed to role-based names (``llm.verify.gateway_judge.openai_compat``,
``nlp.gateway.bearer``, ``provider: bearer_gateway`` …). A running registry
keeps the rows it registered under the old names until they are retired, and
reads validate every row through the schema — so the old provider value must
stay VALID, and an old component id should still resolve to its family.

The retired names are deployment-specific, so this table holds only the
sha256 digest of each retired string, never the string itself. A value is
mapped by hashing it and looking the digest up; anything else passes through
unchanged. The mapping is read-side only: nothing here rewrites a stored row,
so a stored body keeps its content hash.

Transitional. Once the live registry no longer holds a row under a retired
name (the old components retired and their heads gone from every read path),
this module and its call sites can be deleted.
"""

from __future__ import annotations

import hashlib

__all__ = [
    "LEGACY_COMPONENT_ID_DIGESTS",
    "LEGACY_NLP_PROVIDER_DIGESTS",
    "canonical_component_id",
    "canonical_nlp_provider",
    "name_digest",
]

#: sha256(retired component id) -> the component id that replaced it.
LEGACY_COMPONENT_ID_DIGESTS: dict[str, str] = {
    "1eb41344d16063cecc882a19236743132c5509eb28ff50548d6f290b912d9742":
        "llm.verify.gateway_judge.openai_compat",
    "5a1bf9de8a429b8f86018029f2bce371e0c13a5274efbe53c80ebc4d8bdbcf51":
        "llm.verify.gateway_gold_drafter.openai_compat",
    "84d55505859e1d89b3bf1b77407518706d45914c83eed39e70a08ec24ee3e4c2":
        "llm.judge.gateway_rung2_think.openai_compat",
    "f3c539cc975d119223db03cc740e3341ccd57d881f929b8d41695cef7916c4c9":
        "llm.grader.gateway_third_family.openai_compat",
    "9c71b0edeab124bbbc7420b1bc590afd76a967b891a2281e4d0966e4b17e7e62":
        "llm.decide.gateway_typed_choice",
    "0adf64509ad774aba65378d80405129dec982c6bfc252d8cf16213a8c3093fb6":
        "nlp.gateway.bearer",
}

#: sha256(retired NLP provider value) -> the provider value that replaced it.
LEGACY_NLP_PROVIDER_DIGESTS: dict[str, str] = {
    "afb3b8f0514845b640996d98f0a596c6e81125ecdebe28fb0c2bdaf325ecff1e":
        "bearer_gateway",
}


def name_digest(value: str) -> str:
    """The sha256 hex digest of ``value`` (UTF-8) — the table's key form."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_component_id(component_id: str) -> str:
    """The current id for a retired component id; any other id unchanged."""
    return LEGACY_COMPONENT_ID_DIGESTS.get(name_digest(component_id), component_id)


def canonical_nlp_provider(provider: str) -> str:
    """The current NLP provider value for a retired one; any other unchanged."""
    return LEGACY_NLP_PROVIDER_DIGESTS.get(name_digest(provider), provider)
