# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-shot vault-loader for the bring-up.

Promotes selected .env keys into the legba credential vault via the
registry HTTP API. Idempotent: secrets that already exist are skipped
with a soft note rather than raising. Never echoes plaintext.

Usage:
    python3 scripts/bringup_vault_load.py
"""
from __future__ import annotations

import os
import sys
import httpx

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _token import resolve_token  # noqa: E402
from pathlib import Path


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# Mapping: vault secret_id -> .env key that holds the plaintext.
MAPPING = [
    ("llm.primary.api_key",          "OPENAI_API_KEY"),
    ("llm.anthropic.api_key",         "CONSULT_API_KEY"),
    # Cross-family judge endpoints (2026-07-30): OpenRouter free tier +
    # Cerebras PAYG. Consumed by the llm.judge.* stack components only.
    ("llm.judge.openrouter.api_key",  "OPENROUTER_API_KEY"),
    ("llm.judge.cerebras.api_key",    "CEREBRAS_API_KEY"),
    # ACLED OAuth2 password grant — username (the account email) + password.
    ("source.acled.username",         "ACLED_USERNAME"),
    ("source.acled.password",         "ACLED_PASSWORD"),
    ("source.fred.api_key",           "FRED_API_KEY"),
    ("source.nvd.api_key",            "NVD_API_KEY"),
    ("source.firms.map_key",          "FIRMS_MAP_KEY"),
    ("source.comtrade.primary_key",   "COMTRADE_PRIMARY_KEY"),
    ("source.event_registry.api_key", "EVENT_REGISTRY_API_KEY"),
    # Hosted Legba-models NLP service (translate/classify/extract/summarize) —
    # basic-auth user+pass; required by the nlp.local.legba_models stack
    # component so the ner_multilingual/classify/geocode baseline filters
    # install (without these, signals land raw — no geo/tags).
    ("nlp.local.legba_models.api_user", "MODELS_API_USER"),
    ("nlp.local.legba_models.api_pass", "MODELS_API_PASS"),
    # S-2 keyed source descriptors (descriptors/source_*.yaml). Each file
    # declares these vault ids in deps.vault_secrets and ships state: draft;
    # the operator loads the keys here, then flips the descriptor active
    # (see each descriptor header for the exact transition calls).
    ("source.acled.api_key",            "ACLED_API_KEY"),
    ("source.gdelt.bigquery_sa",        "GDELT_BQ_SERVICE_ACCOUNT_JSON"),
    ("source.mediacloud.api_key",       "MEDIACLOUD_API_KEY"),
    ("source.opensanctions.api_key",    "OPENSANCTIONS_API_KEY"),
    ("source.telegram.api_id",          "TELEGRAM_API_ID"),
    ("source.telegram.api_hash",        "TELEGRAM_API_HASH"),
    ("source.telegram.session",         "TELEGRAM_SESSION_B64"),
]

BASE = os.environ.get(
    "LEGBA_REGISTRY_URL",
    "http://127.0.0.1:8090/api/v1/registry",
)
TOKEN = resolve_token()


def already_exists(secret_id: str) -> bool:
    r = httpx.get(
        f"{BASE}/vault/secrets/{secret_id}/exists",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    r.raise_for_status()
    return bool(r.json().get("exists"))


def store(secret_id: str, plaintext: str, notes: str | None) -> dict:
    r = httpx.post(
        f"{BASE}/vault/secrets",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"secret_id": secret_id, "plaintext": plaintext, "notes": notes},
        timeout=15,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"failed to store {secret_id!r}: HTTP {r.status_code} {r.text[:200]}"
        )
    return r.json()


def main() -> int:
    # Repo-relative by default (works on any clone); LEGBA_ENV_FILE overrides.
    env_path = Path(
        os.environ.get("LEGBA_ENV_FILE")
        or Path(__file__).resolve().parents[1] / ".env"
    )
    env = parse_env(env_path)
    failures: list[str] = []
    loaded: list[str] = []
    skipped: list[str] = []
    absent: list[str] = []

    for secret_id, env_key in MAPPING:
        plaintext = env.get(env_key)
        if not plaintext:
            # An unset key is not an error: the vault loads what the operator
            # provided, and every consumer degrades on a missing credential.
            # Failures are reserved for the vault actually rejecting a store.
            absent.append(f"{secret_id} (env {env_key} unset)")
            continue
        try:
            if already_exists(secret_id):
                skipped.append(secret_id)
                continue
            store(secret_id, plaintext, notes=f"loaded from .env:{env_key}")
            loaded.append(secret_id)
        except Exception as exc:
            failures.append(f"{secret_id}: {exc}")

    print("Loaded:")
    for s in loaded:
        print(f"  + {s}")
    print("Skipped (already present):")
    for s in skipped:
        print(f"  = {s}")
    if absent:
        print("Absent (env key unset — consumer degrades):")
        for s in absent:
            print(f"  - {s}")
    if failures:
        print("Failures:")
        for s in failures:
            print(f"  ! {s}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
