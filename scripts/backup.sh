#!/usr/bin/env bash
# Legba live backup — Postgres, Redis, Qdrant, OpenSearch (+ audit)
# Safe to run while the agent is cycling.
#
# Usage:
#   ./scripts/backup.sh                  # all services
#   ./scripts/backup.sh pg               # just postgres
#   ./scripts/backup.sh redis qdrant     # specific services
#
# Output: /var/backups/legba/<timestamp>/

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_ROOT="/var/backups/legba"
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"

# Service connection defaults (host-side ports)
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-legba}"
PG_DB="${PG_DB:-legba}"
PGPASSWORD="${PGPASSWORD:-legba}"
export PGPASSWORD

REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

OS_URL="${OS_URL:-http://localhost:9200}"
OS_AUDIT_URL="${OS_AUDIT_URL:-http://localhost:9201}"

# Compose project (for container exec fallbacks)
COMPOSE_PROJECT="${COMPOSE_PROJECT:-legba}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[backup]${NC} $*"; }
warn() { echo -e "${YELLOW}[backup]${NC} $*"; }
err()  { echo -e "${RED}[backup]${NC} $*" >&2; }

# --- Determine which services to back up ---
ALL_SERVICES="pg redis qdrant opensearch"
if [[ $# -gt 0 ]]; then
    SERVICES="$*"
else
    SERVICES="$ALL_SERVICES"
fi

mkdir -p "$BACKUP_DIR"
log "Backup dir: $BACKUP_DIR"
log "Services: $SERVICES"
echo ""

ERRORS=0

# ============================================================
# PostgreSQL — pg_dump (consistent snapshot, includes AGE graph)
# ============================================================
backup_pg() {
    log "PostgreSQL: starting pg_dump..."
    local outfile="${BACKUP_DIR}/postgres_legba.sql.gz"

    if command -v pg_dump &>/dev/null; then
        pg_dump -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
            --no-owner --no-privileges \
            | gzip > "$outfile"
    else
        # Fall back to running inside the container
        docker compose -p "$COMPOSE_PROJECT" exec -T postgres \
            pg_dump -U "$PG_USER" -d "$PG_DB" --no-owner --no-privileges \
            | gzip > "$outfile"
    fi

    local size
    size=$(du -h "$outfile" | cut -f1)
    log "PostgreSQL: done — ${outfile} (${size})"
}

# ============================================================
# Redis — BGSAVE + copy RDB, or redis-cli --rdb
# ============================================================
backup_redis() {
    log "Redis: triggering BGSAVE and streaming RDB..."
    local outfile="${BACKUP_DIR}/redis_dump.rdb"

    if command -v redis-cli &>/dev/null; then
        redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --rdb "$outfile" >/dev/null
    else
        # Trigger save inside container, then copy out
        local before_save
        before_save=$(docker compose -p "$COMPOSE_PROJECT" exec -T redis \
            redis-cli LASTSAVE 2>/dev/null | tr -d '[:space:]')
        docker compose -p "$COMPOSE_PROJECT" exec -T redis redis-cli BGSAVE >/dev/null
        # Wait for LASTSAVE to change (means save completed)
        for i in $(seq 1 30); do
            sleep 1
            local after_save
            after_save=$(docker compose -p "$COMPOSE_PROJECT" exec -T redis \
                redis-cli LASTSAVE 2>/dev/null | tr -d '[:space:]')
            if [[ "$after_save" != "$before_save" ]]; then
                break
            fi
            [[ $i -eq 30 ]] && warn "Redis: BGSAVE may still be running"
        done
        # Copy RDB out of the container
        local container
        container=$(docker compose -p "$COMPOSE_PROJECT" ps -q redis)
        docker cp "${container}:/data/dump.rdb" "$outfile"
    fi

    local size
    size=$(du -h "$outfile" | cut -f1)
    log "Redis: done — ${outfile} (${size})"
}

# ============================================================
# Qdrant — snapshot API per collection
# ============================================================
backup_qdrant() {
    log "Qdrant: creating snapshots..."
    local qdrant_dir="${BACKUP_DIR}/qdrant"
    mkdir -p "$qdrant_dir"

    # List collections
    local collections
    collections=$(curl -sf "${QDRANT_URL}/collections" \
        | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)['result']['collections']]")

    if [[ -z "$collections" ]]; then
        warn "Qdrant: no collections found"
        return
    fi

    for coll in $collections; do
        log "  Qdrant: snapshotting ${coll}..."

        # Create snapshot — returns {"result": {"name": "...", ...}}
        local snap_name
        snap_name=$(curl -sf -X POST "${QDRANT_URL}/collections/${coll}/snapshots" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])")

        if [[ -z "$snap_name" ]]; then
            err "  Qdrant: failed to create snapshot for ${coll}"
            ((ERRORS++))
            continue
        fi

        # Download snapshot
        curl -sf "${QDRANT_URL}/collections/${coll}/snapshots/${snap_name}" \
            -o "${qdrant_dir}/${coll}_${snap_name}"

        local size
        size=$(du -h "${qdrant_dir}/${coll}_${snap_name}" | cut -f1)
        log "  Qdrant: ${coll} — ${size}"

        # Clean up snapshot on server
        curl -sf -X DELETE "${QDRANT_URL}/collections/${coll}/snapshots/${snap_name}" >/dev/null 2>&1 || true
    done

    log "Qdrant: done"
}

# ============================================================
# OpenSearch — scroll-dump via Python helper (os_dump.py)
# ============================================================
backup_opensearch() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local os_dir="${BACKUP_DIR}/opensearch"

    # Main instance
    log "OpenSearch: dumping legba-* indices..."
    python3 "${script_dir}/os_dump.py" "$OS_URL" "$os_dir" "legba-"

    # Audit instance
    log "OpenSearch (audit): dumping audit indices..."
    local audit_dir="${os_dir}/audit"
    # Audit indices use legba-audit-* prefix typically; dump everything non-system
    python3 "${script_dir}/os_dump.py" "$OS_AUDIT_URL" "$audit_dir" "legba"

    # Compress
    log "OpenSearch: compressing..."
    tar -czf "${BACKUP_DIR}/opensearch.tar.gz" -C "$BACKUP_DIR" opensearch/
    rm -rf "$os_dir"
    local size
    size=$(du -h "${BACKUP_DIR}/opensearch.tar.gz" | cut -f1)
    log "OpenSearch: done — opensearch.tar.gz (${size})"
}

# ============================================================
# Run selected backups
# ============================================================

for svc in $SERVICES; do
    case "$svc" in
        pg|postgres)   backup_pg ;;
        redis)         backup_redis ;;
        qdrant)        backup_qdrant ;;
        opensearch|os) backup_opensearch ;;
        *)             warn "Unknown service: $svc (valid: pg, redis, qdrant, opensearch)" ;;
    esac
    echo ""
done

# ============================================================
# Summary
# ============================================================
echo "================================================"
log "Backup complete: ${BACKUP_DIR}"
echo ""
ls -lh "$BACKUP_DIR"
echo ""
TOTAL=$(du -sh "$BACKUP_DIR" | cut -f1)
log "Total size: ${TOTAL}"

if [[ $ERRORS -gt 0 ]]; then
    err "${ERRORS} error(s) occurred — check output above"
    exit 1
fi
