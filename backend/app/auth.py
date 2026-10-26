"""Server-side authentication and role-based access control.

Accounts live in Postgres (app.models.User) with PBKDF2-hashed passwords
and an optional TOTP secret. POST /api/auth/login (api/auth.py) verifies
both and hands back a signed bearer token; every other API route then
goes through `require_role`, which re-reads the user on each request so a
role change or deletion takes effect immediately rather than when the
token expires.

Everything here is stdlib-only (hashlib/hmac) on purpose - the token is a
plain HMAC-SHA256-signed JSON payload rather than a full JWT, since this
backend is the only thing that ever issues or verifies it.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, get_db
from app.models import User, UserRole

logger = logging.getLogger(__name__)

ROLE_RANK = {UserRole.VIEWER: 0, UserRole.OPERATOR: 1, UserRole.ADMIN: 2}

PBKDF2_ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 8

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
OTP_ISSUER = "Network Control Room"

_bearer = HTTPBearer(auto_error=False)

if settings.auth_secret:
    _signing_key = settings.auth_secret.encode()
else:
    _signing_key = secrets.token_bytes(32)
    logger.warning("AUTH_SECRET is not set - using a random per-process key; sessions won't survive a restart")


# --- passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(digest.hex(), digest_hex)


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


# --- TOTP (RFC 6238: SHA-1, 6 digits, 30s step) -------------------------------


def generate_otp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def otp_auth_uri(secret: str, username: str) -> str:
    label = quote(f"{OTP_ISSUER}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(OTP_ISSUER)}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """Accepts the current step plus `window` steps either side, to allow
    for clock drift between the server and the authenticator app."""
    code = (code or "").strip()
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    counter = int(time.time()) // TOTP_STEP_SECONDS
    return any(hmac.compare_digest(_hotp(secret, counter + delta), code) for delta in range(-window, window + 1))


# --- tokens ------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_token(user: User) -> str:
    payload = {
        "sub": user.id,
        "epoch": user.session_epoch,
        "exp": int(time.time()) + settings.auth_token_ttl_hours * 3600,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(_signing_key, body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def _decode_token(token: str) -> dict | None:
    try:
        body, signature = token.split(".")
    except ValueError:
        return None
    expected = _b64(hmac.new(_signing_key, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# --- request principals ------------------------------------------------------


@dataclass
class Principal:
    """Whoever is making the request - a real User, or the service account
    behind SERVICE_API_TOKEN (user is None)."""

    username: str
    role: UserRole
    user: User | None = None


def _user_for_token(db: Session, token: str) -> User | None:
    payload = _decode_token(token)
    if payload is None:
        return None
    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if user is None or user.session_epoch != payload.get("epoch"):
        return None
    return user


def resolve_principal(db: Session, token: str | None) -> Principal | None:
    if not token:
        return None
    if settings.service_api_token and hmac.compare_digest(token, settings.service_api_token):
        return Principal(username="service", role=UserRole.ADMIN)
    user = _user_for_token(db, token)
    if user is None:
        return None
    return Principal(username=user.username, role=user.role, user=user)


def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Principal:
    principal = resolve_principal(db, credentials.credentials if credentials else None)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_role(minimum: UserRole):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if ROLE_RANK[principal.role] < ROLE_RANK[minimum]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Requires the {minimum.value} role")
        return principal

    return dependency


require_viewer = require_role(UserRole.VIEWER)
require_operator = require_role(UserRole.OPERATOR)
require_admin = require_role(UserRole.ADMIN)


def authenticate_websocket_token(token: str | None) -> Principal | None:
    """Same check as get_principal, for the /ws/events handshake - browsers
    can't set an Authorization header on a WebSocket, so the token comes in
    as a query parameter instead."""
    db = SessionLocal()
    try:
        return resolve_principal(db, token)
    finally:
        db.close()


def seed_default_admin() -> None:
    """Creates `admin` (role admin) on a fresh install, so there's always a
    way in. No-op once any user exists."""
    db = SessionLocal()
    try:
        if db.query(User).first() is not None:
            return
        db.add(User(username="admin", password_hash=hash_password(settings.admin_initial_password), role=UserRole.ADMIN))
        db.commit()
        logger.warning("Seeded default 'admin' account - change its password from Settings > Users")
    finally:
        db.close()
