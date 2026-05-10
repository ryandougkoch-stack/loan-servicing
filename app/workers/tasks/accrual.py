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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import structlog
from sqlalchemy import func, or_, select, text

from app.db.session import get_tenant_session_context
from app.models.ledger import InterestAccrual, JournalEntry, JournalLine, LedgerAccount
from app.models.loan import Loan
from app.models.portfolio import LoanAllocation, Portfolio
from app.services.loan_allocation_service import LoanAllocationService
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
        # Load all active loans whose accrual window has begun. Originated loans
        # have accrual_start_date IS NULL — they accrue from the day status
        # enters ACTIVE_STATUSES. Converted loans have accrual_start_date set
        # to as_of_date and only start accruing on/after that date, so accruals
        # don't fire for the period when the prior servicer owned the loan.
        result = await session.execute(
            select(Loan).where(
                Loan.status.in_(ACTIVE_STATUSES),
                or_(
                    Loan.accrual_start_date.is_(None),
                    Loan.accrual_start_date <= accrual_date,
                ),
            )
        )
        loans = result.scalars().all()

        # Load account IDs
        acct_result = await session.execute(
            select(LedgerAccount.code, LedgerAccount.id).where(LedgerAccount.is_active == True)
        )
        accounts = {row.code: row.id for row in acct_result}

        # Pre-fetch portfolio codes for entry_number suffix uniqueness
        port_result = await session.execute(select(Portfolio.id, Portfolio.code))
        portfolio_codes = {row.id: row.code for row in port_result}

        # Pre-fetch each loan's most recent accrual_date so we can backfill
        # missing days in one pass. Without this, a loan converted with
        # as_of_date in the past silently skips the gap days; the worker only
        # ever wrote one row per nightly run.
        last_accrual_result = await session.execute(
            select(
                InterestAccrual.loan_id,
                func.max(InterestAccrual.accrual_date).label("max_date"),
            )
            .where(InterestAccrual.is_reversed == False)
            .group_by(InterestAccrual.loan_id)
        )
        last_accrual_map = {row.loan_id: row.max_date for row in last_accrual_result}

        skipped = 0
        processed = 0
        backfilled = 0
        errors = 0

        for loan in loans:
            try:
                missing_dates = _missing_accrual_dates(
                    loan, accrual_date, last_accrual_map.get(loan.id)
                )
                if not missing_dates:
                    skipped += 1
                    continue
                for d in missing_dates:
                    accrued = await _accrue_loan(session, loan, d, accounts, portfolio_codes)
                    if accrued:
                        processed += 1
                        if d != accrual_date:
                            backfilled += 1
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
            backfilled=backfilled,
            skipped=skipped,
            errors=errors,
        )


def _missing_accrual_dates(loan, accrual_date: date, last_accrual: date | None) -> list[date]:
    """
    Return the list of dates between the loan's accrual start and accrual_date
    (inclusive) that have no posted accrual yet.

    Start date is:
      - day after last_accrual (if any prior accruals exist for this loan)
      - else accrual_start_date for converted loans
      - else funded_at for originated loans
      - else None → no backfill (loan not yet eligible)

    Returns an ordered list of date objects. Empty if the loan is up-to-date.
    PIK loans must be processed in order: each day's accrual capitalises into
    the next day's principal, so the returned list MUST stay sorted ascending.
    """
    if last_accrual is not None:
        start = last_accrual + timedelta(days=1)
    elif loan.accrual_start_date is not None:
        start = loan.accrual_start_date
    elif loan.funded_at is not None:
        start = loan.funded_at
    else:
        return []
    if start > accrual_date:
        return []
    return [start + timedelta(days=i) for i in range((accrual_date - start).days + 1)]


async def _accrue_loan(
    session,
    loan: Loan,
    accrual_date: date,
    accounts: dict,
    portfolio_codes: dict,
) -> bool:
    """
    Accrue one day's interest for a single loan.

    Phase 3: write-time GL split. One InterestAccrual row (loan-level economics);
    one JournalEntry per active allocation, with DR/CR amounts pro-rated by
    ownership_pct. Cents balanced via largest-remainder so per-fund slices sum
    to the full daily accrual exactly.

    Returns True if accrual was created, False if skipped (already exists or
    no rate/no allocation).
    """
    # Idempotency check (loan-level — one accrual record per loan-day)
    existing = await session.execute(
        select(InterestAccrual).where(
            InterestAccrual.loan_id == loan.id,
            InterestAccrual.accrual_date == accrual_date,
            InterestAccrual.is_reversed == False,
        )
    )
    if existing.scalar_one_or_none():
        return False

    effective_rate = _get_effective_rate(loan, accrual_date)
    if effective_rate is None or effective_rate <= 0:
        return False

    denominator = _get_day_count_denominator(loan.day_count, accrual_date)
    daily_rate = (effective_rate / denominator).quantize(TEN_PLACES)
    accrued_amount = (loan.current_principal * daily_rate).quantize(CENTS, rounding=ROUND_HALF_UP)

    pik_amount = Decimal("0")
    if loan.pik_rate and loan.pik_rate > 0:
        pik_daily = (loan.pik_rate / denominator).quantize(TEN_PLACES)
        pik_amount = (loan.current_principal * pik_daily).quantize(CENTS, rounding=ROUND_HALF_UP)

    # Active allocations as-of accrual_date. Ordered by pct desc so the largest
    # share gets the lowest line numbers — purely cosmetic, but deterministic.
    alloc_result = await session.execute(
        select(LoanAllocation).where(
            LoanAllocation.loan_id == loan.id,
            LoanAllocation.effective_date <= accrual_date,
            or_(
                LoanAllocation.end_date.is_(None),
                LoanAllocation.end_date > accrual_date,
            ),
        ).order_by(LoanAllocation.ownership_pct.desc())
    )
    allocations = list(alloc_result.scalars().all())
    if not allocations:
        # Should never happen — every loan gets a 100% allocation at boarding.
        # Surface loudly rather than silently skip.
        logger.error(
            "loan_has_no_active_allocation",
            loan_id=str(loan.id),
            loan_number=loan.loan_number,
            accrual_date=accrual_date.isoformat(),
        )
        return False

    # Split the accrued + PIK amounts (in cents) across allocations.
    alloc_pcts = [(a.portfolio_id, a.ownership_pct) for a in allocations]
    accrued_cents = int((accrued_amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
    pik_cents = int((pik_amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
    accrued_split = LoanAllocationService.split_with_largest_remainder(accrued_cents, alloc_pcts)
    pik_split = (
        LoanAllocationService.split_with_largest_remainder(pik_cents, alloc_pcts)
        if pik_cents > 0
        else [(pid, 0) for pid, _ in alloc_pcts]
    )

    now = datetime.now(timezone.utc)

    # Create the InterestAccrual record first so journal entries can reference it.
    accrual = InterestAccrual(
        loan_id=loan.id,
        accrual_date=accrual_date,
        beginning_balance=loan.current_principal,
        daily_rate=daily_rate,
        accrued_amount=accrued_amount,
        pik_amount=pik_amount,
        day_count_used=loan.day_count,
        rate_snapshot=effective_rate,
        journal_entry_id=None,  # set below if there's exactly one entry
        created_at=now,
    )
    session.add(accrual)
    await session.flush()  # need accrual.id

    # One JournalEntry per allocation. Each fund's books stay independently correct
    # — Investran exports per portfolio_id and gets accurate sub-ledger.
    entries_created: list[JournalEntry] = []
    date_str = accrual_date.strftime("%Y%m%d")
    loan_prefix = str(loan.id)[:8]

    for alloc, (_, accrued_share_cents), (_, pik_share_cents) in zip(
        allocations, accrued_split, pik_split
    ):
        # Rounding can leave a fund with $0 of both. Don't write empty entries.
        if accrued_share_cents == 0 and pik_share_cents == 0:
            continue

        accrued_share = Decimal(accrued_share_cents) / 100
        pik_share = Decimal(pik_share_cents) / 100
        portfolio_code = portfolio_codes.get(alloc.portfolio_id, str(alloc.portfolio_id)[:8])

        entry = JournalEntry(
            entry_number=f"ACC-{date_str}-{loan_prefix}-{portfolio_code}",
            loan_id=loan.id,
            portfolio_id=alloc.portfolio_id,
            entry_type="accrual",
            entry_date=accrual_date,
            effective_date=accrual_date,
            description=(
                f"Daily interest accrual — {loan.loan_number} "
                f"({alloc.ownership_pct}% to {portfolio_code})"
            ),
            reference_id=accrual.id,
            reference_type="interest_accrual",
            status="posted",
            posted_by=_system_user_id(),
            created_at=now,
        )
        session.add(entry)
        await session.flush()  # need entry.id

        line_no = 0
        if accrued_share > 0:
            line_no += 1
            session.add(JournalLine(
                journal_entry_id=entry.id,
                line_number=line_no,
                account_id=accounts["1110"],
                debit_amount=accrued_share,
                credit_amount=Decimal("0"),
                memo=f"Daily accrual @ {float(effective_rate):.4%} — {alloc.ownership_pct}% share",
            ))
            line_no += 1
            session.add(JournalLine(
                journal_entry_id=entry.id,
                line_number=line_no,
                account_id=accounts["4010"],
                debit_amount=Decimal("0"),
                credit_amount=accrued_share,
                memo=f"Interest income — {loan.loan_number} ({alloc.ownership_pct}% share)",
            ))

        if pik_share > 0:
            line_no += 1
            session.add(JournalLine(
                journal_entry_id=entry.id,
                line_number=line_no,
                account_id=accounts["1100"],
                debit_amount=pik_share,
                credit_amount=Decimal("0"),
                memo=f"PIK interest capitalised — {alloc.ownership_pct}% share",
            ))
            line_no += 1
            session.add(JournalLine(
                journal_entry_id=entry.id,
                line_number=line_no,
                account_id=accounts["4010"],
                debit_amount=Decimal("0"),
                credit_amount=pik_share,
                memo=f"PIK interest income ({alloc.ownership_pct}% share)",
            ))

        entries_created.append(entry)

    # Preserve the 1-to-1 link for non-syndicated loans (single allocation case).
    # For syndicated (multiple entries), reverse-traverse via reference_id instead.
    if len(entries_created) == 1:
        accrual.journal_entry_id = entries_created[0].id

    # Loan-level balances move once, not per-fund.
    loan.accrued_interest += accrued_amount
    if pik_amount > 0:
        loan.current_principal += pik_amount

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
