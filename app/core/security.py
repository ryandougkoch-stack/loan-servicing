"""
app/core/security.py

JWT token creation, verification, and password hashing.

Changes from initial version:
  - Every token now carries a `jti` (JWT ID) — a UUID4 used for revocation.
  - TokenPayload includes jti and token type fields.
  - decode_token validates token type (access vs refresh).
  - Password strength validation added.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from app.core.config import settings

logger = structlog.get_logger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TokenPayload(BaseModel):
    sub: str
    tenant_slug: str
    role: str
    jti: str = "legacy-no-jti"
    type: str = "access"
    exp: Optional[int] = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


def hash_password(plain_password: str) -> str:
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def validate_password_strength(password: str) -> dict:
    feedback = []
    score = 0
    if len(password) >= 12:
        score += 1
    else:
        feedback.append("Must be at least 12 characters")
    if any(c.isupper() for c in password):
        score += 1
    else:
        feedback.append("Include at least one uppercase letter")
    if any(c.isdigit() for c in password):
        score += 1
    else:
        feedback.append("Include at least one number")
    if any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?" for c in password):
        score += 1
    else:
        feedback.append("Include at least one special character")
    weak = {"password123!", "Password123!", "Password1234", "Admin123456!", "Letmein123!"}
    if password in weak:
        score = max(0, score - 2)
        feedback.append("This password is too common")
    return {"score": score, "is_acceptable": score >= 3 and len(password) >= 12, "feedback": feedback}


def create_access_token(
    user_id: uuid.UUID, tenant_slug: str, role: str,
    expires_delta: Optional[timedelta] = None, jti: Optional[str] = None,
) -> tuple[str, str, datetime]:
    jti = jti or str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {
        "sub": str(user_id), "tenant_slug": tenant_slug, "role": role,
        "jti": jti, "type": "access", "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM), jti, expire


def create_refresh_token(
    user_id: uuid.UUID, tenant_slug: str, jti: Optional[str] = None,
) -> tuple[str, str, datetime]:
    jti = jti or str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id), "tenant_slug": tenant_slug, "role": "",
        "jti": jti, "type": "refresh", "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM), jti, expire


def decode_token(token: str, expected_type: Optional[str] = None) -> TokenPayload:
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    token_type = payload.get("type", "access")
    if expected_type and token_type != expected_type:
        raise JWTError(f"Wrong token type: expected '{expected_type}', got '{token_type}'")
    if "jti" not in payload:
        payload["jti"] = "legacy-no-jti"
    if "role" not in payload:
        payload["role"] = ""
    return TokenPayload(**payload)


async def create_token_pair(
    user_id: uuid.UUID, tenant_slug: str, role: str, track_session: bool = True,
) -> TokenPair:
    access_token, access_jti, access_exp = create_access_token(user_id, tenant_slug, role)
    refresh_token, refresh_jti, refresh_exp = create_refresh_token(user_id, tenant_slug)
    if track_session:
        from app.core.token_denylist import token_denylist
        try:
            await token_denylist.track_session(str(user_id), access_jti, access_exp)
        except Exception as e:
            logger.warning("session_tracking_failed", user_id=str(user_id), error=str(e))
    return TokenPair(
        access_token=access_token, refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
