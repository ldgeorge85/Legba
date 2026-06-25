# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the runtime bootstrap of the audit checkpointer.

Wires :func:`legba.runtime.audit_checkpointer_wiring.start_audit_checkpointer`
against the live ``legba-postgres`` container (via the session-scoped
``migrated_pg`` fixture) and verifies:

  1. The checkpointer's asyncio loop wakes on its configured interval
     and writes one row per analyst per head-change to
     ``audit_checkpoints``.
  2. Each row's signature validates against the signer's public key
     (canonical-JSON form, raw 64-byte Ed25519 signature).
  3. ``stop()`` cleanly cancels the task — no further rows land after
     stop even when fresh traces arrive.

The signer defaults to the descriptor-audit-log identity
(:func:`legba.data.registry.signing.load_default_identity`), bridged
into the checkpointer's :class:`Ed25519Signer` shape. Tests pass an
explicit signer with a deterministic seed so the verify step is
reproducible run-to-run.

Real substrate per Lewis's no-mocks rule — no fakes, no in-memory
stand-in for asyncpg.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from nacl.signing import SigningKey

from legba.data.provenance.checkpointer import Ed25519Signer
from legba.runtime.audit_checkpointer_wiring import (
    signer_from_registry_identity,
    start_audit_checkpointer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def deterministic_signer() -> Ed25519Signer:
    """Per-test signer with a fresh 32-byte seed.

    Each test gets a fresh seed so signatures from one test can't
    accidentally verify against another test's verify key.
    """
    seed = SigningKey.generate().encode()
    return Ed25519Signer(
        bytes(seed),
        did="did:legba:runtime:checkpointer-test",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_trace(
    conn: asyncpg.Connection,
    *,
    analyst_id: str,
    analyst_version: str,
    receipt_hash: str,
) -> None:
    """Insert a minimal analyst_traces row.

    Only the columns the checkpointer reads (``analyst_id``,
    ``receipt_hash``, ``run_started_at``) need realistic values; the
    rest are smoke-defaults.
    """
    await conn.execute(
        """
        INSERT INTO analyst_traces (
            run_id, analyst_id, analyst_version, cadence_trigger,
            input_row_refs, intermediate_steps, llm_calls, tool_calls,
            output_row_refs, output_payload, status,
            run_started_at, run_ended_at, receipt_hash
        )
        VALUES (
            $1, $2, $3, 'manual',
            '{}'::UUID[], '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
            '{}'::UUID[], $4::jsonb, 'success',
            NOW(), NOW(), $5
        )
        """,
        uuid4(), analyst_id, analyst_version,
        json.dumps({"smoke": True}), receipt_hash,
    )


def _canonical_payload(
    *,
    analyst_id: str,
    chain_head_hash: str,
    trace_count: int,
    checkpointed_at,
    signer_did: str,
) -> bytes:
    """Rebuild the canonical-JSON payload the checkpointer signed.

    Mirrors :meth:`AuditCheckpointer._sign_and_write` byte-for-byte so
    the verify path uses the same form.
    """
    from legba.data.provenance import canonical_json

    return canonical_json({
        "analyst_id": analyst_id,
        "chain_head_hash": chain_head_hash,
        "trace_count": trace_count,
        "checkpointed_at": checkpointed_at.isoformat(),
        "signer_did": signer_did,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signer_from_registry_identity_roundtrips() -> None:
    """The bridge preserves the signing key — a signature produced by the
    bridged Ed25519Signer verifies against the original SigningIdentity's
    verify_key."""
    from legba.data.registry.signing import SigningIdentity

    sk = SigningKey.generate()
    identity = SigningIdentity(signing_key=sk, signer_did="did:legba:test:bridge")

    signer = signer_from_registry_identity(identity)
    assert signer.did == identity.signer_did

    payload = b"some canonical-JSON body"
    signature = signer.sign(payload)
    # Verify with the ORIGINAL verify key — same underlying nacl key
    # means the bridge didn't accidentally re-seed.
    identity.verify_key.verify(payload, signature)  # raises BadSignatureError on mismatch


@pytest.mark.asyncio
async def test_checkpointer_writes_rows_on_loop(
    pg_pool, deterministic_signer
) -> None:
    """Start with 0.5s interval, insert traces, observe ≥2 rows landed."""
    analyst_id = f"test_analyst_{uuid4().hex[:8]}"
    analyst_version = "ff" * 8

    # Seed the table with an initial trace so the first tick has a head
    # to sign.
    async with pg_pool.acquire() as conn:
        await _insert_trace(
            conn,
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            receipt_hash=hashlib.sha256(b"seed").hexdigest(),
        )

    checkpointer = await start_audit_checkpointer(
        pg_pool,
        signer=deterministic_signer,
        interval_seconds=0.5,
    )
    try:
        # First tick should fire within ~0.5s and pick up the seed trace.
        # Then insert a second distinct head while the loop is running so
        # the next tick (~1.0s mark) picks up a head_changed signal and
        # writes again. Cumulative: ≥2 rows by ~2s.
        await asyncio.sleep(0.7)

        async with pg_pool.acquire() as conn:
            await _insert_trace(
                conn,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                receipt_hash=hashlib.sha256(b"second").hexdigest(),
            )

        # Wait long enough for at least two more wake-ups beyond the
        # initial one (interval = 0.5s).
        await asyncio.sleep(1.5)
    finally:
        await checkpointer.stop()

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, analyst_id, chain_head_hash, trace_count,
                   checkpointed_at, signature, signer_did
            FROM audit_checkpoints
            WHERE analyst_id = $1
            ORDER BY checkpointed_at ASC
            """,
            analyst_id,
        )

    assert len(rows) >= 2, (
        f"expected ≥2 checkpoint rows after 2.2s on a 0.5s interval, "
        f"got {len(rows)}"
    )
    # Two distinct heads → two distinct chain_head_hash values.
    heads = {r["chain_head_hash"] for r in rows}
    assert len(heads) >= 2, (
        f"expected at least 2 distinct chain heads in the checkpoints, "
        f"got {heads}"
    )
    # Signer DID is stamped on every row.
    for r in rows:
        assert r["signer_did"] == deterministic_signer.did
        assert isinstance(r["signature"], (bytes, memoryview))
        # Raw ed25519 signature is 64 bytes.
        assert len(bytes(r["signature"])) == 64


@pytest.mark.asyncio
async def test_checkpoint_signature_validates_against_public_key(
    pg_pool, deterministic_signer
) -> None:
    """Each row's signature verifies against the signer's public key."""
    analyst_id = f"test_analyst_verify_{uuid4().hex[:8]}"
    analyst_version = "ff" * 8

    async with pg_pool.acquire() as conn:
        await _insert_trace(
            conn,
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            receipt_hash=hashlib.sha256(b"v1").hexdigest(),
        )

    checkpointer = await start_audit_checkpointer(
        pg_pool,
        signer=deterministic_signer,
        interval_seconds=0.3,
    )
    try:
        await asyncio.sleep(0.6)
    finally:
        await checkpointer.stop()

    verify_key = deterministic_signer.public_key()

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT analyst_id, chain_head_hash, trace_count,
                   checkpointed_at, signature, signer_did
            FROM audit_checkpoints
            WHERE analyst_id = $1
            ORDER BY checkpointed_at ASC
            """,
            analyst_id,
        )

    assert len(rows) >= 1
    for r in rows:
        payload = _canonical_payload(
            analyst_id=r["analyst_id"],
            chain_head_hash=r["chain_head_hash"],
            trace_count=int(r["trace_count"]),
            checkpointed_at=r["checkpointed_at"],
            signer_did=r["signer_did"],
        )
        # nacl VerifyKey.verify raises BadSignatureError on mismatch.
        verify_key.verify(payload, bytes(r["signature"]))


@pytest.mark.asyncio
async def test_stop_halts_loop_no_new_rows(
    pg_pool, deterministic_signer
) -> None:
    """After ``stop()``, no further rows land even when new traces arrive."""
    analyst_id = f"test_analyst_stop_{uuid4().hex[:8]}"
    analyst_version = "ff" * 8

    async with pg_pool.acquire() as conn:
        await _insert_trace(
            conn,
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            receipt_hash=hashlib.sha256(b"pre").hexdigest(),
        )

    checkpointer = await start_audit_checkpointer(
        pg_pool,
        signer=deterministic_signer,
        interval_seconds=0.3,
    )
    # Let one tick land.
    await asyncio.sleep(0.6)

    await checkpointer.stop()

    async with pg_pool.acquire() as conn:
        pre_stop_count = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_checkpoints WHERE analyst_id = $1",
            analyst_id,
        )

    # Insert a fresh trace AFTER stop. If the loop were still running it
    # would pick this up on its next interval and write another row.
    async with pg_pool.acquire() as conn:
        await _insert_trace(
            conn,
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            receipt_hash=hashlib.sha256(b"post-stop").hexdigest(),
        )

    # Wait three full intervals to give a still-running loop ample time
    # to wake and write.
    await asyncio.sleep(1.2)

    async with pg_pool.acquire() as conn:
        post_stop_count = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_checkpoints WHERE analyst_id = $1",
            analyst_id,
        )

    assert post_stop_count == pre_stop_count, (
        f"expected count unchanged after stop, got "
        f"pre={pre_stop_count} post={post_stop_count}"
    )
    # And confirm the loop's task is really gone.
    assert checkpointer._task is None
