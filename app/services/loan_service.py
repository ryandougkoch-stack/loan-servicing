"""
app/services/loan_service.py

Business logic for loan boarding, retrieval, status transitions,
and payment schedule generation.

The service layer sits between the API endpoints and the database.
It owns all domain rules — the endpoints just handle HTTP concerns.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
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
from app.models.conversion import LoanConversion
from app.schemas.loan import LoanCreate, LoanConversionPayload, STATUS_TRANSITIONS

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
    # Mid-term boarding (loan conversion)
    # -------------------------------------------------------------------------

    async def create_converted_loan(self, payload: LoanCreate, created_by: UUID) -> Loan:
        """
        Board a loan that's already mid-life — transferred from a prior servicer.

        Differences from create_loan:
          - status starts at 'funded' (no boarding phase)
          - boarding_type='converted', accrual_start_date=as_of_date
          - current_principal/accrued_interest/accrued_fees from payload, not 0
          - funded_at = as_of_date (cutover, not today)
          - allocation effective_date = as_of_date
          - LoanConversion record written
          - opening journal entry posted on as_of_date, split per allocation
            (DR Loans Receivable + DR Accrued Interest Receivable / CR
            Conversion Suspense). Settles when prior-servicer cash arrives.

        Pre-condition: payload.conversion is not None (caller routes here).
        """
        if payload.conversion is None:
            raise ValidationError("create_converted_loan requires a conversion block")
        conv: LoanConversionPayload = payload.conversion

        # Block double-imports of the same prior-servicer loan even before the
        # DB unique index fires — we get a friendlier error this way.
        if conv.prior_servicer_loan_id:
            from sqlalchemy import select as _select
            existing = await self.db.execute(
                _select(LoanConversion).where(
                    LoanConversion.prior_servicer_loan_id == conv.prior_servicer_loan_id
                )
            )
            if existing.scalar_one_or_none():
                raise ConflictError(
                    f"Prior servicer loan id '{conv.prior_servicer_loan_id}' already converted",
                )

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
            status="funded",                      # converted loans skip 'boarding'
            primary_borrower_id=payload.primary_borrower_id,
            currency=payload.currency,
            original_balance=payload.original_balance,
            commitment_amount=payload.commitment_amount,
            current_principal=conv.current_principal,   # NOT original_balance
            accrued_interest=conv.accrued_interest,
            accrued_fees=conv.accrued_fees,
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
            boarding_type="converted",
            accrual_start_date=conv.as_of_date,
            funded_at=conv.as_of_date,
            boarding_completed_at=datetime.now(timezone.utc),
        )
        self.db.add(loan)
        await self.db.flush()

        # Allocation effective_date = as_of_date (not origination_date) so we
        # don't claim ownership of a period when we didn't own the loan.
        from app.services.loan_allocation_service import LoanAllocationService
        await LoanAllocationService(self.db).create_initial_allocation(
            loan, created_by, effective_date=conv.as_of_date
        )

        suspense_account_id = await self._get_account_id("2050")

        # LoanConversion record — canonical structured audit of the conversion.
        conversion_row = LoanConversion(
            loan_id=loan.id,
            batch_id=None,
            as_of_date=conv.as_of_date,
            current_principal=conv.current_principal,
            accrued_interest=conv.accrued_interest,
            accrued_fees=conv.accrued_fees,
            last_payment_date=conv.last_payment_date,
            last_payment_amount=conv.last_payment_amount,
            next_due_date=conv.next_due_date,
            paid_to_date_principal=conv.paid_to_date_principal,
            paid_to_date_interest=conv.paid_to_date_interest,
            paid_to_date_fees=conv.paid_to_date_fees,
            prior_servicer_name=conv.prior_servicer_name,
            prior_servicer_loan_id=conv.prior_servicer_loan_id,
            conversion_document_id=conv.conversion_document_id,
            suspense_account_id=suspense_account_id,
            posted_by=created_by,
            posted_at=datetime.now(timezone.utc),
            notes=conv.notes,
        )
        self.db.add(conversion_row)
        await self.db.flush()

        # Opening journal entry — pro-rata across active allocations, balanced
        # via largest-remainder so per-fund cents sum to the full opening
        # amount exactly. Today this is a single 100% allocation, but the loop
        # is structurally identical to the accrual splitter (Phase 3) so
        # syndicated-at-conversion will work without code changes once the
        # payload supports allocations.
        opening_entry_id = await self._post_opening_journal_entry(
            loan, conv, suspense_account_id, created_by
        )
        if opening_entry_id is not None:
            conversion_row.opening_journal_entry_id = opening_entry_id

        # Generate the forward-only payment schedule so the delinquency engine
        # has correct anchor dates on day 1. generate_schedule reads boarding_type
        # and looks up LoanConversion (now flushed) to pick up as_of_date /
        # next_due_date.
        await self.generate_schedule(loan)

        logger.info(
            "loan_converted",
            loan_id=str(loan.id),
            loan_number=loan.loan_number,
            as_of_date=conv.as_of_date.isoformat(),
            current_principal=str(conv.current_principal),
            prior_servicer=conv.prior_servicer_name,
            created_by=str(created_by),
        )
        return loan

    async def _post_opening_journal_entry(
        self,
        loan: Loan,
        conv: LoanConversionPayload,
        suspense_account_id: Optional[UUID],
        posted_by: UUID,
    ) -> Optional[UUID]:
        """
        Post the opening JE on as_of_date, split across active allocations.
        Returns the JE id if exactly one entry was posted (non-syndicated),
        else None (caller still has the LoanConversion id to traverse the
        per-fund entries via reference_id/reference_type).
        """
        from sqlalchemy import select as _select, or_ as _or
        from app.models.ledger import JournalEntry, JournalLine
        from app.models.portfolio import LoanAllocation, Portfolio
        from app.services.loan_allocation_service import LoanAllocationService

        # Account ids — fetch in one query.
        accounts = await self._account_id_map(["1100", "1110", "2050"])
        if suspense_account_id is None:
            suspense_account_id = accounts["2050"]

        principal = conv.current_principal
        accrued = conv.accrued_interest
        if principal == 0 and accrued == 0:
            return None

        # Active allocations as-of as_of_date.
        alloc_result = await self.db.execute(
            _select(LoanAllocation).where(
                LoanAllocation.loan_id == loan.id,
                LoanAllocation.effective_date <= conv.as_of_date,
                _or(
                    LoanAllocation.end_date.is_(None),
                    LoanAllocation.end_date > conv.as_of_date,
                ),
            ).order_by(LoanAllocation.ownership_pct.desc())
        )
        allocations = list(alloc_result.scalars().all())
        if not allocations:
            logger.error(
                "conversion_no_active_allocation",
                loan_id=str(loan.id),
                as_of_date=conv.as_of_date.isoformat(),
            )
            return None

        # Portfolio codes for entry_number suffix uniqueness.
        port_result = await self.db.execute(
            _select(Portfolio.id, Portfolio.code).where(
                Portfolio.id.in_([a.portfolio_id for a in allocations])
            )
        )
        portfolio_codes = {row.id: row.code for row in port_result}

        alloc_pcts = [(a.portfolio_id, a.ownership_pct) for a in allocations]
        principal_cents = int((principal * 100).to_integral_value(rounding=ROUND_HALF_UP))
        accrued_cents = int((accrued * 100).to_integral_value(rounding=ROUND_HALF_UP))
        principal_split = LoanAllocationService.split_with_largest_remainder(principal_cents, alloc_pcts)
        accrued_split = (
            LoanAllocationService.split_with_largest_remainder(accrued_cents, alloc_pcts)
            if accrued_cents > 0 else [(pid, 0) for pid, _ in alloc_pcts]
        )

        from decimal import Decimal as _Dec
        now = datetime.now(timezone.utc)
        date_str = conv.as_of_date.strftime("%Y%m%d")
        loan_prefix = str(loan.id)[:8]
        entries_created: list[UUID] = []

        for alloc, (_, p_share_cents), (_, a_share_cents) in zip(
            allocations, principal_split, accrued_split
        ):
            if p_share_cents == 0 and a_share_cents == 0:
                continue
            p_share = _Dec(p_share_cents) / 100
            a_share = _Dec(a_share_cents) / 100
            portfolio_code = portfolio_codes.get(alloc.portfolio_id, str(alloc.portfolio_id)[:8])

            entry = JournalEntry(
                entry_number=f"CONV-{date_str}-{loan_prefix}-{portfolio_code}",
                loan_id=loan.id,
                portfolio_id=alloc.portfolio_id,
                entry_type="conversion_opening",
                entry_date=conv.as_of_date,
                effective_date=conv.as_of_date,
                description=(
                    f"Mid-term boarding opening — {loan.loan_number} "
                    f"({alloc.ownership_pct}% to {portfolio_code})"
                ),
                reference_id=loan.id,
                reference_type="loan_conversion",
                status="posted",
                posted_by=posted_by,
                created_at=now,
            )
            self.db.add(entry)
            await self.db.flush()

            line_no = 0
            if p_share > 0:
                line_no += 1
                self.db.add(JournalLine(
                    journal_entry_id=entry.id,
                    line_number=line_no,
                    account_id=accounts["1100"],
                    debit_amount=p_share,
                    credit_amount=_Dec("0"),
                    memo=f"Opening principal — {loan.loan_number} ({alloc.ownership_pct}% share)",
                ))
            if a_share > 0:
                line_no += 1
                self.db.add(JournalLine(
                    journal_entry_id=entry.id,
                    line_number=line_no,
                    account_id=accounts["1110"],
                    debit_amount=a_share,
                    credit_amount=_Dec("0"),
                    memo=f"Opening accrued interest — {loan.loan_number} ({alloc.ownership_pct}% share)",
                ))
            line_no += 1
            self.db.add(JournalLine(
                journal_entry_id=entry.id,
                line_number=line_no,
                account_id=suspense_account_id,
                debit_amount=_Dec("0"),
                credit_amount=p_share + a_share,
                memo=f"Conversion suspense — {alloc.ownership_pct}% share, prior servicer settlement pending",
            ))
            entries_created.append(entry.id)

        return entries_created[0] if len(entries_created) == 1 else None

    async def _account_id_map(self, codes: list[str]) -> dict[str, UUID]:
        from app.models.ledger import LedgerAccount
        result = await self.db.execute(
            select(LedgerAccount.code, LedgerAccount.id).where(LedgerAccount.code.in_(codes))
        )
        return {row.code: row.id for row in result}

    async def _get_account_id(self, code: str) -> Optional[UUID]:
        m = await self._account_id_map([code])
        return m.get(code)

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

        Converted loans use forward-only mode: schedule starts at as_of_date
        with current_principal as opening balance, anchored on the
        conversion's next_due_date (or computed if not supplied).
        Pre-cutover periods are intentionally absent — they belong to the
        prior servicer's books.

        Returns the number of periods generated.
        """
        from app.services.amortization_engine import AmortizationEngine

        schedule_start_date = None
        schedule_first_due = None
        if loan.boarding_type == "converted":
            from sqlalchemy import select as _select
            conv = (await self.db.execute(
                _select(LoanConversion).where(LoanConversion.loan_id == loan.id)
            )).scalar_one_or_none()
            if conv is not None:
                schedule_start_date = conv.as_of_date
                schedule_first_due = conv.next_due_date or self._derive_next_due(
                    loan, conv.as_of_date
                )

        engine = AmortizationEngine(
            loan,
            schedule_start_date=schedule_start_date,
            schedule_first_due=schedule_first_due,
        )
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

    @staticmethod
    def _derive_next_due(loan: Loan, as_of_date: date) -> date:
        """
        Fallback when conversion payload omits next_due_date.
        Walks payment_frequency forward from origination_date until we land on
        a due date strictly after as_of_date — that's the next scheduled payment.
        """
        from app.services.amortization_engine import AmortizationEngine
        freq_map = {"MONTHLY": 1, "QUARTERLY": 3, "SEMI_ANNUAL": 6, "ANNUAL": 12, "BULLET": None}
        freq_months = freq_map.get(loan.payment_frequency, 3)
        if freq_months is None:
            return loan.maturity_date

        anchor = loan.first_payment_date or AmortizationEngine._add_months(
            loan.origination_date, freq_months
        )
        candidate = anchor
        while candidate <= as_of_date and candidate < loan.maturity_date:
            candidate = AmortizationEngine._add_months(candidate, freq_months)
        return min(candidate, loan.maturity_date)

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
