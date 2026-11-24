#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/backup_scheduled.sh — the nightly entry point (deploy/systemd/
# legba-backup.{service,timer}, or cron). It:
#
#   1. runs scripts/backup.sh (one encrypted set, shipped to LEGBA_BACKUP_DEST,
#      status file written only on success);
#   2. applies retention at the destination: keep the newest set of each of the
#      last LEGBA_BACKUP_KEEP_DAILY (7) days and of each of the last
#      LEGBA_BACKUP_KEEP_WEEKLY (4) ISO weeks; delete the other sets. The archive
#      pack chain (archive/) is NEVER pruned — evidence is never auto-deleted,
#      and every set's manifest references the whole chain.
#
# Exit codes: 0 ok; 1 backup failed; 2 backup refused (no key, disk guard,
# incomplete escrow, bad destination); 3 set taken but retention failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB=(python3 "$SCRIPT_DIR/backup_lib.py")
ENV_FILE="${LEGBA_ENV_FILE:-$REPO_ROOT/.env}"

log() { echo "[backup-scheduled] $(date -u +%FT%TZ) $*"; }
err() { echo "[backup-scheduled] $(date -u +%FT%TZ) ERROR: $*" >&2; }

knob() {
    local val="${!1:-}"
    [[ -n "$val" ]] || val="$("${LIB[@]}" env-get "$ENV_FILE" "$1" 2>/dev/null || true)"
    printf '%s' "${val:-${2:-}}"
}

DEST="$(knob LEGBA_BACKUP_DEST /var/backups/legba)"
KEEP_DAILY="$(knob LEGBA_BACKUP_KEEP_DAILY 7)"
KEEP_WEEKLY="$(knob LEGBA_BACKUP_KEEP_WEEKLY 4)"

log "starting"
rc=0
bash "$SCRIPT_DIR/backup.sh" || rc=$?
if (( rc != 0 )); then
    err "backup.sh exited $rc — no new set; retention skipped"
    exit "$rc"
fi

# --- retention at the destination --------------------------------------------
DEST_JSON="$("${LIB[@]}" parse-dest "$DEST")"
field() { python3 -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else v)' "$DEST_JSON" "$1"; }
KIND="$(field kind)"

list_sets() {  # complete sets only (set.json present)
    case "$KIND" in
        local)
            local p; p="$(field path)/sets"
            [[ -d "$p" ]] || return 0
            find "$p" -mindepth 2 -maxdepth 2 -name set.json -printf '%h\n' | xargs -r -n1 basename ;;
        ssh)
            local port; port="$(field port)"
            ssh ${port:+-p "$port"} "$(field host)" \
                "cd '$(field path)/sets' 2>/dev/null && for d in */; do [ -f \"\$d/set.json\" ] && echo \"\${d%/}\"; done; true" ;;
        rclone)
            rclone lsf --dirs-only "$(field target)/sets" | sed 's|/$||' ;;
    esac
}
delete_set() {
    case "$KIND" in
        local)  rm -rf -- "$(field path)/sets/$1" ;;
        ssh)    local port; port="$(field port)"
                ssh ${port:+-p "$port"} "$(field host)" "rm -rf -- '$(field path)/sets/$1'" ;;
        rclone) rclone purge "$(field target)/sets/$1" ;;
    esac
}

mapfile -t SETS < <(list_sets || true)
mapfile -t DELETE < <("${LIB[@]}" retention --daily "$KEEP_DAILY" --weekly "$KEEP_WEEKLY" "${SETS[@]}" | grep . || true)
log "destination ($KIND) holds ${#SETS[@]} complete sets; retention ${KEEP_DAILY}d/${KEEP_WEEKLY}w prunes ${#DELETE[@]}"
for s in "${DELETE[@]}"; do
    log "pruning set $s"
    delete_set "$s" || { err "could not prune $s"; exit 3; }
done
log "done"
