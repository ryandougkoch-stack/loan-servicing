"""
app/db/base.py

SQLAlchemy declarative base and shared mixins.
All ORM models inherit from Base.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """
    Declarative base for all ORM models.
    Using the new SQLAlchemy 2.0 mapped_column / Mapped style throughout.
    """
    pass


class UUIDMixin:
    """Primary key as UUID v4."""
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Automatic created_at / updated_at timestamps."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AuditMixin(UUIDMixin, TimestampMixin):
    """
    Combined mixin used by most business entities.
    Provides id, created_at, updated_at.
    """
    pass
