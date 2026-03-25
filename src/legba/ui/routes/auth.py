"""Auth API routes — login, logout, current user, user management.

POST /api/v2/auth/login         — authenticate, set JWT cookie
POST /api/v2/auth/logout        — clear cookie
GET  /api/v2/auth/me            — current user info
PUT  /api/v2/auth/password      — change own password
GET  /api/v2/auth/users         — list users (admin only)
POST /api/v2/auth/users         — create user (admin only)
PUT  /api/v2/auth/users/{id}    — update user role (admin only)
DELETE /api/v2/auth/users/{id}  — delete user (admin only)
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from ..app import get_stores
from ..auth import authenticate_user, create_token, verify_token, hash_password, verify_password, ROLES
from ..responses import api_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/auth", tags=["auth"])

TOKEN_COOKIE_NAME = "legba_token"
TOKEN_MAX_AGE = 86400  # 24 hours


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


@router.post("/login")
async def login(request: Request, body: LoginRequest):
    """Authenticate and set JWT cookie."""
    stores = get_stores(request)
    if not stores.structured._available:
        return api_error(503, "Database unavailable")

    user = await authenticate_user(
        stores.structured._pool, body.username, body.password
    )
    if user is None:
        return api_error(401, "Invalid username or password")

    token = create_token(user["username"], user["role"], expires_in=TOKEN_MAX_AGE)
    response = JSONResponse(
        content={
            "user": {
                "username": user["username"],
                "role": user["role"],
            }
        }
    )
    response.set_cookie(
        key=TOKEN_COOKIE_NAME,
        value=token,
        max_age=TOKEN_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Set True behind TLS termination
        path="/",
    )
    return response


@router.post("/logout")
async def logout():
    """Clear the JWT cookie."""
    response = JSONResponse(content={"message": "Logged out"})
    response.delete_cookie(
        key=TOKEN_COOKIE_NAME,
        path="/",
    )
    return response


@router.get("/me")
async def me(request: Request):
    """Return current user info from JWT cookie."""
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    if not token:
        return api_error(401, "Not authenticated")

    payload = verify_token(token)
    if payload is None:
        return api_error(401, "Invalid or expired token")

    role = payload.get("role", "viewer")
    permissions = sorted(ROLES.get(role, ROLES["viewer"]))

    return JSONResponse(
        content={
            "user": {
                "username": payload.get("sub"),
                "role": role,
                "permissions": permissions,
            }
        }
    )


def _get_user_from_request(request: Request) -> dict | None:
    """Extract and verify user from JWT cookie."""
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    if not token:
        return None
    return verify_token(token)


def _require_admin(request: Request):
    """Check that the current user is an admin."""
    user = _get_user_from_request(request)
    if not user or user.get("role") != "admin":
        return None
    return user


@router.put("/password")
async def change_password(request: Request, body: PasswordChangeRequest):
    """Change own password."""
    user = _get_user_from_request(request)
    if not user:
        return api_error(401, "Not authenticated")

    stores = get_stores(request)
    pool = stores.structured._pool

    # Verify current password
    db_user = await authenticate_user(pool, user["sub"], body.current_password)
    if db_user is None:
        return api_error(400, "Current password is incorrect")

    # Update password
    new_hash = hash_password(body.new_password)
    await pool.execute(
        "UPDATE users SET password_hash = $1 WHERE username = $2",
        new_hash, user["sub"],
    )
    logger.info("Password changed for user %s", user["sub"])
    return JSONResponse({"message": "Password updated"})


@router.get("/users")
async def list_users(request: Request):
    """List all users (admin only)."""
    if not _require_admin(request):
        return api_error(403, "Admin access required")

    stores = get_stores(request)
    rows = await stores.structured._pool.fetch(
        "SELECT id, username, role, created_at, last_login FROM users ORDER BY created_at"
    )
    return JSONResponse({
        "users": [
            {
                "id": str(r["id"]),
                "username": r["username"],
                "role": r["role"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "last_login": r["last_login"].isoformat() if r["last_login"] else None,
            }
            for r in rows
        ]
    })


@router.post("/users")
async def create_user(request: Request, body: CreateUserRequest):
    """Create a new user (admin only)."""
    if not _require_admin(request):
        return api_error(403, "Admin access required")

    if body.role not in ROLES:
        return api_error(400, f"Invalid role. Must be one of: {', '.join(ROLES.keys())}")

    if len(body.password) < 8:
        return api_error(400, "Password must be at least 8 characters")

    stores = get_stores(request)
    pool = stores.structured._pool

    # Check if username exists
    existing = await pool.fetchval(
        "SELECT id FROM users WHERE username = $1", body.username
    )
    if existing:
        return api_error(409, f"Username '{body.username}' already exists")

    from uuid import uuid4
    user_id = uuid4()
    pw_hash = hash_password(body.password)
    await pool.execute(
        "INSERT INTO users (id, username, password_hash, role) VALUES ($1, $2, $3, $4)",
        user_id, body.username, pw_hash, body.role,
    )
    logger.info("Created user %s with role %s", body.username, body.role)
    return JSONResponse({"user_id": str(user_id), "username": body.username, "role": body.role}, status_code=201)


@router.put("/users/{user_id}")
async def update_user(request: Request, user_id: str, body: UpdateUserRequest):
    """Update a user's role or password (admin only)."""
    if not _require_admin(request):
        return api_error(403, "Admin access required")

    stores = get_stores(request)
    pool = stores.structured._pool

    uid = UUID(user_id)
    existing = await pool.fetchrow("SELECT username FROM users WHERE id = $1", uid)
    if not existing:
        return api_error(404, "User not found")

    if body.role:
        if body.role not in ROLES:
            return api_error(400, f"Invalid role. Must be one of: {', '.join(ROLES.keys())}")
        await pool.execute("UPDATE users SET role = $1 WHERE id = $2", body.role, uid)

    if body.password:
        if len(body.password) < 8:
            return api_error(400, "Password must be at least 8 characters")
        pw_hash = hash_password(body.password)
        await pool.execute("UPDATE users SET password_hash = $1 WHERE id = $2", pw_hash, uid)

    logger.info("Updated user %s (id=%s)", existing["username"], user_id)
    return JSONResponse({"message": "User updated"})


@router.delete("/users/{user_id}")
async def delete_user(request: Request, user_id: str):
    """Delete a user (admin only). Cannot delete yourself."""
    admin = _require_admin(request)
    if not admin:
        return api_error(403, "Admin access required")

    stores = get_stores(request)
    pool = stores.structured._pool

    uid = UUID(user_id)
    existing = await pool.fetchrow("SELECT username FROM users WHERE id = $1", uid)
    if not existing:
        return api_error(404, "User not found")

    if existing["username"] == admin["sub"]:
        return api_error(400, "Cannot delete your own account")

    await pool.execute("DELETE FROM users WHERE id = $1", uid)
    logger.info("Deleted user %s (id=%s)", existing["username"], user_id)
    return JSONResponse({"message": "User deleted"})
