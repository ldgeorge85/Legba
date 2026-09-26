<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Direction

Where Legba is going, and the rule that decides what gets built next.

## The glass tower

Legba is built as a tower you can see through.

Each floor rests on the floor below and cites it. Extraction sits at the bottom:
temporal facts and typed signed relations, written once per observation. Above
it, nine bounded units each answer one narrow question over a cited slice. Above
them, a composition quotes those units rather than paraphrasing them, and the
interpretive read is a separate row fenced to the one record it read. Above
that, a banded scorecard says what the verified claims support and says
"insufficient evidence" where they support nothing.

Every floor is transparent in two senses. A reader can drill any sentence down
through every floor to the original item — the citation, the verify verdict, the
receipt, the archived bytes. And the platform publishes what it knows about its
own reliability: per-unit faithfulness, correctness against independently built
references, an external audit of its claims against the open web, calibration
numbers that are honest-null where nothing has been measured and report no skill
where there is none.

The rule that orders the work is **measure, verify and cite before autonomy**.
The more autonomous legs — self-optimisation, forecasting — are held at the top
of the tower rather than dropped from it: they return only as measured
experiments, with a real before-and-after number and a human gate, because
self-tuning an unverified producer optimises the wrong objective. A leg with no
proven skill is stated as having none, here, rather than quietly overclaimed.

The corollary is the honesty contract the whole product runs on: a capability
that is not built is a declared seam that fails loud, never a silent stub and
never fabricated output. `SEAMS.md` is that registry and `STATUS.md` is its
companion — what is built, with its limits.

## What it is

**Legba is an intelligence service you define in YAML and that measures its own
correctness.**

It is **software-defined**. An instance is a set of descriptors: sources, desks,
analyst units, capability packs, model routes, cadences, grants and budgets are
YAML documents registered through one API, and the runtime is generic and reads
them. Adding a country is a target descriptor and the whole spine picks it up by
predicate. Adding a feed is a source descriptor. Adding a whole new desk family
— shipping lanes, commodity flows — is a set of targets and one tag predicate,
with no new analyst kind and no new code path. Changing what the platform
watches, thinks about, or is allowed to do is a descriptor change.

It is a **long-horizon agentic environment**. The analysts are scheduled actors
with memory: a first-person journal and its consolidation, a third-person
chronicle of the public record, and a faculty of interpretive lenses that each
carry one declared falsifiable prior. They act through governed tool packs,
where capability is the intersection of what the analyst declares, what the desk
permits and where the pack applies — and the operator holds the permit leg. And
the platform grades its own output on three axes that are never pooled:
faithfulness to the cited evidence on every layer, correctness against
references built blind to its own substrate, and a standing external audit of
its claims against live search. The numbers are published, including the ones
that are bad.

## What is next

Four legs, in this order. The first is the position the platform is taking: not
another reasoning layer, but a **checking layer** — the machinery that decides
whether what has already been written holds up, and that says so in typed,
drillable terms rather than in a blank.

**1 · Finish the checking layer.** Most of it stands: typed absence as one named
thing on `GET /v3/absence?scope=<desk>` and on every reader surface that used to
show a blank cell; curated collections of the past behind a firewall from the
live plane, with a cited historical observation that resolves in the card and
carries its own stale tense; a contrary-evidence pass that goes looking for
opposition and records a contention rather than a verdict, behind four fences
that stand between a search result and a claimed contradiction. What remains is
the write half of collection history and the source half of `not_collected`,
both declared seams rather than quiet gaps.

**2 · Make the measurement honest at scale, and release on it.** Correctness is
graded nightly for every country desk, but the references it grades against are
built on the free plane and bear on a thin slice of what a desk actually says.
The answer is not a better-sounding number: it is to publish coverage beside
correctness everywhere the number appears, so a high share drawn from a handful
of decided claims cannot read as a strong result. The composition correctness
gate stays **off** while coverage is this thin, and no money is spent on
references until the reasoning plane it would be spent through is settled. When
that lands, the free reference builder is re-measured against topped-up
references rather than retired on suspicion.

**3 · The divergence leg.** Two designs wait on a real model endpoint and are
deliberately unstarted until there is one. The first extends the layered fan-out
with physical-observation feeds, so that physical against narrative is itself a
finding with a declared absence where a layer is missing. The second is the
**model-divergence layer**: the same arithmetic applied to model families rather
than source layers — every family judged the same way, the first measurement a
single desk-day replayed through all of them. Filing it rather than building it
is the point: a divergence measurement over one model family measures nothing.

**4 · Grow the interpretive tier into a faculty of meta agents.** It runs beside
the other three and costs nothing, on the free plane. These are analysts whose
subject is not the wire but the platform's own record — what it has been saying,
where its reads disagree, what it keeps failing to see. They read the verified
tower top, assert no new fact, write no publish edge, and are
faithfulness-verified like everything else; the chorus pass that narrates where
they agree and split refuses to merge them into a consensus voice, because a
disagreement that has been averaged away cannot be checked. The direction is more
declared falsifiable priors, an inquiry whose hypotheses carry a frozen
resolution test before they can resolve, and a yield instrument that measures
whether the tier is worth its own cadence, so pruning follows the measurement
rather than taste. Everything any of them wants to change outside its own entries
goes to the human-gated proposal queue, and that queue is the shape the whole
direction rests on: an agent can write its own next breath and cannot rewrite its
own rules without the operator.

## The standing deferrals

Each of these is designed and gated, never silently half-built. None is claimed
anywhere in the docs until it ships.

| Item | Shape | What it waits on |
|---|---|---|
| **Scoped tokens, then single sign-on** | one bearer becomes a principal resolver over `read` / `operator` / `admin` scopes, with routers declaring a floor; identity arrives later as forward-auth at the edge rather than in-app session machinery | the scope split; in-app single sign-on is rejected until a concrete requirement forces it |
| **Analysis-plane tenancy** | the tenant column and its write-time stamping land first, then per-request enforcement on every substrate read and each hop of a lineage walk | the column — enforcing against tables that do not carry it would be theatre |
| **Serving interop** | the bundle producer and the push client are built; serving a pollable collection and syncing to a peer platform are not | an emitter re-bound to a live producer, and a provisioned destination |
| **Multimodal extraction** | the job plane, the envelope and the derived-signal loop are built; the extraction service ships refusing rather than fabricating | real model backends behind the extraction endpoint, then an object store and non-text renderers |
| **Scale-out** | three single-node truths are deliberate: single-replica streams, one unpartitioned signals table, a single-replica trigger accumulator | volume that justifies the migration; the accumulator's ceiling costs a late fire, never a lost signal |
| **Cheap-model demotion** | the actor-side machinery is built and audited; today the strategy equals an explicit paused window | a second model resolved as a fallback route in the deps builder |
| **Deep-crawl discovery** | returns as a job handler converging on the same validate-before-register gate the descriptor route uses | the handler — nothing enqueues a kind nothing consumes |
| **The standing-question closer** | the watcher half is live; propagating a confirmed match back into the producer's next run is not | a held operator decision, now that the match-precision bar is met |

---

- `STATUS.md` — what is built, with its honest limits.
- `SEAMS.md` — the declared-seam registry.
- `ARCHITECTURE.md` — the shape of what exists today.
- `DATA_MODEL_V3.md` · `V3_IMPLEMENTATION_PLAN.md` — events, the temporal surface and the
  typed graph: the specification and the build order it shipped on.
- `COLLECTIONS.md` · `LAYERS.md` · `CORRECTNESS_GRADER.md` — the checking layer's own pages.
