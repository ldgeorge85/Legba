# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export the workstation vocabulary + the frozen case contract to the UI (C-1b).

Writes ``legba-ui-v3/src/lib/workstationVocabulary.json`` from
``src/legba/data/workstation_vocabulary.py`` (the words a case may use) and
``src/legba/data/analysts/consult_case_schema.py`` (the nine tool schemas, the
step frames, the case record). The UI imports that JSON at build time; the
drift test (``tests/data_pkg/test_consult_case_schema.py``) fails when the
committed file is not byte-identical to what this prints.

Usage::

    PYTHONPATH=src python3 scripts/gen_workstation_vocabulary.py          # write
    PYTHONPATH=src python3 scripts/gen_workstation_vocabulary.py --check  # exit 1 if stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "workstationVocabulary.json"


def render() -> str:
    from legba.data import workstation_vocabulary as vocab
    from legba.data.analysts.consult_case_schema import contract_document

    return vocab.export_document(contract_document())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale — run scripts/gen_workstation_vocabulary.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
