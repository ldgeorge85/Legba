# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Front-door pieces for finishing a cut consult run.

Two moves the panel needs and the API did not have:

* **STOP** — end a run that is still drilling. Before this there was no way to
  stop one: the panel's "Dismiss" only detached the browser, and the run kept
  running and kept billing.
* **SYNTHESIZE FROM EVIDENCE** — take a turn whose synthesis was cut and ask a
  model to write the answer over the evidence that run already gathered, with
  no new drilling. Run c8a0105c gathered 283 citable refs across 50 tool calls
  and then delivered 487 characters of apology; re-asking the question would
  have paid for all fifty calls a second time.

This module holds the SHAPES and the DECISIONS. The routes themselves live in
``consult_api.build_consult_router``, because the recovery has to reach the
same ``ConsultRunManager`` instance the POST path created, and that manager is
a closure local. Keeping the logic here keeps that file under the size gate and
keeps this file testable without an app.

The actor surface is unchanged. A recovery is an ordinary
``AnalystActor/run`` invoke whose ``inputs[0]`` carries ``synthesize_from``;
the analyst kind recognises it and skips its loop. Nothing in the actor plane
learned a new method for this.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: Fidelity labels, mirrored from ``analysts.consult_resynthesis`` so the front
#: door can describe a response it is only passing through.
FIDELITY_EXACT = "exact"
FIDELITY_REBUILT = "rebuilt"
FIDELITY_UNAVAILABLE = "unavailable"


class SynthesizeRequest(BaseModel):
    """Body for ``POST /consult/runs/{request_id}/synthesize``.

    ``model`` defaults to ``opus`` rather than to the plane the original run
    used. That is deliberate and it is the cheap-by-default rule: the run being
    recovered was, by construction, an expensive one that did not finish, and
    the recovery is a SINGLE call over evidence that already exists. There is
    no reason for it to inherit the route that made the first attempt costly.
    An operator who wants the original plane can still name it.
    """

    model: Literal["opus", "fable", "core"] | None = Field(default="opus")
    #: Resolve by turn id instead of run id. Required for any turn written
    #: before migration 0195, which is every turn that existed when this was
    #: built — including the one that motivated it.
    turn_id: str | None = Field(default=None)


class SynthesizeResponse(BaseModel):
    """A recovered answer, plus how faithfully its prompt was reconstructed.

    ``replay_fidelity`` and ``replay_note`` are not decoration. A rebuilt
    replay re-executed the original tool calls against a corpus that has moved
    on, and it could not recover the assistant's own prose between rounds. An
    operator reading the answer has to be able to see that, so both fields ride
    on the response and the panel is required to show the note whenever the
    fidelity is not ``exact``.
    """

    answer: str
    finding_id: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    cited_refs: list[dict[str, Any]] = Field(default_factory=list)
    uncertainty: float | None = None
    unanswered_aspects: list[str] = Field(default_factory=list)
    session_id: str | None = None
    model: str | None = None
    request_id: str | None = None
    turn_id: str | None = None
    parent_turn_id: str | None = None
    replay_fidelity: str = FIDELITY_UNAVAILABLE
    replay_note: str = ""
    synthesis_status: str = "complete"
    resynthesizable: bool = False
    usage: dict[str, Any] = Field(default_factory=dict)


def build_recovery_input(
    evidence: Mapping[str, Any],
    *,
    llm_component_override: str | None,
    request_id: str,
) -> dict[str, Any]:
    """The actor ``inputs[0]`` for a recovery run.

    Carries the persisted evidence under ``synthesize_from`` and NOTHING that
    would let the analyst drill: no ``max_tool_rounds``, no prior ``messages``,
    no pinned context. The kind's recovery path does not read them, and leaving
    them out means a bug there cannot turn a recovery into a second expensive
    run.
    """
    first_input: dict[str, Any] = {
        # ``question`` is still required by the kind's input contract and is
        # what the rebuilt prompt is anchored on.
        "question": evidence.get("question") or "",
        "request_id": request_id,
        "mode": "chat",
        "synthesize_from": {
            "question": evidence.get("question") or "",
            "steps": evidence.get("steps") or [],
            "tool_calls": evidence.get("tool_calls") or [],
            "cited_refs": _ref_ids(evidence.get("cited_refs") or []),
            "replay_transcript": evidence.get("replay_transcript"),
        },
    }
    if llm_component_override is not None:
        first_input["llm_component_override"] = llm_component_override
    return first_input


def _ref_ids(refs: Any) -> list[str]:
    """Flatten persisted cited_refs to bare id strings.

    They are stored as the projected ``{id, kind, ...}`` objects the panel
    renders, but the analyst's payload builder wants ids. Accepts either shape
    so a turn written by any version replays.
    """
    out: list[str] = []
    if not isinstance(refs, list):
        return out
    for ref in refs:
        if isinstance(ref, Mapping):
            value = ref.get("id") or ref.get("ref") or ref.get("uuid")
            if value:
                out.append(str(value))
        elif ref:
            out.append(str(ref))
    return out


def recovery_precheck(evidence: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Whether this turn can be recovered at all, and why not when it cannot.

    Checked BEFORE the actor invoke so an un-recoverable turn costs nothing and
    gets a clear message, instead of a round-trip that ends in a model being
    asked to answer from an empty prompt.
    """
    if evidence is None:
        return False, "no persisted assistant turn was found for that run"
    if evidence.get("replay_transcript"):
        return True, ""
    steps = evidence.get("steps") or []
    has_calls = any(
        isinstance(s, Mapping) and s.get("kind") == "tool_call" for s in steps
    )
    if has_calls:
        return True, ""
    return False, (
        "that turn carries neither a recorded synthesis prompt nor any "
        "re-executable tool calls, so there is no evidence to synthesise over"
    )


def project_recovery(
    projected: Any,
    *,
    evidence: Mapping[str, Any],
    request_id: str,
    chosen_model: str,
    turn_id: str | None,
) -> SynthesizeResponse:
    """Fold the front door's projected ``ConsultResponse`` into the recovery shape.

    ``projected`` is the shared projection every consult transport already
    produces, so the recovery answer is assembled by the same code as a normal
    one — the recovery adds identity (which turn this replaces) and honesty
    (how faithful the prompt was), and invents no other differences.

    The fidelity is defaulted PESSIMISTICALLY: a response that somehow arrives
    without it is reported as ``unavailable`` rather than as a clean replay.
    Silence about fidelity must never read as a guarantee of it.
    """
    get = (
        projected.get
        if isinstance(projected, Mapping)
        else lambda key, default=None: getattr(projected, key, default)
    )
    fidelity = str(get("replay_fidelity") or FIDELITY_UNAVAILABLE)
    note = str(get("replay_note") or "")
    if fidelity != FIDELITY_EXACT and not note:
        note = "the replayed prompt was reconstructed; fidelity is not exact"

    def _dump(items: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in items or []:
            if isinstance(item, Mapping):
                out.append(dict(item))
            elif hasattr(item, "model_dump"):
                out.append(item.model_dump())
        return out

    return SynthesizeResponse(
        answer=get("answer") or "",
        finding_id=get("finding_id"),
        derived_from=[str(d) for d in (get("derived_from") or [])],
        tool_calls=_dump(get("tool_calls")),
        cited_refs=_dump(get("cited_refs")),
        uncertainty=get("uncertainty"),
        unanswered_aspects=[str(u) for u in (get("unanswered_aspects") or [])],
        session_id=evidence.get("session_id"),
        model=chosen_model,
        request_id=request_id,
        turn_id=turn_id,
        parent_turn_id=evidence.get("turn_id"),
        replay_fidelity=fidelity,
        replay_note=note,
        synthesis_status=str(get("synthesis_status") or "complete"),
        resynthesizable=bool(get("resynthesizable")),
        usage=dict(get("usage") or {}),
    )


__all__ = [
    "FIDELITY_EXACT",
    "FIDELITY_REBUILT",
    "FIDELITY_UNAVAILABLE",
    "SynthesizeRequest",
    "SynthesizeResponse",
    "build_recovery_input",
    "project_recovery",
    "recovery_precheck",
]
