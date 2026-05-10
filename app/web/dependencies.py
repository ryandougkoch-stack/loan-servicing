"""
app/web/dependencies.py

Cookie-based auth for the server-rendered HTMX UI.

The JSON API uses Bearer JWT in the Authorization header. The UI uses an
HttpOnly cookie carrying the same access token, set by /web/login. Both
paths decode the same JWT — only the transport differs.

Unauthenticated /web/* requests get redirected to /web/login (not 401), so
ops users land on the form instead of seeing an error response.
"""
from typing import AsyncGenerator, Optional
from uuid import UUID

from fastapi import Cookie, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenPayload, decode_token
from app.db.session import get_tenant_session

ACCESS_COOKIE = "lsp_access"
REFRESH_COOKIE = "lsp_refresh"


class WebAuthRedirect(HTTPException):
    """Raised when a web page request has no/invalid auth — handler redirects."""

    def __init__(self, target: str = "/web/login"):
        super().__init__(status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                         headers={"Location": target})


async def get_web_user(
    lsp_access: Optional[str] = Cookie(default=None),
) -> TokenPayload:
    """Decode the access cookie. Redirects to login on miss/invalid."""
    if not lsp_access:
        raise WebAuthRedirect()
    try:
        return decode_token(lsp_access, expected_type="access")
    except JWTError:
        raise WebAuthRedirect()


async def get_web_user_optional(
    lsp_access: Optional[str] = Cookie(default=None),
) -> Optional[TokenPayload]:
    """Like get_web_user but returns None instead of redirecting (used on /login)."""
    if not lsp_access:
        return None
    try:
        return decode_token(lsp_access, expected_type="access")
    except JWTError:
        return None


async def get_web_db(
    user: TokenPayload = Depends(get_web_user),
) -> AsyncGenerator[AsyncSession, None]:
    """Tenant-scoped session for the cookie-authenticated user."""
    async for session in get_tenant_session(user.tenant_slug):
        yield session


async def get_web_user_id(
    user: TokenPayload = Depends(get_web_user),
) -> UUID:
    return UUID(user.sub)
