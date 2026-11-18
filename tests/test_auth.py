"""Password-mode integration tests in an isolated configured subprocess."""

import os
import pathlib
import subprocess
import sys


def test_password_sessions_csrf_identity_and_rate_limit(tmp_path):
    root = pathlib.Path(__file__).resolve().parent.parent
    config = tmp_path / "kndb.yaml"
    config.write_text(
        f'data:\n  dir: "{(tmp_path / "data").as_posix()}"\n'
        "auth:\n  mode: password\n  cookie_secure: true\n  allow_registration: true\n  login_attempts: 2\n",
        encoding="utf-8",
    )
    probe = r'''
import asyncio, os, sqlite3
from fastapi.testclient import TestClient
from backend.core import db, migrations
from backend.data import users
from backend.core.db import User
from backend.content.auth import hash_password
from sqlalchemy import select

encoded_a = hash_password("a very long secure passphrase")
encoded_b = hash_password("a very long secure passphrase")
assert encoded_a.startswith("$argon2id$") and encoded_a != encoded_b

async def setup():
    await db.init_db()
    await migrations.run()
    await users.create("alice")
    async with db.session() as s:
        u = (await s.execute(select(User).where(User.name == "alice"))).scalar_one()
        u.password_hash = hash_password("a very long secure passphrase")
        await s.commit()
    await db.dispose()
asyncio.run(setup())

import app
with TestClient(app.app, base_url="https://testserver") as client:
    landing = client.get("/")
    assert landing.status_code == 200 and "Make room for deeper thinking" in landing.text
    protected = client.get("/projects", follow_redirects=False)
    assert protected.status_code == 303 and protected.headers["location"].startswith("/login?next=")
    settings = client.get("/settings", follow_redirects=False)
    assert settings.status_code == 303 and "/login?next=" in settings.headers["location"]
    assert "settings" in settings.headers["location"]
    registration_page = client.get("/login?mode=register")
    assert "Create your account" in registration_page.text
    login_page = client.get("/login")
    assert "Welcome back" in login_page.text
    assert "id=\"login-signup-link\" hidden" in login_page.text
    config_info = client.get("/api/auth/config").json()
    assert config_info["registration_enabled"] is True
    for _ in range(2):
        bad = client.post("/api/auth/login", json={"name":"alice", "password":"wrong"})
        assert bad.status_code == 401
    assert client.post("/api/auth/login", json={"name":"alice", "password":"a very long secure passphrase"}).status_code == 429
    # Clear the test bucket to continue exercising the authenticated flow.
    from backend.content.auth import clear_login_failures
    asyncio.run(clear_login_failures("alice", "testclient"))
    login = client.post("/api/auth/login", json={"name":"alice", "password":"a very long secure passphrase"})
    assert login.status_code == 200, login.text
    authenticated_settings = client.get("/settings")
    assert authenticated_settings.status_code == 200
    assert 'id="settings-password-form"' in authenticated_settings.text
    assert "Preferences" in authenticated_settings.text
    csrf = client.cookies.get("kndb_csrf")
    assert "httponly" in login.headers["set-cookie"].lower()
    assert client.get("/api/me", headers={"X-KNDB-User":"someone-else"}).json()["user"]["name"] == "alice"
    assert client.post("/api/projects", json={"name":"No CSRF"}).status_code == 403
    created = client.post("/api/projects", json={"name":"With CSRF"}, headers={"X-KNDB-CSRF":csrf})
    assert created.status_code == 200, created.text
    dbpath = os.path.join(os.environ["TEST_DATA_DIR"], "kndb.db")
    conn = sqlite3.connect(dbpath)
    stored = conn.execute("select token_hash from user_sessions").fetchone()[0]
    assert client.cookies.get("kndb_session") != stored
    changed = client.post("/api/auth/password", json={
        "current_password": "a very long secure passphrase",
        "new_password": "another extremely long secure passphrase",
    }, headers={"X-KNDB-CSRF":csrf})
    assert changed.status_code == 200, changed.text
    assert client.get("/api/me").status_code == 401  # password change revoked session
    assert client.post("/api/auth/login", json={"name":"alice", "password":"a very long secure passphrase"}).status_code == 401
    relogin = client.post("/api/auth/login", json={"name":"alice", "password":"another extremely long secure passphrase"})
    assert relogin.status_code == 200
    csrf = client.cookies.get("kndb_csrf")
    assert client.post("/api/auth/logout", headers={"X-KNDB-CSRF":csrf}).status_code == 200
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/login", json={"name":"missing", "password":"wrong"}).status_code == 401
    signup = client.post("/api/auth/register", json={
        "name": "new learner", "password": "a new and secure passphrase",
    })
    assert signup.status_code == 200, signup.text
    assert signup.json()["user"]["name"] == "new learner", signup.text
    assert client.get("/api/me").json()["user"]["name"] == "new learner"
    # Sign out so duplicate registration is checked as a public visitor.
    signup_csrf = client.cookies.get("kndb_csrf")
    assert client.post("/api/auth/logout", headers={"X-KNDB-CSRF": signup_csrf}).status_code == 200
    duplicate = client.post("/api/auth/register", json={
        "name": "new learner", "password": "a new and secure passphrase",
    })
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "username is unavailable"
    alice_duplicate = client.post("/api/auth/register", json={
        "name": "alice", "password": "a new and secure passphrase",
    })
    assert alice_duplicate.status_code == 409, alice_duplicate.text
    assert alice_duplicate.json()["error"] == "username is unavailable"
    assert "Make room for deeper thinking" in client.get("/").text
print("auth integration ok")
'''
    env = os.environ.copy()
    env.update({
        "KNDB_CONFIG": str(config),
        "KNDB_AUTH_PEPPER": "test-pepper-must-be-at-least-32-bytes-long",
        "TEST_DATA_DIR": str(tmp_path / "data"),
        "PYTHONPATH": str(root),
    })
    run = subprocess.run([sys.executable, "-c", probe], cwd=root, env=env,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "auth integration ok" in run.stdout
