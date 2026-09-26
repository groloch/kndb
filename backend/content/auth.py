"""Password hashing and durable browser-session primitives."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, select

from ..core import config, db
from ..core.db import LoginAttempt, User, UserSession
from ..data import users

# Argon2id is applied once to a pepper-keyed password input. It supplies
# per-hash random salt and tunable memory-hard work; avoid fast double hashing.
_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2,
                                  hash_len=32, salt_len=16)
_DUMMY_HASH: str | None = None


def _derive_key(purpose: bytes) -> bytes:
    return hmac.new(config.AUTH_PEPPER.encode(), b"kndb:" + purpose,
                    hashlib.sha256).digest()


def _password_input(password: str) -> str:
    return hmac.new(_derive_key(b"password-v1"), password.encode(),
                    hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    if not config.AUTH_PEPPER:
        raise RuntimeError("password authentication is not configured")
    return _PASSWORD_HASHER.hash(_password_input(password))


def verify_password(password: str, encoded: str) -> bool:
    if not config.AUTH_PEPPER or not encoded:
        return False
    try:
        return _PASSWORD_HASHER.verify(encoded, _password_input(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
    return _DUMMY_HASH


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_session_token(purpose: bytes, token: str) -> str:
    return hmac.new(_derive_key(purpose), token.encode("ascii"),
                    hashlib.sha256).hexdigest()


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return _hash_session_token(b"session-v1", token)


def _login_bucket(name: str, client: str) -> str:
    value = f"{name.strip().casefold()}\0{client}"
    return hmac.new(_derive_key(b"login-rate-v1"), value.encode(),
                    hashlib.sha256).hexdigest()


async def login_limited(name: str, client: str) -> bool:
    async with db.session() as s:
        row = (await s.execute(select(LoginAttempt).where(
            LoginAttempt.bucket == _login_bucket(name, client)))).scalar_one_or_none()
    if row is None:
        return False
    elapsed = (_now() - datetime.fromisoformat(row.window_started)).total_seconds()
    return (elapsed < config.AUTH_LOGIN_WINDOW_SECONDS
            and row.failures >= config.AUTH_LOGIN_ATTEMPTS)


async def record_login_failure(name: str, client: str) -> None:
    bucket, now = _login_bucket(name, client), _now()
    async with db.session() as s:
        row = (await s.execute(select(LoginAttempt).where(
            LoginAttempt.bucket == bucket))).scalar_one_or_none()
        if row is None:
            s.add(LoginAttempt(bucket=bucket, failures=1, window_started=_stamp(now)))
        elif ((now - datetime.fromisoformat(row.window_started)).total_seconds()
              >= config.AUTH_LOGIN_WINDOW_SECONDS):
            row.failures, row.window_started = 1, _stamp(now)
        else:
            row.failures += 1
        await s.commit()


async def clear_login_failures(name: str, client: str) -> None:
    async with db.session() as s:
        await s.execute(delete(LoginAttempt).where(
            LoginAttempt.bucket == _login_bucket(name, client)))
        await s.commit()


async def signup_limited(client: str) -> bool:
    bucket = hmac.new(_derive_key(b"signup-rate-v1"), client.encode(),
                      hashlib.sha256).hexdigest()
    async with db.session() as s:
        row = (await s.execute(select(LoginAttempt).where(
            LoginAttempt.bucket == bucket))).scalar_one_or_none()
    if row is None:
        return False
    elapsed = (_now() - datetime.fromisoformat(row.window_started)).total_seconds()
    return elapsed < _SIGNUP_WINDOW_SECONDS and row.failures >= _SIGNUP_LIMIT


_SIGNUP_LIMIT = 5
_SIGNUP_WINDOW_SECONDS = 3600


async def record_signup_attempt(client: str) -> None:
    bucket, now = hmac.new(_derive_key(b"signup-rate-v1"), client.encode(),
                           hashlib.sha256).hexdigest(), _now()
    async with db.session() as s:
        row = (await s.execute(select(LoginAttempt).where(
            LoginAttempt.bucket == bucket))).scalar_one_or_none()
        if row is None:
            s.add(LoginAttempt(bucket=bucket, failures=1, window_started=_stamp(now)))
        elif ((now - datetime.fromisoformat(row.window_started)).total_seconds()
              >= _SIGNUP_WINDOW_SECONDS):
            row.failures, row.window_started = 1, _stamp(now)
        else:
            row.failures += 1
        await s.commit()


async def register_user(name: str, password: str) -> dict:
    """Create a password account atomically; never claim a legacy username."""
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("username is required")
    validate_new_password(password)
    from sqlalchemy.dialects.sqlite import insert

    password_hash = await asyncio.to_thread(hash_password, password)
    async with db.session() as s:
        result = await s.execute(insert(User).values(
            name=name, display_name=name, created_at=_stamp(_now()),
            password_hash=password_hash, is_active=True).on_conflict_do_nothing(
                index_elements=[User.name]))
        if result.rowcount != 1:
            raise ValueError("username is unavailable")
        await s.commit()
    await users.create(name)  # also creates the personal workspace
    return await users.get(name)


async def authenticate(name: str, password: str) -> str | None:
    normalized = (name or "").strip()[:120]
    async with db.session() as s:
        user = (await s.execute(select(User).where(User.name == normalized))).scalar_one_or_none()
    encoded = user.password_hash if user and user.is_active else ""
    target = encoded or await asyncio.to_thread(_dummy_hash)
    ok = await asyncio.to_thread(verify_password, password, target)
    if user is None or not user.is_active or not encoded or not ok:
        return None
    if _PASSWORD_HASHER.check_needs_rehash(encoded):
        async with db.session() as s:
            current = (await s.execute(select(User).where(User.name == normalized))).scalar_one_or_none()
            if current is not None:
                current.password_hash = await asyncio.to_thread(hash_password, password)
                await s.commit()
    return normalized


async def create_session(user_name: str, *, revoke_token: str = "") -> tuple[str, str, datetime]:
    if revoke_token:
        await revoke_session(revoke_token)
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    expires = _now() + timedelta(seconds=config.AUTH_SESSION_TTL_SECONDS)
    row = UserSession(token_hash=_token_hash(token), user_name=user_name,
                      created_at=_stamp(_now()), expires_at=_stamp(expires),
                      csrf_hash=_hash_session_token(b"csrf-v1", csrf))
    async with db.session() as s:
        s.add(row)
        await s.commit()
    return token, csrf, expires


async def resolve_session(token: str) -> tuple[str, str] | None:
    if not token:
        return None
    async with db.session() as s:
        row = (await s.execute(select(UserSession).where(
            UserSession.token_hash == _token_hash(token)))).scalar_one_or_none()
        if row is None:
            return None
        if row.expires_at <= _stamp(_now()):
            await s.delete(row)
            await s.commit()
            return None
        user = (await s.execute(select(User).where(User.name == row.user_name))).scalar_one_or_none()
        if user is None or not user.is_active:
            await s.delete(row)
            await s.commit()
            return None
        return user.name, row.csrf_hash


def verify_csrf(csrf: str, expected_hash: str) -> bool:
    return bool(csrf and expected_hash and hmac.compare_digest(
        _hash_session_token(b"csrf-v1", csrf), expected_hash))


async def revoke_session(token: str) -> None:
    if token:
        async with db.session() as s:
            await s.execute(delete(UserSession).where(
                UserSession.token_hash == _token_hash(token)))
            await s.commit()


def validate_new_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < 14:
        raise ValueError("password must be at least 14 characters")
    if len(password) > 1024:
        raise ValueError("password must be no more than 1024 characters")
