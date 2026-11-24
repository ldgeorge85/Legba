# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round-run storage as a PROTOCOL, with an in-memory and a file-backed
implementation (plan P4 §3.2-3.4; lanes G-2/G-3).

Where round runs live in production waits on the data-architecture decision
(design §4.5), so nothing here is a table or a migration. What a store must
do is fixed now:

* **Seal on write.** A step receipt is written with its seal — the sha256 of
  its canonical output (row type, payload, prose) — in one write, and is
  insert-only: writing the same key again with different content raises
  :class:`SealConflict`. The file store re-hashes every receipt it loads, so
  an edited file is refused, not believed.
* **Phases** are upserted while open and frozen once closed.
* **Resume**: a store opened on an existing folder holds every receipt and
  phase record written before the interruption.

Which sealed rows a seat may READ is decided by one function over this
protocol (:func:`legba.data.rounds.visibility.inputs_for`), so the harness
and a later database host apply the same rule.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .spec import canonical_json


class SealConflict(RuntimeError):
    """A sealed receipt or closed phase was about to change, or a stored
    receipt no longer matches its seal."""


def seal_of(row_type: str | None, payload: Any, prose: str | None) -> str:
    return hashlib.sha256(canonical_json(
        {"row_type": row_type, "payload": payload, "prose": prose}).encode("utf-8")).hexdigest()


@dataclass
class Receipt:
    """One seat attempt (design §3.2 "step receipt"). ``status``:

    * ``sealed`` — output parsed, sealed; ``released`` says whether readers
      may have it (gates passed, on time);
    * ``failed`` — the attempt failed (``failure`` says why; raw text kept);
    * ``absent`` — the seat is recorded absent (no output; ``failure`` why).
    """

    key: str
    run_id: str
    seq: int
    seat: str
    path: str
    phase_path: tuple[str, ...]
    scope: dict[str, str]
    attempt: int
    status: str
    released: bool = False
    row_type: str | None = None
    payload: Any = None
    prose: str | None = None
    raw: str | None = None
    failure: str | None = None
    sha256: str | None = None
    sealed_at: str | None = None
    handed: list[dict[str, Any]] = field(default_factory=list)
    card: str | None = None
    card_sha256: str | None = None
    plane: str | None = None
    family: str | None = None
    thinking: str | None = None
    tools: list[str] = field(default_factory=list)
    tools_bound: bool = False
    model_call: bool = False
    gates: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    charged_s: float = 0.0
    offset_s: float = 0.0
    spec: dict[str, Any] = field(default_factory=dict)
    candidate: str | None = None
    #: A model seat's call record (``SeatReply.meta``): per wire call the
    #: plane, the model named and served, thinking and any fallback, tokens
    #: incl. reasoning, duration, gate wait, tool calls. Not sealed content.
    call: dict[str, Any] | None = None
    #: A ``repair: reasons`` retry: which attempt it repairs and the reasons
    #: it was handed (:mod:`.repair`). Not sealed content.
    repair: dict[str, Any] | None = None

    @property
    def id(self) -> str:
        return self.key

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["phase_path"] = list(self.phase_path)
        return out

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Receipt:
        data = dict(raw)
        data["phase_path"] = tuple(data.get("phase_path") or ())
        return cls(**data)


@dataclass
class PhaseRecord:
    """One phase instance (design §3.2 "phase record")."""

    path: str
    phase_id: str
    shape: str
    run_id: str
    status: str = "open"  # open | closed
    opened_seq: int = 0
    closed_seq: int | None = None
    close_reason: str | None = None
    validity: str = "valid"  # valid | invalid | partial
    members: list[dict[str, Any]] = field(default_factory=list)
    tally: dict[str, Any] | None = None
    charged_s: float = 0.0
    offset_s: float = 0.0
    calls: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> PhaseRecord:
        return cls(**dict(raw))


class RunStore(Protocol):
    """What the interpreter needs from storage."""

    def get_run(self) -> dict[str, Any] | None: ...
    def put_run(self, header: Mapping[str, Any]) -> None: ...
    def get_receipt(self, key: str) -> Receipt | None: ...
    def put_receipt(self, receipt: Receipt) -> None: ...
    def receipts(self) -> list[Receipt]: ...
    def get_phase(self, path: str) -> PhaseRecord | None: ...
    def put_phase(self, record: PhaseRecord) -> None: ...
    def phases(self) -> list[PhaseRecord]: ...


def _check_insert(old: Receipt | None, new: Receipt) -> bool:
    """True when ``new`` should be written; raises on a sealed conflict."""
    if new.status == "sealed":
        new_seal = seal_of(new.row_type, new.payload, new.prose)
        if new.sha256 is None:
            new.sha256 = new_seal
        elif new.sha256 != new_seal:
            raise SealConflict(f"receipt {new.key}: seal does not match its content")
    if old is None:
        return True
    if old.status == new.status and old.sha256 == new.sha256 and old.failure == new.failure:
        return False
    raise SealConflict(f"receipt {new.key} is already written and is insert-only")


def _check_phase(old: PhaseRecord | None, new: PhaseRecord) -> None:
    if old is not None and old.status == "closed" and old.to_json() != new.to_json():
        raise SealConflict(f"phase {new.path} is closed")


class MemoryStore:
    """The in-memory store (tests; one process)."""

    def __init__(self) -> None:
        self._run: dict[str, Any] | None = None
        self._receipts: dict[str, Receipt] = {}
        self._phases: dict[str, PhaseRecord] = {}

    def get_run(self) -> dict[str, Any] | None:
        return dict(self._run) if self._run is not None else None

    def put_run(self, header: Mapping[str, Any]) -> None:
        self._run = dict(header)

    def get_receipt(self, key: str) -> Receipt | None:
        return self._receipts.get(key)

    def put_receipt(self, receipt: Receipt) -> None:
        if _check_insert(self._receipts.get(receipt.key), receipt):
            self._receipts[receipt.key] = receipt

    def receipts(self) -> list[Receipt]:
        return sorted(self._receipts.values(), key=lambda r: r.seq)

    def get_phase(self, path: str) -> PhaseRecord | None:
        return self._phases.get(path)

    def put_phase(self, record: PhaseRecord) -> None:
        _check_phase(self._phases.get(record.path), record)
        self._phases[record.path] = record

    def phases(self) -> list[PhaseRecord]:
        return sorted(self._phases.values(), key=lambda p: (p.opened_seq, p.path))


def _write_atomic(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _name(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".json"


class FileStore(MemoryStore):
    """One JSON file per receipt and per phase under ``root`` (the harness
    host's store). Opening a folder that holds an interrupted run loads it,
    re-checking every receipt's seal."""

    def __init__(self, root: str | Path):
        super().__init__()
        self.root = Path(root)
        (self.root / "receipts").mkdir(parents=True, exist_ok=True)
        (self.root / "phases").mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        run = self.root / "run.json"
        if run.is_file():
            self._run = json.loads(run.read_text(encoding="utf-8"))
        for p in sorted((self.root / "receipts").glob("*.json")):
            r = Receipt.from_json(json.loads(p.read_text(encoding="utf-8")))
            if r.status == "sealed" and r.sha256 != seal_of(r.row_type, r.payload, r.prose):
                raise SealConflict(f"{p}: stored receipt does not match its seal")
            self._receipts[r.key] = r
        for p in sorted((self.root / "phases").glob("*.json")):
            ph = PhaseRecord.from_json(json.loads(p.read_text(encoding="utf-8")))
            self._phases[ph.path] = ph

    def put_run(self, header: Mapping[str, Any]) -> None:
        super().put_run(header)
        _write_atomic(self.root / "run.json", dict(header))

    def put_receipt(self, receipt: Receipt) -> None:
        if _check_insert(self._receipts.get(receipt.key), receipt):
            _write_atomic(self.root / "receipts" / _name(receipt.key), receipt.to_json())
            self._receipts[receipt.key] = receipt

    def put_phase(self, record: PhaseRecord) -> None:
        _check_phase(self._phases.get(record.path), record)
        _write_atomic(self.root / "phases" / _name(record.path), record.to_json())
        self._phases[record.path] = record

    def write_record(self, record: Mapping[str, Any], name: str = "record.json") -> Path:
        out = self.root / name
        _write_atomic(out, dict(record))
        return out


def released(receipts: Iterable[Receipt]) -> list[Receipt]:
    return [r for r in receipts if r.status == "sealed" and r.released]
