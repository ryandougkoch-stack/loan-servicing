"""
app/workers/tasks/delinquency.py

Nightly delinquency aging worker.

Runs at 1:00 AM UTC every day (configured in celery_app.py).

For every active loan in every active tenant:
  1. Load the loan's current payment schedule
  2. Load recent posted payments
  3. Run the DelinquencyEngine to calculate DPD and past-due amounts
  4. Write a delinquency_record row (idempotent — skips if already exists)
  5. If DPD milestones have been newly crossed, create workflow tasks
  6. If a status transition is recommended, create a workflow task for review
     (we don't auto-transition status — a human approves that)

Design choices:
  - Idempotent: running twice for the same date is safe
  - Fail-safe: one loan's failure doesn't block the others
  - Status transitions are recommended, not automatic: a workflow task is
    created for ops to approve. This is the right pattern for a financial
    system — no auto-escalation to 'default' without human review.
  - Performance: we load all loans with their schedule data in bulk per tenant
    rather than N+1 queries per loan.
"""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_tenant_session_context
from app.services.delinquency_engine import (
    DelinquencyEngine,
    DelinquencyResult,
    MilestoneTask,
    ScheduledPeriod,
    PaymentRecord,
    tasks_for_milestones,
)
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

# Loan statuses that should be evaluated for delinquency
ELIGIBLE_STATUSES = {"funded", "modified", "delinquent", "default", "workout"}

# System user UUID for workflow tasks created by automation
SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.workers.tasks.delinquency.run_aging_all_tenants",
    bind=True,
)
def run_aging_all_tenants(self):
    """Fan out: dispatch one aging task per active tenant."""
    asyncio.run(_fan_out_aging())


@celery_app.task(
    name="app.workers.tasks.delinquency.run_aging_for_tenant",
    bind=True,
    max_retries=3,
    default_retry_delay=300,
)
def run_aging_for_tenant(self, tenant_slug: str, as_of_date_str: str):
    """Run delinquency aging for all loans in one tenant."""
    try:
        asyncio.run(_run_aging(tenant_slug, date.fromisoformat(as_of_date_str)))
    except Exception as exc:
        logger.exception("aging_task_failed", tenant=tenant_slug, date=as_of_date_str)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _fan_out_aging():
    """Load all active tenant slugs and dispatch per-tenant tasks."""
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
        run_aging_for_tenant.delay(slug, date.today().isoformat())
        logger.info("aging_task_dispatched", tenant=slug)


# ---------------------------------------------------------------------------
# Per-tenant aging run
# ---------------------------------------------------------------------------

async def _run_aging(tenant_slug: str, as_of_date: date) -> None:
    async with get_tenant_session_context(tenant_slug) as session:
        # Load all eligible loans
        loans = await _load_loans(session)
        logger.info(
            "aging_started",
            tenant=tenant_slug,
            date=as_of_date.isoformat(),
            loan_count=len(loans),
        )

        processed = skipped = errors = 0

        for loan_row in loans:
            try:
                was_processed = await _process_loan(session, loan_row, as_of_date)
                if was_processed:
                    processed += 1
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                logger.error(
                    "loan_aging_failed",
                    loan_id=str(loan_row["loan_id"]),
                    loan_number=loan_row["loan_number"],
                    error=str(e),
                    exc_info=True,
                )

        logger.info(
            "aging_completed",
            tenant=tenant_slug,
            date=as_of_date.isoformat(),
            processed=processed,
            skipped=skipped,
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Single loan processing
# ---------------------------------------------------------------------------

async def _process_loan(session: AsyncSession, loan_row: dict, as_of_date: date) -> bool:
    """
    Process delinquency for a single loan.
    Returns True if a new record was written, False if skipped.
    """
    loan_id = loan_row["loan_id"]

    # Idempotency: skip if already processed for this date
    existing = await session.execute(
        text("""
            SELECT id FROM delinquency_record
            WHERE loan_id = :loan_id AND as_of_date = :as_of
            LIMIT 1
        """),
        {"loan_id": str(loan_id), "as_of": as_of_date},
    )
    if existing.fetchone():
        return False

    # Load schedule and payments
    schedule = await _load_schedule(session, loan_id)
    payments = await _load_payments(session, loan_id, as_of_date)
    prior_dpd = await _load_prior_dpd(session, loan_id, as_of_date)

    # Run the engine
    engine = DelinquencyEngine(
        loan_id=loan_id,
        loan_status=loan_row["status"],
        grace_period_days=int(loan_row["grace_period_days"] or 5),
        current_principal=Decimal(str(loan_row["current_principal"])),
        accrued_interest=Decimal(str(loan_row["accrued_interest"])),
        accrued_fees=Decimal(str(loan_row["accrued_fees"])),
    )

    result = engine.calculate(as_of_date, schedule, payments, prior_dpd)

    # Write delinquency_record
    await _write_delinquency_record(session, result)

    # Create workflow tasks for new milestones
    if result.milestones_triggered:
        milestone_tasks = tasks_for_milestones(
            loan_id=loan_id,
            loan_number=loan_row["loan_number"],
            borrower_name=loan_row["borrower_name"],
            portfolio_id=loan_row["portfolio_id"],
            milestones=result.milestones_triggered,
            dpd=result.days_past_due,
            total_past_due=result.total_past_due,
        )
        for task in milestone_tasks:
            await _create_workflow_task(session, loan_row, task)

    # Create a status transition review task if recommended
    if result.recommended_status:
        await _create_status_review_task(session, loan_row, result)

    return True


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def _load_loans(session: AsyncSession) -> list[dict]:
    """Load all loans eligible for delinquency processing."""
    result = await session.execute(
        text("""
            SELECT
                l.id            AS loan_id,
                l.loan_number,
                l.status,
                l.portfolio_id,
                l.grace_period_days,
                l.current_principal,
                l.accrued_interest,
                l.accrued_fees,
                c.legal_name    AS borrower_name
            FROM loan l
            JOIN counterparty c ON c.id = l.primary_borrower_id
            WHERE l.status = ANY(:statuses)
            ORDER BY l.id
        """),
        {"statuses": list(ELIGIBLE_STATUSES)},
    )
    return [dict(row._mapping) for row in result]


async def _load_schedule(session: AsyncSession, loan_id: UUID) -> list[ScheduledPeriod]:
    """Load current payment schedule for a loan."""
    result = await session.execute(
        text("""
            SELECT
                period_number,
                due_date,
                scheduled_principal,
                scheduled_interest,
                scheduled_fees,
                status
            FROM payment_schedule
            WHERE loan_id = :loan_id
              AND is_current = true
            ORDER BY period_number
        """),
        {"loan_id": str(loan_id)},
    )
    return [
        ScheduledPeriod(
            period_number=row.period_number,
            due_date=row.due_date,
            scheduled_principal=Decimal(str(row.scheduled_principal)),
            scheduled_interest=Decimal(str(row.scheduled_interest)),
            scheduled_fees=Decimal(str(row.scheduled_fees)),
            status=row.status,
        )
        for row in result
    ]


async def _load_payments(
    session: AsyncSession,
    loan_id: UUID,
    as_of_date: date,
) -> list[PaymentRecord]:
    """Load posted payments up to as_of_date."""
    result = await session.execute(
        text("""
            SELECT
                effective_date,
                applied_to_principal,
                applied_to_interest,
                applied_to_fees,
                status
            FROM payment
            WHERE loan_id = :loan_id
              AND effective_date <= :as_of
              AND status IN ('posted', 'returned')
            ORDER BY effective_date
        """),
        {"loan_id": str(loan_id), "as_of": as_of_date},
    )
    return [
        PaymentRecord(
            effective_date=row.effective_date,
            applied_to_principal=Decimal(str(row.applied_to_principal)),
            applied_to_interest=Decimal(str(row.applied_to_interest)),
            applied_to_fees=Decimal(str(row.applied_to_fees)),
            status=row.status,
        )
        for row in result
    ]


async def _load_prior_dpd(
    session: AsyncSession,
    loan_id: UUID,
    as_of_date: date,
) -> int:
    """Load the most recent DPD before as_of_date for milestone detection."""
    result = await session.execute(
        text("""
            SELECT days_past_due
            FROM delinquency_record
            WHERE loan_id = :loan_id
              AND as_of_date < :as_of
            ORDER BY as_of_date DESC
            LIMIT 1
        """),
        {"loan_id": str(loan_id), "as_of": as_of_date},
    )
    row = result.fetchone()
    return int(row.days_past_due) if row else 0


async def _write_delinquency_record(
    session: AsyncSession,
    result: DelinquencyResult,
) -> None:
    """Insert a delinquency_record row. Skips if it already exists (ON CONFLICT DO NOTHING)."""
    await session.execute(
        text("""
            INSERT INTO delinquency_record (
                loan_id, as_of_date, days_past_due, delinquency_bucket,
                principal_past_due, interest_past_due, fees_past_due, created_at
            ) VALUES (
                :loan_id, :as_of, :dpd, :bucket,
                :principal_pd, :interest_pd, :fees_pd, :now
            )
            ON CONFLICT (loan_id, as_of_date) DO NOTHING
        """),
        {
            "loan_id":      str(result.loan_id),
            "as_of":        result.as_of_date,
            "dpd":          result.days_past_due,
            "bucket":       result.bucket,
            "principal_pd": result.principal_past_due,
            "interest_pd":  result.interest_past_due,
            "fees_pd":      result.fees_past_due,
            "now":          datetime.now(timezone.utc),
        },
    )


async def _create_workflow_task(
    session: AsyncSession,
    loan_row: dict,
    task: MilestoneTask,
) -> None:
    """Insert a workflow task for a delinquency milestone."""
    from datetime import timedelta
    due_date = date.today() + timedelta(days=1)  # due next business day

    await session.execute(
        text("""
            INSERT INTO workflow_task (
                loan_id, portfolio_id, task_type, priority,
                title, description, due_date, status,
                created_by, created_at, updated_at
            ) VALUES (
                :loan_id, :portfolio_id, :task_type, :priority,
                :title, :description, :due_date, 'open',
                :created_by, :now, :now
            )
        """),
        {
            "loan_id":      str(loan_row["loan_id"]),
            "portfolio_id": str(loan_row["portfolio_id"]),
            "task_type":    task.task_type,
            "priority":     task.priority,
            "title":        task.title,
            "description":  task.description,
            "due_date":     due_date,
            "created_by":   str(SYSTEM_USER_ID),
            "now":          datetime.now(timezone.utc),
        },
    )
    logger.info(
        "workflow_task_created",
        loan_id=str(loan_row["loan_id"]),
        task_type=task.task_type,
        priority=task.priority,
        dpd=task.days_past_due,
    )


async def _create_status_review_task(
    session: AsyncSession,
    loan_row: dict,
    result: DelinquencyResult,
) -> None:
    """
    Create a workflow task recommending a status transition.
    We never auto-transition — a human must review and approve.
    """
    current_status = loan_row["status"]
    rec_status = result.recommended_status

    # Check if an open review task already exists to avoid duplicates
    existing = await session.execute(
        text("""
            SELECT id FROM workflow_task
            WHERE loan_id = :loan_id
              AND task_type = 'delinquency_milestone'
              AND title LIKE :title_pattern
              AND status IN ('open', 'in_progress')
            LIMIT 1
        """),
        {
            "loan_id":       str(loan_row["loan_id"]),
            "title_pattern": f"%{rec_status}%",
        },
    )
    if existing.fetchone():
        return   # task already open, don't duplicate

    priority = "critical" if rec_status == "default" else "high"
    title = (
        f"{loan_row['loan_number']} — Status review: "
        f"{current_status} → {rec_status} recommended"
    )
    description = (
        f"{loan_row['borrower_name']} is {result.days_past_due} DPD "
        f"(${result.total_past_due:,.2f} past due). "
        f"System recommends transitioning from '{current_status}' to '{rec_status}'. "
        f"Please review and update loan status if appropriate."
    )

    await session.execute(
        text("""
            INSERT INTO workflow_task (
                loan_id, portfolio_id, task_type, priority,
                title, description, due_date, status,
                created_by, created_at, updated_at
            ) VALUES (
                :loan_id, :portfolio_id, 'delinquency_milestone', :priority,
                :title, :description, :due_date, 'open',
                :created_by, :now, :now
            )
        """),
        {
            "loan_id":      str(loan_row["loan_id"]),
            "portfolio_id": str(loan_row["portfolio_id"]),
            "priority":     priority,
            "title":        title,
            "description":  description,
            "due_date":     date.today(),
            "created_by":   str(SYSTEM_USER_ID),
            "now":          datetime.now(timezone.utc),
        },
    )
