"""
Real auth: bcrypt password hashing, JWT access tokens.

SECRET_KEY defaults to a dev value — set a real one via environment variable
before this ever touches production traffic.
"""

import os
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

import models
from database import get_db

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-before-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    to_encode["exp"] = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_mfa_pending_token(user_id: int) -> str:
    """Proves 'password was correct' without granting API access — the
    only thing this token is accepted for is completing MFA. 5 minutes,
    not 7 days, and get_current_user explicitly rejects it below."""
    return jwt.encode(
        {"sub": str(user_id), "purpose": "mfa_pending", "exp": datetime.utcnow() + timedelta(minutes=5)},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def decode_mfa_pending_token(token: str) -> int:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(401, "Invalid or expired MFA session")
    if payload.get("purpose") != "mfa_pending":
        raise HTTPException(401, "Invalid MFA session")
    return int(payload["sub"])


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") == "mfa_pending":
            raise credentials_exception  # this token proves password only, not full auth
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if user is None:
        raise credentials_exception
    # Instant revocation: a suspended or erased user's still-valid token stops working on this very request,
    # rather than whenever the JWT happens to expire. See models.UserLock.
    if db.query(models.UserLock.id).filter(models.UserLock.user_id == user.id).first() is not None:
        raise credentials_exception
    return user


# Role hierarchy: each tier includes everything the tiers below it can do.
ROLE_RANK = {"read_only": 0, "standard_user": 1, "manager": 2, "org_admin": 3}


def require_role(min_role: str):
    """FastAPI dependency factory — use as Depends(require_role('manager'))
    in place of Depends(get_current_user) on any endpoint that should be
    restricted. Enforced server-side; a hidden button in the UI is not
    what actually protects this."""

    def checker(user: models.User = Depends(get_current_user)) -> models.User:
        if ROLE_RANK.get(user.role, 0) < ROLE_RANK.get(min_role, 99):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the '{min_role}' role or higher",
            )
        return user

    return checker
