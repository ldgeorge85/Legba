# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every optimizer's parent prompt module must actually import (K-5 prereq).

K5_BLAST_RADIUS §3.2, verified from `analyst_traces`:

    descriptor eval.optimizer.parent_prompt_module_path
        legba.prompts.inline_target.v1          (real)
    value actually used in the workflow input
        legba.prompts.leadership_transition.v1  (does not exist)

Two defects stacked. The descriptor's declared value never reached the run
``options`` — nothing plumbed ``eval.optimizer`` there, ``_merge_descriptor_
options`` only reads ``method.options`` — so the kind's convention default
always won. And the convention was ``legba.prompts.{analyzed_analyst_id}.v1``
while prompt packages are named by KIND (``legba/prompts/inline_target/v1.py``),
so for all 13 ``inline_target`` units it named a module that has never existed.
Masked in production only by ``parent_system_prompt_source: descriptor``, which
sends GEPA down the descriptor fork; remove that option or let the descriptor
text come back empty and GEPA optimizes a ``<<missing prompt module>>`` marker
whose promoted candidate becomes a live analyst's system prompt.

The pin below is tree-wide and runs the REAL resolution: the actor's own
``_inject_optimizer_prompt_options`` builds the options, the kind's own
``resolve_parent_prompt_module_path`` resolves them, and the result is
IMPORTED. This is a K-5 prerequisite — those 17 ``legba.prompts.*`` strings are
the whole load-bearing surface of the moves wave.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.analysts.optimizer import (
    convention_parent_prompt_module_path,
    resolve_parent_prompt_module_path,
    run_method,
)
from legba.data.schemas.analyst import AnalystDescriptor
from legba.runtime.dapr_actors import _inject_optimizer_prompt_options


DESCRIPTOR_DIR = Path(__file__).resolve().parents[2] / "descriptors"


def _analyst_bodies() -> dict[str, dict[str, Any]]:
    """Every analyst descriptor in the tree, keyed by identity.id."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(DESCRIPTOR_DIR.glob("analyst_*.yaml")):
        body = yaml.safe_load(path.read_text())
        if not isinstance(body, dict):
            continue
        ident = body.get("identity") or {}
        if ident.get("id"):
            out[str(ident["id"])] = body
    return out


class _DescriptorTreeConn:
    """Stands in for ``analyst_descriptors`` — same query, tree as the source.

    ``_analyst_kind_for_id`` reads ``SELECT kind ... WHERE descriptor_id = $1
    AND is_head = TRUE``; the registry rows are registered FROM these files, so
    resolving against the tree keeps the test hermetic without inventing a
    different answer than production would give.
    """

    def __init__(self, bodies: dict[str, dict[str, Any]]) -> None:
        self._bodies = bodies

    async def fetchrow(self, _query: str, *params: Any):
        body = self._bodies.get(str(params[0]))
        if body is None:
            return None
        return {"kind": (body.get("identity") or {}).get("kind")}


def _optimizer_descriptors() -> list[tuple[str, AnalystDescriptor]]:
    """Every OPTIMIZER-kind descriptor in the tree, parsed by the real schema."""
    out: list[tuple[str, AnalystDescriptor]] = []
    for path in sorted(DESCRIPTOR_DIR.glob("analyst_*.yaml")):
        body = yaml.safe_load(path.read_text())
        if not isinstance(body, dict):
            continue
        if (body.get("identity") or {}).get("kind") != "optimizer":
            continue
        out.append((path.name, AnalystDescriptor.model_validate(body, strict=False)))
    return out


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


def test_the_tree_actually_has_optimizer_descriptors() -> None:
    """Guard the guard — an empty glob would make the pin below vacuous."""
    found = _optimizer_descriptors()
    assert found, "no optimizer-kind descriptors found; the pin would be vacuous"
    assert {d.identity.id for _n, d in found} >= {
        "country_optimizer", "unit_optimizer",
    }


@pytest.mark.asyncio
async def test_every_optimizer_resolves_a_prompt_module_that_imports() -> None:
    """For EVERY optimizer-bearing descriptor: resolve as production does,
    then import the result.

    A dead path here is not a broken test — it is GEPA about to evolve a
    placeholder into a live system prompt.
    """
    bodies = _analyst_bodies()
    conn = _DescriptorTreeConn(bodies)

    for name, descriptor in _optimizer_descriptors():
        options: dict[str, Any] = {"analyst_id": descriptor.identity.id}
        # The REAL actor-side injection — the thing that was missing.
        await _inject_optimizer_prompt_options(
            conn, options, descriptor, actor_id=f"analyst::{descriptor.identity.id}",
        )
        # The REAL kind-side resolution.
        path = resolve_parent_prompt_module_path(options)
        assert path, f"{name}: no parent prompt module resolved"
        try:
            importlib.import_module(path)
        except Exception as exc:  # pragma: no cover — the failure we're pinning
            raise AssertionError(
                f"{name}: eval.optimizer resolves parent prompt module {path!r}, "
                f"which does not import: {type(exc).__name__}: {exc}"
            ) from exc

        # The CONVENTION arm too, independent of what the descriptor declares:
        # dropping the declaration must degrade to a real module, not to the
        # placeholder the analyst-id form produced. Without this the pin would
        # pass on the broken convention purely because both live optimizers
        # happen to declare a path.
        kind = options.get("analyzed_analyst_kind")
        assert kind, f"{name}: analyzed analyst's kind did not resolve"
        fallback = convention_parent_prompt_module_path(str(kind))
        try:
            importlib.import_module(fallback)
        except Exception as exc:  # pragma: no cover — the failure we're pinning
            raise AssertionError(
                f"{name}: the convention path for analyzed kind {kind!r} is "
                f"{fallback!r}, which does not import: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


@pytest.mark.asyncio
async def test_declared_path_reaches_the_options(monkeypatch) -> None:
    """unit_optimizer's DECLARED value wins — it used to be dead config.

    Live evidence (K5 §3.2): descriptor said ``legba.prompts.inline_target.v1``
    and the workflow input carried ``legba.prompts.leadership_transition.v1``.
    """
    bodies = _analyst_bodies()
    descriptor = AnalystDescriptor.model_validate(
        bodies["unit_optimizer"], strict=False,
    )
    options: dict[str, Any] = {"analyst_id": "unit_optimizer"}

    await _inject_optimizer_prompt_options(
        _DescriptorTreeConn(bodies), options, descriptor, actor_id="a",
    )

    assert options["parent_prompt_module_path"] == "legba.prompts.inline_target.v1"
    # The analyzed unit's KIND is resolved too, so the convention has a correct
    # fallback if the declaration is ever dropped.
    assert options["analyzed_analyst_kind"] == "inline_target"
    assert resolve_parent_prompt_module_path(options) == (
        "legba.prompts.inline_target.v1"
    )


@pytest.mark.asyncio
async def test_an_explicit_per_run_value_beats_the_descriptor() -> None:
    """Precedence ladder, same as every other option: ad-hoc > descriptor."""
    bodies = _analyst_bodies()
    descriptor = AnalystDescriptor.model_validate(
        bodies["unit_optimizer"], strict=False,
    )
    options: dict[str, Any] = {
        "analyst_id": "unit_optimizer",
        "parent_prompt_module_path": "legba.prompts.predictor.v1",
    }

    await _inject_optimizer_prompt_options(
        _DescriptorTreeConn(bodies), options, descriptor, actor_id="a",
    )

    assert options["parent_prompt_module_path"] == "legba.prompts.predictor.v1"


def test_convention_is_the_kind_not_the_analyst_id() -> None:
    """The regression itself, stated as an assertion.

    ``leadership_transition`` is an ANALYST id of KIND ``inline_target``.
    """
    bodies = _analyst_bodies()
    kind = (bodies["leadership_transition"]["identity"])["kind"]
    assert kind == "inline_target"

    by_kind = convention_parent_prompt_module_path(kind)
    assert by_kind == "legba.prompts.inline_target.v1"
    importlib.import_module(by_kind)  # real

    # The old derivation, kept as an explicit tombstone: it never resolved.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("legba.prompts.leadership_transition.v1")


# ---------------------------------------------------------------------------
# Wiring guard — the actor body must actually CALL the injection
#
# Same reasoning as `test_handler_options_x1`'s guard: `AnalystActor.run`
# needs a daprd sidecar no in-process rig provides, so the tests above drive
# the injection directly. That leaves the worst failure mode uncovered — a
# correct, well-tested function production never invokes, which is precisely
# what the declared `parent_prompt_module_path` was for a year.
# ---------------------------------------------------------------------------


def _actor_run_source() -> str:
    import inspect

    from legba.runtime.dapr_actors import AnalystActor

    return inspect.getsource(AnalystActor.run)


def test_actor_run_body_injects_the_optimizer_prompt_options() -> None:
    assert "_inject_optimizer_prompt_options(" in _actor_run_source(), (
        "the actor run path must plumb eval.optimizer into options — an "
        "unwired channel is the K5 §3.2 defect restored"
    )


def test_injection_happens_before_the_handler_is_invoked() -> None:
    """Order is the contract: options must be complete before dispatch."""
    src = _actor_run_source()
    assert src.index("_inject_optimizer_prompt_options(") < src.index(
        "_invoke_run_method("
    )


def test_injection_happens_after_the_payload_options_passthrough() -> None:
    """setdefault + order = precedence: an explicit force-run parameter must
    already be in the mapping when the descriptor's value is merged over it."""
    src = _actor_run_source()
    assert src.index('payload.get("options")') < src.index(
        "_inject_optimizer_prompt_options("
    )


@pytest.mark.asyncio
async def test_unresolvable_parent_module_is_a_loud_noop() -> None:
    """No declared path and no kind => audit row, not a guessed module.

    The old code fell back to ``legba.prompts.{analyst_id}.v1`` unconditionally,
    which is how a non-existent module reached GEPA in the first place.
    """
    rows = [
        {
            "run_id": "00000000-0000-0000-0000-00000000000%d" % i,
            "analyzed_analyst_id": "some_unit",
            "analyzed_analyst_version": "v" + "a" * 16,
            "input": "ctx",
            "gold": "out",
            "trace_status": "success",
            "output_row_refs": [],
            "critique_score": 0.5,
            "critique_scores": {},
            "critique_revision_delta": None,
            "critique_id": None,
        }
        for i in range(3)
    ]

    result = await run_method(rows, {"analyst_id": "some_optimizer"}, None)

    assert result.finding is not None
    assert "unresolved_parent_prompt_module" in result.finding.tags
    assert "candidate" not in (result.finding.data or {})
