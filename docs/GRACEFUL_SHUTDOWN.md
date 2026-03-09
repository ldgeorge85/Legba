# Graceful Shutdown: Soft Timeout with Agent Ping

**Status:** Implemented
**Priority:** High — cycles timeout and lose all REFLECT/PERSIST work

## Problem

The supervisor enforces a hard timeout (currently 600s). When the agent is actively
doing useful work (e.g., cycle 29 did 17 reasoning steps with tool calls), it gets
killed before reaching REFLECT and PERSIST. This meant:

- No reflection data saved (cycle learnings lost)
- No heartbeat/nonce response (supervisor sees it as a failure)
- Memory updates from tool calls never persisted
- Consecutive heartbeat failures accumulate toward the kill threshold

This problem motivated the graceful shutdown feature described below.

## Solution: Soft Timeout → Stop Flag → Ping → Graceful Exit

```
Timeline:
  0s          Supervisor launches agent container
  ...         Agent runs PLAN → REASON+ACT (tool loop)
  600s        SOFT TIMEOUT: supervisor writes /shared/stop_flag.json
  600-660s    Agent sees flag between steps, writes /shared/stop_ping.json
              Agent breaks out of tool loop
  660s        Supervisor sees ping, extends timeout by 50% (300s)
              Agent runs REFLECT → PERSIST (includes nonce liveness check)
  ~700s       Agent exits cleanly (exit 0), supervisor validates heartbeat
```

Failure modes:
- No ping within 150s → hard kill (agent is stuck/crashed)
- Max 1-2 extensions → hard kill (prevent infinite extension)
- Agent exits non-zero → normal failure handling

## Implementation Details

### Signal Mechanism: File-Based (via /shared volume)

Both supervisor and agent share the `/shared` volume (used for
`challenge.json` and `outbox.json`). Two signal files:

| File | Writer | Reader | Content |
|------|--------|--------|---------|
| `/shared/stop_flag.json` | Supervisor | Agent | `{"timestamp": "...", "reason": "timeout"}` |
| `/shared/stop_ping.json` | Agent | Supervisor | `{"timestamp": "...", "cycle": N}` |

Files are cleaned up at the start of each cycle by both the supervisor
(`lifecycle.cleanup_signals()` called in `supervisor/main.py` line 148) and the agent
(`AgentCycle._cleanup_signals()` called during the WAKE phase in `agent/cycle.py` line 114).

**Why files, not Docker signals?** Files are simpler, debuggable (`ls /shared/`),
and avoid async Python signal handler pitfalls. The shared volume is already wired up.

### Supervisor side: `supervisor/lifecycle.py`

The `LifecycleManager` class contains all supervisor-side graceful shutdown logic.

**Constants** (lines 26-32):
- `PING_WAIT_SECONDS = 150` — how long to wait for the agent to acknowledge the stop flag (increased from 60s after observing LLM inference times up to 116s on a single step)
- `EXTENSION_FACTOR = 0.5` — after a ping, extend timeout by this fraction of the original
- `MAX_EXTENSIONS = 2` — maximum number of timeout extensions before hard kill

**`_monitor_with_graceful_shutdown()`** (lines 134-278): The core monitoring loop
that replaces a simple "wait or kill" with a state machine. When the soft deadline
is reached, it writes the stop flag and enters a ping-wait loop. If the agent pings
back, it extends the deadline. If no ping arrives within `PING_WAIT_SECONDS` (150s), or
if `MAX_EXTENSIONS` is exhausted, the agent is hard-killed.

**`_write_stop_flag()`** (lines 280-288): Writes `stop_flag.json` with a UTC
timestamp and reason `"timeout"`.

**`_check_ping()`** (lines 291-293): Checks for existence of `stop_ping.json`.

**`cleanup_signals()`** (lines 306-314): Static method that removes stale signal
files. Called by the supervisor at the start of each cycle in `main.py` line 148.

The `CycleResult` dataclass (lines 38-48) includes a `graceful_shutdown` boolean
that is set to `True` when the agent exits cleanly after a stop flag exchange.

### Agent LLM client: `agent/llm/client.py`

**`reason_with_tools()`** (lines 168-293): Accepts an optional `stop_check`
parameter — a `Callable[[], bool]` that is checked before each reasoning step
(line 203-209). When it returns `True`, a note is added to working memory
("Graceful shutdown: supervisor requested wrap-up. Exiting tool loop to proceed
to REFLECT and PERSIST.") and the tool loop breaks, allowing the cycle to
proceed to REFLECT and PERSIST.

The check runs between steps, so it never interrupts LLM inference. The cost is a
single `os.path.exists()` stat call per step — microseconds.

**Latency note:** If the LLM is mid-inference when the flag is written, the agent
won't see it until after that inference completes. Observed inference times reach
up to ~120s in production (a single reasoning step took 116s). The 150s ping
window accounts for this worst-case latency.

### Agent cycle: `agent/cycle.py`

The `AgentCycle` class contains all agent-side graceful shutdown logic
(lines 311-348):

**`_check_stop_flag()`** (lines 313-315): Returns `True` if
`/shared/stop_flag.json` exists.

**`_send_ping()`** (lines 317-325): Writes `stop_ping.json` with a UTC timestamp
and the current cycle number so the supervisor extends the timeout.

**`_make_stop_checker()`** (lines 327-341): Returns a closure that checks for the
stop flag and pings back exactly once. On first detection, it calls `_send_ping()`,
logs a `graceful_shutdown` event, and returns `True`. Subsequent calls also return
`True` (the flag is still present) but skip the duplicate ping.

**`_cleanup_signals()`** (lines 343-348): Removes stale signal files at the start
of the WAKE phase (line 114).

The stop checker is wired into the REASON+ACT phase at line 385:
```python
stop_check=self._make_stop_checker(),
```

### Supervisor orchestration: `supervisor/main.py`

**`_run_cycle()`** (line 148): Calls `self.lifecycle.cleanup_signals(self.config.paths.shared)`
to remove stale signal files before issuing the challenge.

**`run_cycle_docker()`** call (lines 199-212): Passes `shared_path=self.config.paths.shared`
so the lifecycle manager knows where to write/read signal files.

**`_log_cycle_result()`** (lines 273-301): Logs a distinct message when
`result.graceful_shutdown` is `True`: "Agent completed (graceful shutdown)".

## Interaction with Existing Mechanisms

| Mechanism | Trigger | What happens |
|-----------|---------|-------------|
| **Step budget** (`max_steps=30`) | Tool loop exceeds N steps | `BUDGET_EXHAUSTED_PROMPT` → forced final → REFLECT → PERSIST (already works) |
| **Soft timeout** | Wall clock exceeds Ns | Stop flag → agent breaks loop → REFLECT → PERSIST |
| **Hard timeout** | No ping after stop flag, or max extensions | Container killed, heartbeat fails |
| **Heartbeat/nonce** | PERSIST phase | Dedicated LLM call, validates agent liveness |

The soft timeout and step budget are complementary:
- Step budget handles "too many fast steps"
- Soft timeout handles "steps are slow" (long LLM inference)

## Verified Behavior

- `stop_check` callback in `reason_with_tools` breaks the tool loop early when the flag is detected
- `_check_stop_flag` / `_send_ping` / `_cleanup_signals` file operations are correct
- Supervisor writes the flag, agent detects it between steps and pings back, supervisor extends the timeout
- After the tool loop breaks, the cycle proceeds normally through REFLECT and PERSIST
- `CycleResult.graceful_shutdown` is set appropriately and logged by the supervisor

## Risks and Mitigations

- **Race condition**: Flag written during LLM inference → up to ~120s delay before agent sees it (observed 116s worst case). Mitigated by 150s ping window.
- **Stale files**: Old stop_flag.json from a crashed cycle could trigger immediate shutdown on next cycle. Mitigated by cleanup at cycle start (both supervisor and agent sides).
- **Shared volume latency**: Docker volume writes should be near-instant (same host), but NFS mounts could add latency. Current setup uses local volumes, so not a concern.
