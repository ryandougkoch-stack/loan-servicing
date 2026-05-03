"""
alembic/env.py

Multi-branch migration environment.
  MIGRATE_SHARED=true alembic upgrade head   -> shared schema
  TENANT_SLUG=acme   alembic upgrade head   -> tenant_acme schema
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool, text
from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
import app.models.loan      # noqa
import app.models.ledger    # noqa
import app.models.schedule  # noqa
import app.models.portfolio # noqa
from app.db.base import Base

config = context.config

db_url = settings.DATABASE_URL.replace("+asyncpg", "+psycopg2")
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_target_schema() -> str:
    if os.environ.get("MIGRATE_SHARED", "").lower() in ("true", "1", "yes"):
        return "shared"
    slug = os.environ.get("TENANT_SLUG", "").strip()
    if slug:
        safe = "".join(c for c in slug if c.isalnum() or c == "_")
        if safe:
            return f"tenant_{safe}"
    raise EnvironmentError(
        "Set MIGRATE_SHARED=true OR TENANT_SLUG=<slug> before running alembic."
    )


def run_migrations_offline() -> None:
    schema = get_target_schema()
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True,
        version_table="alembic_version", version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    schema = get_target_schema()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        connection.execute(text('CREATE SCHEMA IF NOT EXISTS shared'))
        if schema != "shared":
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.execute(text(f'SET search_path TO "{schema}", shared, public'))
        context.configure(
            connection=connection, target_metadata=target_metadata,
            version_table="alembic_version", version_table_schema=schema,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
