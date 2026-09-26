<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Ticket and decision ids

Legba's source comments cite short alphanumeric ids — `L-205`, `D-3`, `K-1`,
`RUST-4`, `GLASS-1` — as the "why" behind a non-obvious choice. They were
written for a maintainer holding the planning ledger, and a bare id cannot
always be resolved from the code alone.

This file is where those ids are defined. It explains the prefix scheme, so an
id is at least legible: what class of decision it denotes, and where its
reasoning lives. The ids stay in the code — hundreds of comments depend on
them — and this is the cheap, non-destructive alternative to stripping them.

**This file and [CHANGELOG.md](../CHANGELOG.md) are the only places a ticket id
belongs.** The design and reference docs state rules and reasons in prose; if
a paragraph exists only to explain a ticket, it is history and lives in the
changelog. A code comment citing an id should carry a few words of context so
it reads without the ledger: `# L-205: the target-owned poll path retired
sentence-transformers`.

Full per-id rationale lives in the project's planning history — commit
messages and the internal design ledger — which is not part of this
repository. Where an id cites a section, as in `L-107 §7`, that section number
indexes into the corresponding ledger document.

## The two families

Ids come in two families, and reading them differently is the whole trick.

**Ledger ids are global.** `L-NNN` is a numbered entry in one continuous
design ledger, so `L-107` means the same thing wherever it appears.

**Program letters are local.** Every build program allocates its own single
letters from `A` again, so `D-3` in the assembly program and `D-5` in a merge
lane are unrelated, and `A-2` in the agency work is not `A-0` in the attention
measurement work. A program letter resolves only inside its program, and the
citing comment's own subject is the disambiguator — which is why a comment
that cites a bare letter id and nothing else is a bug in the comment.

**The hyphen carries no meaning.** `D3` and `D-3` are not the same id written
two ways; they are two programs that both reached `D`. `D3` is the no-stubs
rule from the proof-of-concept plan, and `D-3` is the assembly program's audit
arms. Read the surrounding subject, never the punctuation.

## The shapes

| Shape | Reads as | Example |
|---|---|---|
| `L-NNN` | A global ledger entry, optionally with a section. | `L-205`, `L-107 §7` |
| `X-N` / `XN` | A work item in program `X`. | `A-2`, `K-1`, `D3`, `M15` |
| `XN-TN` | A task inside a numbered phase of program `X`. | `P2-T8`, `S3-T4` |
| `XN-N` | A numbered item inside a phase, where the phase itself is numbered. | `P2-1`, `B0-13`, `C5-3` |
| `<program> <phase>` | A named program and one of its phases, written as two words. | `V3 P4a`, `V3 P5` |
| `X-Nx` | A revision of a work item. | `W-1b`, `T-1a`, `R2-FIX-3` |

A handful of ids are cited across the docs as standing rules rather than as
history, and are worth knowing by name: **`D3`**, the no-stubs rule that
[SEAMS.md](SEAMS.md) enforces; **`N-1`**, the proof-of-concept plan item that
built the stub scanner; and **`M13`/`M14`/`M15`**, the demote-only guards in
the verify pass.

## Global prefixes

| Prefix | Reads as | Denotes |
|---|---|---|
| `L-NNN` | Ledger / Lesson | A numbered design-ledger entry or recorded lesson, and by far the most common. Often cited with a section: `L-107 §7`. |
| `P-NN` | Pivot Phase | A phase of the source-first pivot, frequently paired with a pivot section: `P-07 / PIVOT §4.6`. |
| `M-NNN` | Mode | The deployment-mode taxonomy. |
| `OBS-N` | Observability | An observability requirement. |
| `DM-N` | Data Model | A data-model or schema decision. |
| `KC-N` | Keep Compatible | A backward- or shape-compatibility constraint. |
| `OQ-N` | Open Question | An open-question leaning recorded during design, usually cited beside the ledger entry that records it. |
| `MN-N` | Meeting Note | A decision captured in a dated decision note. |
| `DSL-N` | DSL | A descriptor-DSL decision. |
| `LB-N` | Operator brief | An operator decision or brief. |
| `O-N` | Operator | An operator-gated item. |

## Program letters

Each row names the program the letter belongs to and where its citations
cluster in the tree, which is what makes a bare id findable.

| Prefix | Program | Where it appears |
|---|---|---|
| `A-N` | Agency and action packs (the tool-binding plane, budget gating). A later program reuses `A-` for attention measurement. | `data/analysts/agency/`, `data/_polity_match.py` |
| `B-N` | Substrate auth hardening (bearer gating, fail-closed refusals). Also reused for ingest-collapse decisions. | `data/registry/`, `data/filters/dedupe.py` |
| `C-N` | Cleanup and resource-lifecycle decisions (reaping, deletions of dead modules). | `runtime/` |
| `D-N` | The assembly and shared-normalisation program: the one fold site, span origin, the audit arms. Also used for merge lanes in commit messages. | `data/provenance/text_fold.py`, `assembly_arms.py`, `data/analysts/assembly_*.py` |
| `E-N` | Substrate-read and API-endpoint decisions. | `data/registry/` |
| `F-N` | Targeted fix decisions. | `runtime/`, `data/analysts/` |
| `G-N` | Review-gate findings, and later the correctness-grader program's fences. | `data/analysts/deterministic_handlers/_correctness_*.py`, `_reference_fences.py` |
| `H-N` | Denominator honesty — making an aggregate report its true denominator. | `runtime/substrate_query_port.py` |
| `J-N` | Judge items. | `data/provenance/verify.py`, `judge_absence_rubric.py` |
| `K-N` | Known-issue and end-to-end flags, and the module-extraction items under the size gate. | across `data/` |
| `N-N` | The proof-of-concept plan, including the no-stub decision. | `tests/test_no_undeclared_stubs.py`, `runtime/grounding.py` |
| `Q-N` | Judge assessability: claim shape and score state. | `data/provenance/judge_*.py` |
| `R-N` | The reference-builder and correctness track. | `data/analysts/deterministic_handlers/_reference_*.py`, `runtime/grounding.py` |
| `S-N` | Situation objects and source kinds; also the source-catalog waves. | `data/analysts/deterministic_handlers/situation_*.py`, `data/sources/` |
| `T-N` | A task within a program, usually written with its program prefix: `P2-T8`, `T-1a`. | across `data/` and `runtime/` |
| `V-N` | Voice and verify revisions. | `data/analysts/_tradecraft.py`, `data/provenance/judge_pipeline_version.py` |
| `W-N` | Build-wave work items, including the worktree fan-out waves. | across the tree |
| `X-N` | Receipt phase naming, as stamped into a run's intermediate steps. | `runtime/dapr_actors.py`, `runtime/actor_critic.py` |
| `CW-N` | Cross-document corroboration. | `data/filters/dedupe.py`, `data/filters/fact_extractor.py` |
| `DEPTH-N` | The carried-block depth bridge in the assembly regime. | `data/analysts/assembly_payload.py`, `assembly_carry.py` |
| `DL-N`, `LV-N` | The voices program: the journal, the lenses, the chronicle. | `data/analysts/journal_*.py`, `prompts/lens_*/` |
| `DS-N` | The supply-chain domain pack and its ninth unit. | `data/analysts/inline_target.py`, `unit_grounding.py` |
| `FRAME-N` | Composition admissibility windows and framing. | `data/analysts/composition_*.py`, `runtime/actor_substrate_slice.py` |
| `GLASS-N` | The glass-tower verification-surfacing program. | `data/registry/external_audit_api.py`, `judge_stats_api.py` |
| `KW-N` | The claim-watch program: forward consumption, the watcher, the unbuilt closer. | `data/analysts/deterministic_handlers/claim_watch*.py` |
| `LIC-N` | The licence gate on fetched and archived evidence. | `data/schemas/source.py`, `deterministic_handlers/evidence_archiver.py` |
| `MECH-N` | Shared-mechanism decisions — one matcher, one fold, one site. | `data/_polity_match.py`, `data/provenance/text_fold.py` |
| `RUST-N` | The review train that produced the evidence-bytes fix and the optimizer mothball. | `data/analysts/optimizer.py`, `runtime/nats_informer.py` |
| `VOICE-N` | Voice prompt waves. | `data/analysts/composition_prompts.py`, `meta_findings_synthesizer.py` |

`FZ-N`, `FIX-N` and `TIER-N` appear once or twice each and are one-off
decisions inside the program that cites them. The allocating program is not named in the tree; the ids are read from the citing comments.

## Not ticket ids

The same shape appears in identifiers that are read literally.

- **Standards and versions**: `SHA-256`, `ISO-8601`, `UTF-8`, `RFC-####`,
  `BCP-47`, `AGPL-3.0`, `GPT-4`, `NLLB-200`, `COVID-19`, `bge-m3`.
- **Domain names that look like ids**: `G20` is the group of countries, not a
  gate finding; a Wikidata `Q`-id such as `Q22686` is an entity.
- **Grader model families**: `F0`, `F2` and `F3` name the correctness grader's
  model families, not items in program `F`.

## Convention

- A new decision worth citing in code gets a prefix from the tables above,
  most commonly `L-NNN`.
- Cite a section where one exists: `L-107 §7`.
- Add a few words of inline context, so the comment stands without the ledger.
- A new prefix means a new row here, in the same change.
- A dated statement about the decision goes in [CHANGELOG.md](../CHANGELOG.md),
  not in the doc that describes the mechanism.
