# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X1-A2 — the key escrow, end to end through scripts/backup.sh, with a
THROWAWAY OpenPGP key pair generated in the test (never a real key).

  * no public key → refused (exit 2), nothing written;
  * an .env without the vault master key → refused (exit 2), the partial set
    removed;
  * a complete .env → a sealed escrow.tar.gpg that only the throwaway secret
    key opens, carrying the .env byte-for-byte and a names-only inventory; the
    set's manifest is encrypted too and the cleartext files carry no secret.

Needs gpg on PATH (the host has it; the slim test image does not — there the
module skips with that reason). Run on the host with:
  python3 -m pytest -p no:cacheprovider --noconftest -c /dev/null tests/test_backup_escrow.py
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import tarfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKUP = REPO / "scripts" / "backup.sh"
MASTER = "5a" * 32
SIGNER = "6b" * 32

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("zstd") is None,
    reason="gpg + zstd are host tools; the slim test image carries neither",
)


@pytest.fixture
def keypair(tmp_path):
    gh = tmp_path / "gnupg"
    gh.mkdir(mode=0o700)
    base = ["gpg", "--batch", "--quiet", "--homedir", str(gh)]
    subprocess.run(base + ["--passphrase", "", "--pinentry-mode", "loopback",
                           "--quick-gen-key", "x1-a2-escrow-test", "future-default",
                           "default", "never"], check=True, capture_output=True)
    pub = tmp_path / "pub.asc"
    pub.write_bytes(subprocess.run(base + ["--export", "-a"], check=True,
                                   capture_output=True).stdout)
    return gh, pub


def _run(tmp_path, env_text: str | None, pub: pathlib.Path | None):
    envf = tmp_path / "dot.env"
    if env_text is not None:
        envf.write_text(env_text)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LEGBA_ENV_FILE": str(envf),
        "LEGBA_BACKUP_DEST": str(tmp_path / "dest"),
        "LEGBA_BACKUP_STATE_DIR": str(tmp_path / "state"),
        "LEGBA_BACKUP_MAX_DISK_PCT": "100",
    }
    if pub is not None:
        env["LEGBA_BACKUP_PUBKEY"] = str(pub)
    return subprocess.run(["bash", str(BACKUP), "escrow"], env=env,
                          capture_output=True, text=True, timeout=120)


def _sets(tmp_path):
    d = tmp_path / "dest" / "sets"
    return sorted(p for p in d.iterdir()) if d.exists() else []


def test_refuses_without_a_public_key(tmp_path):
    r = _run(tmp_path, f"LEGBA_DATA_MASTER_KEY={MASTER}\n", None)
    assert r.returncode == 2, r.stderr
    assert "LEGBA_BACKUP_PUBKEY" in r.stderr
    assert _sets(tmp_path) == []


def test_refuses_an_incomplete_escrow(tmp_path, keypair):
    _, pub = keypair
    r = _run(tmp_path, f"LEGBA_REGISTRY_SIGNING_KEY={SIGNER}\n", pub)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "LEGBA_DATA_MASTER_KEY" in r.stderr
    assert _sets(tmp_path) == []  # the partial set is removed
    assert not (tmp_path / "state" / "last_backup.json").exists()


def test_escrow_seals_the_env_to_the_throwaway_key(tmp_path, keypair):
    gh, pub = keypair
    env_text = (f"LEGBA_DATA_MASTER_KEY={MASTER}\n"
                f"LEGBA_REGISTRY_SIGNING_KEY={SIGNER}\nOTHER=1\n")
    r = _run(tmp_path, env_text, pub)
    assert r.returncode == 0, r.stdout + r.stderr
    assert MASTER not in r.stdout + r.stderr and SIGNER not in r.stdout + r.stderr
    (set_dir,) = _sets(tmp_path)
    names = sorted(p.name for p in set_dir.iterdir())
    assert names == ["SHA256SUMS", "escrow.tar.gpg", "manifest.json.gpg", "set.json"]
    for clear in ("SHA256SUMS", "set.json"):
        body = (set_dir / clear).read_text()
        assert MASTER not in body and SIGNER not in body
    raw = (set_dir / "escrow.tar.gpg").read_bytes()
    assert MASTER.encode() not in raw  # encrypted at rest

    def dec(p):
        return subprocess.run(["gpg", "--batch", "--quiet", "--homedir", str(gh),
                               "--decrypt", str(p)], check=True,
                              capture_output=True).stdout

    with tarfile.open(fileobj=io.BytesIO(dec(set_dir / "escrow.tar.gpg"))) as tf:
        members = sorted(tf.getnames())
        assert members == ["escrow/env", "escrow/inventory.json"]
        assert tf.extractfile("escrow/env").read().decode() == env_text
        inv = json.loads(tf.extractfile("escrow/inventory.json").read())
    assert inv["complete"] is True
    assert inv["secrets_present"]["LEGBA_DATA_MASTER_KEY"] is True
    assert MASTER not in json.dumps(inv)
    manifest = json.loads(dec(set_dir / "manifest.json.gpg"))
    assert manifest["components"]["escrow"]["inventory"]["complete"] is True
    status = json.loads((tmp_path / "state" / "last_backup.json").read_text())
    assert status["set_id"] == set_dir.name and status["last_backup_at"]

    # a stranger's key cannot open it
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    r2 = subprocess.run(["gpg", "--batch", "--quiet", "--homedir", str(other),
                         "--decrypt", str(set_dir / "escrow.tar.gpg")], capture_output=True)
    assert r2.returncode != 0
