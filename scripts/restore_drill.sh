#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/restore_drill.sh — restore one backup set into a THROWAWAY compose
# project and prove it. An untested backup is not a backup.
#
# It restores into deploy/compose.drill.yml (standalone, project-scoped
# volumes, loopback alternate ports — it runs beside the live stack, or on any
# other docker host), then asserts:
#
#   1. every encrypted file matches the set's SHA256SUMS;
#   2. Postgres: the row counts of the manifest's named tables — taken inside
#      the dump's own snapshot — are reproduced EXACTLY;
#   3. the evidence archive: N random evidence_archive receipts (default 100),
#      drawn from the RESTORED database, have their bytes extracted from the
#      archive pack chain and re-hash to the row's sha256;
#   4. the key escrow opens (with the escrow secret key), carries every
#      required secret, and its vault master key decrypts one stack_credentials
#      row of the restored database (the value is never printed — only the
#      secret id and its byte length);
#   5. Qdrant answers with each collection's point count inside the manifest's
#      [before, after] bracket; OpenSearch answers with exactly the exported
#      document count;
#   6. redis, nats, the dapr database, the cluster globals and the repo tarball
#      decrypt cleanly.
# It prints the restore time per step and in total; on PASS it writes
# last_drill.json into LEGBA_BACKUP_STATE_DIR (the registry's
# /v3/system/backup-status route reads it).
#
# Usage:
#   bash scripts/restore_drill.sh --secret-key /path/backup-secret.asc \
#        [--escrow-secret-key FILE] [--set /dest/sets/<SETID>] \
#        [--project legba_drill] [--sample 100] [--keep]
#   With no --set: the newest complete set under a LOCAL LEGBA_BACKUP_DEST.
#   For a remote destination, copy sets/<SETID>/ and archive/ down first.
#
# Knobs: LEGBA_DRILL_SECRET_KEY / LEGBA_DRILL_ESCROW_SECRET_KEY (instead of the
#   flags), LEGBA_DRILL_MAX_DISK_PCT (93; the drill aborts and tears down above
#   it — OpenSearch's flood stage on a shared disk is 95%), LEGBA_DRILL_WORK,
#   LEGBA_DRILL_VAULT_IMAGE (legba/legba-registry:latest), LEGBA_DRILL_PG_PORT /
#   _QDRANT_PORT / _OS_PORT, LEGBA_BACKUP_STATE_DIR.
#
# Exit: 0 PASS; 1 FAIL (an assertion); 2 refused (bad input, disk, isolation).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB=(python3 "$SCRIPT_DIR/backup_lib.py")
ENV_FILE="${LEGBA_ENV_FILE:-$REPO_ROOT/.env}"
COMPOSE_FILE="$REPO_ROOT/deploy/compose.drill.yml"

log()    { echo "[drill] $*"; }
fail()   { echo "[drill] FAIL: $*" >&2; FAILS+=("$*"); }
refuse() { echo "[drill] REFUSED: $*" >&2; exit 2; }
knob() {
    local val="${!1:-}"
    [[ -n "$val" ]] || val="$("${LIB[@]}" env-get "$ENV_FILE" "$1" 2>/dev/null || true)"
    printf '%s' "${val:-${2:-}}"
}

SET_DIR=""
SECRET_KEY="${LEGBA_DRILL_SECRET_KEY:-}"
ESCROW_SECRET_KEY="${LEGBA_DRILL_ESCROW_SECRET_KEY:-}"
PROJECT="legba_drill"
SAMPLE=100
KEEP=0
while (( $# )); do
    case "$1" in
        --set) SET_DIR="$2"; shift 2 ;;
        --secret-key) SECRET_KEY="$2"; shift 2 ;;
        --escrow-secret-key) ESCROW_SECRET_KEY="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --sample) SAMPLE="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
        *) refuse "unknown argument $1" ;;
    esac
done
ESCROW_SECRET_KEY="${ESCROW_SECRET_KEY:-$SECRET_KEY}"
MAX_DISK_PCT="${LEGBA_DRILL_MAX_DISK_PCT:-93}"
STATE_DIR="$(knob LEGBA_BACKUP_STATE_DIR /var/lib/legba/backup-state)"
VAULT_IMAGE="${LEGBA_DRILL_VAULT_IMAGE:-legba/legba-registry:latest}"
PG_PORT="${LEGBA_DRILL_PG_PORT:-55432}"
Q_URL="http://127.0.0.1:${LEGBA_DRILL_QDRANT_PORT:-56333}"
OS_URL="http://127.0.0.1:${LEGBA_DRILL_OS_PORT:-59200}"
export LEGBA_DRILL_PG_PORT="$PG_PORT"

# --- refusals -------------------------------------------------------------------
[[ "$PROJECT" != legba && "$PROJECT" =~ ^[a-z0-9_-]+$ ]] || refuse "project '$PROJECT' — never the live 'legba' project"
[[ -n "$SECRET_KEY" && -f "$SECRET_KEY" ]] || refuse "--secret-key FILE (the backup's OpenPGP secret key) is required"
[[ -f "$ESCROW_SECRET_KEY" ]] || refuse "--escrow-secret-key $ESCROW_SECRET_KEY is not a file"
if [[ -z "$SET_DIR" ]]; then
    DEST="$(knob LEGBA_BACKUP_DEST /var/backups/legba)"
    DEST_PATH="$("${LIB[@]}" parse-dest "$DEST" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("path","") if d["kind"]=="local" else "")')"
    [[ -n "$DEST_PATH" ]] || refuse "LEGBA_BACKUP_DEST is remote — fetch a set and pass --set"
    SET_DIR="$(find "$DEST_PATH/sets" -mindepth 2 -maxdepth 2 -name set.json -printf '%h\n' 2>/dev/null | LC_ALL=C sort | tail -1)"
fi
[[ -n "$SET_DIR" && -f "$SET_DIR/set.json" ]] || refuse "no complete set (set.json) at '${SET_DIR:-<none>}'"
SET_DIR="$(cd "$SET_DIR" && pwd)"
SET_ID="$(basename "$SET_DIR")"
ROOT="$(cd "$SET_DIR/../.." && pwd)"

DC=(docker compose -p "$PROJECT" -f "$COMPOSE_FILE")
"${DC[@]}" config > /dev/null || refuse "compose config failed for $COMPOSE_FILE"
# Isolation gate: every volume the rendered project would mount must be scoped
# to the drill project, and none may be a volume the live stack uses.
"${DC[@]}" config --format json | python3 -c '
import json, subprocess, sys
proj = sys.argv[1]
cfg = json.load(sys.stdin)
names = [v.get("name", "") for v in (cfg.get("volumes") or {}).values()]
live = set(subprocess.run(["docker", "ps", "--format", "{{.Mounts}}"], capture_output=True,
                          text=True).stdout.replace(",", "\n").split())
bad = [n for n in names if not n.startswith(proj + "_") or n in live]
binds = [m for s in cfg["services"].values() for m in s.get("volumes", []) if m.get("type") == "bind"]
if bad or binds:
    print("isolation gate: volumes %r binds %r" % (bad, binds), file=sys.stderr)
    sys.exit(1)
' "$PROJECT" || refuse "isolation gate: the rendered drill project would touch a non-drill volume"

WORK="${LEGBA_DRILL_WORK:-$(mktemp -d /var/tmp/legba-drill.XXXXXX)}"
mkdir -p "$WORK"; chmod 700 "$WORK"
GH="$WORK/gnupg"; mkdir -p "$GH"; chmod 700 "$GH"
FAILS=()
T_START=$(date -u +%s)
declare -A STEP_S=()

teardown() {
    local rc=$?
    [[ -n "${WATCH_PID:-}" ]] && kill "$WATCH_PID" 2>/dev/null || true
    if [[ $KEEP != 1 ]]; then
        "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
        rm -rf "$WORK"
    else
        log "--keep: project $PROJECT left up; work dir $WORK"
    fi
    exit $rc
}
trap teardown EXIT
trap 'echo "[drill] ABORTED by the disk guard (above ${MAX_DISK_PCT}%)" >&2; exit 2' USR1

step() {  # step NAME CMD... — time a step
    local name="$1"; shift
    local t0; t0=$(date -u +%s)
    "$@"
    STEP_S[$name]=$(( $(date -u +%s) - t0 ))
    log "$name: ${STEP_S[$name]} s (disk $(disk_pct "$WORK")%)"
}

disk_pct() { df --output=pcent "$1" | tail -1 | tr -dc '0-9'; }
DOCKER_ROOT="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
(( $(disk_pct "$DOCKER_ROOT") <= MAX_DISK_PCT - 3 )) || refuse "disk at $(disk_pct "$DOCKER_ROOT")% — too close to the ${MAX_DISK_PCT}% guard to start"
(
    peak=0
    while sleep 10; do
        now="$(disk_pct "$DOCKER_ROOT")"
        if (( now > peak )); then peak=$now; echo "$peak" > "$WORK/disk_peak"; fi
        if (( now > MAX_DISK_PCT || $(disk_pct "$WORK") > MAX_DISK_PCT )); then
            kill -USR1 $$; exit 0
        fi
    done
) &
WATCH_PID=$!

dec() { gpg --batch --quiet --homedir "$GH" --decrypt "$1"; }
pg_drill() { "${DC[@]}" exec -T postgres psql -U legba -d "${PGDB:-legba}" -XAtq -v ON_ERROR_STOP=1 "$@"; }

# ============================================================================
log "set $SET_ID ($ROOT) → throwaway project $PROJECT"

check_sums() {
    (cd "$ROOT" && sha256sum --quiet -c "sets/$SET_ID/SHA256SUMS") || fail "SHA256SUMS mismatch"
}
step sums check_sums

gpg --batch --quiet --homedir "$GH" --import "$SECRET_KEY" "$ESCROW_SECRET_KEY" 2>/dev/null
dec "$SET_DIR/manifest.json.gpg" > "$WORK/manifest.json" || refuse "the manifest does not decrypt with this key"
mj() { python3 -c "import json,sys; m=json.load(open('$WORK/manifest.json')); $1"; }
COMPS="$(mj 'print(" ".join(m["components"]))')"
log "components in the set: $COMPS"

stack_up() { "${DC[@]}" up -d --wait --quiet-pull >/dev/null; }
step stack_up stack_up
echo '{}' > "$WORK/restored.json"
put_restored() {  # put_restored GROUP NAME VALUE
    python3 - "$WORK/restored.json" "$@" <<'PY'
import json, sys
p, g, k, v = sys.argv[1:5]
d = json.load(open(p))
d.setdefault(g, {})[k] = None if v == "" else int(v)
json.dump(d, open(p, "w"))
PY
}

# The drill shares a disk with the live stack (OpenSearch goes read-only at
# 95%). Once a store's count is recorded, its restored copy is dropped so the
# Postgres restore (the big one) has the room; --keep keeps everything.
release() {  # release WHAT CMD...
    local what="$1"; shift
    [[ $KEEP == 1 ]] && return 0
    "$@" >/dev/null 2>&1 && log "released the restored $what (count recorded)" || true
}

# --- escrow ------------------------------------------------------------------
restore_escrow() {
    mkdir -p "$WORK/escrow_x"; chmod 700 "$WORK/escrow_x"
    if ! gpg --batch --quiet --homedir "$GH" --decrypt "$SET_DIR/escrow.tar.gpg" | tar -x -C "$WORK/escrow_x"; then
        fail "escrow does not decrypt with the escrow secret key"; return
    fi
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["complete"] else 1)' \
        "$WORK/escrow_x/escrow/inventory.json" || fail "escrow inventory incomplete"
    log "escrow: opens; inventory $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(", ".join(k for k,v in d["secrets_present"].items() if v))' "$WORK/escrow_x/escrow/inventory.json") present"
}
[[ " $COMPS " == *" escrow "* ]] && step escrow restore_escrow

# --- qdrant ------------------------------------------------------------------
restore_qdrant() {
    local c n
    for c in $(mj 'print(" ".join(m["components"]["qdrant"]["collections"]))'); do
        mkdir -p "$WORK/qdrant"
        dec "$SET_DIR/qdrant/$c.snapshot.zst.gpg" | zstd -q -dc > "$WORK/qdrant/$c.snapshot"
        curl -sf -X POST "$Q_URL/collections/$c/snapshots/upload?priority=snapshot&wait=true" \
            -F "snapshot=@$WORK/qdrant/$c.snapshot" >/dev/null || fail "qdrant: $c snapshot upload refused"
        rm -f "$WORK/qdrant/$c.snapshot"
        n="$(curl -sf -X POST "$Q_URL/collections/$c/points/count" -H 'content-type: application/json' \
            -d '{"exact": true}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["count"])' || true)"
        put_restored qdrant "$c" "$n"
        log "qdrant: $c → ${n:-absent} points"
        release "qdrant collection $c" curl -sf -X DELETE "$Q_URL/collections/$c"
    done
}
[[ " $COMPS " == *" qdrant "* ]] && step qdrant restore_qdrant

# --- opensearch --------------------------------------------------------------
restore_opensearch() {
    local idx n
    for idx in $(mj 'print(" ".join(m["components"]["opensearch"]["indices"]))'); do
        dec "$SET_DIR/opensearch/$idx.meta.json.gpg" > "$WORK/os_meta.json"
        dec "$SET_DIR/opensearch/$idx.ndjson.zst.gpg" | zstd -q -dc \
            | "${LIB[@]}" os-import "$OS_URL" "$WORK/os_meta.json" >/dev/null || fail "opensearch: $idx import failed"
        n="$("${LIB[@]}" os-count "$OS_URL" "$idx" || true)"
        put_restored opensearch "$idx" "$n"
        log "opensearch: $idx → ${n:-absent} docs"
        release "opensearch index $idx" curl -sf -X DELETE "$OS_URL/$idx"
    done
}
[[ " $COMPS " == *" opensearch "* ]] && step opensearch restore_opensearch

# --- postgres ----------------------------------------------------------------
restore_postgres() {
    # Restore into EMPTY databases cloned from template0: the image's init
    # creates the AGE extension in the default database, and the dump carries
    # its own `CREATE SCHEMA ag_catalog`. One transaction per database: with
    # wal_level=minimal the COPYs into tables created in that transaction skip
    # the WAL (the drill shares the live disk), and the first error aborts the
    # restore by name instead of leaving a half-restored database to count.
    PGDB=postgres pg_drill -c "DROP DATABASE IF EXISTS legba" \
        -c "CREATE DATABASE legba TEMPLATE template0" -c "CREATE DATABASE dapr TEMPLATE template0" >/dev/null
    if ! dec "$SET_DIR/postgres_legba.dump.gpg" \
        | "${DC[@]}" exec -T postgres pg_restore -U legba -d legba --single-transaction 2> "$WORK/pg_restore.log"; then
        fail "pg_restore (legba): $(grep 'error:' "$WORK/pg_restore.log" | head -3 | tr '\n' ' ')"
    fi
    if ! dec "$SET_DIR/postgres_dapr.dump.gpg" \
        | "${DC[@]}" exec -T postgres pg_restore -U legba -d dapr --single-transaction 2> "$WORK/pg_restore_dapr.log"; then
        fail "pg_restore (dapr): $(grep 'error:' "$WORK/pg_restore_dapr.log" | head -2 | tr '\n' ' ')"
    fi
    dec "$SET_DIR/postgres_globals.sql.gpg" > /dev/null || fail "globals do not decrypt"
    local list t line
    list="$(mj 'print(",".join("'"'"'%s'"'"'" % t for t in m["components"]["postgres"]["table_counts"]))')"
    pg_drill -c "SELECT t || '|' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %s', t), false, true, '')))[1]::text FROM unnest(ARRAY[$list]::text[]) AS t WHERE to_regclass(t) IS NOT NULL" \
        > "$WORK/pg_counts.txt"
    while IFS= read -r line; do
        t="${line%|*}"
        put_restored postgres "$t" "${line##*|}"
    done < "$WORK/pg_counts.txt"
    log "postgres: $(wc -l < "$WORK/pg_counts.txt") named tables counted on the restored database"
}
[[ " $COMPS " == *" postgres "* ]] && step postgres restore_postgres

# --- vault: one secret decrypts with the escrowed master key -----------------
vault_check() {
    [[ -f "$WORK/escrow_x/escrow/env" ]] || { fail "vault: no escrowed .env to read the master key from"; return; }
    local out
    out="$(docker run --rm --network host \
        -v "$WORK/escrow_x/escrow:/escrow:ro" -v "$SCRIPT_DIR:/drill:ro" \
        -e DRILL_PG_PORT="$PG_PORT" --entrypoint python "$VAULT_IMAGE" -c '
import asyncio, os, pathlib, sys
sys.path.insert(0, "/drill")
import asyncpg, backup_lib
from legba.data.registry.credentials import CredentialVault
key = backup_lib.env_value(pathlib.Path("/escrow/env").read_text(), "LEGBA_DATA_MASTER_KEY")
if not key:
    print("NO_MASTER_KEY"); sys.exit(1)
async def main():
    conn = await asyncpg.connect(host="127.0.0.1", port=int(os.environ["DRILL_PG_PORT"]),
                                 user="legba", password="legba", database="legba")
    row = await conn.fetchrow("SELECT secret_id, version, nonce, ciphertext FROM stack_credentials "
                              "WHERE is_current ORDER BY random() LIMIT 1")
    await conn.close()
    if row is None:
        print("NO_ROWS"); sys.exit(1)
    plain = CredentialVault(None, master_key=bytes.fromhex(key)).decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]))
    print(f"OK {row[0]} v{row[1]} {len(plain)} bytes")
asyncio.run(main())
' 2>&1 | tail -1)" || true
    if [[ "$out" == OK* ]]; then
        log "vault: secret ${out#OK } decrypts with the escrowed master key"
    else
        fail "vault: $out"
    fi
}
[[ " $COMPS " == *" escrow "* && " $COMPS " == *" postgres "* ]] && step vault vault_check

# --- archive: N random receipts re-hash -----------------------------------------
archive_sample() {
    mkdir -p "$WORK/lists" "$WORK/sample"
    local p
    for p in $(mj 'print(" ".join(m["components"]["archive"]["packs"]))'); do
        [[ -f "$ROOT/archive/$p.list.gpg" ]] || { fail "archive: pack $p missing from $ROOT/archive"; return; }
        dec "$ROOT/archive/$p.list.gpg" > "$WORK/lists/$p.list"
    done
    pg_drill -F $'\t' -c "SELECT object_ref, sha256 FROM evidence_archive WHERE object_ref LIKE 'cas:sha256/%' AND sha256 IS NOT NULL ORDER BY random() LIMIT $SAMPLE" \
        | python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import backup_lib
for ln in sys.stdin:
    ref, sha = ln.rstrip("\n").split("\t")
    rel = backup_lib.archive_relpath(ref)
    if rel:
        print(f"{rel}\t{sha}")' "$SCRIPT_DIR" > "$WORK/sample.tsv"
    cut -f1 "$WORK/sample.tsv" > "$WORK/sample.paths"
    "${LIB[@]}" pack-lookup "$WORK/lists" "$WORK/sample.paths" > "$WORK/lookup.json"
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); m=d.get("",[]); print("\n".join(m)); sys.exit(1 if m else 0)' \
        "$WORK/lookup.json" > "$WORK/unpacked.txt" || fail "archive: $(wc -l < "$WORK/unpacked.txt") sampled receipts are in no pack"
    for p in $(python3 -c 'import json,sys; print(" ".join(k for k in json.load(open(sys.argv[1])) if k))' "$WORK/lookup.json"); do
        python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))[sys.argv[2]]))' "$WORK/lookup.json" "$p" > "$WORK/members.txt"
        dec "$ROOT/archive/$p.tar.zst.gpg" | zstd -q -dc | tar -x -C "$WORK/sample" -T "$WORK/members.txt" 2> "$WORK/tar.err" \
            || fail "archive: extraction from $p failed: $(head -3 "$WORK/tar.err" | tr '\n' ' ')"
    done
    local res
    res="$("${LIB[@]}" verify-sample "$WORK/sample.tsv" "$WORK/sample")" || fail "archive sample: $res"
    SAMPLE_RESULT="$res"
    log "archive: $(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print("%d/%d receipts re-hash to their sha256" % (d["ok"], d["checked"]))' "$res")"
}
SAMPLE_RESULT='{}'
[[ " $COMPS " == *" archive "* && " $COMPS " == *" postgres "* ]] && step archive archive_sample

# --- the rest decrypts -----------------------------------------------------------
decrypt_rest() {
    local f
    for f in redis.rdb.tar.zst.gpg nats.tar.zst.gpg repo.tar.zst.gpg; do
        [[ -f "$SET_DIR/$f" ]] || continue
        dec "$SET_DIR/$f" | zstd -q -t || fail "$f does not decrypt + decompress"
    done
}
step decrypt_rest decrypt_rest

# --- verdict -------------------------------------------------------------------
"${LIB[@]}" verify "$WORK/manifest.json" "$WORK/restored.json" > "$WORK/verify.txt" || true
while IFS= read -r line; do [[ -n "$line" ]] && fail "${line#FAIL }"; done < "$WORK/verify.txt"

TOTAL=$(( $(date -u +%s) - T_START ))
RESTORE=$(( ${STEP_S[qdrant]:-0} + ${STEP_S[opensearch]:-0} + ${STEP_S[postgres]:-0} ))
echo "================================================================"
log "set $SET_ID: restore ${RESTORE} s (qdrant ${STEP_S[qdrant]:-0} + opensearch ${STEP_S[opensearch]:-0} + postgres ${STEP_S[postgres]:-0}); drill total ${TOTAL} s"
log "peak disk fill during the drill: $(cat "$WORK/disk_peak" 2>/dev/null || echo "not sampled")% (guard ${MAX_DISK_PCT}%)"
log "restored counts: $(cat "$WORK/restored.json")"
if (( ${#FAILS[@]} )); then
    log "VERDICT: FAIL (${#FAILS[@]})"
    printf '  - %s\n' "${FAILS[@]}"
    exit 1
fi
log "VERDICT: PASS"
"${LIB[@]}" status "$STATE_DIR" drill "$(python3 -c '
import datetime, json, sys
print(json.dumps({"last_drill_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                  "set_id": sys.argv[1], "restore_s": int(sys.argv[2]), "total_s": int(sys.argv[3]),
                  "sample": json.loads(sys.argv[4]), "restored": json.load(open(sys.argv[5]))}))' \
    "$SET_ID" "$RESTORE" "$TOTAL" "$SAMPLE_RESULT" "$WORK/restored.json")"
log "wrote $STATE_DIR/last_drill.json"
