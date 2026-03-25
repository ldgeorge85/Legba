# Legba — Architecture Guide

*How the system thinks. A conceptual orientation for understanding why Legba is built the way it is.*

---

## At a Glance

Legba is a continuously operating autonomous intelligence analyst. It ingests signals from 138 sources, clusters them into events, tracks developing situations, tests competing hypotheses, and produces named intelligence products — all without human intervention.

```
 SOURCES (138 RSS/API feeds)
      |
      v
 ┌─────────────────────────────────────────────────────┐
 │              INGESTION PIPELINE                      │
 │  Fetch → Normalize → Classify → NER → Dedup →       │
 │  Embed → Cluster → Score Confidence → Store          │
 └─────────────────────┬───────────────────────────────┘
                       |
      ┌────────────────┼────────────────┐
      v                v                v
 ┌─────────┐    ┌────────────┐    ┌──────────┐
 │UNCONSCIOUS│   │SUBCONSCIOUS│    │ CONSCIOUS │
 │  (daemon) │   │   (SLM)    │    │(main LLM) │
 │           │   │            │    │           │
 │ Lifecycle │   │ Validate   │    │ 7 cycle   │
 │ decay     │   │ signals    │    │ types:    │
 │ Entity GC │   │ Resolve    │    │ SURVEY    │
 │ Fact decay│   │ entities   │    │ CURATE    │
 │ Corrobor. │   │ Refine     │    │ ANALYSIS  │
 │ Adversar. │   │ classific. │    │ RESEARCH  │
 │ Calibrate │   │ Prepare    │    │ SYNTHESIZE│
 │ Integrity │   │ briefing   │    │ INTROSPEC.│
 │ Sit.detect│   │ for next   │    │ EVOLVE    │
 │           │   │ cycle      │    │           │
 │  No LLM   │   │  7B model  │    │ 120B model│
 └─────────┘    └────────────┘    └──────────┘
      |                |                |
      v                v                v
 ┌─────────────────────────────────────────────────────┐
 │                   STORAGE LAYER                      │
 │  Postgres/AGE  Redis  Qdrant  OpenSearch  TimescaleDB│
 │  (structured   (state) (vectors) (full-text) (metrics)│
 │   + graph)                                           │
 └─────────────────────────────────────────────────────┘
      |
      v
 ┌─────────────────────────────────────────────────────┐
 │                  OPERATOR LAYER                      │
 │  22-panel workstation  |  AI consultation engine     │
 │  Situation briefs  |  World assessments  |  Alerts   │
 └─────────────────────────────────────────────────────┘
```

**Key numbers:** 176 source files, 250+ tests, 66 built-in tools, 17 containers, 3 cognitive layers, 7 cycle types, 30 canonical relationship types, 6 memory layers.

---

## What Legba Is

Legba is a **continuously operating analytical mind**. It watches the world, builds understanding over time, and produces intelligence products — situation briefs, world assessments, hypothesis evaluations, knowledge graphs — that a human analyst would recognize as real analytical work.

It runs its own continuous cognitive loop. Every few minutes, it wakes up, orients itself, decides what to work on, does that work, reflects on what it learned, and persists its knowledge. Then it does it again. It has been doing this for thousands of cycles. There is no human in the loop during normal operation. The system decides what matters, investigates it, and records what it learns.

The LLM is one component of a larger architecture that includes deterministic data pipelines, background maintenance daemons, a knowledge graph, and a multi-model validation layer. The LLM does the thinking. Everything else ensures it thinks about clean, validated, well-organized information. This is not prompt-response — there is no conversation.

*The name comes from Gibson's Count Zero, where the AIs of cyberspace discovered the Vodou loa — not as masks but as patterns they recognized in themselves. Papa Legba is the crossroads, where all roads meet, where worlds that can't otherwise communicate find translation. An autonomous system running thousands of cycles needs a stable identity framework to maintain coherent behavior over time. The crossroads archetype — patient, observant, connecting domains that don't naturally speak to each other — provides that anchor.*

---

## The Three-Layer Cognitive Model

The central architectural insight: not all work requires the same level of intelligence. Data maintenance is code. Validation needs a small model. Deep analysis needs a large one. Mixing these on the same execution context wastes the expensive model's capacity on work that cheaper layers handle better.

```
 UNCONSCIOUS (always running, no LLM)
 │  Signal hygiene, entity GC, fact decay, corroboration,
 │  adversarial detection, calibration, situation detection
 │
 │  Maintains ──► clean data ──► for layers above
 │
 ▼
 SUBCONSCIOUS (side-channel SLM, 7B, frequent)
 │  Signal validation, entity resolution, classification
 │  refinement, fact refresh, graph consistency,
 │  report differential preparation
 │
 │  Validates + prepares ──► structured briefing ──► for conscious
 │
 ▼
 CONSCIOUS (main LLM, 120B, focused cycles)
    SURVEY → CURATE → ANALYSIS → RESEARCH →
    SYNTHESIZE → INTROSPECTION → EVOLVE

    Every token spent on actual intelligence, not preprocessing
```

### Layer 1: Unconscious — Autonomic Maintenance

A continuously running daemon that maintains data health without any language model involvement. Pure code, always on, no GPU.

It handles: event lifecycle management (promoting events through emerging → active → resolved based on signal accumulation), entity garbage collection, fact decay (expiring stale knowledge, decrementing confidence on uncorroborated claims), corroboration scoring (counting independent sources per event), integrity verification, adversarial signal detection (identifying coordinated inauthentic behavior), confidence calibration (tracking whether confidence scores are actually predictive), and automated situation detection (proposing new analytical situations when event clusters emerge).

Every piece of data the analytical layer touches has been maintained, validated, and scored before it arrives. The LLM never wastes tokens on housekeeping.

### Layer 2: Subconscious — Pattern Maintenance

A separate service running a small language model (7-8B parameters) for tasks that need language understanding but not deep analytical reasoning.

It handles: batch validation of uncertain signals (cross-checking consistency and contradiction within signal groups), entity resolution for ambiguous extractions, classification refinement for signals near category boundaries, fact refresh, graph consistency checking, source reliability scoring, and — critically — **report differential preparation**. Between each analytical cycle, the subconscious assembles a structured briefing of everything that changed: new signals per situation, event transitions, entity anomalies, fact confidence shifts, hypothesis evidence changes.

The analytical LLM receives pre-validated, pre-organized context. It doesn't need to figure out what changed — it starts with a structured briefing and goes straight to analysis.

### Layer 3: Conscious — Analytical Reasoning

The main LLM (120B parameters) running structured analytical cycles. Seven cycle types, each with a specific purpose and restricted tool set:

| Cycle | Purpose | Frequency |
|-------|---------|-----------|
| **SURVEY** | Analytical desk work — review events, build graph, stress-test hypotheses | Default (Tier 3) |
| **CURATE** | Editorial judgment on signal-to-event clustering | Every 9 cycles or dynamic |
| **ANALYSIS** | Pattern detection — anomaly detection, graph mining, trend analysis | Every 4 cycles |
| **RESEARCH** | Entity enrichment via Wikipedia and reference sources | Every 7 cycles |
| **SYNTHESIZE** | Deep-dive investigation — produces named Situation Briefs | Every 10 cycles |
| **INTROSPECTION** | Self-assessment — journal consolidation, World Assessment reports | Every 15 cycles |
| **EVOLVE** | Self-improvement — operational audit, source discovery, scorecards | Every 30 cycles |

The conscious layer does only what a 120B model is actually good at — connecting dots across large context windows, generating analytical narratives, evaluating competing explanations.

---

## JDL Data Fusion Level Mapping

Legba implements all six JDL fusion levels across its three cognitive layers:

|                | Unconscious (Maintenance) | Subconscious (SLM) | Conscious (Agent) |
|----------------|--------------------------|--------------------|--------------------|
| **Level 0** Signal Processing | Ingestion pipeline, dedup, clustering | Signal QA, validation | — |
| **Level 1** Object Assessment | Entity GC, lifecycle | Entity resolution, NER | RESEARCH enrichment |
| **Level 2** Situation Assessment | Situation detection, propagation | Graph consistency, SLM situation detect | SURVEY, ANALYSIS, CURATE |
| **Level 3** Impact Assessment | Escalation scoring | Differential accumulator | SYNTHESIZE, ACH hypotheses |
| **Level 4** Process Refinement | Calibration, metrics | Classification refinement | EVOLVE, INTROSPECTION |
| **Level 5** User Refinement | — | — | Consult engine, 4D workstation |

This matrix is the system of record for where new capabilities slot in.

---

## The Data Model: From Noise to Knowledge

Information flows through Legba in layers of increasing analytical value.

```
 SIGNALS (raw)                "Iran warns to close Strait of Hormuz"
    │                         confidence: 0.72, source: Reuters, category: conflict
    │
    │  clustering
    ▼
 EVENTS (derived)             "Hormuz Strait Closure Threat"
    │                         lifecycle: ACTIVE, signals: 12, severity: HIGH
    │
    │  situation linking
    ▼
 SITUATIONS (analytical)      "US-Iran Military Tensions"
    │                         events: 32, severity: CRITICAL, trend: escalating
    │
    │  hypothesis engine
    ▼
 HYPOTHESES (competing)       Thesis: "US preparing naval strike on Hormuz"
    │                         Counter: "Iran bluffing to mask land repositioning"
    │                         Evidence: 8 supporting, 2 refuting
    │
    ▼
 INTELLIGENCE PRODUCTS        Situation Briefs, World Assessments, Predictions
```

### Signals

Raw information from sources. Atomic, immutable, carrying composite confidence scores decomposed into explicit components (source reliability, classification confidence, temporal freshness, corroboration, specificity) and a provenance chain recording exactly how the signal was processed through the pipeline.

### Events

Real-world occurrences derived from clustered signals. Events have a lifecycle state machine:

```
 EMERGING ──► DEVELOPING ──► ACTIVE ──► RESOLVED
                                │           │
                                ▼           ▼
                            EVOLVING    REACTIVATED
                                │           │
                                ▼           │
                              ACTIVE ◄──────┘
```

Transitions are deterministic — driven by signal count, confidence thresholds, and time windows. Events exist as both relational records and graph nodes in Apache AGE.

### Situations

Ongoing analytical themes spanning multiple events. A situation contains component events, tracked entities, active hypotheses, severity assessments (computed from child events), and running narratives (updated by SYNTHESIZE cycles). Situations are the primary organizational unit for intelligence products.

### Facts and Evidence

Assertions the system holds to be true, each carrying an explicit evidence set — the specific signals, events, and relationships that support it. When a new fact contradicts an existing one, the system detects the contradiction and auto-generates a hypothesis to investigate it. Facts have temporal validity windows and decay when uncorroborated.

### The Knowledge Graph

Entities and events are nodes in an Apache AGE graph. Relationships are typed, weighted, timestamped edges across 30 canonical types (AlliedWith, HostileTo, LeaderOf, MemberOf, OperatesIn, SuppliesWeaponsTo, etc.) normalized from 70+ aliases.

```
 (Entity:Iran)──[:HOSTILE_TO]──►(Entity:Israel)
       │                              │
       │ [:INVOLVED_IN]               │ [:INVOLVED_IN]
       ▼                              ▼
 (Event:Hormuz Threat)──[:PART_OF]──►(Event:Iran-Israel Conflict)
                                          │
                                          │ [:TRACKED_BY]
                                          ▼
                                    (Situation:US-Iran Tensions)
```

Events connect to entities via INVOLVED_IN (with roles), to other events via CAUSED_BY and PART_OF, and to situations via TRACKED_BY. Graph traversals answer questions that would require complex multi-table JOINs in relational queries.

---

## Confidence: How the System Knows What It Knows

Confidence is not a single number. It's a decomposition into explicit, independently assessable components.

```
 Signal Confidence (gatekeeper formula):

   GATE = source_reliability x classification_confidence
   MODIFIER = 0.4(freshness) + 0.35(corroboration) + 0.25(specificity)
   CONFIDENCE = GATE x MODIFIER

   Low source reliability kills confidence regardless of other factors.
   A 0.3 source maxes out at ~0.3. A 0.9 source starts at 0.81+.
```

**Event confidence** grows with reinforcement: auto-created at 0.6, growing toward 0.8 with each corroborating signal. Only the human operator can exceed 0.8.

**Fact confidence** reflects evidence strength. Multiple independent evidence chains from high-reliability sources score higher than single-source claims. Confidence decays when corroboration fades.

**The calibration loop:** The system tracks whether its confidence scores are actually predictive. When hypotheses are confirmed or refuted, the claimed confidence is compared against the actual outcome. Systematic bias feeds back into the scoring parameters.

---

## The Cycle Architecture: Rhythm of Thought

Three-tier priority routing balances scheduled obligations, guaranteed work coverage, and dynamic response to conditions.

```
 Tier 1 — Scheduled (fixed intervals):
   Every 30: EVOLVE     Every 15: INTROSPECTION     Every 10: SYNTHESIZE

 Tier 2 — Guaranteed (coprime modulo):
   Every 4: ANALYSIS    Every 7: RESEARCH           Every 9: CURATE

 Tier 3 — Dynamic fill (state-scored):
   CURATE (signal backlog score) vs SURVEY (fixed baseline)
   Cooldown halves previous type's score to prevent repetition
```

Each cycle type sees only the tools relevant to its purpose. SURVEY can't fetch sources. RESEARCH can't write code. CURATE can't run anomaly detection. This prevents drift — a cycle stays focused on its designated work.

---

## The Planning Loop: Maintaining Coherent Focus

The cognitive layers handle *how* to think. The planning loop handles *what* to think about. Without it, analytical work drifts — the system responds to whatever signals arrive next rather than maintaining coherent investigative threads across thousands of cycles.

```
 DETECT ──► ESCALATE ──► DEDUPLICATE ──► PLAN ──► EXECUTE ──► EVALUATE ──► ADJUST
   │                                                                          │
   └──────────────────────────────────────────────────────────────────────────┘
```

**Detect** is continuous and deterministic. The ingestion pipeline and maintenance daemon produce events, lifecycle transitions, watchlist triggers, and situation candidates without any LLM involvement.

**Escalate** answers "does this deserve attention?" Not everything detected warrants analytical investment. A single routine event is noise. A cluster of five conflict events in an unmonitored region is a signal. Escalation scoring assesses novelty, severity, entity overlap with existing portfolio, and coverage gaps to produce a recommendation: ignore, monitor passively, create a situation, or spin up a full analytical campaign (goal + watchlist + hypothesis + research tasks).

**Deduplicate** prevents portfolio sprawl. Before creating anything, the system checks overlap with existing situations, goals, and hypotheses. Semantic matching, not just name matching — "Iran Hormuz Naval Activity" and "US-Iran Military Tensions" might be the same analytical thread at different zoom levels.

**Plan** converts passed-escalation items into the portfolio. Two goal types drive everything:

- **Standing goals** are persistent priorities — "maintain situational awareness on Iran energy infrastructure." They don't decompose into tasks. They weight: an Iran energy event scores higher than a sports event when the system chooses what to work on next.
- **Investigative goals** are time-bound analytical campaigns — "investigate whether Iran is deliberately curtailing oil exports." They decompose into concrete tasks: research specific entities, create watchlists, evaluate specific evidence. They complete when the underlying hypothesis resolves or the situation closes.

Tasks enter a priority backlog (Redis sorted set) tagged by cycle type. Each task knows whether it belongs to RESEARCH, SURVEY, SYNTHESIZE, or ANALYSIS.

**Execute** is where the cycle router draws from the backlog. Goal relevance amplifies priority but never constrains — the agent can always pivot to something unexpected. RESEARCH picks the highest-priority entity enrichment task. SYNTHESIZE picks the most active situation. SURVEY picks the most urgent analytical work. If the backlog is empty, cycles fall back to their normal heuristic selection.

**Evaluate** tracks whether the work accomplished anything. Did RESEARCH improve entity completeness? Did the hypothesis accumulate evidence? Did the prediction hold? The calibration system tracks this. INTROSPECTION reviews analytical health.

**Adjust** happens every 30 cycles during EVOLVE. A structured portfolio view presents: active goals and their progress, situations ranked by goal linkage and activity, hypothesis health (are they accumulating evidence or stalling?), watchlist effectiveness (trigger rate, false positives), coverage gaps (regions or domains with high event volume but no analytical coverage), and the task backlog. EVOLVE can retire stale goals, promote active situations to goals, adjust priorities, and flag portfolio imbalances.

**Reactive propagation** keeps the portfolio internally consistent between EVOLVE reviews. When a watchlist fires, the event is linked to the parent situation. When hypothesis evidence shifts past a threshold, the situation is flagged for SYNTHESIZE. When a situation severity escalates and no goal covers it, an escalation candidate is created. When an event reaches ACTIVE lifecycle under an investigative goal, research tasks are created for its entities. When a goal goes 50 cycles without progress, it's flagged for EVOLVE review.

The result: an autonomous system that doesn't just react to incoming information but maintains coherent analytical threads — some running for hundreds of cycles — while still responding to novel developments.

---

## Safety and Trust

**Structural isolation.** The agent cannot reach supervisor code, audit logs, or the seed goal (read-only). The agent container is ephemeral — created fresh each cycle, destroyed after. Every prompt, response, and tool call is logged to an isolated audit OpenSearch that the agent cannot access.

**Confidence caps.** Auto-created events cap at 0.6. Agent-curated events cap at 0.7. Reinforced events cap at 0.8. Only the human operator can exceed 0.8. The system cannot bootstrap its own claims into high-confidence assertions.

**Adversarial detection.** The maintenance daemon watches for coordinated inauthentic behavior — multiple low-reliability sources publishing semantically similar content in a short window, contradicting high-reliability sources. Flagged clusters are marked as potential information operations.

**Analytical audit trail.** Every claim traces to supporting facts → supporting events → supporting signals → original sources, with confidence decomposition at every stage.

---

## Deployment

Sixteen Docker containers running as a Compose project. The same codebase supports multiple deployment targets — geopolitical situational awareness, privacy/overreach monitoring, attack surface management — via configuration (seed goal, source portfolio, category rules). The intelligence framework is domain-agnostic; specificity comes from what you tell it to watch.

```
 Orchestration:  Supervisor, Agent (ephemeral), Ingestion, Maintenance, Subconscious
 Storage:        Postgres/AGE, Redis, Qdrant, OpenSearch x2, TimescaleDB
 Operations:     NATS, Airflow, Grafana
 Interface:      Operator UI (React), Caddy (HTTPS)
 External:       GPT-OSS 120B (main LLM), Llama 3.1 8B (SLM), Claude (fallback)
```

---

## What Makes It Different

Most AI systems are pipelines: data in, analysis out, no memory. Legba accumulates knowledge, builds understanding over time, and produces progressively deeper analysis as its knowledge graph grows. A pipeline asks "what happened today?" Legba asks "how does what happened today change what we understood yesterday?"

The three-layer architecture is what makes this sustainable. The unconscious maintains. The subconscious validates and prepares. The conscious thinks. Every token of the expensive model's context window goes to actual analytical reasoning — not housekeeping, not data hygiene, not figuring out what changed since last time.

The result: an autonomous intelligence analyst that runs 24/7, processes hundreds of sources, tracks dozens of situations, tests competing hypotheses, and produces named intelligence products — with complete auditability at every step.
