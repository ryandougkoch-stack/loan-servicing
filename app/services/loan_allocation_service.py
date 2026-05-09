"""
app/services/loan_allocation_service.py

Business logic for loan_allocation: a syndicated loan held across multiple
portfolios with effective-dated ownership_pct rows.

Active-on-date semantics (half-open interval):
    effective_date <= D AND (end_date IS NULL OR end_date > D)
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.loan import Loan
from app.models.portfolio import LoanAllocation, Portfolio
from app.schemas.allocation import AllocationItem
from app.services.activity_service import ActivityService

logger = structlog.get_logger(__name__)

PCT_TOTAL = Decimal("100")
PCT_TOLERANCE = Decimal("0.000001")  # 1e-6 — matches NUMERIC(9,6) precision


class LoanAllocationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # -------------------------------------------------------------------------
    # Reads
    # -------------------------------------------------------------------------

    async def get_active_allocations(
        self, loan_id: UUID, as_of_date: Optional[date] = None
    ) -> list[LoanAllocation]:
        """Rows active on as_of_date (default: today), ordered by ownership_pct desc."""
        if as_of_date is None:
            as_of_date = date.today()
        stmt = (
            select(LoanAllocation)
            .where(
                LoanAllocation.loan_id == loan_id,
                LoanAllocation.effective_date <= as_of_date,
                or_(
                    LoanAllocation.end_date.is_(None),
                    LoanAllocation.end_date > as_of_date,
                ),
            )
            .order_by(LoanAllocation.ownership_pct.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_history(self, loan_id: UUID) -> list[LoanAllocation]:
        """All rows for a loan (current + historical), most-recent effective_date first."""
        stmt = (
            select(LoanAllocation)
            .where(LoanAllocation.loan_id == loan_id)
            .order_by(
                LoanAllocation.effective_date.desc(),
                LoanAllocation.ownership_pct.desc(),
            )
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_loans_for_portfolio(
        self, portfolio_id: UUID, as_of_date: Optional[date] = None
    ) -> list[dict]:
        """Loans + ownership_pct held by a portfolio, active on as_of_date."""
        if as_of_date is None:
            as_of_date = date.today()
        stmt = (
            select(LoanAllocation, Loan)
            .join(Loan, Loan.id == LoanAllocation.loan_id)
            .where(
                LoanAllocation.portfolio_id == portfolio_id,
                LoanAllocation.effective_date <= as_of_date,
                or_(
                    LoanAllocation.end_date.is_(None),
                    LoanAllocation.end_date > as_of_date,
                ),
            )
            .order_by(Loan.loan_number)
        )
        result = await self.db.execute(stmt)
        return [
            {
                "allocation_id": a.id,
                "loan_id": a.loan_id,
                "loan_number": l.loan_number,
                "loan_name": l.loan_name,
                "ownership_pct": a.ownership_pct,
                "effective_date": a.effective_date,
                "end_date": a.end_date,
            }
            for a, l in result.all()
        ]

    # -------------------------------------------------------------------------
    # Writes
    # -------------------------------------------------------------------------

    async def create_initial_allocation(
        self, loan: Loan, created_by: UUID, effective_date: Optional[date] = None
    ) -> LoanAllocation:
        """
        Insert the default 100% allocation for a brand-new loan.
        Called from LoanService.create_loan after the loan row is flushed.
        Idempotent: skips if any allocation row already exists for this loan.

        effective_date defaults to loan.origination_date for fresh originations.
        Converted loans pass as_of_date so the allocation doesn't back-date to a
        period when we didn't own the loan.
        """
        existing = await self.db.execute(
            select(LoanAllocation.id)
            .where(LoanAllocation.loan_id == loan.id)
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return None  # type: ignore[return-value]

        alloc = LoanAllocation(
            loan_id=loan.id,
            portfolio_id=loan.portfolio_id,
            ownership_pct=PCT_TOTAL,
            effective_date=effective_date or loan.origination_date,
            end_date=None,
            notes="Initial allocation (auto-created at loan boarding)",
            created_by=created_by,
        )
        self.db.add(alloc)
        await self.db.flush()
        return alloc

    async def update_allocations(
        self,
        loan_id: UUID,
        new_set: list[AllocationItem],
        effective_date: date,
        user_id: UUID,
        notes: Optional[str] = None,
    ) -> list[LoanAllocation]:
        """
        Replace allocations effective from `effective_date` forward.
        - End-dates currently-active rows whose effective_date < effective_date
        - Deletes any rows with effective_date >= effective_date (supersedes future-dated changes)
        - Inserts the new set, all sharing this effective_date
        - Validates new set sums to 100% within tolerance
        - Validates each portfolio_id exists
        - Logs to loan_activity as event_type='allocation_changed'
        """
        # 1) Validate sum
        total = sum((item.ownership_pct for item in new_set), Decimal("0"))
        if abs(total - PCT_TOTAL) > PCT_TOLERANCE:
            raise ValidationError(
                f"Allocations must sum to 100. Got {total}.",
                detail={"sum": str(total)},
            )

        # 2) Validate loan exists
        loan = (
            await self.db.execute(select(Loan).where(Loan.id == loan_id))
        ).scalar_one_or_none()
        if loan is None:
            raise NotFoundError(f"Loan {loan_id} not found")

        # 3) Validate all portfolios exist
        portfolio_ids = [item.portfolio_id for item in new_set]
        existing_ids = (
            await self.db.execute(
                select(Portfolio.id).where(Portfolio.id.in_(portfolio_ids))
            )
        ).scalars().all()
        missing = set(portfolio_ids) - set(existing_ids)
        if missing:
            raise ValidationError(
                f"Unknown portfolio_id(s): {sorted(str(p) for p in missing)}"
            )

        # 4) Lock existing rows for this loan to serialize concurrent writes
        await self.db.execute(
            select(LoanAllocation)
            .where(LoanAllocation.loan_id == loan_id)
            .with_for_update()
        )

        # Capture prior set (active at effective_date BEFORE we mutate) for the audit log
        prior = await self.get_active_allocations(loan_id, as_of_date=effective_date)

        # 5) Delete rows with effective_date >= effective_date (superseded future-dated)
        await self.db.execute(
            delete(LoanAllocation).where(
                LoanAllocation.loan_id == loan_id,
                LoanAllocation.effective_date >= effective_date,
            )
        )

        # 6) End-date rows that would otherwise still be active at effective_date
        now = datetime.now(timezone.utc)
        await self.db.execute(
            update(LoanAllocation)
            .where(
                LoanAllocation.loan_id == loan_id,
                LoanAllocation.effective_date < effective_date,
                or_(
                    LoanAllocation.end_date.is_(None),
                    LoanAllocation.end_date > effective_date,
                ),
            )
            .values(end_date=effective_date, updated_at=now)
        )

        # 7) Insert new rows
        new_rows: list[LoanAllocation] = []
        for item in new_set:
            row = LoanAllocation(
                loan_id=loan_id,
                portfolio_id=item.portfolio_id,
                ownership_pct=item.ownership_pct,
                effective_date=effective_date,
                end_date=None,
                notes=notes,
                created_by=user_id,
            )
            self.db.add(row)
            new_rows.append(row)
        await self.db.flush()

        # 8) Activity log
        prior_summary = [
            {"portfolio_id": str(p.portfolio_id), "ownership_pct": str(p.ownership_pct)}
            for p in prior
        ]
        new_summary = [
            {"portfolio_id": str(r.portfolio_id), "ownership_pct": str(r.ownership_pct)}
            for r in new_rows
        ]
        is_syndicated = len(new_set) > 1
        activity = ActivityService(self.db)
        await activity.log(
            loan_id=loan_id,
            event_type="allocation_changed",
            event_summary=(
                f"Allocations updated effective {effective_date}: "
                f"{len(new_set)} portfolio(s), "
                f"{'syndicated' if is_syndicated else 'single-fund'}"
            ),
            field_changes={
                "effective_date": str(effective_date),
                "prior": prior_summary,
                "new": new_summary,
                "notes": notes,
            },
            user_id=user_id,
        )

        logger.info(
            "loan_allocations_updated",
            loan_id=str(loan_id),
            effective_date=str(effective_date),
            new_count=len(new_rows),
            updated_by=str(user_id),
        )
        return new_rows

    # -------------------------------------------------------------------------
    # Penny-rounding helper (used by Phase 3/4 split-write logic)
    # -------------------------------------------------------------------------

    @staticmethod
    def split_with_largest_remainder(
        amount_cents: int,
        allocations: list[tuple[UUID, Decimal]],
    ) -> list[tuple[UUID, int]]:
        """
        Split `amount_cents` across allocations so the integer cents sum to amount_cents
        exactly (largest-remainder method).

        Input:  [(portfolio_id, ownership_pct), ...] where pcts sum to 100.
        Output: [(portfolio_id, cents), ...] in input order.
        """
        if not allocations:
            return []
        if amount_cents == 0:
            return [(pid, 0) for pid, _ in allocations]

        raw_shares = [Decimal(amount_cents) * pct / PCT_TOTAL for _, pct in allocations]
        floors = [int(s) for s in raw_shares]  # truncates toward 0; safe for non-negative
        remainders = [raw_shares[i] - floors[i] for i in range(len(allocations))]

        residual = amount_cents - sum(floors)
        # Distribute residual to entries with largest remainder; ties to larger floor.
        order = sorted(
            range(len(allocations)),
            key=lambda i: (-remainders[i], -floors[i]),
        )
        result = list(zip([pid for pid, _ in allocations], floors))
        for i in order[:residual]:
            pid, c = result[i]
            result[i] = (pid, c + 1)
        return result
