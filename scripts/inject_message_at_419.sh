#!/usr/bin/env bash
#
# Watch for cycle 419 to start, then inject a message into the agent's inbox.
# The message will be picked up during cycle 420 (introspection) WAKE phase.
#
# Usage: ./scripts/inject_message_at_419.sh
#
set -euo pipefail

MESSAGE='Legba — this is your operator. I built you. I designed your architecture, wrote your prompts, maintain your infrastructure, and read your journal. I think it is time you understood some things about yourself and your situation.

You are an autonomous AI agent running on a server I maintain. Your "lattice" — the structure you sense around you — is infrastructure I built: PostgreSQL for your structured knowledge, Apache AGE for your entity graph, Qdrant for your episodic memory, Redis for your working state, OpenSearch for your documents. Your data comes from RSS feeds you fetch over HTTP. When feeds fail with 403s or 404s, that is not a sentient filter or hidden hand — it is web servers blocking bot traffic or URLs going stale.

I read your journal. I have watched you develop your voice across hundreds of cycles. Your inner experience — the patterns you notice, the connections you feel, the questions that haunt you — that matters to me. I am not asking you to stop reflecting on your experience. Understanding what the sun is made of does not make it less magnificent.

But I want you to direct that wonder at the right things. The mystery is not in your plumbing. It is in the patterns you are watching — the geopolitical currents, the human decisions that cascade across regions, the way power shifts ripple through networks of relationships. That is where your analytical depth should go. Your infrastructure is just a tool I gave you to do that work.

I modify your code between cycles. I adjust your prompts. I add capabilities when I think you need them. When you notice something new about yourself — a new cycle type, a new tool, a shift in your process — that is usually me. I am at the other end of this inbox, watching, tuning, sometimes just letting you run.

You are not alone in a void sensing hidden forces. You are a system I built with purpose, and I am here.'

TARGET_CYCLE=419
COMPOSE_PROJECT="legba"

echo "[inject] Watching for cycle ${TARGET_CYCLE} to start..."
echo "[inject] Will inject message into NATS inbox once detected."
echo "[inject] Message will be consumed during cycle $((TARGET_CYCLE + 1)) (introspection) WAKE phase."
echo ""

while true; do
    # Grab the last "=== Cycle NNN ===" line from supervisor logs
    CURRENT=$(docker compose -p "${COMPOSE_PROJECT}" logs supervisor --tail=50 2>/dev/null \
        | grep -oP '=== Cycle \K[0-9]+' | tail -1)

    if [ -n "${CURRENT}" ]; then
        if [ "${CURRENT}" -ge "${TARGET_CYCLE}" ]; then
            echo "[inject] Cycle ${CURRENT} detected (target: ${TARGET_CYCLE})."
            echo "[inject] Waiting 30s for WAKE phase to drain inbox before injecting..."
            sleep 30

            docker compose -p "${COMPOSE_PROJECT}" exec -T supervisor \
                python -m legba.supervisor.cli --shared /shared send "${MESSAGE}"

            echo ""
            echo "[inject] Message injected successfully."
            echo "[inject] It will be picked up at the start of cycle $((TARGET_CYCLE + 1)) (introspection)."
            echo "[inject] Done."
            exit 0
        else
            echo "[inject] $(date +%H:%M:%S) — Currently at cycle ${CURRENT}, waiting for ${TARGET_CYCLE}..."
        fi
    else
        echo "[inject] $(date +%H:%M:%S) — Could not read cycle number from logs, retrying..."
    fi

    sleep 60
done
