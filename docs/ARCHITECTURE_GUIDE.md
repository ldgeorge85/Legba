# Legba — Architecture Guide

*How the system thinks. A conceptual orientation for understanding why Legba is built the way it is.*

---

## What Legba Actually Is

Legba is not a chatbot, not an AutoGPT-style goal chaser, and not a data pipeline with an LLM bolted on. It is a **continuously operating analytical mind** — a system designed to watch the world, build understanding over time, and produce intelligence products that a human analyst would recognize as real work.

The name comes from Papa Legba, the Vodou loa of the crossroads — the figure who stands where all roads meet, who translates between worlds that can't otherwise communicate. Legba sits at the intersection of information streams (geopolitical, economic, technological, environmental, conflict) and finds the connections between them.

The key distinction: Legba doesn't react to prompts. It runs its own continuous cognitive loop. Every few minutes, it wakes up, orients itself, decides what to work on, does that work, reflects on what it learned, and persists its knowledge. Then it does it again. And again. It has been doing this for thousands of cycles.

---

## The Three-Layer Cognitive Model

A human mind doesn't consciously manage its heartbeat, digest food, and solve differential equations on the same thread. The brain separates autonomous maintenance from background processing from focused reasoning. Legba does the same thing.

### Layer 1: Unconscious — Autonomic Maintenance

**What it is:** A continuously running daemon that maintains data health without any language model involvement. Pure code, always on, no GPU.

**What it does:** Event lifecycle management (promoting events from "emerging" to "active" to "resolved" based on signal accumulation), entity garbage collection (marking dormant entities, cleaning orphaned graph edges), fact decay (expiring stale knowledge, decrementing confidence on uncorroborated claims), corroboration scoring (counting independent sources per event), integrity verification (ensuring evidence chains aren't broken), adversarial signal detection (identifying coordinated inauthentic behavior through velocity spikes and semantic echoes), confidence calibration (tracking whether the system's confidence scores are actually predictive), and automated situation detection (proposing new analytical situations when event clusters emerge).

**Why it matters:** Every piece of data the analytical layer touches has been maintained, validated, and scored before it arrives. The LLM never wastes tokens on housekeeping.

### Layer 2: Subconscious — Pattern Maintenance

**What it is:** A separate service running a small language model (7-8B parameters) for tasks that need language understanding but not deep analytical reasoning.

**What it does:** Batch validation of uncertain signals (cross-checking consistency, specificity, and contradiction within signal groups), entity resolution for ambiguous extractions, classification refinement for signals near category boundaries, fact refresh (checking whether existing knowledge still holds against recent evidence), graph consistency checking, source reliability scoring, and — critically — **report differential preparation**. Between each analytical cycle, the subconscious assembles a structured briefing of everything that changed: new signals per situation, event lifecycle transitions, entity anomalies, fact confidence shifts, hypothesis evidence changes. This briefing is what the conscious layer reads when it wakes up.

**Why it matters:** The analytical LLM receives pre-validated, pre-classified, pre-organized context. It doesn't need to figure out what changed — it's told, precisely, in structured form. Every token goes to reasoning, not reconnaissance.

### Layer 3: Conscious — Analytical Reasoning

**What it is:** The main LLM (120B parameters) running structured analytical cycles. This is Legba's focused attention.

**What it does:** Seven cycle types, each with a specific purpose and restricted tool set:

- **SURVEY** — Analytical desk work. Reviews events, builds graph relationships, stress-tests hypotheses against new evidence. The default cycle when nothing else is scheduled.
- **CURATE** — Editorial judgment on raw signal-to-event clustering. Promotes singletons, refines auto-created events, sets severity, links events to situations.
- **ANALYSIS** — Pattern detection. Runs anomaly detection, graph mining, temporal trend analysis. Evaluates hypotheses.
- **RESEARCH** — Entity enrichment. Investigates specific entities via Wikipedia and reference sources. Fills knowledge gaps.
- **SYNTHESIZE** — Deep-dive investigation. Picks a situation, investigates thoroughly, produces a named Situation Brief with thesis, evidence, competing hypotheses, predictions, and unknowns.
- **INTROSPECTION** — Self-assessment. Consolidates journal entries, produces a World Assessment report, reviews mission progress.
- **EVOLVE** — Self-improvement. Audits operational patterns, discovers new sources, produces scorecards.

**Why it matters:** The conscious layer does only what a 120B model is actually good at — connecting dots across large context windows, generating analytical narratives, evaluating competing explanations. It never resolves entity ambiguities, validates classifications, or checks data integrity. Those are handled before it wakes up.

---

## The Data Model: From Noise to Knowledge

Information flows through Legba in layers of increasing analytical value. Each layer has its own storage, its own confidence model, and its own lifecycle.

### Signals → Events → Situations

A **signal** is a raw piece of information from a source — an RSS item, an API response, an alert. Signals are atomic, immutable, and carry composite confidence scores decomposed into explicit components (source reliability, classification confidence, temporal freshness, corroboration, specificity). Every signal carries a provenance chain recording exactly how it was processed.

An **event** is a real-world occurrence derived from clustered signals. When multiple signals report the same thing, the clustering algorithm groups them into an event. Events have a lifecycle (emerging → developing → active → evolving → resolved) that tracks their development over time. They exist as both relational records (for fast queries) and graph nodes (for relationship analysis).

A **situation** is an ongoing analytical theme that spans multiple events. "US-Iran Military Tensions" is a situation. It contains component events, tracked entities, active hypotheses, severity assessments, and running narratives. Situations are the primary organizational unit for intelligence products.

### Facts and Evidence

A **fact** is an assertion the system holds to be true based on accumulated evidence. "Iran is hostile to Israel" is a fact. Every fact carries an explicit evidence set — the specific signals, events, and relationships that support it. When a new fact contradicts an existing one (allied vs hostile, different leaders), the system detects the contradiction and can auto-generate a hypothesis to investigate it.

Facts have temporal validity windows and decay when uncorroborated. The maintenance daemon continuously checks whether facts are still supported by recent evidence, decrementing confidence on stale claims and expiring facts past their validity.

### The Knowledge Graph

Entities (countries, organizations, people, locations, armed groups) and events are nodes in an Apache AGE graph. Their relationships are typed, weighted, and timestamped edges. The 30 canonical relationship types (AlliedWith, HostileTo, LeaderOf, MemberOf, OperatesIn, SuppliesWeaponsTo, etc.) are normalized from 70+ aliases, keeping the schema consistent without constraining the LLM's natural language.

Events in the graph connect to entities via INVOLVED_IN edges (with roles: actor, location, observer, victim), to other events via CAUSED_BY and PART_OF edges, and to situations via TRACKED_BY edges. This means Cypher queries can traverse from "who is involved in events that caused oil price spikes?" to "what situations track events where Iran is an actor?" — questions that would require multiple JOINs across relational tables but are natural graph traversals.

### Hypotheses (Analysis of Competing Hypotheses)

Legba maintains structured hypotheses as first-class objects. Each hypothesis has a thesis, a counter-thesis, diagnostic evidence (what would prove or disprove each), and accumulated supporting and refuting signals. The hypothesis engine doesn't do Bayesian math — it tracks signal counts and lets the LLM make qualitative judgments about which evidence supports which thesis. When contradictory facts are detected, the system auto-generates hypotheses to investigate them.

---

## Confidence: How the System Knows What It Knows

Confidence is not a single number. It's a decomposition into explicit components, each independently assessable and traceable.

**Signal confidence** uses a gatekeeper formula. Source reliability and classification confidence are multiplicative gates — a bad source kills confidence regardless of how specific the claim is. Temporal freshness, corroboration (independent source count), and specificity are additive modifiers within the range set by the gates.

**Event confidence** grows with reinforcement. Auto-created events start at 0.6. Each new signal that clusters into the event increases confidence toward 0.8. Agent-curated events can reach 0.7. Only the human operator can set confidence above 0.8.

**Fact confidence** reflects evidence strength. Facts with multiple independent evidence chains from high-reliability sources score higher than single-source claims. Confidence decays when corroboration fades. Contradicted facts maintain their confidence until the contradiction is resolved — the system doesn't automatically believe the newer claim.

**The calibration loop:** The system tracks whether its confidence scores are actually predictive. When hypotheses are confirmed or refuted, when situations resolve, the claimed confidence at the time is compared against the actual outcome. Systematic over- or under-confidence feeds back into the scoring parameters.

---

## The Cycle Architecture: Rhythm of Thought

Not all work is equally important or equally urgent. Legba uses a three-tier priority routing system to balance scheduled obligations, guaranteed work coverage, and dynamic response to conditions.

**Tier 1 — Scheduled outputs** (fixed intervals): EVOLVE every 30 cycles, INTROSPECTION every 15, SYNTHESIZE every 10. These produce the primary intelligence products and must run on schedule.

**Tier 2 — Guaranteed work** (coprime modulo intervals): ANALYSIS every 4 cycles, RESEARCH every 7, CURATE every 9. The coprime intervals ensure these never collide and every combination of concurrent types eventually occurs.

**Tier 3 — Dynamic fill**: When no Tier 1 or Tier 2 cycle is due, CURATE and SURVEY compete on a score. CURATE scores by uncurated signal backlog; SURVEY scores at a fixed baseline. A cooldown mechanism halves the previous dynamic type's score to prevent repetition. This ensures the system does analytical desk work (SURVEY) when data is clean, and triages new signals (CURATE) when the backlog grows.

Each cycle type sees only the tools relevant to its purpose. SURVEY can't fetch sources. RESEARCH can't write code. CURATE can't run anomaly detection. This prevents drift — a cycle that starts as curation won't end up doing entity enrichment because the tool isn't available.

---

## Safety and Trust

### Structural Isolation

The agent cannot reach the supervisor's code or process. The seed goal is read-only. The audit log (where every prompt, response, and tool call is recorded) lives in a separate OpenSearch instance on a separate network that the agent cannot access. The agent container is ephemeral — created fresh for each cycle, destroyed after.

### Confidence Caps

Auto-created events (from clustering) are capped at 0.6 confidence. Agent-created events are capped at 0.7. Reinforced events cap at 0.8. Only the human operator can exceed 0.8. This prevents the system from bootstrapping its own claims into high-confidence assertions without human validation.

### Adversarial Detection

The system watches for coordinated inauthentic behavior — multiple low-reliability sources publishing semantically similar content about the same entity in a short window, contradicting high-reliability sources. When detected, the signal cluster is flagged as a potential information operation rather than being treated as corroborated intelligence.

### Analytical Audit Trail

Every analytical claim traces to supporting facts, which trace to supporting events, which trace to supporting signals, which trace to original sources. When a customer or operator questions a conclusion, the answer isn't "the AI thinks so" — it's the complete evidence chain with confidence decomposition at every stage.

---

## Deployment Model

The entire platform runs as a Docker Compose project. Sixteen containers: supervisor, ephemeral agent, ingestion service, maintenance daemon, subconscious service, Postgres with Apache AGE (graph), Redis, Qdrant (vectors), OpenSearch (full-text), OpenSearch audit (isolated), NATS (messaging), TimescaleDB (time-series metrics), Grafana (dashboards), Airflow (scheduled pipelines), and two UI containers (operator workstation + Caddy reverse proxy).

The same codebase supports multiple deployment targets — geopolitical situational awareness, privacy and government overreach monitoring, attack surface management cybersecurity — via configuration (seed goal, source portfolio, category rules). The intelligence framework is domain-agnostic; the domain specificity comes from what you tell it to watch and what you seed it with.

---

## What Makes It Different

Most AI intelligence systems are pipelines: data in, analysis out, no memory. Legba is a **mind** — it accumulates knowledge, builds understanding over time, and produces progressively deeper analysis as its knowledge graph grows. A pipeline asks "what happened today?" Legba asks "how does what happened today change what we understood yesterday, and what does that mean for tomorrow?"

The three-layer cognitive architecture is what makes this sustainable. Without it, the analytical LLM burns tokens on data hygiene. With it, every token goes to reasoning. The unconscious layer maintains. The subconscious layer validates and prepares. The conscious layer thinks.

The result is an autonomous intelligence analyst that runs 24/7, processes hundreds of sources, tracks dozens of situations, tests competing hypotheses, and produces named intelligence products — all without human intervention, but with complete auditability at every step.
