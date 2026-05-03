#!/usr/bin/env python3
"""
scripts/seed_dev.py

Bootstrap a local development environment from scratch.

What it does:
  1. Runs shared schema migrations
  2. Creates a test tenant ('dev_fund')
  3. Runs tenant schema migrations
  4. Creates an admin user
  5. Creates a portfolio and 3 sample loans with payment schedules

Usage:
  python scripts/seed_dev.py
  python scripts/seed_dev.py --reset   # drop and recreate everything

After running:
  curl -X POST http://localhost:8000/api/v1/auth/login \\
    -H 'Content-Type: application/json' \\
    -d '{"email": "admin@devfund.com", "password": "DevAdmin123!", "tenant_slug": "dev_fund"}'
"""
import asyncio
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TENANT_SLUG = "dev_fund"
ADMIN_EMAIL = "admin@devfund.com"
ADMIN_PASSWORD = "DevAdmin123!"
ADMIN_NAME = "Dev Admin"


async def run_migrations():
    """Schemas already created manually - skip migrations."""
    print("  ✓ shared schema (already exists)")
    print(f"  ✓ tenant_{TENANT_SLUG} (already exists)")


async def seed_shared(session):
    """Create tenant + admin user in shared schema."""
    from sqlalchemy import text

    await session.execute(text("SET search_path TO shared, public"))

    # Create tenant
    existing = await session.execute(
        text("SELECT id FROM shared.tenants WHERE slug = :slug"),
        {"slug": TENANT_SLUG},
    )
    if existing.fetchone():
        print(f"  Tenant '{TENANT_SLUG}' already exists, skipping.")
        return

    tenant_result = await session.execute(
        text("""
            INSERT INTO shared.tenants (slug, name, status, plan)
            VALUES (:slug, :name, 'active', 'standard')
            RETURNING id
        """),
        {"slug": TENANT_SLUG, "name": "Dev Fund LLC"},
    )
    tenant_id = tenant_result.fetchone()[0]

    from app.core.security import hash_password
    user_result = await session.execute(
        text("""
            INSERT INTO shared.users (email, full_name, password_hash, status)
            VALUES (:email, :name, :hash, 'active')
            RETURNING id
        """),
        {"email": ADMIN_EMAIL, "name": ADMIN_NAME, "hash": hash_password(ADMIN_PASSWORD)},
    )
    user_id = user_result.fetchone()[0]

    await session.execute(
        text("""
            INSERT INTO shared.tenant_memberships (tenant_id, user_id, role, is_active)
            VALUES (:tid, :uid, 'admin', true)
        """),
        {"tid": str(tenant_id), "uid": str(user_id)},
    )

    print(f"  ✓ tenant: {TENANT_SLUG} (id={tenant_id})")
    print(f"  ✓ admin user: {ADMIN_EMAIL} (id={user_id})")


async def seed_tenant(session):
    """Create portfolio, counterparties, and loans in tenant schema."""
    from sqlalchemy import text

    await session.execute(text(f"SET search_path TO tenant_{TENANT_SLUG}, shared, public"))

    # Portfolio
    portfolio_result = await session.execute(
        text("""
            INSERT INTO portfolio (code, name, fund_type, base_currency, inception_date, status)
            VALUES ('DEV001', 'Dev Credit Fund I', 'Private Credit', 'USD', '2022-01-01', 'active')
            RETURNING id
        """)
    )
    portfolio_id = portfolio_result.fetchone()[0]
    print(f"  ✓ portfolio: Dev Credit Fund I (id={portfolio_id})")

    # Borrowers
    borrower_ids = []
    for name, entity_type in [
        ("Acme Holdings LLC", "LLC"),
        ("Globex Corp", "Corp"),
        ("Initech Industries LP", "LP"),
    ]:
        r = await session.execute(
            text("""
                INSERT INTO counterparty (type, legal_name, entity_type, kyc_status, country)
                VALUES ('borrower', :name, :et, 'approved', 'US')
                RETURNING id
            """),
            {"name": name, "et": entity_type},
        )
        borrower_ids.append(r.fetchone()[0])
    print(f"  ✓ borrowers: {len(borrower_ids)} created")

    # Loans
    loans = [
        {
            "loan_number": "DEV-000001",
            "loan_name": "Acme Term Loan",
            "borrower_id": borrower_ids[0],
            "original_balance": Decimal("5000000"),
            "coupon_rate": Decimal("0.09"),
            "origination_date": date(2023, 1, 15),
            "maturity_date": date(2026, 1, 15),
            "payment_frequency": "QUARTERLY",
            "amortization_type": "bullet",
            "rate_type": "fixed",
            "status": "funded",
        },
        {
            "loan_number": "DEV-000002",
            "loan_name": "Globex Senior Secured",
            "borrower_id": borrower_ids[1],
            "original_balance": Decimal("10000000"),
            "coupon_rate": Decimal("0.0150"),  # spread over SOFR
            "origination_date": date(2023, 6, 1),
            "maturity_date": date(2026, 6, 1),
            "payment_frequency": "QUARTERLY",
            "amortization_type": "interest_only",
            "rate_type": "floating",
            "status": "funded",
        },
        {
            "loan_number": "DEV-000003",
            "loan_name": "Initech Bridge Loan",
            "borrower_id": borrower_ids[2],
            "original_balance": Decimal("2500000"),
            "coupon_rate": Decimal("0.12"),
            "origination_date": date(2023, 9, 1),
            "maturity_date": date(2024, 9, 1),  # already matured — good for testing
            "payment_frequency": "QUARTERLY",
            "amortization_type": "bullet",
            "rate_type": "fixed",
            "status": "delinquent",  # intentionally delinquent for demo
        },
    ]

    for loan_data in loans:
        r = await session.execute(
            text("""
                INSERT INTO loan (
                    portfolio_id, loan_number, loan_name, status,
                    primary_borrower_id, currency,
                    original_balance, current_principal, accrued_interest, accrued_fees,
                    rate_type, coupon_rate, day_count,
                    origination_date, maturity_date, payment_frequency, amortization_type,
                    grace_period_days, funded_at
                ) VALUES (
                    :portfolio_id, :loan_number, :loan_name, :status,
                    :borrower_id, 'USD',
                    :balance, :balance, 0, 0,
                    :rate_type, :coupon_rate, 'ACT/360',
                    :orig_date, :mat_date, :freq, :amort_type,
                    5, :orig_date
                )
                RETURNING id
            """),
            {
                "portfolio_id": str(portfolio_id),
                "loan_number": loan_data["loan_number"],
                "loan_name": loan_data["loan_name"],
                "status": loan_data["status"],
                "borrower_id": str(loan_data["borrower_id"]),
                "balance": loan_data["original_balance"],
                "rate_type": loan_data["rate_type"],
                "coupon_rate": loan_data["coupon_rate"],
                "orig_date": loan_data["origination_date"],
                "mat_date": loan_data["maturity_date"],
                "freq": loan_data["payment_frequency"],
                "amort_type": loan_data["amortization_type"],
            },
        )
        loan_id = r.fetchone()[0]
        print(f"  ✓ loan: {loan_data['loan_number']} — {loan_data['loan_name']} (id={loan_id})")


async def main():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.core.config import settings

    # Run migrations first
    await run_migrations()

    engine = create_async_engine(
        settings.DATABASE_URL,
        connect_args={"server_settings": {"search_path": "shared,public"}}
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    print("\nSeeding shared schema...")
    async with Session() as session:
        await seed_shared(session)
        await session.commit()

    print(f"\nSeeding tenant schema (tenant_{TENANT_SLUG})...")
    async with Session() as session:
        await seed_tenant(session)
        await session.commit()

    await engine.dispose()

    print(f"""
=== Dev environment ready ===

Login:
  POST http://localhost:8000/api/v1/auth/login
  {{
    "email": "{ADMIN_EMAIL}",
    "password": "{ADMIN_PASSWORD}",
    "tenant_slug": "{TENANT_SLUG}"
  }}

Docs: http://localhost:8000/docs
""")


if __name__ == "__main__":
    asyncio.run(main())
