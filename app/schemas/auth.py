"""
app/schemas/auth.py

Pydantic schemas for authentication and user management endpoints.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
    tenant_slug: str = Field(
        min_length=1,
        max_length=50,
        description="The tenant (fund admin client) slug to log into",
    )


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int          # seconds until access token expires
    user: "UserProfile"


# ---------------------------------------------------------------------------
# Token refresh / logout
# ---------------------------------------------------------------------------

class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    """
    Client should send the access token in the Authorization header.
    Optionally also send the refresh token to revoke it too.
    """
    refresh_token: Optional[str] = None
    logout_all_devices: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    role: str
    tenant_slug: str
    mfa_enabled: bool
    last_login_at: Optional[datetime]
    portfolio_scope: Optional[list[UUID]] = None   # null = all portfolios


class UserProfileUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Password management
# ---------------------------------------------------------------------------

class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=12, description="Minimum 12 characters")
    confirm_password: str

    @model_validator(mode="after")
    def passwords_match(self) -> "ChangePasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("new_password and confirm_password do not match")
        return self

    @model_validator(mode="after")
    def password_not_same(self) -> "ChangePasswordRequest":
        if self.current_password == self.new_password:
            raise ValueError("New password must differ from current password")
        return self


class PasswordStrengthResult(BaseModel):
    score: int           # 0-4 (zxcvbn-style)
    is_acceptable: bool
    feedback: list[str]


# ---------------------------------------------------------------------------
# Tenant onboarding (platform admin only)
# ---------------------------------------------------------------------------

class TenantOnboardRequest(BaseModel):
    slug: str = Field(
        min_length=2,
        max_length=50,
        pattern=r"^[a-z0-9_]+$",
        description="Lowercase alphanumeric + underscore. Used as schema name.",
    )
    name: str = Field(min_length=1, max_length=200)
    fund_type: Optional[str] = None
    plan: str = "standard"

    # First admin user for this tenant
    admin_email: EmailStr
    admin_full_name: str = Field(min_length=1)
    admin_password: str = Field(min_length=12)


class TenantOnboardResponse(BaseModel):
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    admin_user_id: UUID
    admin_email: str
    schema_name: str


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    status: str
    plan: str
    created_at: datetime


# ---------------------------------------------------------------------------
# User management (admin endpoints)
# ---------------------------------------------------------------------------

VALID_ROLES = {"admin", "ops", "finance", "compliance", "reporting", "api_service"}


class InviteUserRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    role: str
    portfolio_scope: Optional[list[UUID]] = None   # null = all portfolios
    temporary_password: str = Field(min_length=12)

    @model_validator(mode="after")
    def validate_role(self) -> "InviteUserRequest":
        if self.role not in VALID_ROLES:
            raise ValueError(f"Invalid role. Must be one of: {sorted(VALID_ROLES)}")
        return self


class UpdateUserRoleRequest(BaseModel):
    role: str
    portfolio_scope: Optional[list[UUID]] = None

    @model_validator(mode="after")
    def validate_role(self) -> "UpdateUserRoleRequest":
        if self.role not in VALID_ROLES:
            raise ValueError(f"Invalid role. Must be one of: {sorted(VALID_ROLES)}")
        return self


class UserListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    mfa_enabled: bool
    last_login_at: Optional[datetime]
    portfolio_scope: Optional[list[UUID]]


# ---------------------------------------------------------------------------
# Forward reference resolution
# ---------------------------------------------------------------------------
LoginResponse.model_rebuild()
