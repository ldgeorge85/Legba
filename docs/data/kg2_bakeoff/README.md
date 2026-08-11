# K-G2 bake-off artefacts

Measurement output for `docs/TYPING_BAKEOFF_2026-08-03.md`. Read that first —
these are its raw numbers, not a standalone result.

Everything here was produced read-only against the live substrate on
2026-08-03 by `scripts/kg2_pool_measure.py` → `kg2_sample_prep.py` →
`kg2_typing_bakeoff.py` → `kg2_bakeoff_score.py`. Nothing was written to
production.

| file | what it is |
|---|---|
| `pool_summary.json` | the 174,632-row pending pool scored against the qualification bar: independent-source histogram, the bar sweep (11 settings), and the retention counts |
| `sample_candidates.csv` | the 200 deterministically-sampled bake-off candidates with every qualification component (`SAMPLE_SEED = 20260803`; an unchanged pool reproduces this file exactly) |
| `worksheet.csv` | **the sample worksheet** — all 200 candidates with evidence excerpt and all four models' decision, confidence and rationale side by side |
| `handcheck_worksheet.csv` | **UNLABELED, for the operator.** The 40 most-contested candidates (even 2-2 splits first) with three empty `OPERATOR_*` columns. The labels are the operator's call — see the report §6.4 for why an agent's guess would be worse than none |
| `agreement.json` | pairwise agreement matrices (raw edge/reject, Cohen's κ, exact `rel_type`), per-model accept behaviour, accept rate by qualification stratum, split-case counts |
| `economics.json` | per-model edges/call, tokens/edge, wall/edge, parse-failure rate, USD |
| `batch_size_sweep.json` | the N ∈ {1, 6, 12, 24, 40} sweep behind the recommended batch size |

**A note on the worksheets.** Evidence excerpts are short spans of already-
ingested public news copy, retained because a verdict is unreadable without the
text it was made on. Entity names are the raw NER surfaces, junk included —
`Trump / almost four months` is in there, and is supposed to be.
