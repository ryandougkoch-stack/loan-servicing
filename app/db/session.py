"""
app/db/session.py

Database session management with multi-tenant schema routing.

Architecture:
  - Single PostgreSQL database
  - Schema-per-tenant: each client gets their own schema (e.g. "tenant_acme")
  - Shared schema holds platform-level tables (tenants, users, memberships)
  - Every request sets search_path to: <tenant_schema>, shared, public
  - The tenant_schema is resolved from the JWT token in the request

Key design decision: we use SQLAlchemy's `execution_options` to inject
a SET search_path command at connection checkout time. This means:
  1. No connection pool pollution — each connection is configured on use
  2. No raw SQL sprinkled through business logic
  3. Tenant isolation is enforced at the session layer, not the query layer
"""
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _build_engine(database_url: str, pool: bool = True) -> AsyncEngine:
    """
    Create an async engine.
    pool=False is used for Alembic migrations which manage their own connections.
    """
    kwargs = dict(
        echo=settings.DEBUG,
        future=True,
    )
    if pool:
        kwargs.update(
            pool_size=settings.DATABASE_POOL_SIZE,
            max_overflow=settings.DATABASE_MAX_OVERFLOW,
            pool_timeout=settings.DATABASE_POOL_TIMEOUT,
            pool_pre_ping=True,         # detect stale connections
            pool_recycle=3600,          # recycle connections every hour
        )
    else:
        kwargs["poolclass"] = NullPool

    return create_async_engine(database_url, **kwargs)


engine = _build_engine(settings.DATABASE_URL)


# ---------------------------------------------------------------------------
# Session factory (no tenant context — use TenantSession for request scopes)
# ---------------------------------------------------------------------------

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ---------------------------------------------------------------------------
# Tenant-scoped session
# ---------------------------------------------------------------------------

def _make_tenant_schema_name(tenant_slug: str) -> str:
    """
    Convert a tenant slug to a PostgreSQL schema name.
    Slugs are validated on tenant creation; this just adds the prefix.
    """
    # Extra safety: strip anything that isn't alphanumeric or underscore
    safe_slug = "".join(c for c in tenant_slug if c.isalnum() or c == "_")
    if not safe_slug:
        raise ValueError(f"Invalid tenant slug: {tenant_slug!r}")
    return f"tenant_{safe_slug}"


async def get_tenant_session(tenant_slug: str) -> AsyncGenerator[AsyncSession, None]:
    """
    Async generator that yields a session with search_path set to the
    tenant's schema. Use as a FastAPI dependency (see dependencies.py).

    The SET LOCAL search_path command scopes to the current transaction,
    so it resets automatically when the session is closed.
    """
    schema = _make_tenant_schema_name(tenant_slug)
    search_path = f"{schema}, shared, public"

    async with AsyncSessionLocal() as session:
        try:
            # Set schema routing for this session
            await session.execute(
                text(f"SET LOCAL search_path TO {search_path}")
            )
            logger.debug("tenant_schema_set", schema=schema)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_tenant_session_context(tenant_slug: str) -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager version for use outside FastAPI dependency injection
    (e.g. Celery workers, scripts, background tasks).

    Usage:
        async with get_tenant_session_context("acme") as session:
            result = await session.execute(...)
    """
    schema = _make_tenant_schema_name(tenant_slug)
    search_path = f"{schema}, shared, public"

    async with AsyncSessionLocal() as session:
        try:
            await session.execute(
                text(f"SET LOCAL search_path TO {search_path}")
            )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_shared_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Session scoped to the shared schema only.
    Used for platform-level operations: tenant creation, user auth, etc.
    """
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("SET LOCAL search_path TO shared, public"))
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Schema provisioning (called when a new tenant is onboarded)
# ---------------------------------------------------------------------------

async def provision_tenant_schema(tenant_slug: str) -> None:
    """
    Create a new schema for a tenant and run the tenant schema DDL.
    Called once during tenant onboarding.

    This uses a superuser connection because it requires CREATE SCHEMA
    privileges and DDL execution.

    In production, this should be wrapped in a migration tool (Alembic)
    rather than executing raw DDL here. This version is for MVP simplicity.
    """
    schema = _make_tenant_schema_name(tenant_slug)

    superuser_engine = _build_engine(
        settings.DATABASE_SUPERUSER_URL or settings.DATABASE_URL,
        pool=False,
    )

    async with superuser_engine.begin() as conn:
        # Create schema
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        logger.info("tenant_schema_created", schema=schema)

        # Set path and run tenant DDL
        # In production, use Alembic programmatic migrations instead
        await conn.execute(text(f'SET search_path TO "{schema}", shared, public'))

        # Future: trigger Alembic head migration for this schema
        # For now, log that manual migration is required
        logger.warning(
            "tenant_schema_ddl_pending",
            schema=schema,
            message="Run alembic upgrade head with TENANT_SCHEMA env var set",
        )

    await superuser_engine.dispose()


async def drop_tenant_schema(tenant_slug: str, cascade: bool = False) -> None:
    """
    Drop a tenant schema. cascade=True also drops all objects in the schema.
    USE WITH EXTREME CAUTION — irreversible.
    Only callable in non-production environments.
    """
    if settings.is_production:
        raise RuntimeError("Schema drop is not permitted in production.")

    schema = _make_tenant_schema_name(tenant_slug)
    drop_sql = f'DROP SCHEMA IF EXISTS "{schema}"'
    if cascade:
        drop_sql += " CASCADE"

    superuser_engine = _build_engine(
        settings.DATABASE_SUPERUSER_URL or settings.DATABASE_URL,
        pool=False,
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(text(drop_sql))
        logger.warning("tenant_schema_dropped", schema=schema, cascade=cascade)

    await superuser_engine.dispose()
