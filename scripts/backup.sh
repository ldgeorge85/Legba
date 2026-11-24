#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/backup.sh — one ENCRYPTED backup set of the whole deployment.
#
# What a set holds (every artifact encrypted to an OpenPGP public key BEFORE it
# is written — no plaintext copy ever lands on disk, here or at the destination):
#
#   postgres    pg_dump -Fc of `legba` taken inside an exported REPEATABLE READ
#               snapshot, with the named tables' row counts read IN THAT SAME
#               snapshot (so a restore must reproduce them exactly); `dapr` and
#               the cluster globals beside it.
#   archive     the content-addressed evidence archive (docker volume
#               legba_archive), INCREMENTAL: files are immutable and named by
#               their sha256, so each run packs only the names never shipped to
#               this destination before. Packs form a chain under archive/ that
#               retention never prunes (evidence is never auto-deleted).
#   qdrant      one snapshot per collection (the snapshot API), its exact point
#               count read before and after.
#   opensearch  a logical export (scroll → NDJSON) of every user index, plus its
#               mappings/settings. The live node runs without `path.repo`, and a
#               snapshot repository would cost an OpenSearch restart; the export
#               restores on any node of the same major version.
#   redis, nats the RDB and the JetStream store directory.
#   repo        seeds/ and planning/ (gitignored, curated, on this host only).
#   escrow      a SEPARATE sealed artifact (escrow.tar.gpg, to
#               LEGBA_BACKUP_ESCROW_PUBKEY, default the main key): .env — which
#               carries the vault master key and the registry/checkpoint signing
#               key — plus any signing keyfile the .env points at, plus a names-
#               only inventory. The run REFUSES when a required secret is absent.
#
# Layout at the destination:
#   sets/<SETID>/set.json          cleartext completeness marker (no content)
#   sets/<SETID>/SHA256SUMS        cleartext sums of the encrypted files
#   sets/<SETID>/manifest.json.gpg counts, sizes, the pack chain, fingerprints
#   sets/<SETID>/*.gpg             the artifacts above
#   archive/pack-<SETID>.tar.zst.gpg + .list.gpg   the archive chain
#
# Knobs (environment first, then LEGBA_BACKUP_* lines in .env):
#   LEGBA_BACKUP_PUBKEY         armored OpenPGP public key file — REQUIRED
#   LEGBA_BACKUP_ESCROW_PUBKEY  key for the escrow artifact (default: the above)
#   LEGBA_BACKUP_DEST           /local/path | ssh://user@host[:port]/path |
#                               user@host:/path | rclone:<remote>:<path> |
#                               s3://bucket/path   (default /var/backups/legba)
#   LEGBA_BACKUP_STAGING        local staging for remote destinations
#                               (default /var/backups/legba/staging)
#   LEGBA_BACKUP_STATE_DIR      archive index + status files
#                               (default /var/lib/legba/backup-state)
#   LEGBA_BACKUP_MAX_DISK_PCT   refuse/abort above this fill of the set's
#                               filesystem (default 90; OpenSearch's flood stage
#                               is 95% on the same disk)
#   LEGBA_BACKUP_REPO_ROOT      where seeds/ + planning/ live (default: this repo)
#   LEGBA_BACKUP_COUNT_TABLES   space-separated tables counted into the manifest
#
# Usage:
#   bash scripts/backup.sh                    # every component
#   bash scripts/backup.sh postgres escrow    # a subset (still one set)
#
# Exit: 0 = set complete at the destination; 1 = failed (partial set removed);
#       2 = refused (no key, incomplete escrow, disk guard, bad destination).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB=(python3 "$SCRIPT_DIR/backup_lib.py")
ENV_FILE="${LEGBA_ENV_FILE:-$REPO_ROOT/.env}"

log()  { echo "[backup] $*"; }
err()  { echo "[backup] ERROR: $*" >&2; }
refuse() { echo "[backup] REFUSED: $*" >&2; exit 2; }

knob() {  # knob NAME DEFAULT — environment, then .env, then default
    local name="$1" def="${2:-}" val="${!1:-}"
    if [[ -z "$val" ]]; then
        val="$("${LIB[@]}" env-get "$ENV_FILE" "$name" 2>/dev/null || true)"
    fi
    printf '%s' "${val:-$def}"
}

PUBKEY="$(knob LEGBA_BACKUP_PUBKEY)"
ESCROW_PUBKEY="$(knob LEGBA_BACKUP_ESCROW_PUBKEY "$PUBKEY")"
DEST="$(knob LEGBA_BACKUP_DEST /var/backups/legba)"
STAGING="$(knob LEGBA_BACKUP_STAGING /var/backups/legba/staging)"
STATE_DIR="$(knob LEGBA_BACKUP_STATE_DIR /var/lib/legba/backup-state)"
MAX_DISK_PCT="$(knob LEGBA_BACKUP_MAX_DISK_PCT 90)"
BACKUP_REPO_ROOT="$(knob LEGBA_BACKUP_REPO_ROOT "$REPO_ROOT")"
PG_CONTAINER="$(knob LEGBA_BACKUP_PG_CONTAINER legba-postgres-1)"
REDIS_CONTAINER="$(knob LEGBA_BACKUP_REDIS_CONTAINER legba-redis-1)"
NATS_CONTAINER="$(knob LEGBA_BACKUP_NATS_CONTAINER legba-nats-1)"
ARCHIVE_VOLUME="$(knob LEGBA_BACKUP_ARCHIVE_VOLUME legba_archive)"
QDRANT_URL="$(knob LEGBA_BACKUP_QDRANT_URL http://127.0.0.1:6333)"
OS_URL="$(knob LEGBA_BACKUP_OS_URL http://127.0.0.1:9200)"
COUNT_TABLES="$(knob LEGBA_BACKUP_COUNT_TABLES "signals evidence_archive stack_credentials analyst_outputs analyst_traces facts entity_profiles graph_arcs proposed_edges observations descriptors ag_catalog.ag_graph ag_catalog.ag_label")"

ALL_COMPONENTS="postgres archive qdrant opensearch redis nats repo escrow"
COMPONENTS="${*:-$ALL_COMPONENTS}"
for c in $COMPONENTS; do
    [[ " $ALL_COMPONENTS " == *" $c "* ]] || refuse "unknown component '$c' (valid: $ALL_COMPONENTS)"
done

# --- refusals before anything is written ------------------------------------
[[ -n "$PUBKEY" ]] || refuse "LEGBA_BACKUP_PUBKEY is not set — nothing leaves this host unencrypted (see .env.example)"
[[ -f "$PUBKEY" ]] || refuse "LEGBA_BACKUP_PUBKEY=$PUBKEY is not a file"
[[ -f "$ESCROW_PUBKEY" ]] || refuse "LEGBA_BACKUP_ESCROW_PUBKEY=$ESCROW_PUBKEY is not a file"
command -v gpg >/dev/null || refuse "gpg not found"
command -v zstd >/dev/null || refuse "zstd not found"
DEST_JSON="$("${LIB[@]}" parse-dest "$DEST")" || refuse "LEGBA_BACKUP_DEST is not a destination: $DEST"
DEST_KIND="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["kind"])' "$DEST_JSON")"
DEST_KEY="$(printf '%s' "$DEST" | sha256sum | cut -c1-16)"
if [[ "$DEST_KIND" == local ]]; then
    ROOT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["path"])' "$DEST_JSON")"
else
    ROOT="$STAGING"
fi

SET_ID="$("${LIB[@]}" set-id)"
SET_DIR="$ROOT/sets/$SET_ID"
PACK_DIR="$ROOT/archive"
WORK="$SET_DIR/.work"
FRAG="$WORK/frag"
INDEX_DIR="$STATE_DIR/archive_index"
INDEX_FILE="$INDEX_DIR/$DEST_KEY.list"
PACKS_FILE="$INDEX_DIR/$DEST_KEY.packs"
STARTED_EPOCH="$(date -u +%s)"
NEW_PACK=""

umask 077
mkdir -p "$SET_DIR" "$PACK_DIR" "$FRAG" "$INDEX_DIR"
chmod 700 "$WORK"

GH="$(mktemp -d)"
PG_SNAP=""
cleanup() {
    local rc=$?
    rm -rf "$GH"
    if [[ $rc -ne 0 ]]; then
        # A killed `docker exec` does not kill the in-container pg_dump (it
        # would hold its snapshot until its pipe breaks) — stop it by name.
        [[ -n "$PG_SNAP" ]] && docker exec "$PG_CONTAINER" pkill -f -- "--snapshot=$PG_SNAP" 2>/dev/null
        err "set $SET_ID FAILED (rc=$rc) — removing the partial set"
        rm -rf "$SET_DIR"
        [[ -n "$NEW_PACK" ]] && rm -f "$PACK_DIR/$NEW_PACK".tar.zst.gpg "$PACK_DIR/$NEW_PACK".list.gpg
    fi
    exit $rc
}
trap cleanup EXIT
trap "exit 143" TERM INT HUP

fpr_of() {  # fpr_of KEYFILE — first primary fingerprint, imported into $GH
    gpg --batch --quiet --homedir "$GH" --import "$1" 2>/dev/null
    gpg --batch --homedir "$GH" --with-colons --import-options show-only --import "$1" 2>/dev/null \
        | awk -F: '$1=="fpr"{print $10; exit}'
}
FPR="$(fpr_of "$PUBKEY")"
ESCROW_FPR="$(fpr_of "$ESCROW_PUBKEY")"
[[ -n "$FPR" && -n "$ESCROW_FPR" ]] || refuse "could not read a public key from LEGBA_BACKUP_PUBKEY / _ESCROW_PUBKEY"

enc_to() {  # enc_to FPR OUTFILE  (stdin → encrypted file)
    gpg --batch --yes --quiet --homedir "$GH" --trust-model always --compress-algo none \
        -r "$1" -e -o "$2"
}
enc() { enc_to "$FPR" "$1"; }

disk_pct() { df --output=pcent "$1" | tail -1 | tr -dc '0-9'; }
disk_guard() {
    local p
    p="$(disk_pct "$ROOT")"
    if (( p > MAX_DISK_PCT )); then
        err "disk guard: $(df -h --output=target,pcent "$ROOT" | tail -1) is above ${MAX_DISK_PCT}% — stopping before $1"
        exit 2
    fi
}
fsize() { stat -c %s "$1"; }

# --- preflight: projected fill must stay under the guard ---------------------
ARCHIVE_MP=""
if [[ " $COMPONENTS " == *" archive "* ]]; then
    ARCHIVE_MP="$(docker volume inspect -f '{{.Mountpoint}}' "$ARCHIVE_VOLUME")"
fi
preflight() {
    local est=0 b
    if [[ " $COMPONENTS " == *" postgres "* ]]; then
        b="$(docker exec "$PG_CONTAINER" psql -U legba -d legba -XAtc "select pg_database_size('legba')+pg_database_size('dapr')")"
        est=$(( est + b / 2 ))
    fi
    if [[ -n "$ARCHIVE_MP" ]]; then
        (cd "$ARCHIVE_MP" && find . -type f -printf '%P\n') | LC_ALL=C sort > "$WORK/archive_current.list"
        "${LIB[@]}" archive-delta "$INDEX_FILE" "$WORK/archive_current.list" > "$WORK/archive_new.list"
        b="$( (cd "$ARCHIVE_MP" && tr '\n' '\0' < "$WORK/archive_new.list" | xargs -0 -r stat -c %s) | awk '{s+=$1} END {printf "%.0f\n", s}')"
        echo "$b" > "$WORK/archive_new.bytes"
        est=$(( est + b / 2 ))
    fi
    if [[ " $COMPONENTS " == *" qdrant "* ]]; then
        # upper bound: the collection storage itself (snapshots barely compress)
        b="$(du -sb "$(docker volume inspect -f '{{.Mountpoint}}' "$(knob LEGBA_BACKUP_QDRANT_VOLUME legba_qdrant_data)")" 2>/dev/null | cut -f1 || echo 0)"
        est=$(( est + ${b:-0} ))
    fi
    local size used avail
    read -r size used avail < <(df -B1 --output=size,used,avail "$ROOT" | tail -1)
    # df's Use% basis — used / (used + avail), so the projection and the
    # disk_guard above read the same scale (used/size ran ~5 points low: the
    # root-reserved blocks are in size but never usable). N-M, 11-13.
    local projected=$(( (used + est) * 100 / (used + avail) ))
    log "preflight: ~$(( est / 1048576 )) MiB to write; $ROOT projected ${projected}% (guard ${MAX_DISK_PCT}%)"
    if (( projected > MAX_DISK_PCT )); then
        err "disk guard: projected ${projected}% > ${MAX_DISK_PCT}% — refusing to start"
        exit 2
    fi
}
preflight

# ============================================================================
# components
# ============================================================================

backup_postgres() {
    log "postgres: exporting a snapshot, counting, dumping"
    local snap line t list=""
    for t in $COUNT_TABLES; do list+="'$t',"; done
    list="${list%,}"
    coproc PSQL { docker exec -i "$PG_CONTAINER" psql -U legba -d legba -XAtq -v ON_ERROR_STOP=1; }
    local pin="${PSQL[1]}" pout="${PSQL[0]}"
    echo "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;" >&"$pin"
    echo "SELECT pg_export_snapshot();" >&"$pin"
    read -r -t 120 snap <&"$pout" || { err "postgres: no exported snapshot"; return 1; }
    PG_SNAP="$snap"
    cat >&"$pin" <<SQL
SELECT t || '|' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %s', t), false, true, '')))[1]::text
  FROM unnest(ARRAY[$list]::text[]) AS t
 WHERE to_regclass(t) IS NOT NULL;
SELECT '__END__';
SQL
    : > "$WORK/pg_counts.txt"
    while read -r -t 3600 line <&"$pout"; do
        [[ "$line" == "__END__" ]] && break
        echo "$line" >> "$WORK/pg_counts.txt"
    done
    docker exec "$PG_CONTAINER" pg_dump -U legba -d legba --snapshot="$snap" -Fc -Z zstd:3 --no-sync \
        | enc "$SET_DIR/postgres_legba.dump.gpg"
    echo "COMMIT;" >&"$pin"
    exec {pin}>&-
    wait "$PSQL_PID" 2>/dev/null || true
    docker exec "$PG_CONTAINER" pg_dump -U legba -d dapr -Fc -Z zstd:3 --no-sync \
        | enc "$SET_DIR/postgres_dapr.dump.gpg"
    docker exec "$PG_CONTAINER" pg_dumpall -U legba --globals-only \
        | enc "$SET_DIR/postgres_globals.sql.gpg"
    python3 - "$WORK/pg_counts.txt" "$snap" "$(fsize "$SET_DIR/postgres_legba.dump.gpg")" > "$FRAG/postgres.json" <<'PY'
import json, sys
counts = {}
for ln in open(sys.argv[1]):
    name, _, n = ln.strip().rpartition("|")
    if name:
        counts[name] = int(n)
print(json.dumps({"table_counts": counts, "snapshot": sys.argv[2],
                  "databases": ["legba", "dapr"], "dump_bytes": int(sys.argv[3])}))
PY
    log "postgres: done ($(du -h "$SET_DIR/postgres_legba.dump.gpg" | cut -f1); $(wc -l < "$WORK/pg_counts.txt") tables counted in the snapshot)"
}

backup_archive() {
    local total new bytes
    total="$(wc -l < "$WORK/archive_current.list")"
    new="$(grep -c . "$WORK/archive_new.list" || true)"
    bytes="$(cat "$WORK/archive_new.bytes")"
    log "archive: $total files in $ARCHIVE_VOLUME; $new never shipped to this destination ($(( bytes / 1048576 )) MiB raw)"
    local pack=null
    if (( new > 0 )); then
        NEW_PACK="pack-$SET_ID"
        tar -C "$ARCHIVE_MP" -cf - --no-recursion -T "$WORK/archive_new.list" \
            | zstd -q -3 -T0 | enc "$PACK_DIR/$NEW_PACK.tar.zst.gpg"
        enc "$PACK_DIR/$NEW_PACK.list.gpg" < "$WORK/archive_new.list"
        pack="\"$NEW_PACK\""
        log "archive: packed $NEW_PACK ($(du -h "$PACK_DIR/$NEW_PACK.tar.zst.gpg" | cut -f1))"
    fi
    { [[ -f "$PACKS_FILE" ]] && cat "$PACKS_FILE"; [[ -n "$NEW_PACK" ]] && echo "$NEW_PACK"; } \
        | grep . > "$WORK/packs_after.list" || true
    python3 - "$total" "$new" "$bytes" "$pack" "$WORK/packs_after.list" > "$FRAG/archive.json" <<'PY'
import json, sys
packs = [p.strip() for p in open(sys.argv[5]) if p.strip()]
print(json.dumps({"files_total": int(sys.argv[1]), "files_new": int(sys.argv[2]),
                  "bytes_new_raw": int(sys.argv[3]), "pack": json.loads(sys.argv[4]),
                  "packs": packs}))
PY
}

qdrant_count() {
    curl -sf -X POST "$QDRANT_URL/collections/$1/points/count" -H 'content-type: application/json' \
        -d '{"exact": true}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["count"])'
}

backup_qdrant() {
    mkdir -p "$SET_DIR/qdrant"
    local colls c before after snap
    colls="$(curl -sf "$QDRANT_URL/collections" | python3 -c 'import json,sys; [print(c["name"]) for c in json.load(sys.stdin)["result"]["collections"]]')"
    echo '{}' > "$WORK/qdrant.json"
    for c in $colls; do
        disk_guard "qdrant/$c"
        before="$(qdrant_count "$c")"
        snap="$(curl -sf -X POST "$QDRANT_URL/collections/$c/snapshots?wait=true" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')"
        after="$(qdrant_count "$c")"
        curl -sf "$QDRANT_URL/collections/$c/snapshots/$snap" | zstd -q -3 -T0 | enc "$SET_DIR/qdrant/$c.snapshot.zst.gpg"
        curl -sf -X DELETE "$QDRANT_URL/collections/$c/snapshots/$snap" >/dev/null || true
        python3 - "$WORK/qdrant.json" "$c" "$before" "$after" "$snap" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d[sys.argv[2]] = {"before": int(sys.argv[3]), "after": int(sys.argv[4]), "snapshot": sys.argv[5]}
json.dump(d, open(p, "w"))
PY
        log "qdrant: $c — $before..$after points ($(du -h "$SET_DIR/qdrant/$c.snapshot.zst.gpg" | cut -f1))"
    done
    python3 -c 'import json,sys; print(json.dumps({"collections": json.load(open(sys.argv[1]))}))' "$WORK/qdrant.json" > "$FRAG/qdrant.json"
}

backup_opensearch() {
    mkdir -p "$SET_DIR/opensearch"
    local idx before after n
    echo '{}' > "$WORK/os.json"
    for idx in $("${LIB[@]}" os-indices "$OS_URL"); do
        disk_guard "opensearch/$idx"
        before="$("${LIB[@]}" os-count "$OS_URL" "$idx")"
        "${LIB[@]}" os-meta "$OS_URL" "$idx" | enc "$SET_DIR/opensearch/$idx.meta.json.gpg"
        "${LIB[@]}" os-export "$OS_URL" "$idx" 2> "$WORK/os_n" | zstd -q -3 -T0 \
            | enc "$SET_DIR/opensearch/$idx.ndjson.zst.gpg"
        n="$(tail -1 "$WORK/os_n")"
        after="$("${LIB[@]}" os-count "$OS_URL" "$idx")"
        python3 - "$WORK/os.json" "$idx" "$before" "$after" "$n" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d[sys.argv[2]] = {"before": int(sys.argv[3]), "after": int(sys.argv[4]), "exported": int(sys.argv[5])}
json.dump(d, open(p, "w"))
PY
        log "opensearch: $idx — $n docs exported ($(du -h "$SET_DIR/opensearch/$idx.ndjson.zst.gpg" | cut -f1))"
    done
    python3 -c 'import json,sys; print(json.dumps({"method": "logical-scroll", "indices": json.load(open(sys.argv[1]))}))' "$WORK/os.json" > "$FRAG/opensearch.json"
}

backup_redis() {
    local before after
    before="$(docker exec "$REDIS_CONTAINER" redis-cli LASTSAVE | tr -dc '0-9')"
    docker exec "$REDIS_CONTAINER" redis-cli BGSAVE >/dev/null
    for _ in $(seq 1 120); do
        after="$(docker exec "$REDIS_CONTAINER" redis-cli LASTSAVE | tr -dc '0-9')"
        [[ "$after" != "$before" ]] && break
        sleep 1
    done
    [[ "$after" != "$before" ]] || { err "redis: BGSAVE did not complete in 120 s"; return 1; }
    docker cp "$REDIS_CONTAINER:/data/dump.rdb" - | zstd -q -3 | enc "$SET_DIR/redis.rdb.tar.zst.gpg"
    echo '{"file": "redis.rdb.tar.zst.gpg"}' > "$FRAG/redis.json"
    log "redis: done"
}

backup_nats() {
    docker cp "$NATS_CONTAINER:/data" - | zstd -q -3 -T0 | enc "$SET_DIR/nats.tar.zst.gpg"
    echo '{"file": "nats.tar.zst.gpg", "method": "store-dir copy (crash-consistent)"}' > "$FRAG/nats.json"
    log "nats: done ($(du -h "$SET_DIR/nats.tar.zst.gpg" | cut -f1))"
}

backup_repo() {
    local d have=()
    for d in seeds planning; do [[ -d "$BACKUP_REPO_ROOT/$d" ]] && have+=("$d"); done
    (( ${#have[@]} )) || { err "repo: neither seeds/ nor planning/ under $BACKUP_REPO_ROOT"; return 1; }
    tar -C "$BACKUP_REPO_ROOT" -cf - "${have[@]}" | zstd -q -3 -T0 | enc "$SET_DIR/repo.tar.zst.gpg"
    python3 -c 'import json,sys; print(json.dumps({"dirs": sys.argv[1:]}))' "${have[@]}" > "$FRAG/repo.json"
    log "repo: ${have[*]} ($(du -h "$SET_DIR/repo.tar.zst.gpg" | cut -f1))"
}

backup_escrow() {
    [[ -f "$ENV_FILE" ]] || refuse "escrow: $ENV_FILE does not exist"
    local args=(-C "$(dirname "$ENV_FILE")" --transform "s|^$(basename "$ENV_FILE")\$|escrow/env|" "$(basename "$ENV_FILE")")
    local kf_flags=() name path
    for name in LEGBA_REGISTRY_SIGNING_KEY_FILE LEGBA_SIGNING_KEY_PATH; do
        path="$(python3 -c '
import sys, pathlib
sys.path.insert(0, sys.argv[1])
import backup_lib
v = backup_lib.env_value(pathlib.Path(sys.argv[2]).read_text(), sys.argv[3])
print(v or "")' "$SCRIPT_DIR" "$ENV_FILE" "$name")"
        if [[ -n "$path" && -f "$path" ]]; then
            args+=(-C "$(dirname "$path")" --transform "s|^$(basename "$path")\$|escrow/keys/$name|" "$(basename "$path")")
            kf_flags+=(--keyfile "$name=1")
        elif [[ -n "$path" ]]; then
            kf_flags+=(--keyfile "$name=0")
            log "escrow: $name points at a path not on this host (a container path?) — recorded as not included"
        fi
    done
    "${LIB[@]}" escrow-inventory "$ENV_FILE" "${kf_flags[@]}" > "$WORK/escrow_inventory.json"
    if ! python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["complete"] else 1)' "$WORK/escrow_inventory.json"; then
        refuse "escrow incomplete — missing: $(python3 -c 'import json,sys; print(", ".join(json.load(open(sys.argv[1]))["missing_required"]))' "$WORK/escrow_inventory.json")"
    fi
    args+=(-C "$WORK" --transform 's|^escrow_inventory.json$|escrow/inventory.json|' escrow_inventory.json)
    tar -cf - "${args[@]}" | enc_to "$ESCROW_FPR" "$SET_DIR/escrow.tar.gpg"
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps({"inventory": d, "recipient": sys.argv[2]}))' \
        "$WORK/escrow_inventory.json" "$ESCROW_FPR" > "$FRAG/escrow.json"
    log "escrow: sealed to $ESCROW_FPR"
}

# ============================================================================
# run
# ============================================================================
log "set $SET_ID → $DEST_KIND destination; components: $COMPONENTS"
for c in $COMPONENTS; do
    disk_guard "$c"
    t0=$(date -u +%s)
    "backup_$c"
    log "$c: $(( $(date -u +%s) - t0 )) s"
done

# --- manifest, sums, completeness marker --------------------------------------
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
META="$(python3 -c 'import json,sys; print(json.dumps({"pubkey_fpr": sys.argv[1], "escrow_fpr": sys.argv[2], "host": sys.argv[3], "git_sha": sys.argv[4], "dest_kind": sys.argv[5]}))' \
    "$FPR" "$ESCROW_FPR" "$(hostname)" "$GIT_SHA" "$DEST_KIND")"
"${LIB[@]}" manifest "$SET_ID" "$FRAG" --meta "$META" | enc "$SET_DIR/manifest.json.gpg"
(
    cd "$ROOT"
    find "sets/$SET_ID" -type f -name '*.gpg' -printf '%P\n' | sed "s|^|sets/$SET_ID/|" | LC_ALL=C sort | xargs -r sha256sum
    [[ -n "$NEW_PACK" ]] && sha256sum "archive/$NEW_PACK.tar.zst.gpg" "archive/$NEW_PACK.list.gpg"
    true
) > "$SET_DIR/SHA256SUMS"
BYTES="$(du -sb "$SET_DIR" | cut -f1)"
[[ -n "$NEW_PACK" ]] && BYTES=$(( BYTES + $(fsize "$PACK_DIR/$NEW_PACK.tar.zst.gpg") ))
if [[ " $COMPONENTS " == *" archive "* ]]; then
    mv "$WORK/archive_current.list" "$INDEX_DIR/.pending-$SET_ID.list"
    mv "$WORK/packs_after.list" "$INDEX_DIR/.pending-$SET_ID.packs"
fi
rm -rf "$WORK"
python3 -c 'import json,sys; print(json.dumps({"format": "legba-backup/1", "set_id": sys.argv[1], "complete": True, "components": sys.argv[2].split(), "bytes": int(sys.argv[3])}, indent=2))' \
    "$SET_ID" "$COMPONENTS" "$BYTES" > "$SET_DIR/set.json"
chmod 644 "$SET_DIR/set.json" "$SET_DIR/SHA256SUMS"

# --- ship ----------------------------------------------------------------------
ship() {
    case "$DEST_KIND" in
        local) return 0 ;;
        ssh)
            local host port path sshcmd
            host="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["host"])' "$DEST_JSON")"
            port="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["port"] or "")' "$DEST_JSON")"
            path="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["path"])' "$DEST_JSON")"
            sshcmd="ssh${port:+ -p $port}"
            $sshcmd "$host" "mkdir -p '$path/archive' '$path/sets'"
            if [[ -n "$NEW_PACK" ]]; then
                rsync -a -e "$sshcmd" "$PACK_DIR/$NEW_PACK".* "$host:$path/archive/"
            fi
            rsync -a -e "$sshcmd" --exclude set.json "$SET_DIR/" "$host:$path/sets/$SET_ID/"
            rsync -a -e "$sshcmd" "$SET_DIR/set.json" "$host:$path/sets/$SET_ID/set.json"
            ;;
        rclone)
            command -v rclone >/dev/null || { err "rclone not installed"; return 1; }
            local target
            target="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["target"])' "$DEST_JSON")"
            if [[ -n "$NEW_PACK" ]]; then
                rclone copy "$PACK_DIR" "$target/archive" --include "$NEW_PACK.*"
            fi
            rclone copy "$SET_DIR" "$target/sets/$SET_ID" --exclude set.json
            rclone copyto "$SET_DIR/set.json" "$target/sets/$SET_ID/set.json"
            ;;
    esac
    if [[ "${LEGBA_BACKUP_KEEP_STAGED:-0}" != 1 ]]; then
        rm -rf "$SET_DIR"
        [[ -n "$NEW_PACK" ]] && rm -f "$PACK_DIR/$NEW_PACK".*
    fi
    return 0
}
ship

# --- commit the archive index only now that the pack is at the destination ----
if [[ " $COMPONENTS " == *" archive "* ]]; then
    mv "$INDEX_DIR/.pending-$SET_ID.list" "$INDEX_FILE"
    mv "$INDEX_DIR/.pending-$SET_ID.packs" "$PACKS_FILE"
fi
NEW_PACK=""   # shipped: the failure trap must no longer remove it

DURATION=$(( $(date -u +%s) - STARTED_EPOCH ))
"${LIB[@]}" status "$STATE_DIR" backup "$(python3 -c 'import json,sys,datetime; print(json.dumps({"last_backup_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "set_id": sys.argv[1], "dest_kind": sys.argv[2], "bytes": int(sys.argv[3]), "duration_s": int(sys.argv[4]), "components": sys.argv[5].split()}))' "$SET_ID" "$DEST_KIND" "$BYTES" "$DURATION" "$COMPONENTS")"
log "set $SET_ID complete: $(( BYTES / 1048576 )) MiB in ${DURATION} s -> $DEST_KIND destination"
