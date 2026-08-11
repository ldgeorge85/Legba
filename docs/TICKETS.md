# Ticket & decision-ID glossary

Legba's source comments and design docs reference short alphanumeric IDs such as
`L-205`, `P-16`, `A-2`, `DM-3`, `G3`. These are **internal design-decision and
work-item references** carried over from the project's planning history. They were
written for maintainers who have the planning ledger in front of them, so an
external reader cannot always resolve a bare ID from the code alone.

This file explains the **prefix scheme** so the IDs are at least *legible* — what
class of decision each prefix denotes and where to look for the full rationale.
The IDs are intentionally left in the code (hundreds of comments cite them as the
"why" behind a non-obvious choice); this glossary is the cheaper, non-destructive
alternative to stripping them.

The full per-ID rationale lives in the project planning history (commit messages,
the `planning/` directory, and the design-ledger documents). When an ID cites a
section — e.g. `L-107 §7` — that section number indexes into the corresponding
ledger document.

## Prefix scheme

| Prefix | Reads as | Denotes | Notes / where to look |
|--------|----------|---------|------------------------|
| `L-NNN` | **L**edger / **L**esson | A numbered design-ledger entry or recorded lesson. By far the most common (~1200 refs). Often cited with a section, e.g. `L-107 §7`, `L-102 §5`. | The design ledger; the section index lives inside each ledger entry. Range roughly `L-1`..`L-248`. |
| `P-NN` | **P**ivot **P**hase | A phase / sub-section of the source-first **PIVOT** (the pivot proposal / build-plan documents, which live in the project planning history, not this repo). Frequently paired with a PIVOT section, e.g. `P-07 / PIVOT §4.6`. | `PIVOT §x.y` cross-refs. Range `P-1`..`P-17`. |
| `A-N` | **A**gency | Agency / action-pack work items (the agentic tool-binding plane, media-loop closure, budget gating). e.g. `A-2` media-loop close, `A-3` `AgencyToolBinding`, `A-5` budget check. | Agency build notes. |
| `B-N` | **B**earer / substrate-auth | Substrate authentication & authorization hardening (bearer-token gating, fail-closed 503s). e.g. `B-1` stop publishing tokens, `B-2` `require_bearer`. | Auth-hardening notes. |
| `C-N` | **C**leanup / reaper | Resource-lifecycle and reaper/zombie-cleanup decisions. e.g. `C-3` released-zombie reaping, `C-1` prediction stopgap. | Cleanup-pass notes. |
| `S-N` | **S**ituation / **S**ource-kind | Situation-object and source-kind decisions. e.g. `S-2` situation-close temporal frame, `S-3` `json_api` generic source kind. | Phase-5 situations plan. |
| `E-N` | **E**ndpoint | Substrate-read / API endpoint decisions. e.g. `E-1` cross-target read, `E-2` lineage-walk, `E-5` source-credibility CRUD. | API-surface notes. |
| `F-N` | **F**ix / decision | Targeted fix decisions, usually dated. e.g. `F-1` (drop an enqueue path, 2026-06-09), `F-2` `demote_and_continue`. | Inline-dated decisions. |
| `G-N` / `G\d` | **G**ate / review-**G** | Review-gate findings (often written `G1`..`G5`, also `review-G3`). Version-drift sweeps, form-verdict parity, budget gates. | Adversarial/review ledgers. |
| `K-N` | **K**nown-issue / e2e | End-to-end / known-issue flags. e.g. `K-3` predictor e2e. | e2e notes. |
| `W-N` / `W\d` | **W**ave | Build-wave work items (the worktree fan-out waves `W-1`..`W-4`, also `W1`..`W3`). | Build-plan waves. |
| `M-NNN` | **M**ode / deployment | Deployment-mode taxonomy. e.g. `M-036` deployment-mode tax. | Mode taxonomy. |
| `DM-N` | **D**ata-**M**odel | Data-model / schema decisions. e.g. `DM-3` pre-descriptor back-tag sentinel, `DM-6` PascalCase aliases, `DM-7` shared-schema knob. | Schema decisions. |
| `LB-N` | **L**ewis **B**rief | Operator (Lewis) decisions/briefs, usually dated. e.g. `LB-3` substrate clean reset, `LB-12` bot-catalog decision. | Operator decision log. |
| `KC-N` | **K**eep-**C**ompatible | Backward-/shape-compatibility constraints. e.g. `KC-2` "stay open dict". | Compat constraints. |
| `OQ-N` | **O**pen **Q**uestion | Open-question leanings recorded during design. e.g. `OQ-4` (cited with `L-106 §5`). | Design open-questions. |
| `OBS-N` | **OBS**ervability | Observability requirements, indexed into `legba_observability.md`. e.g. `OBS-6` per-minute metrics. | `legba_observability.md`. |
| `MN-N` | **M**eeting **N**ote | Decisions captured in a dated meeting/decision note. e.g. `MN-3 Q13` (2026-05-12). | Meeting/decision notes. |
| `DSL-N` | **DSL** | Descriptor-DSL decisions. e.g. `DSL-3`. | Descriptor-DSL notes. |
| `O-N` | **O**perator | Operator-gated item. | Operator log. |

### Not ticket IDs (false-positive shapes)

The same `XXX-NNN` shape also appears in standards/identifiers that are **not**
internal tickets and should be read literally: `SHA-256`, `ISO-8601`, `UTF-8`,
`RFC-####`, `BCP-47`, `GPT-4`. These are excluded from the scheme above.

## Convention going forward

- New design decisions worth citing in code get a prefixed ID from the table
  above (most commonly `L-NNN` for a ledger entry/lesson).
- When citing an ID in a comment, prefer to add a few words of inline context so
  the comment is self-explanatory even without the ledger
  (e.g. `# L-205: target-owned poll path retired sentence-transformers`).
- Cite a section when one exists: `L-107 §7`.
- Keep this glossary updated when a new prefix is introduced.
