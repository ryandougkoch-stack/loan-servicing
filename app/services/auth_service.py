"""
app/services/auth_service.py

Authentication and user management business logic.

All database operations target the shared schema (shared.users,
shared.tenants, shared.tenant_memberships) because these are
platform-level entities, not tenant-specific.

The shared session is passed in from the endpoint layer — the service
itself is schema-agnostic.

Security invariants enforced here:
  - Passwords are never stored or logged in plaintext
  - Failed login attempts are logged with IP for monitoring
  - Login failure messages are deliberately vague (no user enumeration)
  - Deactivated users cannot log in
  - Suspended tenants block all logins for that tenant
  - Role changes take effect on next login (refresh doesn't pick up changes
    — that would require re-fetching role on every refresh, which we do)
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.security import (
    TokenPair,
    create_token_pair,
    decode_token,
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.schemas.auth import (
    InviteUserRequest,
    LoginRequest,
    TenantOnboardRequest,
    TenantOnboardResponse,
    UpdateUserRoleRequest,
    UserListItem,
    UserProfile,
)

logger = structlog.get_logger(__name__)


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db   # shared schema session

    # -------------------------------------------------------------------------
    # Login
    # -------------------------------------------------------------------------

    async def login(
        self,
        request: LoginRequest,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> tuple[TokenPair, UserProfile]:
        """
        Authenticate a user and issue a token pair.
        Returns (TokenPair, UserProfile).
        Raises PermissionDeniedError with a deliberately vague message on failure.
        """
        GENERIC_FAILURE = "Invalid email, password, or tenant"

        # Load user by email
        user = await self._get_user_by_email(request.email)
        if not user:
            logger.warning("login_failed_no_user", email=request.email, ip=ip_address)
            await self._audit("login_failed", None, None,
                              {"reason": "user_not_found", "email": request.email}, ip_address)
            raise PermissionDeniedError(GENERIC_FAILURE)

        # Check user is active
        if not user["is_active"] or user["status"] != "active":
            logger.warning("login_failed_inactive", user_id=str(user["id"]), ip=ip_address)
            await self._audit("login_failed", user["id"], None,
                              {"reason": "user_inactive"}, ip_address)
            raise PermissionDeniedError(GENERIC_FAILURE)

        # Verify password
        if not user["password_hash"] or not verify_password(request.password, user["password_hash"]):
            logger.warning("login_failed_bad_password", user_id=str(user["id"]), ip=ip_address)
            await self._audit("login_failed", user["id"], None,
                              {"reason": "bad_password"}, ip_address)
            raise PermissionDeniedError(GENERIC_FAILURE)

        # Load tenant membership
        membership = await self._get_membership(user["id"], request.tenant_slug)
        if not membership:
            logger.warning("login_failed_no_membership",
                           user_id=str(user["id"]), tenant=request.tenant_slug, ip=ip_address)
            await self._audit("login_failed", user["id"], None,
                              {"reason": "no_membership", "tenant": request.tenant_slug}, ip_address)
            raise PermissionDeniedError(GENERIC_FAILURE)

        if not membership["is_active"]:
            await self._audit("login_failed", user["id"], None,
                              {"reason": "membership_inactive"}, ip_address)
            raise PermissionDeniedError(GENERIC_FAILURE)

        # Check tenant is active
        tenant = await self._get_tenant(request.tenant_slug)
        if not tenant or tenant["status"] != "active":
            await self._audit("login_failed", user["id"], None,
                              {"reason": "tenant_suspended", "tenant": request.tenant_slug}, ip_address)
            raise PermissionDeniedError(GENERIC_FAILURE)

        # Issue tokens
        token_pair = await create_token_pair(
            user_id=user["id"],
            tenant_slug=request.tenant_slug,
            role=membership["role"],
        )

        # Update last_login_at
        await self.db.execute(
            text("UPDATE shared.users SET last_login_at = :now WHERE id = :id"),
            {"now": datetime.now(timezone.utc), "id": str(user["id"])},
        )

        # Audit successful login
        await self._audit("login_success", user["id"], tenant["id"],
                          {"tenant_slug": request.tenant_slug, "role": membership["role"]},
                          ip_address, user_agent=user_agent)

        profile = UserProfile(
            id=user["id"],
            email=user["email"],
            full_name=user["full_name"],
            role=membership["role"],
            tenant_slug=request.tenant_slug,
            mfa_enabled=user["mfa_enabled"],
            last_login_at=datetime.now(timezone.utc),
            portfolio_scope=membership["portfolio_scope"],
        )

        logger.info("login_success", user_id=str(user["id"]),
                    tenant=request.tenant_slug, role=membership["role"])
        return token_pair, profile

    # -------------------------------------------------------------------------
    # Refresh
    # -------------------------------------------------------------------------

    async def refresh_token(self, refresh_token_str: str) -> TokenPair:
        """
        Validate a refresh token and issue a new access token.
        Re-fetches the user's current role so any changes take effect immediately.
        """
        from jose import JWTError
        from app.core.token_denylist import token_denylist

        try:
            payload = decode_token(refresh_token_str, expected_type="refresh")
        except JWTError:
            raise PermissionDeniedError("Invalid or expired refresh token")

        # Check refresh token hasn't been revoked
        if await token_denylist.is_revoked(payload.jti):
            raise PermissionDeniedError("Refresh token has been revoked")

        # Re-fetch current role (may have changed since original login)
        user_id = UUID(payload.sub)
        membership = await self._get_membership_by_user_id(user_id, payload.tenant_slug)
        if not membership or not membership["is_active"]:
            raise PermissionDeniedError("User no longer has access to this tenant")

        # Revoke the used refresh token (rotation: one use per refresh token)
        from datetime import datetime as dt
        expires_at = dt.fromtimestamp(payload.exp, tz=timezone.utc) if payload.exp else dt.now(timezone.utc)
        await token_denylist.revoke_token(payload.jti, expires_at)

        # Issue new token pair with current role
        new_pair = await create_token_pair(
            user_id=user_id,
            tenant_slug=payload.tenant_slug,
            role=membership["role"],
        )
        logger.info("token_refreshed", user_id=str(user_id), tenant=payload.tenant_slug)
        return new_pair

    # -------------------------------------------------------------------------
    # Logout
    # -------------------------------------------------------------------------

    async def logout(
        self,
        access_token_jti: str,
        access_token_exp: Optional[int],
        refresh_token_str: Optional[str],
        logout_all_devices: bool,
        user_id: str,
        ip_address: Optional[str] = None,
    ) -> None:
        """Revoke the current token(s)."""
        from app.core.token_denylist import token_denylist
        from datetime import datetime as dt

        # Revoke access token
        if access_token_exp:
            exp_dt = dt.fromtimestamp(access_token_exp, tz=timezone.utc)
            await token_denylist.revoke_token(access_token_jti, exp_dt)

        # Revoke refresh token if provided
        if refresh_token_str:
            try:
                rt_payload = decode_token(refresh_token_str, expected_type="refresh")
                if rt_payload.exp:
                    rt_exp_dt = dt.fromtimestamp(rt_payload.exp, tz=timezone.utc)
                    await token_denylist.revoke_token(rt_payload.jti, rt_exp_dt)
            except Exception:
                pass  # If it's invalid, it's already harmless

        # Logout all devices: revoke every tracked session
        if logout_all_devices:
            await token_denylist.revoke_all_user_tokens(user_id)

        await self._audit("logout", UUID(user_id), None,
                          {"all_devices": logout_all_devices}, ip_address)
        logger.info("logout", user_id=user_id, all_devices=logout_all_devices)

    # -------------------------------------------------------------------------
    # Current user
    # -------------------------------------------------------------------------

    async def get_current_user(
        self, user_id: UUID, tenant_slug: str
    ) -> UserProfile:
        """Fetch the full profile for the currently authenticated user."""
        user = await self._get_user_by_id(user_id)
        if not user:
            raise NotFoundError("User not found")

        membership = await self._get_membership_by_user_id(user_id, tenant_slug)
        if not membership:
            raise NotFoundError("Membership not found")

        return UserProfile(
            id=user["id"],
            email=user["email"],
            full_name=user["full_name"],
            role=membership["role"],
            tenant_slug=tenant_slug,
            mfa_enabled=user["mfa_enabled"],
            last_login_at=user["last_login_at"],
            portfolio_scope=membership["portfolio_scope"],
        )

    async def update_profile(
        self, user_id: UUID, full_name: str
    ) -> UserProfile:
        """Update the current user's display name."""
        await self.db.execute(
            text("UPDATE shared.users SET full_name = :name, updated_at = :now WHERE id = :id"),
            {"name": full_name, "now": datetime.now(timezone.utc), "id": str(user_id)},
        )
        return await self.get_current_user_raw(user_id)

    # -------------------------------------------------------------------------
    # Password change
    # -------------------------------------------------------------------------

    async def change_password(
        self,
        user_id: UUID,
        current_password: str,
        new_password: str,
        ip_address: Optional[str] = None,
    ) -> None:
        """Change a user's password after verifying their current one."""
        user = await self._get_user_by_id(user_id)
        if not user:
            raise NotFoundError("User not found")

        if not verify_password(current_password, user["password_hash"]):
            await self._audit("password_change_failed", user_id, None,
                              {"reason": "wrong_current_password"}, ip_address)
            raise PermissionDeniedError("Current password is incorrect")

        strength = validate_password_strength(new_password)
        if not strength["is_acceptable"]:
            raise ValidationError(
                "Password does not meet strength requirements",
                detail=strength["feedback"],
            )

        new_hash = hash_password(new_password)
        await self.db.execute(
            text("""
                UPDATE shared.users
                SET password_hash = :hash, updated_at = :now
                WHERE id = :id
            """),
            {"hash": new_hash, "now": datetime.now(timezone.utc), "id": str(user_id)},
        )
        await self._audit("password_changed", user_id, None, {}, ip_address)
        logger.info("password_changed", user_id=str(user_id))

    # -------------------------------------------------------------------------
    # Tenant onboarding (platform admin)
    # -------------------------------------------------------------------------

    async def onboard_tenant(
        self, request: TenantOnboardRequest
    ) -> TenantOnboardResponse:
        """
        Create a new tenant with its first admin user.
        Provisions the tenant schema via the DB session layer.
        """
        # Check slug uniqueness
        existing = await self.db.execute(
            text("SELECT id FROM shared.tenants WHERE slug = :slug"),
            {"slug": request.slug},
        )
        if existing.fetchone():
            raise ConflictError(f"Tenant slug '{request.slug}' is already taken")

        # Check admin email uniqueness
        existing_user = await self._get_user_by_email(request.admin_email)

        # Validate password strength
        strength = validate_password_strength(request.admin_password)
        if not strength["is_acceptable"]:
            raise ValidationError(
                "Admin password does not meet requirements",
                detail=strength["feedback"],
            )

        # Create tenant
        tenant_result = await self.db.execute(
            text("""
                INSERT INTO shared.tenants (slug, name, status, plan)
                VALUES (:slug, :name, 'active', :plan)
                RETURNING id
            """),
            {"slug": request.slug, "name": request.name, "plan": request.plan},
        )
        tenant_id = tenant_result.fetchone()[0]

        # Create or reuse the admin user
        if existing_user:
            admin_user_id = existing_user["id"]
        else:
            user_result = await self.db.execute(
                text("""
                    INSERT INTO shared.users (email, full_name, password_hash, status)
                    VALUES (:email, :name, :hash, 'active')
                    RETURNING id
                """),
                {
                    "email": request.admin_email,
                    "name": request.admin_full_name,
                    "hash": hash_password(request.admin_password),
                },
            )
            admin_user_id = user_result.fetchone()[0]

        # Create admin membership
        await self.db.execute(
            text("""
                INSERT INTO shared.tenant_memberships
                    (tenant_id, user_id, role, is_active)
                VALUES (:tenant_id, :user_id, 'admin', true)
                ON CONFLICT (tenant_id, user_id) DO NOTHING
            """),
            {"tenant_id": str(tenant_id), "user_id": str(admin_user_id)},
        )

        # Provision the schema (creates the schema; DDL migration runs separately)
        from app.db.session import provision_tenant_schema
        await provision_tenant_schema(request.slug)

        schema_name = f"tenant_{request.slug}"
        logger.info("tenant_onboarded", tenant_id=str(tenant_id),
                    slug=request.slug, admin_email=request.admin_email)

        return TenantOnboardResponse(
            tenant_id=tenant_id,
            tenant_slug=request.slug,
            tenant_name=request.name,
            admin_user_id=admin_user_id,
            admin_email=request.admin_email,
            schema_name=schema_name,
        )

    # -------------------------------------------------------------------------
    # User management (admin endpoints)
    # -------------------------------------------------------------------------

    async def list_users(self, tenant_slug: str) -> list[UserListItem]:
        """List all users in the current tenant."""
        result = await self.db.execute(
            text("""
                SELECT
                    u.id, u.email, u.full_name, u.mfa_enabled,
                    u.last_login_at,
                    m.role, m.is_active, m.portfolio_scope
                FROM shared.users u
                JOIN shared.tenant_memberships m
                    ON m.user_id = u.id
                JOIN shared.tenants t
                    ON t.id = m.tenant_id
                WHERE t.slug = :slug
                ORDER BY u.full_name
            """),
            {"slug": tenant_slug},
        )
        rows = result.mappings().all()
        return [
            UserListItem(
                id=row["id"],
                email=row["email"],
                full_name=row["full_name"],
                role=row["role"],
                is_active=row["is_active"],
                mfa_enabled=row["mfa_enabled"],
                last_login_at=row["last_login_at"],
                portfolio_scope=row["portfolio_scope"],
            )
            for row in rows
        ]

    async def invite_user(
        self,
        tenant_slug: str,
        request: InviteUserRequest,
        invited_by: UUID,
    ) -> UserListItem:
        """
        Create or find a user and add them to the tenant with the given role.
        In production: send an email invite with a one-time link instead of
        a temporary password.
        """
        strength = validate_password_strength(request.temporary_password)
        if not strength["is_acceptable"]:
            raise ValidationError("Temporary password too weak", detail=strength["feedback"])

        # Get tenant_id
        tenant = await self._get_tenant(tenant_slug)
        if not tenant:
            raise NotFoundError(f"Tenant '{tenant_slug}' not found")

        # Find or create user
        existing = await self._get_user_by_email(request.email)
        if existing:
            user_id = existing["id"]
            # Check not already a member
            existing_membership = await self._get_membership_by_user_id(user_id, tenant_slug)
            if existing_membership:
                raise ConflictError(f"User {request.email} is already a member of this tenant")
        else:
            result = await self.db.execute(
                text("""
                    INSERT INTO shared.users (email, full_name, password_hash, status)
                    VALUES (:email, :name, :hash, 'active')
                    RETURNING id
                """),
                {
                    "email": request.email,
                    "name": request.full_name,
                    "hash": hash_password(request.temporary_password),
                },
            )
            user_id = result.fetchone()[0]

        # Create membership
        await self.db.execute(
            text("""
                INSERT INTO shared.tenant_memberships
                    (tenant_id, user_id, role, portfolio_scope, is_active)
                VALUES (:tenant_id, :user_id, :role, :scope, true)
            """),
            {
                "tenant_id": str(tenant["id"]),
                "user_id": str(user_id),
                "role": request.role,
                "scope": request.portfolio_scope,
            },
        )

        await self._audit("user_invited", invited_by, tenant["id"],
                          {"invitee_email": request.email, "role": request.role})

        logger.info("user_invited", invitee=request.email, role=request.role,
                    tenant=tenant_slug, invited_by=str(invited_by))

        return UserListItem(
            id=user_id,
            email=request.email,
            full_name=request.full_name,
            role=request.role,
            is_active=True,
            mfa_enabled=False,
            last_login_at=None,
            portfolio_scope=request.portfolio_scope,
        )

    async def update_user_role(
        self,
        tenant_slug: str,
        target_user_id: UUID,
        request: UpdateUserRoleRequest,
        updated_by: UUID,
    ) -> UserListItem:
        """Update a user's role within the tenant. Admin only."""
        # Can't demote yourself
        if target_user_id == updated_by:
            raise ValidationError("You cannot change your own role")

        tenant = await self._get_tenant(tenant_slug)
        if not tenant:
            raise NotFoundError(f"Tenant '{tenant_slug}' not found")

        result = await self.db.execute(
            text("""
                UPDATE shared.tenant_memberships
                SET role = :role,
                    portfolio_scope = :scope
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                RETURNING user_id
            """),
            {
                "role": request.role,
                "scope": request.portfolio_scope,
                "user_id": str(target_user_id),
                "tenant_id": str(tenant["id"]),
            },
        )
        if not result.fetchone():
            raise NotFoundError("User not found in this tenant")

        await self._audit("user_role_updated", updated_by, tenant["id"],
                          {"target_user_id": str(target_user_id), "new_role": request.role})

        # Return updated profile
        user = await self._get_user_by_id(target_user_id)
        return UserListItem(
            id=user["id"],
            email=user["email"],
            full_name=user["full_name"],
            role=request.role,
            is_active=True,
            mfa_enabled=user["mfa_enabled"],
            last_login_at=user["last_login_at"],
            portfolio_scope=request.portfolio_scope,
        )

    async def deactivate_user(
        self,
        tenant_slug: str,
        target_user_id: UUID,
        deactivated_by: UUID,
    ) -> None:
        """Deactivate a user's membership (not their account — only this tenant)."""
        if target_user_id == deactivated_by:
            raise ValidationError("You cannot deactivate your own account")

        tenant = await self._get_tenant(tenant_slug)
        result = await self.db.execute(
            text("""
                UPDATE shared.tenant_memberships
                SET is_active = false
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                RETURNING user_id
            """),
            {"user_id": str(target_user_id), "tenant_id": str(tenant["id"])},
        )
        if not result.fetchone():
            raise NotFoundError("User not found in this tenant")

        # Revoke all their active tokens (immediate effect)
        from app.core.token_denylist import token_denylist
        await token_denylist.revoke_all_user_tokens(str(target_user_id))

        await self._audit("user_deactivated", deactivated_by, tenant["id"],
                          {"target_user_id": str(target_user_id)})
        logger.info("user_deactivated", target=str(target_user_id), by=str(deactivated_by))

    # -------------------------------------------------------------------------
    # Internal DB helpers
    # -------------------------------------------------------------------------

    async def _get_user_by_email(self, email: str) -> Optional[dict]:
        result = await self.db.execute(
            text("""
                SELECT id, email, full_name, password_hash, status,
                       mfa_enabled, last_login_at, true AS is_active
                FROM shared.users
                WHERE LOWER(email) = LOWER(:email)
                LIMIT 1
            """),
            {"email": email},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None

    async def _get_user_by_id(self, user_id: UUID) -> Optional[dict]:
        result = await self.db.execute(
            text("""
                SELECT id, email, full_name, password_hash, status,
                       mfa_enabled, last_login_at, true AS is_active
                FROM shared.users
                WHERE id = :id
            """),
            {"id": str(user_id)},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None

    async def _get_membership(self, user_id: UUID, tenant_slug: str) -> Optional[dict]:
        result = await self.db.execute(
            text("""
                SELECT m.role, m.is_active, m.portfolio_scope
                FROM shared.tenant_memberships m
                JOIN shared.tenants t ON t.id = m.tenant_id
                WHERE m.user_id = :user_id
                  AND t.slug = :slug
                LIMIT 1
            """),
            {"user_id": str(user_id), "slug": tenant_slug},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None

    async def _get_membership_by_user_id(
        self, user_id: UUID, tenant_slug: str
    ) -> Optional[dict]:
        return await self._get_membership(user_id, tenant_slug)

    async def _get_tenant(self, slug: str) -> Optional[dict]:
        result = await self.db.execute(
            text("SELECT id, slug, name, status FROM shared.tenants WHERE slug = :slug"),
            {"slug": slug},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None

    async def _audit(
        self,
        event_type: str,
        user_id: Optional[UUID],
        tenant_id: Optional[UUID],
        event_data: dict,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        """Write to the platform audit log (shared schema)."""
        try:
            await self.db.execute(
                text("""
                    INSERT INTO shared.platform_audit_log
                        (tenant_id, user_id, event_type, event_data, ip_address, user_agent)
                    VALUES
                        (:tenant_id, :user_id, :event_type, :event_data::jsonb,
                         :ip::inet, :ua)
                """),
                {
                    "tenant_id": str(tenant_id) if tenant_id else None,
                    "user_id":   str(user_id) if user_id else None,
                    "event_type": event_type,
                    "event_data": __import__("json").dumps(
                        {k: str(v) if not isinstance(v, (str, int, bool, type(None))) else v
                         for k, v in event_data.items()}
                    ),
                    "ip": ip_address,
                    "ua": user_agent,
                },
            )
        except Exception as e:
            # Audit failures must never block the primary operation
            logger.error("audit_write_failed", event_type=event_type, error=str(e))
