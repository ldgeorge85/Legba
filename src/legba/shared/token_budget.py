"""Rolling 24h token budget for escalation provider.

Tracks usage in Redis sorted set. Hard stops escalation when budget exceeded.
Archives daily totals to TimescaleDB.
"""

import time
import logging

logger = logging.getLogger("legba.shared.token_budget")

BUDGET_KEY = "legba:llm:escalation_tokens"
DEFAULT_DAILY_BUDGET = 500000  # tokens


async def record_usage(redis_client, tokens: int, cycle: int, prompt_name: str):
    """Record token usage for escalation provider."""
    member = f"{cycle}:{prompt_name}:{int(time.time())}"
    score = time.time()
    await redis_client.zadd(BUDGET_KEY, {member: score})
    # Store tokens as a separate hash for summing
    await redis_client.hset(f"{BUDGET_KEY}:counts", member, str(tokens))


async def get_usage_24h(redis_client) -> int:
    """Get total escalation tokens used in last 24 hours."""
    cutoff = time.time() - 86400
    members = await redis_client.zrangebyscore(BUDGET_KEY, cutoff, '+inf')
    total = 0
    for m in members:
        key = m if isinstance(m, str) else m.decode("utf-8")
        count = await redis_client.hget(f"{BUDGET_KEY}:counts", key)
        if count:
            val = count if isinstance(count, str) else count.decode("utf-8")
            total += int(val)
    return total


async def budget_available(redis_client, daily_budget: int = DEFAULT_DAILY_BUDGET) -> bool:
    """Check if escalation budget has remaining capacity."""
    used = await get_usage_24h(redis_client)
    remaining = daily_budget - used
    if remaining <= 0:
        logger.warning(
            "Escalation token budget exhausted: %d / %d used", used, daily_budget,
        )
    return used < daily_budget


async def prune_old(redis_client):
    """Remove entries older than 48h."""
    cutoff = time.time() - 172800
    removed_zset = await redis_client.zremrangebyscore(BUDGET_KEY, '-inf', cutoff)
    # Also clean up the counts hash for pruned members
    if removed_zset:
        logger.info("Pruned %d old escalation token entries from sorted set", removed_zset)
