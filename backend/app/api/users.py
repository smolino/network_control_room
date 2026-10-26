"""Admin-only account management (Settings > Users). Guards against locking
everyone out: the last admin can't be deleted or demoted, and nobody can
delete their own account."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import Principal, hash_password, require_admin, validate_password
from app.db import get_db
from app.models import User, UserRole
from app.schemas import UserCreateIn, UserOut, UserUpdateIn

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_admin)])


def _get_user(db: Session, user_id: int) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _is_last_admin(db: Session, user: User) -> bool:
    return user.role == UserRole.ADMIN and db.query(User).filter(User.role == UserRole.ADMIN).count() <= 1


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return [UserOut.from_user(u) for u in db.query(User).order_by(User.username).all()]


@router.post("", response_model=UserOut)
def create_user(payload: UserCreateIn, db: Session = Depends(get_db)):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username can't be empty")
    if db.query(User).filter(func.lower(User.username) == username.lower()).first():
        raise HTTPException(status_code=400, detail="A user with that username already exists")
    validate_password(payload.password)
    user = User(username=username, password_hash=hash_password(payload.password), role=payload.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.from_user(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdateIn, db: Session = Depends(get_db)):
    """Role change, password reset and/or OTP reset. Resetting a password or
    OTP also revokes that user's existing sessions."""
    user = _get_user(db, user_id)
    if payload.role is not None and payload.role != user.role:
        if _is_last_admin(db, user):
            raise HTTPException(status_code=400, detail="Can't demote the last remaining admin")
        user.role = payload.role
    if payload.password is not None:
        validate_password(payload.password)
        user.password_hash = hash_password(payload.password)
        user.session_epoch += 1
    if payload.reset_otp and user.otp_secret:
        user.otp_secret = None
        user.session_epoch += 1
    db.commit()
    db.refresh(user)
    return UserOut.from_user(user)


@router.delete("/{user_id}")
def delete_user(user_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    if principal.user is not None and principal.user.id == user.id:
        raise HTTPException(status_code=400, detail="Can't delete the account you're signed in as")
    if _is_last_admin(db, user):
        raise HTTPException(status_code=400, detail="Can't delete the last remaining admin")
    db.delete(user)
    db.commit()
    return {"deleted": user_id}
