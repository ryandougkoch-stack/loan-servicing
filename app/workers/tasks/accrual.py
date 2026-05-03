"""
app/workers/tasks/accrual.py

Daily interest accrual task.

For every active loan in a tenant:
  1. Calculate today's daily interest (principal × daily_rate)
  2. Add PIK interest to principal if applicable
  3. Create a journal entry (DR Accrued Interest Receivable / CR Interest Income)
  4. Update loan.accrued_interest
  5. Write an interest_accrual record for the audit trail

This task is idempotent: if it runs twice for the same date it will
detect the existing accrual_date record and skip.
"""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import structlog
from sqlalchemy import select, text

from app.db.session import get_tenant_session_context
from app.models.ledger import InterestAccrual, JournalEntry, JournalLine, LedgerAccount
from app.models.loan import Loan
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

CENTS = Decimal("0.01")
TEN_PLACES = Decimal("0.0000000001")

# Day count denominators
DAY_COUNT_DENOMINATORS = {
    "ACT/360": Decimal("360"),
    "ACT/365": Decimal("365"),
    "30/360":  Decimal("360"),
    "ACT/ACT": None,  # handled specially — denominator is days in the year
}

ACTIVE_STATUSES = {"funded", "modified", "delinquent", "default", "workout"}


@celery_app.task(name="app.workers.tasks.accrual.run_daily_accrual_all_tenants", bind=True)
def run_daily_accrual_all_tenants(self):
    """
    Fan out: load all active tenant slugs and dispatch one accrual task per tenant.
    """
    asyncio.run(_fan_out_accrual())


async def _fan_out_accrual():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import text as sa_text
    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with Session() as session:
        await session.execute(sa_text("SET search_path TO shared, public"))
        result = await session.execute(
            sa_text("SELECT slug FROM tenants WHERE status = 'active'")
        )
        slugs = [row.slug for row in result]

    await engine.dispose()

    for slug in slugs:
        run_daily_accrual_for_tenant.delay(slug, date.today().isoformat())


@celery_app.task(
    name="app.workers.tasks.accrual.run_daily_accrual_for_tenant",
    bind=True,
    max_retries=3,
    default_retry_delay=300,
)
def run_daily_accrual_for_tenant(self, tenant_slug: str, accrual_date_str: str):
    """
    Run daily interest accrual for all active loans in one tenant.
    """
    try:
        asyncio.run(_run_accrual(tenant_slug, date.fromisoformat(accrual_date_str)))
    except Exception as exc:
        logger.exception("accrual_task_failed", tenant=tenant_slug, date=accrual_date_str)
        raise self.retry(exc=exc)


async def _run_accrual(tenant_slug: str, accrual_date: date) -> None:
    async with get_tenant_session_context(tenant_slug) as session:
        # Load all active loans
        result = await session.execute(
            select(Loan).where(Loan.status.in_(ACTIVE_STATUSES))
        )
        loans = result.scalars().all()

        # Load account IDs
        acct_result = await session.execute(
            select(LedgerAccount.code, LedgerAccount.id).where(LedgerAccount.is_active == True)
        )
        accounts = {row.code: row.id for row in acct_result}

        skipped = 0
        processed = 0
        errors = 0

        for loan in loans:
            try:
                accrued = await _accrue_loan(session, loan, accrual_date, accounts)
                if accrued:
                    processed += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(
                    "loan_accrual_failed",
                    loan_id=str(loan.id),
                    loan_number=loan.loan_number,
                    error=str(e),
                )
                errors += 1

        logger.info(
            "accrual_completed",
            tenant=tenant_slug,
            date=accrual_date.isoformat(),
            processed=processed,
            skipped=skipped,
            errors=errors,
        )


async def _accrue_loan(
    session,
    loan: Loan,
    accrual_date: date,
    accounts: dict,
) -> bool:
    """
    Accrue one day's interest for a single loan.
    Returns True if accrual was created, False if skipped (already exists).
    """
    # Idempotency check
    existing = await session.execute(
        select(InterestAccrual).where(
            InterestAccrual.loan_id == loan.id,
            InterestAccrual.accrual_date == accrual_date,
            InterestAccrual.is_reversed == False,
        )
    )
    if existing.scalar_one_or_none():
        return False  # Already accrued for this date

    # Determine effective rate
    effective_rate = _get_effective_rate(loan, accrual_date)
    if effective_rate is None or effective_rate <= 0:
        return False

    # Calculate daily rate and accrual amount
    denominator = _get_day_count_denominator(loan.day_count, accrual_date)
    daily_rate = (effective_rate / denominator).quantize(TEN_PLACES)
    accrued_amount = (loan.current_principal * daily_rate).quantize(CENTS, rounding=ROUND_HALF_UP)

    # PIK: capitalise into principal
    pik_amount = Decimal("0")
    if loan.pik_rate and loan.pik_rate > 0:
        pik_daily = (loan.pik_rate / denominator).quantize(TEN_PLACES)
        pik_amount = (loan.current_principal * pik_daily).quantize(CENTS, rounding=ROUND_HALF_UP)

    now = datetime.now(timezone.utc)

    # Create journal entry
    entry_number = f"ACC-{accrual_date.strftime('%Y%m%d')}-{str(loan.id)[:8]}"
    entry = JournalEntry(
        entry_number=entry_number,
        loan_id=loan.id,
        portfolio_id=loan.portfolio_id,
        entry_type="accrual",
        entry_date=accrual_date,
        effective_date=accrual_date,
        description=f"Daily interest accrual — {loan.loan_number}",
        status="posted",
        posted_by=_system_user_id(),
        created_at=now,
    )
    session.add(entry)
    await session.flush()

    # DR Accrued Interest Receivable
    session.add(JournalLine(
        journal_entry_id=entry.id,
        line_number=1,
        account_id=accounts["1110"],
        debit_amount=accrued_amount,
        credit_amount=Decimal("0"),
        memo=f"Daily accrual @ {float(effective_rate):.4%}",
    ))
    # CR Interest Income
    session.add(JournalLine(
        journal_entry_id=entry.id,
        line_number=2,
        account_id=accounts["4010"],
        debit_amount=Decimal("0"),
        credit_amount=accrued_amount,
        memo=f"Interest income — {loan.loan_number}",
    ))

    # If PIK: additionally DR Loans Receivable / CR Interest Income
    if pik_amount > 0:
        session.add(JournalLine(
            journal_entry_id=entry.id,
            line_number=3,
            account_id=accounts["1100"],  # Loans Receivable - Principal
            debit_amount=pik_amount,
            credit_amount=Decimal("0"),
            memo="PIK interest capitalised to principal",
        ))
        session.add(JournalLine(
            journal_entry_id=entry.id,
            line_number=4,
            account_id=accounts["4010"],
            debit_amount=Decimal("0"),
            credit_amount=pik_amount,
            memo="PIK interest income",
        ))

    # Create accrual record
    accrual = InterestAccrual(
        loan_id=loan.id,
        accrual_date=accrual_date,
        beginning_balance=loan.current_principal,
        daily_rate=daily_rate,
        accrued_amount=accrued_amount,
        pik_amount=pik_amount,
        day_count_used=loan.day_count,
        rate_snapshot=effective_rate,
        journal_entry_id=entry.id,
        created_at=now,
    )
    session.add(accrual)

    # Update loan balances
    loan.accrued_interest += accrued_amount
    if pik_amount > 0:
        loan.current_principal += pik_amount  # PIK capitalises into principal

    return True


def _get_effective_rate(loan: Loan, as_of_date: date) -> Decimal | None:
    """
    Return the effective annual interest rate for a loan on a given date.
    For floating rate loans, this should look up the latest rate reset.
    For MVP, returns coupon_rate with floor/cap applied.
    """
    if loan.rate_type == "fixed":
        return loan.coupon_rate
    elif loan.rate_type in ("floating", "mixed"):
        # In full implementation: look up rate_reset table for latest effective rate
        # For MVP: use coupon_rate as a proxy (will be replaced when rate reset service is built)
        base_rate = loan.coupon_rate or Decimal("0")
        spread = loan.spread or Decimal("0")
        rate = base_rate + spread
        if loan.rate_floor:
            rate = max(rate, loan.rate_floor)
        if loan.rate_cap:
            rate = min(rate, loan.rate_cap)
        return rate
    elif loan.rate_type == "pik":
        return loan.coupon_rate  # entire coupon is PIK
    elif loan.rate_type == "zero_coupon":
        return None  # no daily accrual; yield is recognised at payoff
    return loan.coupon_rate


def _get_day_count_denominator(day_count: str, accrual_date: date) -> Decimal:
    if day_count == "ACT/ACT":
        import calendar
        days_in_year = 366 if calendar.isleap(accrual_date.year) else 365
        return Decimal(str(days_in_year))
    return DAY_COUNT_DENOMINATORS.get(day_count, Decimal("360"))


def _system_user_id():
    """Return a sentinel UUID representing the system/automation user."""
    import uuid
    return uuid.UUID("00000000-0000-0000-0000-000000000001")
