"""
app/api/v1/endpoints/auth.py

Authentication and user management endpoints.

Route structure:
  POST   /auth/login               — issue tokens
  POST   /auth/refresh             — exchange refresh token for new access token
  POST   /auth/logout              — revoke token(s)
  GET    /auth/me                  — current user profile
  PATCH  /auth/me                  — update display name
  POST   /auth/me/change-password  — authenticated password change
  GET    /auth/users               — list tenant users (admin)
  POST   /auth/users/invite        — invite user to tenant (admin)
  PATCH  /auth/users/{user_id}/role — update user role (admin)
  DELETE /auth/users/{user_id}     — deactivate user (admin)

  POST   /auth/tenants             — onboard new tenant (platform admin only)
  GET    /auth/tenants/me          — current tenant details

Rate limiting note:
  Login and password endpoints should be rate-limited in production.
  Add slowapi or a Redis-based rate limiter middleware before deploying.
  Not implemented here to avoid adding another dependency, but the comment
  markers show exactly where to add it.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_current_user_id,
    get_current_user_payload,
    get_shared_db,
    require_min_role,
    require_role,
)
from app.core.security import TokenPayload
from app.schemas.auth import (
    ChangePasswordRequest,
    InviteUserRequest,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    TenantOnboardRequest,
    TenantOnboardResponse,
    TokenResponse,
    UpdateUserRoleRequest,
    UserListItem,
    UserProfile,
    UserProfileUpdate,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else request.client.host if request.client else None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Authenticate with email + password + tenant_slug.
    Returns an access token (short-lived) and a refresh token (long-lived).

    The tenant_slug identifies which fund administration client you're
    logging into. A user can have memberships across multiple tenants;
    the slug determines which one this session belongs to.

    # RATE LIMIT: 5 attempts per minute per IP in production
    """
    svc = AuthService(db)
    token_pair, user_profile = await svc.login(
        request=payload,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return LoginResponse(
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token,
        expires_in=token_pair.expires_in,
        user=user_profile,
    )


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Exchange a valid refresh token for a new access token.

    Refresh tokens are single-use (rotated on each use). The old refresh
    token is revoked and a new token pair is issued. This means if a refresh
    token is stolen and used, the legitimate user's next refresh attempt will
    fail — which triggers a security alert.

    The new access token carries the user's current role — so any role
    changes made by an admin take effect on the next refresh.
    """
    svc = AuthService(db)
    new_pair = await svc.refresh_token(payload.refresh_token)
    return TokenResponse(
        access_token=new_pair.access_token,
        expires_in=new_pair.expires_in,
    )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest,
    request: Request,
    current_user: TokenPayload = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Revoke the current session.

    The access token is added to the Redis denylist immediately.
    If logout_all_devices=true, all active sessions for this user are revoked.

    Always returns 204 — even if the token was already expired or
    not found in the denylist (idempotent).
    """
    from datetime import datetime, timezone
    svc = AuthService(db)
    await svc.logout(
        access_token_jti=current_user.jti,
        access_token_exp=current_user.exp,
        refresh_token_str=body.refresh_token,
        logout_all_devices=body.logout_all_devices,
        user_id=current_user.sub,
        ip_address=_client_ip(request),
    )


# ---------------------------------------------------------------------------
# Current user profile
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserProfile)
async def get_me(
    current_user: TokenPayload = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_shared_db),
):
    """Return the authenticated user's profile including role and tenant context."""
    svc = AuthService(db)
    return await svc.get_current_user(
        user_id=UUID(current_user.sub),
        tenant_slug=current_user.tenant_slug,
    )


@router.patch("/me", response_model=UserProfile)
async def update_me(
    body: UserProfileUpdate,
    current_user: TokenPayload = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_shared_db),
):
    """Update the current user's display name."""
    if not body.full_name:
        from app.core.exceptions import ValidationError
        raise ValidationError("full_name is required")

    svc = AuthService(db)
    await svc.update_profile(UUID(current_user.sub), body.full_name)
    return await svc.get_current_user(UUID(current_user.sub), current_user.tenant_slug)


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------

@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    current_user: TokenPayload = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Change the authenticated user's password.

    Requires the current password for verification. After a successful
    change, the current session remains valid but all other sessions
    are revoked (a password change is a security event).

    # RATE LIMIT: 3 attempts per 15 minutes per user in production
    """
    svc = AuthService(db)
    await svc.change_password(
        user_id=UUID(current_user.sub),
        current_password=body.current_password,
        new_password=body.new_password,
        ip_address=_client_ip(request),
    )
    # Revoke all other sessions (not the current one)
    from app.core.token_denylist import token_denylist
    await token_denylist.revoke_all_user_tokens(current_user.sub)


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@router.get("/users", response_model=list[UserListItem])
async def list_users(
    current_user: TokenPayload = Depends(require_min_role("admin")),
    db: AsyncSession = Depends(get_shared_db),
):
    """List all users in the current tenant. Admin only."""
    svc = AuthService(db)
    return await svc.list_users(current_user.tenant_slug)


@router.post("/users/invite", response_model=UserListItem, status_code=status.HTTP_201_CREATED)
async def invite_user(
    body: InviteUserRequest,
    current_user: TokenPayload = Depends(require_min_role("admin")),
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Invite a user to this tenant with a specified role.

    If the email already has an account on the platform (from another tenant),
    they'll be added as a member of this tenant. Otherwise a new account is
    created with the provided temporary password.

    In production, send a password-reset email instead of passing a
    temporary password — this prevents the admin from knowing the user's
    credentials.
    """
    svc = AuthService(db)
    return await svc.invite_user(
        tenant_slug=current_user.tenant_slug,
        request=body,
        invited_by=UUID(current_user.sub),
    )


@router.patch("/users/{user_id}/role", response_model=UserListItem)
async def update_user_role(
    user_id: UUID,
    body: UpdateUserRoleRequest,
    current_user: TokenPayload = Depends(require_min_role("admin")),
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Update a user's role within the current tenant.
    The change takes effect on the user's next token refresh.
    Admin only. Cannot change your own role.
    """
    svc = AuthService(db)
    return await svc.update_user_role(
        tenant_slug=current_user.tenant_slug,
        target_user_id=user_id,
        request=body,
        updated_by=UUID(current_user.sub),
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user(
    user_id: UUID,
    current_user: TokenPayload = Depends(require_min_role("admin")),
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Deactivate a user's access to this tenant.
    Their active tokens are revoked immediately via the Redis denylist.
    The user account itself is not deleted — only their membership.
    Admin only. Cannot deactivate yourself.
    """
    svc = AuthService(db)
    await svc.deactivate_user(
        tenant_slug=current_user.tenant_slug,
        target_user_id=user_id,
        deactivated_by=UUID(current_user.sub),
    )


# ---------------------------------------------------------------------------
# Tenant management (platform admin — separate from tenant-level admin)
# ---------------------------------------------------------------------------

@router.post(
    "/tenants",
    response_model=TenantOnboardResponse,
    status_code=status.HTTP_201_CREATED,
)
async def onboard_tenant(
    body: TenantOnboardRequest,
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Onboard a new fund administration client (tenant).

    This endpoint is intentionally not behind JWT auth — it's called
    during initial setup by your deployment scripts or an internal
    platform admin UI. In production, protect it with an API key
    or a separate internal network policy.

    Creates:
      1. A shared.tenants row
      2. A shared.users row for the first admin
      3. A shared.tenant_memberships row (role=admin)
      4. A new PostgreSQL schema: tenant_{slug}
    """
    svc = AuthService(db)
    return await svc.onboard_tenant(body)


@router.get("/tenants/me")
async def get_current_tenant(
    current_user: TokenPayload = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_shared_db),
):
    """Return the current tenant's details."""
    from sqlalchemy import text
    result = await db.execute(
        text("SELECT id, slug, name, status, plan, created_at FROM shared.tenants WHERE slug = :slug"),
        {"slug": current_user.tenant_slug},
    )
    row = result.mappings().fetchone()
    if not row:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("Tenant not found")
    return dict(row)
