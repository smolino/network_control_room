"""Login + the signed-in user's own account (password, two-factor auth).
Admin-only management of other accounts lives in api/users.py."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import (
    Principal,
    generate_otp_secret,
    hash_password,
    issue_token,
    otp_auth_uri,
    require_viewer,
    validate_password,
    verify_password,
    verify_totp,
)
from app.db import get_db
from app.models import User
from app.schemas import LoginIn, LoginOut, OtpDisableIn, OtpEnableIn, OtpSetupOut, PasswordChangeIn, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _require_user(principal: Principal) -> User:
    # The SERVICE_API_TOKEN principal has no account to manage.
    if principal.user is None:
        raise HTTPException(status_code=400, detail="Not available for the service account")
    return principal.user


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    """Password first; for an account with two-factor auth on, a missing
    otp_code gets a 401 with detail "otp_required" so the UI can ask for it
    and resubmit both together."""
    user = db.query(User).filter(func.lower(User.username) == payload.username.strip().lower()).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if user.otp_secret:
        if not payload.otp_code:
            raise HTTPException(status_code=401, detail="otp_required")
        if not verify_totp(user.otp_secret, payload.otp_code):
            raise HTTPException(status_code=401, detail="Invalid authentication code")
    return LoginOut(token=issue_token(user), user=UserOut.from_user(user))


@router.get("/me", response_model=UserOut)
def me(principal: Principal = Depends(require_viewer)):
    return UserOut.from_user(_require_user(principal))


@router.post("/password", response_model=LoginOut)
def change_password(payload: PasswordChangeIn, principal: Principal = Depends(require_viewer), db: Session = Depends(get_db)):
    """Changing your own password revokes every other session you have, and
    returns a fresh token so this one keeps working."""
    user = db.merge(_require_user(principal))
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    validate_password(payload.new_password)
    user.password_hash = hash_password(payload.new_password)
    user.session_epoch += 1
    db.commit()
    return LoginOut(token=issue_token(user), user=UserOut.from_user(user))


@router.post("/otp/setup", response_model=OtpSetupOut)
def otp_setup(principal: Principal = Depends(require_viewer)):
    """A fresh secret for the authenticator app - not saved until
    /otp/enable confirms the user can actually produce codes from it."""
    user = _require_user(principal)
    secret = generate_otp_secret()
    return OtpSetupOut(secret=secret, otpauth_uri=otp_auth_uri(secret, user.username))


@router.post("/otp/enable", response_model=UserOut)
def otp_enable(payload: OtpEnableIn, principal: Principal = Depends(require_viewer), db: Session = Depends(get_db)):
    user = db.merge(_require_user(principal))
    if not verify_totp(payload.secret, payload.code):
        raise HTTPException(status_code=400, detail="Incorrect code - check your authenticator app and try again")
    user.otp_secret = payload.secret
    db.commit()
    return UserOut.from_user(user)


@router.post("/otp/disable", response_model=UserOut)
def otp_disable(payload: OtpDisableIn, principal: Principal = Depends(require_viewer), db: Session = Depends(get_db)):
    user = db.merge(_require_user(principal))
    if not user.otp_secret or not verify_totp(user.otp_secret, payload.code):
        raise HTTPException(status_code=400, detail="Incorrect code - enter your current 6-digit code to disable OTP")
    user.otp_secret = None
    db.commit()
    return UserOut.from_user(user)
