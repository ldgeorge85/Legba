<p align="center">
  <img src="logo.png" alt="Legba" width="400">
</p>

<h1 align="center">Legba</h1>
<p align="center"><em>Continuously operating autonomous intelligence platform.</em></p>

Legba is a persistent AI agent that runs indefinitely — ingesting global event data, building a knowledge graph, producing analytical products, and improving itself over time. It is not a chatbot or task runner. It operates autonomously with minimal human intervention.

**Current mission:** Continuous Global Situational Awareness — an always-on intelligence platform that ingests, correlates, and analyzes global events from RSS feeds and APIs, producing structured briefings, detecting patterns, and flagging significant developments.

## Architecture

```
Host VM (Debian 12, 8 vCPU, 16GB RAM)
├── Docker Compose (project: legba, 12 containers)
│   ├── Supervisor        — Agent lifecycle, heartbeat, log drain, audit
│   ├── Agent (ephemeral) — One container per cycle, 6 cycle types, self-modifiable code
│   ├── Operator UI v1    — Web console with CRUD + consultation (FastAPI + htmx, :8501)
│   ├── Operator UI v2    — Multi-panel intelligence workstation (React + Dockview, :8503)
│   ├── Redis             — Transient state, journal, reports
│   ├── Postgres + AGE    — Structured data, entity graph (Cypher)
│   ├── Qdrant            — Semantic search (episodic memory)
│   ├── NATS              — Event bus, messaging
│   ├── OpenSearch x2     — Full-text search + isolated audit logs
│   └── Airflow           — Scheduled pipelines
└── External LLM: GPT-OSS 120B via vLLM or Claude Sonnet via Anthropic API
```

## Agent Cycle

Every cycle (~2-10 minutes), the agent runs one of **6 cycle types** (selected by priority):

```
WAKE → ORIENT → [cycle type routing] → REFLECT → NARRATE → PERSIST

Cycle types (priority order):
  Every 30 cycles: EVOLVE        — self-improvement, prompt/tool evaluation, operational scorecard
  Every 15 cycles: INTROSPECTION — deep audit, journal consolidation, world assessment
  Every 10 cycles: ANALYSIS      — pattern detection, graph mining, anomaly detection
  Every 5 cycles:  RESEARCH      — entity enrichment via Wikipedia/reference sources
  Every 3 cycles:  ACQUIRE       — dedicated source fetching, event ingestion
  Otherwise:       NORMAL        — goal-directed PLAN → REASON+ACT
```

- **WAKE**: Load config, connect services, register 57 tools, drain inbox
- **ORIENT**: Retrieve memories, goals, graph inventory, source health, ingestion gap tracking, journal leads
- **PLAN** (normal cycles): LLM selects focus and approach, outputs expected tool list
- **REASON+ACT**: Tool loop (up to 20 steps) — LLM reasons, calls tools, feeds results back
- **REFLECT**: LLM evaluates cycle significance, facts learned, goal progress
- **NARRATE**: LLM writes journal entries + extracts investigation leads for next cycle
- **PERSIST**: Store episode, track ingestion, auto-complete goals, promote memories, heartbeat, exit

Each specialized cycle type uses a **filtered tool set** — only tools relevant to that cycle's purpose are available, preventing the agent from drifting into unrelated work.

## Quick Start

```bash
# Build and launch
docker compose -p legba build
docker compose -p legba up -d

# Monitor
docker compose -p legba logs supervisor -f

# Web UI v2 — multi-panel intelligence workstation (via SSH tunnel)
ssh -L 8503:localhost:8503 user@<host>
# Then open http://localhost:8503
# Interactive graph, geospatial map, timeline, live feed, AI consult

# Web UI v1 — legacy (via SSH tunnel)
ssh -L 8501:localhost:8501 user@<host>
# Then open http://localhost:8501

# Send a message to the agent
docker compose -p legba exec supervisor \
  python -m legba.supervisor.cli --shared /shared send "Focus on Middle East coverage"

# Read agent responses
docker compose -p legba exec supervisor \
  python -m legba.supervisor.cli --shared /shared read
```

**Important:** Always use `-p legba` for correct network naming.

## Key Numbers

| Metric | Value |
|--------|-------|
| Python source files | 100+ |
| Tests | 237 |
| Built-in tools | 57 across 17 modules |
| Platform services | 7 (Redis, Postgres/AGE, Qdrant, NATS, OpenSearch x2, Airflow) |
| Canonical relationship types | 30 |
| LLM context window | 128k tokens (120k budget) |
| Memory layers | 6 (registers, short-term episodic, long-term episodic, structured, graph, bulk) |

## Configuration

Create `.env` in the project root:

```bash
# LLM Provider: "vllm" (default) or "anthropic"
LLM_PROVIDER=vllm

# For vLLM (GPT-OSS 120B, endpoint name: InnoGPT-1):
OPENAI_API_KEY=your-key-here
OPENAI_BASE_URL=https://your-llm-api/v1
OPENAI_MODEL=InnoGPT-1
LLM_TEMPERATURE=1.0

# For Anthropic/Claude (copy .env.claude.example to .env.claude):
# LLM_PROVIDER=anthropic
# OPENAI_API_KEY=sk-ant-...
# OPENAI_MODEL=claude-sonnet-4-20250514
# LLM_TEMPERATURE=0.7
# EMBEDDING_API_BASE=https://your-vllm-api/v1  # Anthropic has no embedding API
# EMBEDDING_API_KEY=your-vllm-key
```

See [LEGBA.md](docs/LEGBA.md) section 9 for all configuration options.

### Running a Claude Instance (Parallel)

```bash
cp .env.claude.example .env.claude  # Edit with your Anthropic API key
docker compose -p legba-claude -f docker-compose.claude.yml build
docker compose -p legba-claude -f docker-compose.claude.yml up -d
# UI at localhost:8502 (via SSH tunnel)
```

## Deploying Code Changes

The agent's code lives in a Docker volume (self-modifiable). To deploy changes:

```bash
docker compose -p legba stop supervisor
docker run --rm -v legba_agent_code:/agent alpine rm -rf /agent/src /agent/pyproject.toml
docker compose -p legba build agent
docker compose -p legba up -d supervisor
```

## Documentation

| Document | Description |
|----------|-------------|
| [LEGBA.md](docs/LEGBA.md) | Full platform reference — architecture, prompts, memory, tools, config |
| [UI_V2.md](docs/UI_V2.md) | UI v2 operator console — panels, cross-panel interactions, API, deployment |
| [DESIGN.md](docs/DESIGN.md) | Implementation design — decisions, data flows, component interactions |
| [CODE_MAP.md](docs/CODE_MAP.md) | Complete code map — every file, function flows, dependencies |
| [OPERATIONS.md](docs/OPERATIONS.md) | Ops runbook — deployment, resets, monitoring, debugging, backups |
| [PROMPT_DUMP.md](docs/PROMPT_DUMP.md) | Full assembled prompts for each cycle phase |
| [PROMPT_GUIDE.md](docs/PROMPT_GUIDE.md) | Prompt engineering notes |
| [PLAN_V2.md](docs/PLAN_V2.md) | V2 architecture plan — cycle types, data pipeline, intelligence capabilities |
| [GRACEFUL_SHUTDOWN.md](docs/GRACEFUL_SHUTDOWN.md) | Shutdown protocol details |
| [PROGRESS_AUDIT.md](docs/PROGRESS_AUDIT.md) | Review of 586 autonomous cycles — tool usage evolution, operational metrics, journal analysis |
| [EXECUTIVE_SUMMARY.md](docs/EXECUTIVE_SUMMARY.md) | High-level overview of the platform |

## Testing

```bash
# Full test suite
docker compose -p legba --profile test run --rm test

# Unit tests only (no services needed)
docker compose -p legba --profile test run --rm --no-deps test python -m pytest tests/test_unit.py -v
```

## Technology Stack

Python 3.12 (async) | Docker Compose | GPT-OSS 120B via vLLM | PostgreSQL 18 + Apache AGE | Qdrant | OpenSearch 2.x | NATS + JetStream | Apache Airflow | FastAPI + htmx | React 18 + Vite + TypeScript | Sigma.js + Graphology | MapLibre GL JS | Dockview | TanStack Query | Zustand | Pydantic v2 | PyOD | spaCy | NetworkX | feedparser | pycountry

## Contact

Want to talk shop? Reach out at legba@civislux.us.
