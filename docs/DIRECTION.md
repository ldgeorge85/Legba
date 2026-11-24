<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Direction

Where Legba is going, and the rule that orders the work. What the platform is, and how the platform
differs from the geopolitics example instance, is in the root [README](../README.md); what is built
today is in [STATUS.md](STATUS.md).

## The rule that orders the work

Measure, verify and cite before autonomy. Each layer rests on the layer below and cites it: extraction,
then bounded units that each answer one narrow question over a cited slice, then compositions that quote
those units, then a scorecard that says what the verified claims support. The more autonomous parts
(self-optimisation, forecasting, agents that change things) come back only as measured experiments with
a before-and-after number and a human gate, because tuning an unverified producer optimises the wrong
objective. A part with no proven skill is stated as having none.

The corollary is the honesty contract: a capability that is not built is a declared seam that fails
loudly ([SEAMS.md](SEAMS.md)), never a silent stub and never fabricated output.

## What is next

Seven lines of work. The first three continue the checking layer, the platform's main position: the
machinery that decides whether what has already been written holds up, and says so in typed, drillable
terms.

**1 · Finish the checking layer.** Typed absence, curated collections of the past and the
contrary-evidence pass stand. What remains is the write half of collection history and the source half
of `not_collected`, both declared seams.

**2 · Make the record itself checkable.** Citations are stored as rows and every row names its writer.
The held record of statements (what was stated, on what support, with a hash-chained status history) is
built and fed only from one shadow path; it becomes something a reader sees only after its producing
prompts are reviewed and its verify script holds on live data. Source origins and origin groups are
derived and correctable; the next step is for independence counting to use them, so two outlets of one
publisher stop counting as two confirmations.

**3 · Make measurement honest at scale.** Correctness is graded against references that bear on a thin
slice of what the reads say. The answer is coverage published beside correctness wherever the number
appears, not a better-sounding number; the composition correctness gate stays off while coverage is
thin. The forecast ledger keeps every score withheld until enough sealed questions resolve, and then
publishes Brier scores with their baseline and sample size.

**4 · The divergence line.** Two designs wait on a real model endpoint. One extends layered source
fan-out with physical-observation feeds, so that physical against narrative is itself a finding, with a
declared absence where a layer is missing. The other applies the same arithmetic to model families
instead of source layers: every family judged the same way, starting with a single desk-day replayed
through all of them. A divergence measurement over one model family measures nothing, so neither is
started before there is more than one.

**5 · The interpretive tier as a faculty of meta agents.** Analysts whose subject is the platform's own
record: what it has been saying, where its reads disagree, what it keeps failing to see. They assert no
new fact, write no publish edge, are faithfulness-verified like everything else, and are never averaged
into a consensus voice. The direction is more declared falsifiable priors, inquiries whose hypotheses
carry a frozen resolution test, and a yield measure that decides whether the tier is worth its cadence.
Anything any of them wants to change outside its own entries goes to the human-gated proposal queue.

**6 · Consult drives.** Consult already presents a case: a governed, cited sequence of ordinary console
actions that a reader can stop, undo and replay. The next shape is changes a person asks for through
Consult (sources, targets, units, entities, facts) through governed write tools, with the person's own
role carried into the run; today every such change needs an admin (SEAMS #86).

**7 · Accounts beyond one shared record.** Sign-in, three roles and a credential per service exist
behind a switch. Next are groups and memberships, single sign-on, permissions per source and per set,
and narrowed service credentials (SEAMS #83–#87). A second instance profile brought up end to end is
what proves the platform separable from the example; every seam it hits becomes a platform change, not
a fork ([PROFILES.md](PROFILES.md)).

## Standing deferrals

Each is designed and gated, and none is claimed elsewhere in the docs until it ships.

| Item | Shape | What it waits on |
|---|---|---|
| **Analysis-plane tenancy** | a tenant column with write-time stamping first, then enforcement on every substrate read and each hop of a lineage walk | the column; enforcing against tables that do not carry it would be theatre |
| **Serving interop** | the bundle producer and the push client are built; serving a pollable collection and syncing to a peer platform are not | an emitter bound to a live producer, and a provisioned destination |
| **Multimodal extraction** | the job plane, the envelope and the derived-signal loop are built; the extraction service refuses rather than fabricating | real model backends behind the extraction endpoint, then an object store and non-text renderers |
| **Scale-out** | single-replica streams, one unpartitioned signals table and a single-replica trigger accumulator are deliberate | volume that justifies the migration; the accumulator's ceiling costs a late fire, never a lost signal |
| **Cheap-model demotion** | the actor-side machinery is built and audited; the strategy equals an explicit paused window | a second model resolved as a fallback route |
| **Deep-crawl discovery** | a job handler converging on the same validate-before-register gate the descriptor route uses | the handler; nothing enqueues a kind nothing consumes |
| **The standing-question closer** | the watcher half is live; feeding a confirmed match back into the producer's next run is not | an owner's decision, now that the match-precision bar is met |

---

- [STATUS.md](STATUS.md): what is built, with its limits.
- [SEAMS.md](SEAMS.md): the declared-seam register.
- [ARCHITECTURE.md](ARCHITECTURE.md): the shape of what exists.
- [COLLECTIONS.md](COLLECTIONS.md) · [DATA_SOURCES.md](DATA_SOURCES.md) · [CORRECTNESS_GRADER.md](CORRECTNESS_GRADER.md): the checking layer's own pages.
