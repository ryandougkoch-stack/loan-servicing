"""
app/api/dependencies.py

FastAPI dependency injection — auth, tenant session, RBAC, denylist check.
"""
from typing import Callable
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenPayload, decode_token
from app.db.session import get_tenant_session, get_shared_session

logger = structlog.get_logger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)
ROLE_HIERARCHY = ["reporting", "compliance", "ops", "finance", "admin"]


async def get_current_user_payload(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
) -> TokenPayload:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except JWTError as e:
        logger.warning("jwt_validation_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        from app.core.token_denylist import token_denylist
        if await token_denylist.is_revoked(payload.jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except HTTPException:
        raise
    except Exception:
        pass  # Redis unavailable — fail open
    return payload


def require_role(*allowed_roles: str) -> Callable:
    async def _check(payload: TokenPayload = Depends(get_current_user_payload)) -> TokenPayload:
        if payload.role not in allowed_roles and payload.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{payload.role}' is not permitted.")
        return payload
    return _check


def require_min_role(min_role: str) -> Callable:
    min_index = ROLE_HIERARCHY.index(min_role)
    async def _check(payload: TokenPayload = Depends(get_current_user_payload)) -> TokenPayload:
        try:
            user_index = ROLE_HIERARCHY.index(payload.role)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Unknown role: {payload.role}")
        if user_index < min_index:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Minimum role required: {min_role}")
        return payload
    return _check


async def get_db(payload: TokenPayload = Depends(get_current_user_payload)) -> AsyncSession:
    async for session in get_tenant_session(payload.tenant_slug):
        yield session


async def get_shared_db() -> AsyncSession:
    async for session in get_shared_session():
        yield session


async def get_current_user_id(
    payload: TokenPayload = Depends(get_current_user_payload),
) -> UUID:
    return UUID(payload.sub)
