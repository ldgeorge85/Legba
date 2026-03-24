"""Portfolio view builder for EVOLVE cycle context.

Builds a structured summary of the current analytical portfolio:
goals, situations, hypotheses, watchlists, predictions, coverage gaps,
and task backlog. Injected into the EVOLVE prompt to give the agent
a comprehensive view of the system's analytical posture.

No side effects — read-only queries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from .task_backlog import BACKLOG_KEY

logger = logging.getLogger("legba.shared.portfolio")


async def build_portfolio_view(pool: asyncpg.Pool, redis_client) -> str:
    """Build a structured portfolio summary for EVOLVE cycle context.

    Returns formatted markdown text showing:
    - Active goals (standing + investigative) with progress
    - Situation coverage map (by region/domain)
    - Hypothesis health (evidence accumulation rates)
    - Watchlist effectiveness (trigger rates)
    - Prediction track record
    - Coverage gaps (high-activity regions/domains with no goal)
    - Task backlog summary
    """
    sections: list[str] = []
    sections.append("# Portfolio Overview\n")

    try:
        async with pool.acquire() as conn:
            # ---------------------------------------------------------------
            # 1. Active Goals
            # ---------------------------------------------------------------
            goals_section = await _build_goals_section(conn)
            if goals_section:
                sections.append(goals_section)

            # ---------------------------------------------------------------
            # 2. Situation Coverage Map
            # ---------------------------------------------------------------
            situations_section = await _build_situations_section(conn)
            if situations_section:
                sections.append(situations_section)

            # ---------------------------------------------------------------
            # 3. Hypothesis Health
            # ---------------------------------------------------------------
            hypotheses_section = await _build_hypotheses_section(conn)
            if hypotheses_section:
                sections.append(hypotheses_section)

            # ---------------------------------------------------------------
            # 4. Watchlist Effectiveness
            # ---------------------------------------------------------------
            watchlist_section = await _build_watchlist_section(conn)
            if watchlist_section:
                sections.append(watchlist_section)

            # ---------------------------------------------------------------
            # 5. Prediction Track Record
            # ---------------------------------------------------------------
            predictions_section = await _build_predictions_section(conn)
            if predictions_section:
                sections.append(predictions_section)

            # ---------------------------------------------------------------
            # 6. Coverage Gaps
            # ---------------------------------------------------------------
            gaps_section = await _build_coverage_gaps_section(conn)
            if gaps_section:
                sections.append(gaps_section)

    except Exception as e:
        sections.append(f"\n(Portfolio query error: {e})\n")

    # ---------------------------------------------------------------
    # 7. Task Backlog Summary (from Redis)
    # ---------------------------------------------------------------
    backlog_section = await _build_backlog_section(redis_client)
    if backlog_section:
        sections.append(backlog_section)

    return "\n".join(sections)


# -----------------------------------------------------------------------
# Section builders
# -----------------------------------------------------------------------

async def _build_goals_section(conn: asyncpg.Connection) -> str:
    """Build the active goals section with standing/investigative split."""
    lines = ["## Active Goals\n"]

    try:
        rows = await conn.fetch("""
            SELECT id, data, goal_type, priority, status, created_at
            FROM goals
            WHERE status IN ('active', 'paused', 'blocked')
            ORDER BY priority ASC, created_at ASC
            LIMIT 30
        """)

        if not rows:
            lines.append("No active goals.\n")
            return "\n".join(lines)

        standing = []
        investigative = []

        for row in rows:
            raw = row["data"]
            data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
            ctx = data.get("context") or {}
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except (json.JSONDecodeError, TypeError):
                    ctx = {}
            elif not isinstance(ctx, dict):
                ctx = {}

            goal_info = {
                "id": str(row["id"])[:8],
                "description": (data.get("description") or "?")[:100],
                "priority": row["priority"] or 5,
                "progress": data.get("progress_pct") or 0.0,
                "goal_type": row["goal_type"] or "goal",
                "status": row["status"] or "active",
                "goal_purpose": data.get("goal_purpose", ctx.get("goal_purpose", "standing")),
                "linked_situation": data.get("linked_situation_id", ctx.get("linked_situation_id")),
                "linked_hypothesis": data.get("linked_hypothesis_id", ctx.get("linked_hypothesis_id")),
            }

            # Stale marker
            stale_marker = ""
            last_progress_str = data.get("last_progress_at")
            if last_progress_str:
                try:
                    lp = datetime.fromisoformat(str(last_progress_str))
                    if lp.tzinfo is None:
                        lp = lp.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - lp
                    if age.days > 7:
                        stale_marker = f" **[STALE {age.days}d]**"
                except (ValueError, TypeError):
                    pass
            elif row["created_at"]:
                ca = row["created_at"]
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - ca
                if age.days > 7:
                    stale_marker = f" **[STALE {age.days}d]**"

            goal_info["stale_marker"] = stale_marker

            if goal_info["goal_purpose"] == "investigative" or goal_info["linked_situation"]:
                investigative.append(goal_info)
            else:
                standing.append(goal_info)

        if standing:
            lines.append("### Standing Goals")
            for g in standing:
                lines.append(
                    f"- [{g['id']}] p={g['priority']} {g['status']} "
                    f"{g['progress']:.0f}% — {g['description']}{g['stale_marker']}"
                )

        if investigative:
            lines.append("\n### Investigative Goals")
            for g in investigative:
                refs = []
                if g.get("linked_situation"):
                    refs.append(f"sit={str(g['linked_situation'])[:8]}")
                if g.get("linked_hypothesis"):
                    refs.append(f"hyp={str(g['linked_hypothesis'])[:8]}")
                ref_str = f" ({', '.join(refs)})" if refs else ""
                lines.append(
                    f"- [{g['id']}] p={g['priority']} {g['status']} "
                    f"{g['progress']:.0f}%{ref_str} — {g['description']}{g['stale_marker']}"
                )

        lines.append("")

    except Exception as e:
        lines.append(f"(Goal query failed: {e})\n")

    return "\n".join(lines)


async def _build_situations_section(conn: asyncpg.Connection) -> str:
    """Build situation coverage map by region/domain."""
    lines = ["## Situation Coverage Map\n"]

    try:
        rows = await conn.fetch("""
            SELECT s.id, s.name, s.status, s.event_count, s.category,
                   s.intensity_score, s.data
            FROM situations s
            WHERE s.status IN ('active', 'escalating', 'proposed')
            ORDER BY s.intensity_score DESC
            LIMIT 20
        """)

        if not rows:
            lines.append("No active situations.\n")
            return "\n".join(lines)

        # Group by category/region
        by_region: dict[str, list] = {}
        for row in rows:
            raw = row["data"]
            data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
            regions = data.get("regions") or ["unspecified"]
            category = row["category"] or "general"

            for region in regions[:2]:
                key = f"{region}/{category}"
                entry = {
                    "name": row["name"][:60],
                    "status": row["status"],
                    "events": row["event_count"] or 0,
                    "intensity": row["intensity_score"] or 0.0,
                }
                by_region.setdefault(key, []).append(entry)

        for key in sorted(by_region.keys()):
            entries = by_region[key]
            lines.append(f"**{key}** ({len(entries)} situation{'s' if len(entries) != 1 else ''}):")
            for e in entries:
                lines.append(
                    f"  - {e['name']} [{e['status']}] {e['events']} events, "
                    f"intensity={e['intensity']:.2f}"
                )

        lines.append("")

    except Exception as e:
        lines.append(f"(Situation query failed: {e})\n")

    return "\n".join(lines)


async def _build_hypotheses_section(conn: asyncpg.Connection) -> str:
    """Build hypothesis health summary with evidence accumulation indicators."""
    lines = ["## Hypothesis Health\n"]

    try:
        rows = await conn.fetch("""
            SELECT h.id, h.thesis, h.evidence_balance, h.status,
                   h.last_evaluated_cycle,
                   array_length(h.supporting_signals, 1) as support_count,
                   array_length(h.refuting_signals, 1) as refute_count,
                   s.name as situation_name
            FROM hypotheses h
            LEFT JOIN situations s ON s.id = h.situation_id
            WHERE h.status = 'active'
            ORDER BY abs(h.evidence_balance) DESC
            LIMIT 15
        """)

        if not rows:
            lines.append("No active hypotheses.\n")
            return "\n".join(lines)

        # Summary stats
        total_active = len(rows)
        no_evidence = sum(
            1 for h in rows
            if (h["support_count"] or 0) + (h["refute_count"] or 0) == 0
        )
        lines.append(
            f"{total_active} active hypotheses"
            f"{f', {no_evidence} with ZERO evidence' if no_evidence else ''}:\n"
        )

        for h in rows:
            sup = h["support_count"] or 0
            ref = h["refute_count"] or 0
            total_evidence = sup + ref
            balance = h["evidence_balance"] or 0
            sit = h["situation_name"] or "unlinked"
            last_eval = h["last_evaluated_cycle"] or 0

            # Evidence accumulation rate indicator
            if total_evidence == 0:
                health = "no evidence"
            elif abs(balance) > total_evidence * 0.7:
                health = "one-sided"
            else:
                health = "contested"

            lines.append(
                f"- [{str(h['id'])[:8]}] ({sit}) balance={balance:+d} "
                f"({sup}+/{ref}-) [{health}] last_eval=cycle {last_eval}"
            )
            lines.append(f"  {h['thesis'][:100]}")

        lines.append("")

    except Exception as e:
        lines.append(f"(Hypothesis query failed: {e})\n")

    return "\n".join(lines)


async def _build_watchlist_section(conn: asyncpg.Connection) -> str:
    """Build watchlist effectiveness summary with trigger rates."""
    lines = ["## Watchlist Effectiveness\n"]

    try:
        rows = await conn.fetch("""
            SELECT w.id, w.name,
                   COUNT(wt.id) AS trigger_count,
                   MAX(wt.triggered_at) AS last_trigger
            FROM watchlist w
            LEFT JOIN watch_triggers wt ON wt.watch_id = w.id
            WHERE w.active = true
            GROUP BY w.id, w.name
            ORDER BY COUNT(wt.id) DESC
            LIMIT 15
        """)

        if not rows:
            lines.append("No active watchlists.\n")
            return "\n".join(lines)

        active_count = len(rows)
        total_triggers = sum(r["trigger_count"] for r in rows)
        zero_trigger = sum(1 for r in rows if r["trigger_count"] == 0)

        lines.append(
            f"{active_count} active watchlists, {total_triggers} total triggers, "
            f"{zero_trigger} with zero triggers.\n"
        )

        for w in rows:
            last = ""
            if w["last_trigger"]:
                lt = w["last_trigger"]
                if lt.tzinfo is None:
                    lt = lt.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - lt
                last = f" (last: {age.days}d ago)" if age.days > 0 else " (last: today)"
            lines.append(
                f"- {w['name'][:50]} — {w['trigger_count']} triggers{last}"
            )

        lines.append("")

    except Exception as e:
        lines.append(f"(Watchlist query failed: {e})\n")

    return "\n".join(lines)


async def _build_predictions_section(conn: asyncpg.Connection) -> str:
    """Build prediction track record summary."""
    lines = ["## Prediction Track Record\n"]

    try:
        rows = await conn.fetch("""
            SELECT id, data FROM predictions
            ORDER BY created_at DESC
            LIMIT 30
        """)

        if not rows:
            lines.append("No predictions recorded.\n")
            return "\n".join(lines)

        active = 0
        confirmed = 0
        refuted = 0
        expired = 0

        for row in rows:
            raw = row["data"]
            data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
            status = data.get("status", "active")
            if status == "active":
                active += 1
            elif status == "confirmed":
                confirmed += 1
            elif status == "refuted":
                refuted += 1
            elif status in ("expired", "superseded"):
                expired += 1

        total = len(rows)
        resolved = confirmed + refuted
        accuracy = (confirmed / resolved * 100) if resolved > 0 else 0

        lines.append(
            f"Total: {total} | Active: {active} | "
            f"Confirmed: {confirmed} | Refuted: {refuted} | Expired: {expired}"
        )
        if resolved > 0:
            lines.append(f"Accuracy (resolved): {accuracy:.0f}% ({confirmed}/{resolved})")

        lines.append("")

    except Exception as e:
        lines.append(f"(Prediction query failed: {e})\n")

    return "\n".join(lines)


async def _build_coverage_gaps_section(conn: asyncpg.Connection) -> str:
    """Identify high-activity regions/domains with no covering situation."""
    lines = ["## Coverage Gaps\n"]

    try:
        # Find regions with many recent events but no active situation
        event_regions = await conn.fetch("""
            SELECT
                COALESCE(
                    (data->'geo_countries'->>0),
                    (data->'locations'->>0),
                    'unknown'
                ) AS region,
                COUNT(*) AS event_count
            FROM events
            WHERE created_at > NOW() - INTERVAL '7 days'
            GROUP BY region
            HAVING COUNT(*) >= 3
            ORDER BY COUNT(*) DESC
            LIMIT 15
        """)

        if not event_regions:
            lines.append("No significant coverage gaps detected.\n")
            return "\n".join(lines)

        # Get regions covered by active situations
        covered_regions: set[str] = set()
        sit_rows = await conn.fetch(
            "SELECT data FROM situations WHERE status IN ('active', 'escalating')"
        )
        for row in sit_rows:
            raw = row["data"]
            data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
            for r in (data.get("regions") or []):
                if r:
                    covered_regions.add(r.lower())

        gaps = []
        for row in event_regions:
            region = row["region"]
            if region and region.lower() not in covered_regions and region != "unknown":
                gaps.append((region, row["event_count"]))

        if gaps:
            lines.append("Regions with recent activity but no situation coverage:")
            for region, count in gaps[:8]:
                lines.append(f"- **{region}**: {count} events (last 7d)")
        else:
            lines.append("All active regions have situation coverage.")

        lines.append("")

    except Exception as e:
        lines.append(f"(Coverage gap query failed: {e})\n")

    return "\n".join(lines)


async def _build_backlog_section(redis_client) -> str:
    """Build task backlog summary from Redis."""
    lines = ["## Task Backlog\n"]

    try:
        raw_tasks = await redis_client.zrevrangebyscore(
            BACKLOG_KEY, "+inf", "-inf", withscores=True,
        )

        if not raw_tasks:
            lines.append("No pending tasks.\n")
            return "\n".join(lines)

        # Count by type and cycle
        by_type: dict[str, int] = {}
        by_cycle: dict[str, int] = {}
        total = 0

        for member, score in raw_tasks:
            try:
                task = json.loads(member)
            except (json.JSONDecodeError, TypeError):
                continue

            if task.get("status") != "pending":
                continue

            total += 1
            task_type = task.get("task_type", "unknown")
            by_type[task_type] = by_type.get(task_type, 0) + 1

            cycle = task.get("cycle_type", "any")
            by_cycle[cycle] = by_cycle.get(cycle, 0) + 1

        lines.append(f"**{total} pending tasks**\n")

        if by_type:
            lines.append("By type:")
            for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
                lines.append(f"  - {t}: {c}")

        if by_cycle:
            lines.append("By cycle type:")
            for ct, c in sorted(by_cycle.items(), key=lambda x: -x[1]):
                lines.append(f"  - {ct}: {c}")

        # Show top 5 highest-priority tasks
        lines.append("\nHighest priority tasks:")
        shown = 0
        for member, score in raw_tasks:
            if shown >= 5:
                break
            try:
                task = json.loads(member)
            except (json.JSONDecodeError, TypeError):
                continue
            if task.get("status") != "pending":
                continue
            target = task.get("target", {})
            summary = ""
            for key in ("situation_name", "entity_name", "goal_description", "hypothesis_id"):
                if key in target:
                    summary = f"{key}={str(target[key])[:40]}"
                    break
            lines.append(
                f"  - [{task.get('task_type', '?')}] priority={score:.2f} "
                f"cycle={task.get('cycle_type', 'any')} {summary}"
            )
            shown += 1

        lines.append("")

    except Exception as e:
        lines.append(f"(Backlog query failed: {e})\n")

    return "\n".join(lines)
