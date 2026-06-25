# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G3 — derived signals inherit the PARENT signal's modality.

``build_derived_signal`` previously hardcoded ``modality='text'`` on every
derived row, so a modality-pinned subscription (e.g. one scoped to
``modality='audio'``) never saw the derived output of an audio job — the
derived row fell out of the modality-pinned slice exactly the way geo/tags
once did (the A-2 close already inherits those). These are pure-function unit
tests (no DB / NATS) of the modality-inheritance fix.
"""

from __future__ import annotations

from uuid import uuid4

from legba.data.jobs.media import (
    MediaExtractionResult,
    ProcessMediaInput,
    build_derived_signal,
)


def _input(parent_id, *, modality: str = "audio") -> ProcessMediaInput:
    return ProcessMediaInput(
        media_ref="https://cdn.example/clip.mp3",
        extraction="transcribe",
        derived_from=parent_id,
        modality=modality,          # the JOB's media modality (audio/video/...)
        language_hint="pt",
    )


def _result(text: str = "hosted transcript") -> MediaExtractionResult:
    return MediaExtractionResult(
        extraction="transcribe", text=text, model="hosted-test", source="hosted",
    )


def _parent_row(**over):
    row = {
        "source_id": "source.g3",
        "source_version": "v1",
        "owner_tenant": "default",
        "modality": "audio",
        "geo": ["BR"],
        "tags": ["g20"],
        "entity_classes": ["org"],
        "language": "pt",
    }
    row.update(over)
    return row


def test_derived_signal_inherits_parent_audio_modality() -> None:
    parent_id = uuid4()
    derived = build_derived_signal(
        job_id=uuid4(),
        parent_row=_parent_row(modality="audio"),
        inp=_input(parent_id, modality="audio"),
        result=_result(),
    )
    # The G3 fix: the derived row carries the parent's modality, NOT 'text'.
    assert derived.modality == "audio"


def test_derived_signal_inherits_parent_video_modality() -> None:
    parent_id = uuid4()
    derived = build_derived_signal(
        job_id=uuid4(),
        parent_row=_parent_row(modality="video"),
        inp=_input(parent_id, modality="video"),
        result=_result(),
    )
    assert derived.modality == "video"


def test_derived_signal_inherits_parent_image_modality() -> None:
    parent_id = uuid4()
    derived = build_derived_signal(
        job_id=uuid4(),
        parent_row=_parent_row(modality="image"),
        inp=_input(parent_id, modality="image"),
        result=MediaExtractionResult(
            extraction="caption", text="a caption", model="m", source="hosted",
        ),
    )
    assert derived.modality == "image"


def test_derived_signal_falls_back_to_text_when_parent_modality_absent() -> None:
    """A parent row missing the modality column → fall back to 'text'
    (never crash, never emit an empty/invalid modality)."""
    parent_id = uuid4()
    row = _parent_row()
    row.pop("modality")
    derived = build_derived_signal(
        job_id=uuid4(),
        parent_row=row,
        inp=_input(parent_id),
        result=_result(),
    )
    assert derived.modality == "text"


def test_derived_signal_falls_back_to_text_when_parent_modality_empty() -> None:
    parent_id = uuid4()
    derived = build_derived_signal(
        job_id=uuid4(),
        parent_row=_parent_row(modality=""),
        inp=_input(parent_id),
        result=_result(),
    )
    assert derived.modality == "text"


def test_modality_inheritance_does_not_regress_geo_tag_inheritance() -> None:
    """Guard: the modality change must not disturb the A-2 geo/tag/entity
    inheritance that already worked."""
    parent_id = uuid4()
    derived = build_derived_signal(
        job_id=uuid4(),
        parent_row=_parent_row(modality="audio"),
        inp=_input(parent_id, modality="audio"),
        result=_result(),
    )
    assert derived.modality == "audio"
    assert derived.geo == ["BR"]
    assert derived.tags == ["g20"]
    assert derived.entity_classes == ["org"]
    assert derived.language == "pt"
    # The extracted text + lineage still land correctly.
    assert derived.payload["text"] == "hosted transcript"
    assert derived.derived_from == [parent_id]
    assert derived.mime_type == "text/plain"
