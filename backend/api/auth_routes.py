"""Password login/logout and authenticated password changes."""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select

from backend.api.deps import current_user
from backend.content import auth
from backend.core import config, db
from backend.core.db import User, UserSession
from backend.data import users

router = APIRouter()


def _set_auth_cookies(response: Response, session: str, csrf: str, expires):
    max_age = max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(config.AUTH_COOKIE_NAME, session, max_age=max_age,
                        httponly=True, secure=config.AUTH_COOKIE_SECURE,
                        samesite="lax", path="/")
    response.set_cookie("kndb_csrf", csrf, max_age=max_age, httponly=False,
                        secure=config.AUTH_COOKIE_SECURE, samesite="lax", path="/")


def _clear_auth_cookies(response: Response):
    response.delete_cookie(config.AUTH_COOKIE_NAME, path="/", secure=config.AUTH_COOKIE_SECURE,
                           httponly=True, samesite="lax")
    response.delete_cookie("kndb_csrf", path="/", secure=config.AUTH_COOKIE_SECURE,
                           httponly=False, samesite="lax")


@router.get("/api/auth/config")
async def auth_config():
    return {"ok": True, "mode": config.AUTH_MODE,
            "registration_enabled": (config.AUTH_MODE == "password"
                                     and config.AUTH_SELF_REGISTRATION)}


@router.post("/api/auth/login")
async def login(request: Request, response: Response, body: Optional[dict] = None):
    """Verify credentials and establish a fresh, CSRF-bound browser session."""
    if config.AUTH_MODE != "password":
        raise HTTPException(404, "password authentication is disabled")
    body = body or {}
    name, password = body.get("name", ""), body.get("password", "")
    if (not isinstance(name, str) or not isinstance(password, str)
            or len(password) > 1024):
        raise HTTPException(401, "Invalid username or password")
    client = request.client.host if request.client else "unknown"
    if await auth.login_limited(name, client):
        raise HTTPException(429, "Too many sign-in attempts; try again later")
    user = await auth.authenticate(name, password)
    if user is None:
        await auth.record_login_failure(name, client)
        raise HTTPException(401, "Invalid username or password")
    await auth.clear_login_failures(name, client)
    token, csrf, expires = await auth.create_session(
        user, revoke_token=request.cookies.get(config.AUTH_COOKIE_NAME, ""))
    _set_auth_cookies(response, token, csrf, expires)
    safe_user = await users.get(user)
    if safe_user:
        safe_user.pop("is_active", None)
    return {"ok": True, "user": safe_user}


@router.post("/api/auth/register")
async def register(request: Request, response: Response, body: Optional[dict] = None):
    """Self-registration, available only when explicitly enabled by an operator."""
    if config.AUTH_MODE != "password" or not config.AUTH_SELF_REGISTRATION:
        raise HTTPException(404, "account registration is disabled")
    body = body or {}
    name, password = body.get("name", ""), body.get("password", "")
    if not isinstance(name, str) or not isinstance(password, str):
        raise HTTPException(400, "username and password are required")
    name = users.norm(name)
    if not name:
        raise HTTPException(400, "username is required")
    try:
        auth.validate_new_password(password)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    client = request.client.host if request.client else "unknown"
    if await auth.signup_limited(client):
        raise HTTPException(429, "Too many account creation attempts; try again later")
    await auth.record_signup_attempt(client)
    try:
        created = await auth.register_user(name, password)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    token, csrf, expires = await auth.create_session(created["name"])
    _set_auth_cookies(response, token, csrf, expires)
    created.pop("is_active", None)
    return {"ok": True, "user": created}


@router.get("/api/auth/me")
async def auth_me(user_name: str = Depends(current_user)):
    safe_user = await users.get(user_name)
    if safe_user:
        safe_user.pop("is_active", None)
    return {"ok": True, "user": safe_user}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    if config.AUTH_MODE == "password":
        token = request.cookies.get(config.AUTH_COOKIE_NAME, "")
        session = await auth.resolve_session(token)
        if session is None:
            raise HTTPException(401, "authentication required")
        if not auth.verify_csrf(request.headers.get("X-KNDB-CSRF", ""), session[1]):
            raise HTTPException(403, "CSRF validation failed")
        await auth.revoke_session(token)
    _clear_auth_cookies(response)
    return {"ok": True}


@router.post("/api/auth/password")
async def change_password(body: Optional[dict] = None,
                          user_name: str = Depends(current_user)):
    """Change own password and revoke every active session for the account."""
    if config.AUTH_MODE != "password":
        raise HTTPException(404, "password authentication is disabled")
    body = body or {}
    current, new = body.get("current_password", ""), body.get("new_password", "")
    if not isinstance(current, str) or not isinstance(new, str):
        raise HTTPException(400, "current and new passwords are required")
    if await auth.authenticate(user_name, current) != user_name:
        raise HTTPException(403, "current password is incorrect")
    try:
        auth.validate_new_password(new)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    async with db.session() as s:
        user = (await s.execute(select(User).where(User.name == user_name))).scalar_one()
        user.password_hash = await asyncio.to_thread(auth.hash_password, new)
        await s.execute(delete(UserSession).where(UserSession.user_name == user_name))
        await s.commit()
    return {"ok": True}
