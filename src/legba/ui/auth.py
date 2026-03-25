"""Simple JWT authentication for Legba UI.

Tier 1: users table, 3 roles (admin/analyst/viewer), JWT tokens in HttpOnly cookies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("legba.ui.auth")

# Simple JWT implementation (no external dependency)
# Uses HMAC-SHA256 for signing

SECRET_KEY: Optional[str] = None  # Set from env on startup

ROLES = {
    "admin": {"read", "write", "delete", "admin"},
    "analyst": {"read", "write"},
    "viewer": {"read"},
}


def init_auth(secret_key: str) -> None:
    """Initialize auth with the signing key."""
    global SECRET_KEY
    SECRET_KEY = secret_key


def hash_password(password: str) -> str:
    """Hash password with SHA-256 + random salt."""
    salt = os.urandom(16).hex()
    hashed = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{hashed}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored salt:hash."""
    salt, hashed = stored_hash.split(":", 1)
    candidate = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return hmac.compare_digest(candidate, hashed)


def create_token(username: str, role: str, expires_in: int = 86400) -> str:
    """Create a JWT-like token (HMAC-SHA256 signed).

    Parameters
    ----------
    username : str
        The subject (user) for the token.
    role : str
        The user's role (admin/analyst/viewer).
    expires_in : int
        Token lifetime in seconds (default 24h).

    Returns
    -------
    str
        A three-part base64url token: header.payload.signature
    """
    if SECRET_KEY is None:
        raise RuntimeError("Auth not initialized — call init_auth() first")
    payload = {
        "sub": username,
        "role": role,
        "exp": int(time.time()) + expires_in,
        "iat": int(time.time()),
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    signing_input = f"{header_b64}.{payload_b64}"
    signature = hmac.new(SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256).hexdigest()
    return f"{header_b64}.{payload_b64}.{signature}"


def verify_token(token: str) -> Optional[dict]:
    """Verify and decode a token.

    Returns the payload dict on success, or None on any failure
    (bad signature, expired, malformed).
    """
    if SECRET_KEY is None:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, signature = parts
        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
        # Pad base64 for decoding
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def has_permission(role: str, permission: str) -> bool:
    """Check if a role has the given permission."""
    return permission in ROLES.get(role, set())


# ---------------------------------------------------------------------------
# Database helpers (users table)
# ---------------------------------------------------------------------------

async def ensure_users_table(pool) -> None:
    """Create the users table if it does not exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_login TIMESTAMPTZ
            );
        """)


async def seed_default_admin(pool) -> None:
    """Insert a default admin user if no users exist.

    Default credentials: admin / legba-admin
    Should be changed on first login in production.
    """
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count == 0:
            pw_hash = hash_password("legba-admin")
            await conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3)",
                "admin",
                pw_hash,
                "admin",
            )
            logger.info("Seeded default admin user (username: admin)")


async def authenticate_user(pool, username: str, password: str) -> Optional[dict]:
    """Authenticate a user against the database.

    Returns a dict with user info on success, None on failure.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, password_hash, role FROM users WHERE username = $1",
            username,
        )
        if row is None:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        # Update last_login
        await conn.execute(
            "UPDATE users SET last_login = NOW() WHERE id = $1",
            row["id"],
        )
        return {
            "id": str(row["id"]),
            "username": row["username"],
            "role": row["role"],
        }
