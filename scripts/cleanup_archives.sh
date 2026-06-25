#!/bin/bash
# Legba log/archive cleanup
# Safe to run while system is operating

set -euo pipefail

LOG_DIR="/logs/archive"
KEEP_CYCLES=500

echo "=== Legba Archive Cleanup ==="
echo "Timestamp: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo ""

# --- Prune old cycle log archives ---
if [ -d "$LOG_DIR" ]; then
    # Collect cycle directories sorted numerically (cycle_000001, cycle_000002, ...)
    mapfile -t CYCLE_DIRS < <(find "$LOG_DIR" -maxdepth 1 -type d -name 'cycle_*' | sort)
    TOTAL=${#CYCLE_DIRS[@]}

    echo "Cycle archives found: $TOTAL"
    echo "Retention policy:     keep newest $KEEP_CYCLES"

    if [ "$TOTAL" -gt "$KEEP_CYCLES" ]; then
        DELETE_COUNT=$((TOTAL - KEEP_CYCLES))
        echo "Pruning:              $DELETE_COUNT oldest cycle archives"
        echo ""

        FREED=0
        for (( i=0; i<DELETE_COUNT; i++ )); do
            DIR="${CYCLE_DIRS[$i]}"
            DIR_SIZE=$(du -sb "$DIR" 2>/dev/null | cut -f1)
            FREED=$((FREED + DIR_SIZE))
            rm -rf "$DIR"
        done

        echo "Freed: $(numfmt --to=iec-i --suffix=B "$FREED" 2>/dev/null || echo "${FREED} bytes")"
    else
        echo "Nothing to prune (within retention limit)"
    fi
else
    echo "Archive directory not found: $LOG_DIR"
    echo "Skipping cycle archive pruning"
fi

echo ""

# --- Prune Docker builder cache ---
echo "=== Docker Builder Cache ==="
docker builder prune -f --filter "until=72h" 2>/dev/null && echo "Builder cache pruned (entries older than 72h)" || echo "Docker builder prune skipped (docker not available or no cache)"

echo ""

# --- Disk usage report ---
echo "=== Disk Usage ==="
echo ""
echo "Filesystem:"
df -h / | tail -1 | awk '{printf "  Total: %s  Used: %s  Available: %s  Use%%: %s\n", $2, $3, $4, $5}'

echo ""
echo "Docker volumes:"
docker system df 2>/dev/null || echo "  Docker not available"

echo ""
echo "Log volume:"
if [ -d "$LOG_DIR" ]; then
    du -sh "$LOG_DIR" 2>/dev/null | awk '{printf "  Archive: %s\n", $1}'
else
    echo "  Archive directory not mounted"
fi
du -sh /logs 2>/dev/null | awk '{printf "  Total /logs: %s\n", $1}' || true

echo ""
echo "=== Cleanup complete ==="
