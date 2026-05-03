"""
app/services/loan_service.py

Business logic for loan boarding, retrieval, status transitions,
and payment schedule generation.

The service layer sits between the API endpoints and the database.
It owns all domain rules — the endpoints just handle HTTP concerns.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import (
    ConflictError,
    LoanNotFoundError,
    ValidationError,
)
from app.models.loan import Loan
from app.models.ledger import Payment
from app.schemas.loan import LoanCreate, STATUS_TRANSITIONS

logger = structlog.get_logger(__name__)

# Loan number prefix — e.g. "LSP-000001"
LOAN_NUMBER_PREFIX = "LSP"


class LoanService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # -------------------------------------------------------------------------
    # Retrieval
    # -------------------------------------------------------------------------

    async def get_loan_or_404(self, loan_id: UUID) -> Loan:
        stmt = (
            select(Loan)
            .where(Loan.id == loan_id)
            .options(
                selectinload(Loan.primary_borrower),
                selectinload(Loan.guarantors),
                selectinload(Loan.collaterals),
                selectinload(Loan.covenants),
            )
        )
        result = await self.db.execute(stmt)
        loan = result.scalar_one_or_none()
        if not loan:
            raise LoanNotFoundError(f"Loan {loan_id} not found")
        return loan

    async def get_loan_by_number(self, loan_number: str) -> Optional[Loan]:
        result = await self.db.execute(
            select(Loan).where(Loan.loan_number == loan_number)
        )
        return result.scalar_one_or_none()

    async def list_loans(
        self,
        portfolio_id: Optional[UUID] = None,
        status_filter: Optional[str] = None,
        borrower_id: Optional[UUID] = None,
        client_id: Optional[UUID] = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[Loan], int]:
        stmt = select(Loan)
        count_stmt = select(func.count()).select_from(Loan)

        if portfolio_id:
            # Phase 2: filter through loan_allocation so syndicated loans surface
            # in every fund that holds them, not just the lead fund.
            from app.models.portfolio import LoanAllocation
            stmt = (
                stmt.join(LoanAllocation, LoanAllocation.loan_id == Loan.id)
                .where(
                    LoanAllocation.portfolio_id == portfolio_id,
                    LoanAllocation.end_date.is_(None),
                )
            )
            count_stmt = (
                count_stmt.join(LoanAllocation, LoanAllocation.loan_id == Loan.id)
                .where(
                    LoanAllocation.portfolio_id == portfolio_id,
                    LoanAllocation.end_date.is_(None),
                )
            )
        if status_filter:
            stmt = stmt.where(Loan.status == status_filter)
            count_stmt = count_stmt.where(Loan.status == status_filter)
        if borrower_id:
            stmt = stmt.where(Loan.primary_borrower_id == borrower_id)
            count_stmt = count_stmt.where(Loan.primary_borrower_id == borrower_id)
        if client_id:
            from app.models.portfolio import Portfolio
            stmt = stmt.join(Portfolio, Portfolio.id == Loan.portfolio_id).where(Portfolio.client_id == client_id)
            count_stmt = count_stmt.join(Portfolio, Portfolio.id == Loan.portfolio_id).where(Portfolio.client_id == client_id)

        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = (
            stmt
            .order_by(Loan.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        loans = (await self.db.execute(stmt)).scalars().all()
        return list(loans), total

    # -------------------------------------------------------------------------
    # Creation / boarding
    # -------------------------------------------------------------------------

    async def create_loan(self, payload: LoanCreate, created_by: UUID) -> Loan:
        # Check for duplicate loan number if provided
        if payload.loan_number:
            existing = await self.get_loan_by_number(payload.loan_number)
            if existing:
                raise ConflictError(
                    f"Loan number '{payload.loan_number}' already exists",
                    detail={"loan_id": str(existing.id)},
                )
            loan_number = payload.loan_number
        else:
            loan_number = await self._generate_loan_number()

        loan = Loan(
            portfolio_id=payload.portfolio_id,
            loan_number=loan_number,
            loan_name=payload.loan_name,
            status="boarding",
            primary_borrower_id=payload.primary_borrower_id,
            currency=payload.currency,
            original_balance=payload.original_balance,
            commitment_amount=payload.commitment_amount,
            current_principal=payload.original_balance,  # starts at original balance
            accrued_interest=Decimal("0"),
            accrued_fees=Decimal("0"),
            rate_type=payload.rate_type,
            coupon_rate=payload.coupon_rate,
            pik_rate=payload.pik_rate,
            rate_floor=payload.rate_floor,
            rate_cap=payload.rate_cap,
            spread=payload.spread,
            index_code=payload.index_code,
            day_count=payload.day_count,
            origination_date=payload.origination_date,
            first_payment_date=payload.first_payment_date,
            maturity_date=payload.maturity_date,
            payment_frequency=payload.payment_frequency,
            amortization_type=payload.amortization_type,
            interest_only_period_months=payload.interest_only_period_months,
            balloon_amount=payload.balloon_amount,
            grace_period_days=payload.grace_period_days,
            late_fee_type=payload.late_fee_type,
            late_fee_amount=payload.late_fee_amount,
            default_rate=payload.default_rate,
            investran_loan_id=payload.investran_loan_id,
            servicer_notes=payload.servicer_notes,
            created_by=created_by,
        )

        self.db.add(loan)
        await self.db.flush()  # get the ID without committing

        # Auto-create the default 100% allocation so the loan always has an
        # active row in loan_allocation. Phase 2 KPI rollups join through
        # loan_allocation, so a loan without one would silently disappear.
        from app.services.loan_allocation_service import LoanAllocationService
        await LoanAllocationService(self.db).create_initial_allocation(loan, created_by)

        logger.info(
            "loan_created",
            loan_id=str(loan.id),
            loan_number=loan.loan_number,
            created_by=str(created_by),
        )
        return loan

    # -------------------------------------------------------------------------
    # Status transitions
    # -------------------------------------------------------------------------

    async def update_status(
        self,
        loan_id: UUID,
        new_status: str,
        updated_by: UUID,
        notes: Optional[str] = None,
    ) -> Loan:
        loan = await self.get_loan_or_404(loan_id)
        current_status = loan.status

        allowed = STATUS_TRANSITIONS.get(current_status, set())
        if new_status not in allowed:
            raise ValidationError(
                f"Cannot transition loan from '{current_status}' to '{new_status}'",
                detail={"allowed_transitions": list(allowed)},
            )

        loan.status = new_status

        # Set timestamps for specific transitions
        now = datetime.now(timezone.utc)
        if new_status == "funded" and not loan.funded_at:
            loan.funded_at = date.today()
            loan.boarding_completed_at = now
        elif new_status == "paid_off":
            loan.paid_off_at = date.today()
        elif new_status == "default":
            if not loan.default_triggered_at:
                loan.default_triggered_at = date.today()

        await self.db.flush()

        logger.info(
            "loan_status_updated",
            loan_id=str(loan_id),
            from_status=current_status,
            to_status=new_status,
            updated_by=str(updated_by),
        )
        return loan

    # -------------------------------------------------------------------------
    # Payment schedule
    # -------------------------------------------------------------------------

    async def get_payment_schedule(self, loan_id: UUID) -> list[dict]:
        from app.models.loan import Loan
        from sqlalchemy import text

        result = await self.db.execute(
            select(Loan).where(Loan.id == loan_id)
        )
        loan = result.scalar_one_or_none()
        if not loan:
            raise LoanNotFoundError(f"Loan {loan_id} not found")

        # Import here to avoid circular imports
        from app.models.ledger import Payment
        stmt = (
            select(
                __import__("app.models.loan", fromlist=["PaymentSchedule"]).PaymentSchedule
                if hasattr(__import__("app.models.loan", fromlist=["PaymentSchedule"]), "PaymentSchedule")
                else None
            )
        )

        # Direct query for schedule periods
        from sqlalchemy import text
        rows = await self.db.execute(
            text("""
                SELECT
                    id, period_number, period_start_date, period_end_date,
                    due_date, scheduled_principal, scheduled_interest,
                    scheduled_fees, scheduled_escrow, total_scheduled,
                    days_in_period, interest_rate_used, beginning_balance,
                    ending_balance, status
                FROM payment_schedule
                WHERE loan_id = :loan_id AND is_current = true
                ORDER BY period_number
            """),
            {"loan_id": str(loan_id)},
        )
        return [dict(row._mapping) for row in rows]

    async def generate_schedule(self, loan: Loan) -> int:
        """
        Generate the payment schedule using the amortization engine.
        Supersedes existing open periods.
        Returns the number of periods generated.
        """
        from app.services.amortization_engine import AmortizationEngine

        engine = AmortizationEngine(loan)
        periods = engine.generate()

        # Mark existing open periods as superseded
        await self.db.execute(
            update(PaymentSchedule)
            .where(
                PaymentSchedule.loan_id == loan.id,
                PaymentSchedule.status == "open",
                PaymentSchedule.is_current == True,
            )
            .values(is_current=False)
        )

        # Insert new periods
        for period in periods:
            from datetime import datetime, timezone
            d = period.to_dict()
            d.pop('total_scheduled', None)
            now = datetime.now(timezone.utc)
            d['created_at'] = now
            d['updated_at'] = now
            self.db.add(PaymentSchedule(loan_id=loan.id, **d))

        await self.db.flush()

        logger.info(
            "schedule_generated",
            loan_id=str(loan.id),
            periods=len(periods),
        )
        return len(periods)

    async def create_modification(
        self,
        loan_id,
        modification_type: str,
        effective_date,
        created_by,
        new_rate=None,
        new_maturity_date=None,
        new_payment_frequency=None,
        description=None,
        notes=None,
    ):
        from app.models.portfolio import LoanModification
        from datetime import datetime, timezone
        loan = await self.get_loan_or_404(loan_id)
        now = datetime.now(timezone.utc)
        mod = LoanModification(
            loan_id=loan.id,
            modification_type=modification_type,
            effective_date=effective_date,
            description=description,
            status="pending",
            prior_rate=loan.coupon_rate if modification_type == "rate_change" else None,
            new_rate=new_rate if modification_type == "rate_change" else None,
            prior_maturity=loan.maturity_date if modification_type == "maturity_extension" else None,
            new_maturity=new_maturity_date if modification_type == "maturity_extension" else None,
            requested_by=created_by,
            created_at=now,
        )
        self.db.add(mod)
        await self.db.flush()
        if modification_type == "rate_change" and new_rate is not None:
            loan.coupon_rate = new_rate
            if loan.status == "funded":
                loan.status = "modified"
        elif modification_type == "maturity_extension" and new_maturity_date is not None:
            loan.maturity_date = new_maturity_date
            if new_payment_frequency:
                loan.payment_frequency = new_payment_frequency
            if loan.status == "funded":
                loan.status = "modified"
        mod.status = "applied"
        mod.applied_by = created_by
        mod.applied_at = now
        await self.generate_schedule(loan)
        logger.info("modification_applied", loan_id=str(loan.id),
                    modification_type=modification_type, modification_id=str(mod.id))
        return mod

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _generate_loan_number(self) -> str:
        """Generate a sequential loan number: LSP-000001, LSP-000002, etc."""
        result = await self.db.execute(
            select(func.count()).select_from(Loan)
        )
        count = result.scalar_one()
        return f"{LOAN_NUMBER_PREFIX}-{(count + 1):06d}"



# Import PaymentSchedule here to avoid circular at module level
from sqlalchemy import update as _update
try:
    from app.models.schedule import PaymentSchedule
except ImportError:
    PaymentSchedule = None
