# Evidence archive tiering (hot → cold)

For someone whose evidence archive is outgrowing the host's disk. How the
archive keeps recent bytes on the host and retires older bytes to blob storage
that can live anywhere, how to verify and restore them, and how to move the
cold tier to another endpoint. The code is
[`src/legba/data/archive_tier.py`](../../src/legba/data/archive_tier.py),
[`src/legba/data/archive_store.py`](../../src/legba/data/archive_store.py) and
the script [`scripts/archive_tier.py`](../../scripts/archive_tier.py)..

## The rules

* **Compressed in place.** Every hot object rests zstd-compressed under
  `{LEGBA_ARCHIVE_ROOT}/{sha[:2]}/{sha}`; reads return the original bytes,
  re-verified against the address (`scripts/archive_compact.py` did the
  one-off compaction).
* **At least 90 days hot.** `LEGBA_ARCHIVE_HOT_DAYS` (default `90`). An object
  is a cold candidate only when EVERY row that references it is older than
  the window: `evidence_archive.sha256`/`media_sha256` (age =
  `coalesce(archived_at, updated_at)`) and every `fetch_versions` row still
  holding the bytes (age = `last_seen_at`). A page re-cited yesterday stays
  hot however old its first citation is. The rule is re-checked per object
  just before its hot copy is removed.
* **Referenced bytes are retired, never deleted.** Nothing in the tier deletes
  from the cold backend. Unreferenced fetch-receipt bytes are pruned by
  `scripts/fetch_receipts_prune.py` (hash, metadata and extracted text kept);
  uncited archive rows by the `archive_retention` handler (off by default).
* **The database is the ledger.** `evidence_archive_bundles` (one row per
  uploaded, read-back-verified bundle) and `evidence_archive_cold` (one row per
  content address: bundle, byte range, codec, `hot_removed_at`), migration
  0243. A bundle that is uploaded but not recorded is never read.

## Configuration

| Variable | Meaning |
| --- | --- |
| `LEGBA_ARCHIVE_COLD_BACKEND` | `s3` (any S3-compatible store) or `local` (a directory, e.g. a second disk). Unset = no cold tier; the mover refuses. |
| `LEGBA_ARCHIVE_S3_ENDPOINT` | e.g. `http://seaweedfs:8333` (path-style by default, `LEGBA_ARCHIVE_S3_PATH_STYLE=0` for virtual-host style). |
| `LEGBA_ARCHIVE_S3_BUCKET` / `_PREFIX` / `_REGION` | bucket (create it once by hand), key prefix (default `legba-archive`), region. |
| `LEGBA_ARCHIVE_S3_ACCESS_KEY_ID` / `LEGBA_ARCHIVE_S3_SECRET_ACCESS_KEY` | credentials, in `.env` only (the `_ACCESS_KEY_ENV` / `_SECRET_KEY_ENV` knobs rename them). |
| `LEGBA_ARCHIVE_COLD_LOCAL_ROOT` | the directory for `local`. |
| `LEGBA_ARCHIVE_HOT_DAYS` | the hot window, default 90. Do not set it below 90. |
| `LEGBA_ARCHIVE_PACK_MB` | bundle size at rest, default 512. |

**Readers need the same backend env as the mover.** Every reader goes through
`EvidenceStore.from_env` or `archive_tier.read_original`, which read a retired
object from its bundle with one ranged GET: the archiver's ingest-copy lookup,
the instrument snapshot loader, the fetch-receipt reader
(`fetch_receipts.read_version_body`). A container that reads the archive but
lacks the cold env sees retired objects as absent. Readers find an object
either through the database or, with no database at hand, through the local
cold index `{LEGBA_ARCHIVE_ROOT}/cold-index/<sha[:2]>.tsv` (appended by the
mover after each database record; about 170 bytes per retired object).

A cold object that is recorded but cannot be read RAISES, it never reads as
absent: `ColdTierUnavailable` (endpoint unreachable; connect timeout 5 s,
3 attempts) or `ColdObjectInvalid` (bundle missing, member does not verify).
The archiver logs `evidence_archiver.ingest_copy.cold_unreadable` and falls
back to its re-fetch; the snapshot loader reports the diff unavailable with
the reason. Nothing is ever marked lost by a failed read.

## Running a tiering pass

Inside the runtime image, with the archive volume and the database env (the
container must also carry the cold-backend env):

```bash
docker run --rm --network legba_default --env-file .env \
  -v legba_archive:/var/lib/legba/archive -v "$PWD/scripts:/s:ro" \
  --entrypoint python legba/legba-runtime-dapr:latest /s/archive_tier.py          # plan (dry run)
# ... the same with --apply                                                         # move
```

The dry run prints candidates per month, bytes at rest and bundles planned,
and writes nothing. `--apply` does, per bundle: verify every hot object
against its address (a mismatch is reported and not packed) → build the tar →
verify it locally → upload (the key is the tar's sha256, so a re-upload is a
no-op) → download it back in full and verify every member → record bundle and
locations in one transaction → append the local index → per object, re-check
the hot window and remove the hot copy. Useful flags: `--limit N` (objects
considered per run, default 20 000), `--keep-hot` (copy only),
`--spool DIR` (bundle build space: needs about twice `LEGBA_ARCHIVE_PACK_MB`
free), `--hot-days N` (override; never below 90 on live).

Exit code 0 = clean, 1 = something failed (listed in the JSON), 2 = refused.

**Interrupted runs.** Killed before the record: nothing is recorded, every hot
copy is intact, an uploaded bundle (if any) is unrecorded and ignored; the
next run rebuilds and, if the bundle bytes are identical, reuses the key. If
the bytes sitting at that key are damaged (an upload corrupted in flight), the
mover replaces them — the single exception to never-overwrite, allowed only
for an object that does not hash to its own key and only with bytes that do.
Killed after the record: the hot copies that were not yet removed are removed
by the next `--apply` (`resumed_checked` in its report).

## Verifying

```bash
... /s/archive_tier.py --verify
```

Downloads every recorded bundle in full and checks: the bundle's sha256, every
recorded member against its address, the `.index.json` stored beside it
against the database, and the member counts. Read-only. Run it after every
pass that moved anything, and monthly. It costs one full download of the cold
tier.

## Restoring from cold

* One object: nothing to do — a read returns it. To put it back on the host:
  `--restore <sha256> [<sha256> ...]`.
* A month: `--restore-month YYYY-MM`. Everything: `--restore-all`.

Each restored object is re-verified before it is written, and
`hot_removed_at` is cleared. A later `--apply` retires restored objects again
from the existing bundles (no new upload) once they are outside the window.
Without any Legba code a bundle is a plain tar: `tar -xf <bundle>.tar` gives
`objects/<sha[:2]>/<sha>` (each a zstd frame: `zstd -d`) and `index.json`.

## Moving the cold tier to another endpoint

Bundle keys are content hashes, so the database never changes; only the
endpoint does.

1. Stand up the new store and create the bucket.
2. Describe the OLD backend under a prefix and the NEW one as the normal env:
   `LEGBA_ARCHIVE_SRC_COLD_BACKEND=s3 LEGBA_ARCHIVE_SRC_S3_ENDPOINT=<old>
   LEGBA_ARCHIVE_SRC_S3_BUCKET=<old bucket> ...` plus
   `LEGBA_ARCHIVE_COLD_BACKEND=s3 LEGBA_ARCHIVE_S3_ENDPOINT=<new> ...`.
3. `--copy-from LEGBA_ARCHIVE_SRC_` copies every recorded bundle and its index
   and verifies each copy on the new endpoint (idempotent; a bundle that does
   not verify at the SOURCE is reported, not copied).
4. `--verify` against the new endpoint, then repoint every container's env
   and roll. Keep the old store until a `--verify` after the roll is clean.

If the database's local index ever disagrees, `--rebuild-index` rewrites
`cold-index/` from `evidence_archive_cold`.

## The object store (SeaweedFS)

A ready compose patch adds SeaweedFS under the `coldstore` profile:
[`deploy/patches/seaweedfs-coldstore.patch`](../../deploy/patches/seaweedfs-coldstore.patch)
(not applied). Settings that matter:

* Give it 1 GiB of memory and `GOMEMLIMIT=640MiB`. At 512 MiB it can be
  killed for memory during a full read-back.
* Run it with `-master.volumePreallocate=false`. With preallocation it
  reserves whole 512 MB volumes regardless of how much data they hold.
* The credentials file `deploy/seaweedfs/s3.json` is gitignored; generate it
  with a random key pair (`{"identities":[{"name":"legba-archive",
  "credentials":[{"accessKey":"…","secretKey":"…"}],"actions":["Admin","Read",
  "Write","List","Tagging"]}]}`, mode 600) and put the same pair in `.env`.

## Recommended schedule

* Daily: `scripts/fetch_receipts_prune.py --apply` once its dry run has been
  read (not before).
* Weekly (when tiering is live): `archive_tier.py --apply`, then `--verify` if
  anything moved.
* Monthly: a full `--verify`.

## When to start live tiering

Compression in place usually buys a host a long time before it needs a cold
tier. Measure the archive's size at rest and its monthly growth against free
disk, and start live tiering when free disk will run short within a few months
at that rate, or when an off-machine endpoint becomes available; a cold tier
on the same disk only moves bytes. Before turning it on, run a full pass on a
copy of the archive in a throwaway stack: plan, apply, `--verify`, read every
retired object back, then `--restore-all`. The first live pass retires the
oldest month whose citing rows are all older than the hot window (90 days by
default).
