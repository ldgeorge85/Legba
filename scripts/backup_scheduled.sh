#!/usr/bin/env bash
# Legba scheduled backup — runs the full backup, prunes old generations, and
# ships the newest generation offsite (resilience-observability W-1b §5).
#
# Designed to be invoked by cron or the systemd timer
# (deploy/systemd/legba-backup.{service,timer}). It wraps scripts/backup.sh:
#
#   1. Run a full backup of every service (pg, redis, qdrant, nats).
#   2. Retention: keep the newest LEGBA_BACKUP_KEEP generations under
#      /var/backups/legba, delete the rest.
#   3. Offsite: copy the newest generation to LEGBA_BACKUP_OFFSITE_DEST via the
#      configured tool (rsync / aws s3 / rclone). FAIL LOUD if configured but the
#      push fails; WARN LOUD (and record a marker) if offsite is NOT configured —
#      offsite is a declared SEAM until an operator wires a destination (see
#      docs/SEAMS.md). A local-only backup with no offsite is NOT disaster-proof.
#
# Env knobs (all optional; sane defaults):
#   LEGBA_BACKUP_KEEP            generations to retain locally   (default 14)
#   LEGBA_BACKUP_OFFSITE_DEST   offsite destination URI/path     (default: unset)
#   LEGBA_BACKUP_OFFSITE_TOOL   rsync | s3 | rclone              (default: rsync)
#
# Exit codes: 0 = ok; 1 = backup failed; 2 = offsite configured but failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ROOT="/var/backups/legba"
KEEP="${LEGBA_BACKUP_KEEP:-14}"
OFFSITE_DEST="${LEGBA_BACKUP_OFFSITE_DEST:-}"
OFFSITE_TOOL="${LEGBA_BACKUP_OFFSITE_TOOL:-rsync}"

log()  { echo "[backup-scheduled] $*"; }
warn() { echo "[backup-scheduled] WARN: $*" >&2; }
err()  { echo "[backup-scheduled] ERROR: $*" >&2; }

log "Starting scheduled backup at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# --- 1. Full backup -------------------------------------------------------
if ! bash "${SCRIPT_DIR}/backup.sh"; then
    err "backup.sh reported errors — see output above"
    exit 1
fi

# Identify the newest generation directory just produced.
NEWEST="$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d | sort | tail -1)"
if [[ -z "$NEWEST" ]]; then
    err "no backup generation found under ${BACKUP_ROOT} after backup.sh"
    exit 1
fi
log "Newest generation: ${NEWEST}"

# --- 2. Retention ---------------------------------------------------------
mapfile -t GENS < <(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d | sort)
TOTAL=${#GENS[@]}
log "Local generations: ${TOTAL}; retention: keep newest ${KEEP}"
if (( TOTAL > KEEP )); then
    DELETE=$((TOTAL - KEEP))
    for (( i=0; i<DELETE; i++ )); do
        log "Pruning old generation: ${GENS[$i]}"
        rm -rf "${GENS[$i]}"
    done
fi

# --- 3. Offsite -----------------------------------------------------------
if [[ -z "$OFFSITE_DEST" ]]; then
    # Declared SEAM: a backup that lives only on the same host is not
    # disaster-proof. We refuse to pretend otherwise — warn loudly and drop a
    # marker so the absence is visible in the backup tree. Operators wire a
    # destination by setting LEGBA_BACKUP_OFFSITE_DEST (see docs/SEAMS.md +
    # docs/RUNBOOK.md backup section).
    warn "LEGBA_BACKUP_OFFSITE_DEST is unset — backups are LOCAL-ONLY (not offsite)."
    warn "This is a declared SEAM: set LEGBA_BACKUP_OFFSITE_DEST to enable offsite."
    echo "OFFSITE NOT CONFIGURED — local-only backup at $(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        > "${NEWEST}/OFFSITE_NOT_CONFIGURED.txt"
    log "Scheduled backup complete (local-only)."
    exit 0
fi

log "Shipping ${NEWEST} offsite to ${OFFSITE_DEST} via ${OFFSITE_TOOL}"
case "$OFFSITE_TOOL" in
    rsync)
        rsync -az --delete "${NEWEST}/" "${OFFSITE_DEST%/}/$(basename "$NEWEST")/" \
            || { err "rsync offsite push failed"; exit 2; }
        ;;
    s3)
        command -v aws >/dev/null || { err "aws CLI not found for s3 offsite"; exit 2; }
        aws s3 cp --recursive "${NEWEST}" "${OFFSITE_DEST%/}/$(basename "$NEWEST")" \
            || { err "aws s3 offsite push failed"; exit 2; }
        ;;
    rclone)
        command -v rclone >/dev/null || { err "rclone not found for offsite"; exit 2; }
        rclone copy "${NEWEST}" "${OFFSITE_DEST%/}/$(basename "$NEWEST")" \
            || { err "rclone offsite push failed"; exit 2; }
        ;;
    *)
        err "unknown LEGBA_BACKUP_OFFSITE_TOOL=${OFFSITE_TOOL} (valid: rsync, s3, rclone)"
        exit 2
        ;;
esac

log "Offsite push complete. Scheduled backup done at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
