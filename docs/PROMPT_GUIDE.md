# Legba Prompt Customization Guide

Quick reference for where and how to customize every prompt injection point in the agent.

For detailed phase-by-phase assembly documentation, see [PROMPT_REFERENCE.md](PROMPT_REFERENCE.md).

---

## Prompt Injection Points

| Prompt Section | File:Variable | When Injected | Purpose | Customization Notes |
|---------------|---------------|--------------|---------|---------------------|
| System prompt (identity + rules) | `templates.py:SYSTEM_PROMPT` | Every LLM call | Core identity, behavioral rules, cycle metadata | Edit section 1 for identity, section 4 for behaviors, section 5 for purpose |
| Reasoning level | `templates.py:SYSTEM_PROMPT` line 1 | Every call | `Reasoning: high` header | Controls CoT depth. Valid options: `high`, `medium`, `low` only. Invalid values (e.g. `off`, `none`) are ignored and default to `high`. |
| Tool definitions | `format.py:format_tool_definitions()` | Every call with tools | TypeScript namespace listing of available tools | Auto-generated from tool registry. Edit tool descriptions in their handler modules |
| Tool calling instructions | `templates.py:TOOL_CALLING_INSTRUCTIONS` | Every call with tools | JSON tool call format and examples | Update if call format changes. Currently: `{"actions": [{"tool": "name", "args": {...}}]}` |
| Goal context | `templates.py:GOAL_CONTEXT_TEMPLATE` | ORIENT phase | Seed goal + active goals list | Edit framing text, not goal content (goals are dynamic) |
| Plan prompt | `templates.py:PLAN_PROMPT` | PLAN phase | Cycle planning guidance | Edit for mission-specific planning criteria |
| Reflect prompt | `templates.py:REFLECT_PROMPT` | REFLECT phase | Cycle evaluation + JSON schema | Edit JSON schema if adding new reflection fields |
| Mission review | `templates.py:MISSION_REVIEW_PROMPT` | Every 15 cycles | Strategic goal tree review | Edit evaluation criteria and JSON output schema |
| Memory guidance | `templates.py:MEMORY_MANAGEMENT_GUIDANCE` | System prompt addon | Memory tool usage rules, anti-patterns | Edit to change memory behaviors |
| Efficiency guidance | `templates.py:EFFICIENCY_GUIDANCE` | System prompt addon | Work pace, dedup, sub-agent usage | Edit to change work style |
| Analytics guidance | `templates.py:ANALYTICS_GUIDANCE` | System prompt addon | Reference table for analytical tools | Update when adding new analytical tools |
| Orchestration guidance | `templates.py:ORCHESTRATION_GUIDANCE` | System prompt addon | Airflow workflow tools reference | Update when adding workflow features |
| SA guidance | `templates.py:SA_GUIDANCE` | System prompt addon | Source management, event pipeline, HTTP behavior | Mission-specific. Edit for different intelligence domains |
| Entity guidance | `templates.py:ENTITY_GUIDANCE` | System prompt addon | Entity profiles, resolution, temporal relationships | Mission-specific. Edit relationship types for different domains |
| Re-grounding | `templates.py:REGROUND_PROMPT` | Every 8 tool steps | Working memory checkpoint during long loops | Rarely needs editing. Interval: `client.py:REGROUND_INTERVAL` |
| Budget exhausted | `templates.py:BUDGET_EXHAUSTED_PROMPT` | Step limit hit | Forces final response with state save | Rarely needs editing |
| Bootstrap addon | `templates.py:BOOTSTRAP_PROMPT_ADDON` | Cycles 1-5 | Early orientation checklist | Edit for different onboarding sequences |
| Reporting reminder | `templates.py:REPORTING_REMINDER` | Reporting cycles | Status report format and example | Edit report structure. Interval: `assembler.py` constructor |
| Liveness check | `templates.py:LIVENESS_PROMPT` | PERSIST phase | Nonce challenge for liveness verification | Do not modify — tied to supervisor protocol |
| Subagent system | `subagent.py:SUBAGENT_SYSTEM_PROMPT` | Sub-agent calls | Sub-agent identity + rules | Edit for delegation style |
| Seed goal | `seed_goal/goal.txt` | WAKE phase | Primary mission text | **Main mission customization point** |
| Identity primer | `seed_goal/identity.txt` | WAKE phase | Self-concept anchor | **Identity customization point** |
| Operating principles | `seed_goal/operating_principles.txt` | WAKE phase | Analytical tradecraft principles | Edit for domain-specific tradecraft |
| Prompt assembly | `assembler.py:PromptAssembler` | All phases | Combines all above + memories + goals | Edit to change injection order or add new sections |

---

## LLM Communication Format

| Layer | File | Format |
|-------|------|--------|
| Provider | `llm/provider.py` | `/v1/chat/completions` with single `{"role": "user"}` message. Temp 1.0, no max_tokens, no stop tokens. |
| Message format | `llm/format.py` | `Message(role, content)` objects combined into single user message by `to_chat_messages()` |
| Tool calls (output) | `llm/tool_parser.py` | `{"actions": [{"tool": "name", "args": {...}}]}` — single JSON object parsed from LLM text |
| Tool results (input) | `llm/format.py` | `[Tool Result: tool_name]\n{result}` as user messages |
| Response cleaning | `llm/format.py` | `strip_harmony_response()` removes Harmony markers from model output |

---

## Common Customization Scenarios

### Change the agent's identity
Edit `templates.py:SYSTEM_PROMPT` section 1 ("WHO YOU ARE") and `seed_goal/identity.txt`.

### Change the mission
Edit `seed_goal/goal.txt`. This is loaded during WAKE and injected via `GOAL_CONTEXT_TEMPLATE`.

### Add a new tool
1. Create handler in `src/legba/agent/tools/builtins/`
2. Register in `cycle.py:_register_builtin_tools()`
3. Tool definition auto-appears in LLM context via `format_tool_definitions()`

### Change tool call format
1. Update `templates.py:TOOL_CALLING_INSTRUCTIONS` with new format and examples
2. Update `llm/tool_parser.py` to parse the new format
3. Update examples in `REPORTING_REMINDER`

### Add a new system prompt section
1. Add the template string in `templates.py`
2. Append it in `assembler.py:_build_system_text()`

### Change reporting frequency
Modify `report_interval` parameter in `cycle.py` where `PromptAssembler` is constructed.

### Change context budget
Modify `max_context_tokens` parameter in `cycle.py` where `PromptAssembler` is constructed. Default: 120000 (of 128k window).
