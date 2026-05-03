#!/usr/bin/env python3
"""
scripts/migrate_all_tenants.py

Run Alembic migrations against all active tenant schemas.

Usage:
  python scripts/migrate_all_tenants.py
  python scripts/migrate_all_tenants.py --dry-run
  python scripts/migrate_all_tenants.py --tenant acme_fund   # single tenant only

This script is designed to be run:
  - After deploying a new version of the application
  - As part of CI/CD before running integration tests
  - Manually when adding a new migration to all existing tenants

Exit codes:
  0  All migrations succeeded
  1  One or more migrations failed (see stderr for details)
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def get_active_tenant_slugs(single_tenant: str = None) -> list[str]:
    """Load all active tenant slugs from the shared schema."""
    if single_tenant:
        return [single_tenant]

    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import text
    from app.core.config import settings

    async def _fetch():
        engine = create_async_engine(settings.DATABASE_URL)
        async_session = async_sessionmaker(engine, expire_on_commit=False)
        async with async_session() as session:
            await session.execute(text("SET search_path TO shared, public"))
            result = await session.execute(
                text("SELECT slug FROM shared.tenants WHERE status = 'active' ORDER BY slug")
            )
            slugs = [row.slug for row in result]
        await engine.dispose()
        return slugs

    return asyncio.run(_fetch())


def run_migration(slug: str, dry_run: bool = False) -> bool:
    """Run alembic upgrade head for a single tenant. Returns True on success."""
    env = {**os.environ, "TENANT_SLUG": slug}

    if dry_run:
        cmd = ["alembic", "upgrade", "head", "--sql"]
        print(f"[DRY RUN] Would run: TENANT_SLUG={slug} alembic upgrade head")
        return True

    cmd = ["alembic", "upgrade", "head"]
    result = subprocess.run(
        cmd,
        env=env,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print(f"  ✓  tenant_{slug}")
        return True
    else:
        print(f"  ✗  tenant_{slug}: FAILED", file=sys.stderr)
        print(f"     stdout: {result.stdout.strip()}", file=sys.stderr)
        print(f"     stderr: {result.stderr.strip()}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Migrate all tenant schemas")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would run without executing")
    parser.add_argument("--tenant", type=str, default=None,
                        help="Migrate a single tenant by slug")
    args = parser.parse_args()

    # Always migrate shared schema first
    print("\n=== Migrating shared schema ===")
    shared_env = {**os.environ, "MIGRATE_SHARED": "true"}
    shared_result = subprocess.run(
        ["alembic", "upgrade", "head"],
        env=shared_env,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if shared_result.returncode != 0:
        print("FAILED: shared schema migration failed", file=sys.stderr)
        print(shared_result.stderr, file=sys.stderr)
        sys.exit(1)
    print("  ✓  shared schema")

    # Migrate tenant schemas
    print("\n=== Migrating tenant schemas ===")
    try:
        slugs = get_active_tenant_slugs(args.tenant)
    except Exception as e:
        print(f"Failed to load tenant slugs: {e}", file=sys.stderr)
        sys.exit(1)

    if not slugs:
        print("  No active tenants found.")
        sys.exit(0)

    results = []
    for slug in slugs:
        ok = run_migration(slug, dry_run=args.dry_run)
        results.append((slug, ok))

    failed = [s for s, ok in results if not ok]
    succeeded = [s for s, ok in results if ok]

    print(f"\n=== Summary: {len(succeeded)} succeeded, {len(failed)} failed ===")
    if failed:
        print("Failed tenants:", ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
