# Phase: Project Rebirth

**Status**: In Progress
**Started**: 2026-03-03

## Overview

Platform rename, identity rework, LLM connector simplification, and documentation refresh.

## Task Tracking Matrix

### Phase 0: Planning & Memory
| # | Task | Status | Notes |
|---|------|--------|-------|
| 0.1 | Create this planning doc | DONE | |
| 0.2 | Update MEMORY.md | DONE | |

### Phase 1: Cleanup & Git Init
| # | Task | Status | Notes |
|---|------|--------|-------|
| 1.1 | Delete temp/generated files | DONE | cycle transcripts, feedback docs, archive/, __pycache__, bot-info.txt, EXECUTIVE_SUMMARY.md |
| 1.2 | Create .gitignore | DONE | |
| 1.3 | git init + initial commit | DONE | |

### Phase 2: Rename Skynet → Legba
| # | Task | Status | Notes |
|---|------|--------|-------|
| 2.1 | Python package rename (src/skynet → src/legba) | DONE | |
| 2.2 | All Python imports | DONE | 60+ files |
| 2.3 | pyproject.toml | DONE | |
| 2.4 | Docker Compose (volumes, env vars, services) | DONE | |
| 2.5 | Dockerfiles + entrypoints | DONE | |
| 2.6 | Redis key prefix | DONE | skynet: → legba: |
| 2.7 | NATS subjects/streams | DONE | skynet.* → legba.* |
| 2.8 | Qdrant collections | DONE | skynet_* → legba_* |
| 2.9 | OpenSearch indices | DONE | skynet-events-* → legba-events-* |
| 2.10 | Postgres DB/user/pass | DONE | |
| 2.11 | AGE graph name | DONE | skynet_graph → legba_graph |
| 2.12 | UI templates | DONE | |
| 2.13 | Documentation files rename + content | DONE | |
| 2.14 | Config class names (LegbaConfig etc.) | DONE | |

### Phase 3: Legba Identity & Soul
| # | Task | Status | Notes |
|---|------|--------|-------|
| 3.1 | System prompt identity section (§1) | DONE | |
| 3.2 | Purpose section (§5) | DONE | |
| 3.3 | seed_goal/goal.txt | DONE | |
| 3.4 | seed_goal/identity.txt | DONE | |
| 3.5 | seed_goal/operating_principles.txt | DONE | |
| 3.6 | docs/bot-info.txt | DONE | |
| 3.7 | User-Agent string | DONE | Legba-SA/1.0 |

### Phase 4: LLM Connector Simplification
| # | Task | Status | Notes |
|---|------|--------|-------|
| 4.1 | provider.py: add chat_complete() | DONE | Initially used /v1/chat/completions; switched to /v1/completions (see 4.10-4.12) |
| 4.2 | harmony.py → format.py | DONE | Chat message builder |
| 4.3 | tool_parser.py: JSON extraction | DONE | Balanced brace extraction for nested args |
| 4.4 | client.py: use chat_complete | DONE | |
| 4.5 | templates.py: update TOOL_CALLING_INSTRUCTIONS | DONE | |
| 4.6 | assembler.py: return message dicts | DONE | |
| 4.7 | cycle.py: update for new format | DONE | |
| 4.8 | subagent.py: update for new format | DONE | |
| 4.9 | Keep harmony_legacy.py as rollback | DONE | |
| 4.10 | Fix tool_parser.py regex (nested braces) | DONE | `[^{}]*` couldn't match `"args": {...}` — switched to `_extract_balanced_braces()` |
| 4.11 | Switch provider from /chat/completions to /completions | DONE | chat/completions triggers InnoGPT-1 reasoning parser bug: "Expected 2 output messages, but got N" on multi-turn. Raw completions avoids this. |
| 4.12 | Add retry logic + error body capture | DONE | Retries 500/502/503/429 with exponential backoff; captures API error body for diagnostics |
| 4.13 | Strip reasoning prefix from completions output | DONE | Model outputs `assistantanalysis`/`assistantfinal` markers; stripped before returning content |

### Phase 5: Prompt Structure Guide
| # | Task | Status | Notes |
|---|------|--------|-------|
| 5.1 | Create docs/PROMPT_GUIDE.md | DONE | |
| 5.2 | Update docs/PROMPT_REFERENCE.md | DONE | |

### Phase 6: Verification
| # | Task | Status | Notes |
|---|------|--------|-------|
| 6.1 | grep -r "skynet" src/ — zero hits | DONE | |
| 6.2 | grep -r "Skynet" docs/ — zero hits (except historical) | DONE | |
| 6.3 | Python syntax check (all .py files) | DONE | Fixed 2 sed-corruption bugs in opensearch files |

### Phase 7: Tear Down, Build, Deploy
| # | Task | Status | Notes |
|---|------|--------|-------|
| 7.1 | docker compose down (stop old stack) | DONE | Used `docker compose -p legba` for correct network/container naming |
| 7.2 | Back up + remove old volumes | DONE | 12 volumes backed up to `backups/skynet_volumes_2026-03-03.tar.gz` (76MB), then removed |
| 7.3 | docker compose build | DONE | Rebuilt supervisor, ui, agent images |
| 7.4 | docker compose up (infra) | DONE | postgres, redis, qdrant, nats, opensearch all healthy |
| 7.5 | docker compose up supervisor | DONE | Agent launches successfully |
| 7.6 | Verify UI at :8501 shows "Legba" branding | DONE | |
| 7.7 | Verify agent cycle completes with JSON tool calls | DONE | Cycle 23+ completing with 11-15 actions per cycle ||

## Gaps & Notes

- Data migration not needed — fresh start was planned for SA pivot
- Old Docker volumes backed up to `backups/` then removed as part of 7.2
- Harmony token format kept as harmony_legacy.py for rollback safety
- **Agent code volume gotcha**: The agent entrypoint seeds `/agent/src` from the Docker image on first boot, then reuses the volume on subsequent boots via `PYTHONPATH=/agent/src`. Code changes require clearing the volume (`rm -rf /agent/src`) before restarting so the entrypoint re-seeds from the updated image.
- **InnoGPT-1 reasoning mode**: The model internally produces reasoning + content. `/v1/chat/completions` enforces a "2 output messages" constraint that breaks on multi-turn conversations (N assistant messages → N output pairs → server rejects with 400). Solved by using `/v1/completions` with `<|start|>role<|message|>content<|end|>` prompt format and stripping `assistantanalysis`/`assistantfinal` markers from output.
- **Deploy procedure**: `docker compose -p legba` required for correct network naming (`legba_default`). Supervisor launches agent via `docker run --network legba_default`.
