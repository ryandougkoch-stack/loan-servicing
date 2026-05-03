"""
app/services/payment_service.py

Payment posting service.

Responsibilities:
  1. Validate the loan can accept a payment
  2. Determine days late and assess late fees
  3. Apply the payment waterfall (fees → interest → principal → escrow → suspense)
  4. Generate a balanced double-entry journal entry
  5. Update loan principal and accrued balances
  6. Update the payment schedule period status
  7. Log everything to the audit trail

The waterfall order is currently hardcoded to the standard private credit
convention: fees first, then interest, then principal. A configurable
rules engine is a Phase 2 feature.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    LoanNotFoundError,
    PaymentPostingError,
    LedgerImbalanceError,
    NotFoundError,
)
from app.models.ledger import (
    Fee, InterestAccrual, JournalEntry, JournalLine, LedgerAccount, Payment
)
from app.models.loan import Loan
from app.models.portfolio import LoanAllocation, Portfolio
from app.schemas.payment import PaymentCreate
from app.services.loan_allocation_service import LoanAllocationService

logger = structlog.get_logger(__name__)

CENTS = Decimal("0.01")

# ---------------------------------------------------------------------------
# Standard waterfall order
# ---------------------------------------------------------------------------
WATERFALL = [
    "fees",
    "interest",
    "principal",
    "escrow",
    "advances",
    "penalty",  # only used for payoff payments — recognized as income, not a receivable
]

# ---------------------------------------------------------------------------
# Payable loan statuses
# ---------------------------------------------------------------------------
PAYABLE_STATUSES = {"funded", "modified", "delinquent", "default", "workout", "payoff_pending"}

# ---------------------------------------------------------------------------
# Payment number prefix
# ---------------------------------------------------------------------------
PAYMENT_NUMBER_PREFIX = "PMT"


class PaymentService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # -------------------------------------------------------------------------
    # Retrieval
    # -------------------------------------------------------------------------

    async def get_payment_or_404(self, payment_id: UUID) -> Payment:
        result = await self.db.execute(
            select(Payment).where(Payment.id == payment_id)
        )
        payment = result.scalar_one_or_none()
        if not payment:
            raise NotFoundError(f"Payment {payment_id} not found")
        return payment

    async def list_payments(
        self,
        loan_id: Optional[UUID] = None,
        portfolio_id: Optional[UUID] = None,
        status_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[Payment], int]:
        stmt = select(Payment)
        count_stmt = select(func.count()).select_from(Payment)

        if loan_id:
            stmt = stmt.where(Payment.loan_id == loan_id)
            count_stmt = count_stmt.where(Payment.loan_id == loan_id)
        if status_filter:
            stmt = stmt.where(Payment.status == status_filter)
            count_stmt = count_stmt.where(Payment.status == status_filter)

        total = (await self.db.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(Payment.effective_date.desc()).offset((page - 1) * page_size).limit(page_size)
        payments = (await self.db.execute(stmt)).scalars().all()
        return list(payments), total

    # -------------------------------------------------------------------------
    # Core: post a payment
    # -------------------------------------------------------------------------

    async def post_payment(
        self,
        payload: PaymentCreate,
        posted_by: UUID,
        prepayment_penalty: Decimal = Decimal("0"),
    ) -> Payment:
        # 1. Load the loan
        loan = await self._get_payable_loan(payload.loan_id)

        # 2. Calculate days late
        days_late, late_fee = await self._assess_late_fee(loan, payload)

        # 3. Apply waterfall (penalty bucket populated only when caller passes it
        # — typically payoff_service for payment_type='payoff').
        waterfall = self._apply_waterfall(
            gross_amount=payload.gross_amount,
            loan=loan,
            late_fee=late_fee,
            prepayment_penalty=prepayment_penalty,
        )

        # 4. Build payment record
        payment_number = await self._generate_payment_number()
        payment = Payment(
            loan_id=loan.id,
            payment_number=payment_number,
            payment_type=payload.payment_type,
            payment_method=payload.payment_method,
            received_date=payload.received_date,
            effective_date=payload.effective_date,
            gross_amount=payload.gross_amount,
            applied_to_fees=waterfall["fees"],
            applied_to_interest=waterfall["interest"],
            applied_to_principal=waterfall["principal"],
            applied_to_escrow=waterfall["escrow"],
            applied_to_advances=waterfall["advances"],
            applied_to_penalty=waterfall["penalty"],
            held_in_suspense=waterfall["suspense"],
            period_id=payload.period_id,
            reference_number=payload.reference_number,
            bank_account_last4=payload.bank_account_last4,
            status="pending",
            late_fee_assessed=late_fee,
            days_late=days_late,
            posted_by=posted_by,
            notes=payload.notes,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.db.add(payment)
        await self.db.flush()  # get payment.id

        # 5. Create journal entry
        journal_entry = await self._create_payment_journal_entry(
            loan=loan,
            payment=payment,
            waterfall=waterfall,
            posted_by=posted_by,
        )
        payment.journal_entry_id = journal_entry.id
        payment.status = "posted"

        # 6. Update loan balances
        await self._update_loan_balances(loan, waterfall)

        # 7. Update schedule period if linked
        if payload.period_id:
            await self._update_schedule_period(payload.period_id, waterfall)

        # 8. Assess late fee as a Fee record if applicable
        if late_fee > 0:
            await self._record_late_fee(loan, payment, late_fee, posted_by)

        await self.db.flush()

        logger.info(
            "payment_posted",
            payment_id=str(payment.id),
            payment_number=payment_number,
            loan_id=str(loan.id),
            gross_amount=str(payload.gross_amount),
            waterfall={k: str(v) for k, v in waterfall.items()},
            days_late=days_late,
            late_fee=str(late_fee),
            posted_by=str(posted_by),
        )
        return payment

    # -------------------------------------------------------------------------
    # Reversal
    # -------------------------------------------------------------------------

    async def reverse_payment(
        self,
        payment_id: UUID,
        reason: str,
        reversed_by: UUID,
    ) -> Payment:
        original = await self.get_payment_or_404(payment_id)

        if original.status != "posted":
            raise PaymentPostingError(
                f"Cannot reverse payment in status '{original.status}'. Only 'posted' payments can be reversed."
            )

        loan = await self._get_payable_loan(original.loan_id)

        # Phase 4: a payment may have N journal entries (one per allocation).
        # Reverse all of them by walking reference_id, not the single
        # payment.journal_entry_id pointer.
        original_entries_result = await self.db.execute(
            select(JournalEntry).where(
                JournalEntry.reference_id == original.id,
                JournalEntry.reference_type == "payment",
                JournalEntry.is_reversed == False,
            )
        )
        original_entries = list(original_entries_result.scalars().all())
        if not original_entries:
            raise PaymentPostingError(
                "Cannot reverse payment: no posted journal entries linked to it."
            )

        for original_je in original_entries:
            reversal_je = await self._create_reversal_journal_entry(
                original_entry=original_je,
                loan=loan,
                reason=reason,
                reversed_by=reversed_by,
            )
            original_je.is_reversed = True
            original_je.reversed_by_entry_id = reversal_je.id
            original_je.status = "reversed"

        original.status = "reversed"

        # Undo loan balance changes. Penalty has no loan-level state to undo
        # (it's recognized as income at posting), so we don't include it here.
        reverse_waterfall = {
            "fees": -original.applied_to_fees,
            "interest": -original.applied_to_interest,
            "principal": -original.applied_to_principal,
            "escrow": -original.applied_to_escrow,
            "advances": -original.applied_to_advances,
            "suspense": -original.held_in_suspense,
        }
        await self._update_loan_balances(loan, reverse_waterfall)

        await self.db.flush()

        logger.info(
            "payment_reversed",
            payment_id=str(payment_id),
            reversal_entry_id=str(reversal_je.id),
            reason=reason,
            reversed_by=str(reversed_by),
        )
        return original

    # -------------------------------------------------------------------------
    # Waterfall calculation
    # -------------------------------------------------------------------------

    def _apply_waterfall(
        self,
        gross_amount: Decimal,
        loan: Loan,
        late_fee: Decimal,
        prepayment_penalty: Decimal = Decimal("0"),
    ) -> dict[str, Decimal]:
        """
        Apply payment to loan components in waterfall order.
        Returns a dict of allocations that sum to gross_amount.

        Standard private credit waterfall:
          1. Fees (including late fee)
          2. Accrued interest
          3. Principal
          4. Escrow
          5. Advances
          6. Prepayment penalty (only when caller passes one — payoff path)
          7. Remainder → suspense

        `prepayment_penalty` is the loan's contractual penalty due on early
        payoff. It's not a receivable being cleared — it's income recognized
        when collected, so it has its own bucket and journal account (4040).
        """
        remaining = gross_amount
        result = {
            k: Decimal("0")
            for k in ["fees", "interest", "principal", "escrow", "advances", "penalty", "suspense"]
        }

        # Fees outstanding (existing accrued fees + new late fee)
        fees_due = (loan.accrued_fees + late_fee).quantize(CENTS)
        result["fees"] = min(remaining, fees_due)
        remaining -= result["fees"]

        # Accrued interest
        if remaining > 0:
            result["interest"] = min(remaining, loan.accrued_interest.quantize(CENTS))
            remaining -= result["interest"]

        # Principal
        if remaining > 0:
            result["principal"] = min(remaining, loan.current_principal.quantize(CENTS))
            remaining -= result["principal"]

        # Escrow / advances (placeholders — not modelled in MVP)
        result["escrow"] = Decimal("0")
        result["advances"] = Decimal("0")

        # Prepayment penalty (only on payoff). Cap at remaining so a short-paid
        # payoff doesn't over-recognize income.
        if remaining > 0 and prepayment_penalty > 0:
            result["penalty"] = min(remaining, prepayment_penalty.quantize(CENTS))
            remaining -= result["penalty"]

        # Suspense: anything left over
        result["suspense"] = remaining.quantize(CENTS)

        # Sanity check: allocations must sum to gross
        total = sum(result.values())
        if total != gross_amount:
            raise LedgerImbalanceError(
                f"Waterfall imbalance: allocated {total} but gross was {gross_amount}"
            )

        return result

    # -------------------------------------------------------------------------
    # Late fee calculation
    # -------------------------------------------------------------------------

    async def _assess_late_fee(
        self,
        loan: Loan,
        payload: PaymentCreate,
    ) -> tuple[int, Decimal]:
        """
        Returns (days_late, late_fee_amount).
        days_late is calculated from the most recent unpaid due date.
        """
        if not loan.late_fee_type or not loan.late_fee_amount:
            return 0, Decimal("0")

        # Find the most recent past-due period
        from sqlalchemy import text
        result = await self.db.execute(
            text("""
                SELECT due_date, total_scheduled
                FROM payment_schedule
                WHERE loan_id = :loan_id
                  AND status IN ('open', 'partial')
                  AND is_current = true
                  AND due_date < :effective_date
                ORDER BY due_date DESC
                LIMIT 1
            """),
            {"loan_id": str(loan.id), "effective_date": payload.effective_date},
        )
        row = result.fetchone()

        if not row:
            return 0, Decimal("0")

        due_date = row.due_date
        days_late = max(0, (payload.effective_date - due_date).days - loan.grace_period_days)

        if days_late <= 0:
            return 0, Decimal("0")

        # Calculate the fee
        if loan.late_fee_type == "flat":
            fee = loan.late_fee_amount.quantize(CENTS)
        elif loan.late_fee_type == "percent_of_payment":
            fee = (row.total_scheduled * loan.late_fee_amount).quantize(CENTS)
        elif loan.late_fee_type == "percent_of_balance":
            fee = (loan.current_principal * loan.late_fee_amount).quantize(CENTS)
        else:
            fee = Decimal("0")

        return days_late, fee

    # -------------------------------------------------------------------------
    # Journal entry generation
    # -------------------------------------------------------------------------

    async def _create_payment_journal_entry(
        self,
        loan: Loan,
        payment: Payment,
        waterfall: dict[str, Decimal],
        posted_by: UUID,
    ) -> JournalEntry:
        """
        Phase 4: write-time GL split.

        Creates one JournalEntry per active allocation (as-of payment.effective_date),
        with each waterfall bucket pro-rated by ownership_pct using largest-remainder
        so per-bucket cents reconcile exactly across funds. Per-fund DR(cash) =
        sum of that fund's per-bucket credits, so each entry balances individually.

        Returns the highest-share entry (allocations are sorted by ownership_pct desc),
        so the caller's existing `payment.journal_entry_id = entry.id` assignment
        still gives a meaningful single-entry pointer for non-syndicated loans.
        Reversals find ALL entries via reference_id = payment.id, so the multi-entry
        case is handled correctly.

        Standard line shape per entry (zero-amount lines are skipped):
          DR  Cash - Operating                (per-fund cash share)
          CR  Accrued Interest Receivable     (per-fund interest share)
          CR  Loans Receivable - Principal    (per-fund principal share)
          CR  Fees Receivable                 (per-fund fees share)
          CR  Suspense Liability              (per-fund suspense share)
        """
        accounts = await self._load_account_map()

        # Active allocations as-of the payment's effective_date. Sorted by pct desc
        # so the largest fund gets the lowest line numbers and is returned first.
        alloc_result = await self.db.execute(
            select(LoanAllocation).where(
                LoanAllocation.loan_id == loan.id,
                LoanAllocation.effective_date <= payment.effective_date,
                or_(
                    LoanAllocation.end_date.is_(None),
                    LoanAllocation.end_date > payment.effective_date,
                ),
            ).order_by(LoanAllocation.ownership_pct.desc())
        )
        allocations = list(alloc_result.scalars().all())
        if not allocations:
            raise LedgerImbalanceError(
                f"Loan {loan.loan_number} has no active allocation "
                f"as-of {payment.effective_date}; cannot post payment."
            )

        # Pre-fetch portfolio codes for entry_number suffixes
        port_result = await self.db.execute(
            select(Portfolio.id, Portfolio.code).where(
                Portfolio.id.in_([a.portfolio_id for a in allocations])
            )
        )
        portfolio_codes = {row.id: row.code for row in port_result}

        alloc_pcts = [(a.portfolio_id, a.ownership_pct) for a in allocations]

        # Split each waterfall bucket independently. Each bucket's cents
        # reconcile exactly across funds via largest-remainder.
        bucket_splits: dict[str, list[tuple[UUID, int]]] = {}
        for bucket in ("fees", "interest", "principal", "escrow", "advances", "penalty", "suspense"):
            bucket_cents = int(
                (waterfall[bucket] * 100).to_integral_value(rounding=ROUND_HALF_UP)
            )
            bucket_splits[bucket] = LoanAllocationService.split_with_largest_remainder(
                bucket_cents, alloc_pcts
            )

        base_entry_number = await self._generate_entry_number()
        now = datetime.now(timezone.utc)
        entries_created: list[JournalEntry] = []

        for i, alloc in enumerate(allocations):
            fees_c      = bucket_splits["fees"][i][1]
            interest_c  = bucket_splits["interest"][i][1]
            principal_c = bucket_splits["principal"][i][1]
            escrow_c    = bucket_splits["escrow"][i][1]
            advances_c  = bucket_splits["advances"][i][1]
            penalty_c   = bucket_splits["penalty"][i][1]
            suspense_c  = bucket_splits["suspense"][i][1]
            cash_c = fees_c + interest_c + principal_c + escrow_c + advances_c + penalty_c + suspense_c
            if cash_c == 0:
                # Tiny payment + small allocation can round to zero everywhere.
                # Skip the empty entry rather than write zero-amount lines.
                continue

            portfolio_code = portfolio_codes.get(alloc.portfolio_id, str(alloc.portfolio_id)[:8])
            pct_str = f"{alloc.ownership_pct}% to {portfolio_code}"

            entry = JournalEntry(
                entry_number=f"{base_entry_number}-{portfolio_code}",
                loan_id=loan.id,
                portfolio_id=alloc.portfolio_id,
                entry_type="payment",
                entry_date=now.date(),
                effective_date=payment.effective_date,
                description=(
                    f"Payment posted: {payment.payment_number} | "
                    f"{payment.payment_method.upper()} ({pct_str})"
                ),
                reference_id=payment.id,
                reference_type="payment",
                status="posted",
                posted_by=posted_by,
                created_at=now,
            )
            self.db.add(entry)
            await self.db.flush()  # need entry.id for lines

            lines: list[JournalLine] = []
            line_num = 1

            lines.append(JournalLine(
                journal_entry_id=entry.id,
                line_number=line_num,
                account_id=accounts["1010"],  # Cash - Operating
                debit_amount=Decimal(cash_c) / 100,
                credit_amount=Decimal("0"),
                memo=(
                    f"Payment received ({pct_str}): "
                    f"{payment.reference_number or payment.payment_number}"
                ),
            ))
            line_num += 1

            if interest_c > 0:
                lines.append(JournalLine(
                    journal_entry_id=entry.id,
                    line_number=line_num,
                    account_id=accounts["1110"],
                    debit_amount=Decimal("0"),
                    credit_amount=Decimal(interest_c) / 100,
                    memo=f"Interest collected ({pct_str})",
                ))
                line_num += 1

            if principal_c > 0:
                lines.append(JournalLine(
                    journal_entry_id=entry.id,
                    line_number=line_num,
                    account_id=accounts["1100"],
                    debit_amount=Decimal("0"),
                    credit_amount=Decimal(principal_c) / 100,
                    memo=f"Principal repayment ({pct_str})",
                ))
                line_num += 1

            if fees_c > 0:
                lines.append(JournalLine(
                    journal_entry_id=entry.id,
                    line_number=line_num,
                    account_id=accounts["1120"],
                    debit_amount=Decimal("0"),
                    credit_amount=Decimal(fees_c) / 100,
                    memo=f"Fees collected ({pct_str})",
                ))
                line_num += 1

            if suspense_c > 0:
                lines.append(JournalLine(
                    journal_entry_id=entry.id,
                    line_number=line_num,
                    account_id=accounts["2030"],
                    debit_amount=Decimal("0"),
                    credit_amount=Decimal(suspense_c) / 100,
                    memo=f"Unapplied cash to suspense ({pct_str})",
                ))
                line_num += 1

            if penalty_c > 0:
                lines.append(JournalLine(
                    journal_entry_id=entry.id,
                    line_number=line_num,
                    account_id=accounts["4040"],  # Prepayment Penalty Income
                    debit_amount=Decimal("0"),
                    credit_amount=Decimal(penalty_c) / 100,
                    memo=f"Prepayment penalty income ({pct_str})",
                ))
                line_num += 1

            # Per-entry balance check (largest-remainder guarantees this, but verify).
            total_debits = sum((l.debit_amount for l in lines), Decimal("0"))
            total_credits = sum((l.credit_amount for l in lines), Decimal("0"))
            if total_debits != total_credits:
                raise LedgerImbalanceError(
                    f"Per-fund journal entry imbalance for {portfolio_code}: "
                    f"debits={total_debits}, credits={total_credits}"
                )

            for line in lines:
                self.db.add(line)

            entries_created.append(entry)

        await self.db.flush()

        if not entries_created:
            # Should not happen — gross_amount > 0 means at least one fund
            # gets non-zero cents from largest-remainder.
            raise LedgerImbalanceError(
                f"No journal entries created for payment {payment.payment_number}"
            )

        return entries_created[0]

    async def _create_reversal_journal_entry(
        self,
        original_entry: JournalEntry,
        loan: Loan,
        reason: str,
        reversed_by: UUID,
    ) -> JournalEntry:
        """
        Create an offsetting (reversing) journal entry.
        Each debit becomes a credit and vice versa.
        """
        # Load original lines
        from sqlalchemy.orm import selectinload
        result = await self.db.execute(
            select(JournalEntry)
            .where(JournalEntry.id == original_entry.id)
            .options(selectinload(JournalEntry.lines))
        )
        original = result.scalar_one()

        entry_number = await self._generate_entry_number()
        now = datetime.now(timezone.utc)

        reversal = JournalEntry(
            entry_number=entry_number,
            loan_id=loan.id,
            # Reversal lands in the SAME fund's books as the original entry,
            # not the lead-fund pointer on the loan.
            portfolio_id=original_entry.portfolio_id,
            entry_type="reversal",
            entry_date=now.date(),
            effective_date=now.date(),
            description=f"Reversal of {original_entry.entry_number}: {reason}",
            reference_id=original_entry.id,
            reference_type="journal_entry",
            reversal_of_entry_id=original_entry.id,
            status="posted",
            posted_by=reversed_by,
            created_at=now,
        )
        self.db.add(reversal)
        await self.db.flush()

        for i, line in enumerate(original.lines, start=1):
            self.db.add(JournalLine(
                journal_entry_id=reversal.id,
                line_number=i,
                account_id=line.account_id,
                # Swap debits and credits
                debit_amount=line.credit_amount,
                credit_amount=line.debit_amount,
                memo=f"Reversal: {line.memo or ''}",
            ))

        await self.db.flush()
        return reversal

    # -------------------------------------------------------------------------
    # Balance updates
    # -------------------------------------------------------------------------

    async def _update_loan_balances(
        self, loan: Loan, waterfall: dict[str, Decimal]
    ) -> None:
        """
        Update loan's running balances based on waterfall allocations.
        Negative values (reversals) are handled correctly by addition.
        """
        loan.current_principal = max(
            Decimal("0"),
            loan.current_principal - waterfall.get("principal", Decimal("0"))
        )
        loan.accrued_interest = max(
            Decimal("0"),
            loan.accrued_interest - waterfall.get("interest", Decimal("0"))
        )
        loan.accrued_fees = max(
            Decimal("0"),
            loan.accrued_fees - waterfall.get("fees", Decimal("0"))
        )

    async def _update_schedule_period(
        self, period_id: UUID, waterfall: dict[str, Decimal]
    ) -> None:
        """Mark the schedule period as paid or partial based on allocation."""
        from sqlalchemy import text
        row = await self.db.execute(
            text("SELECT scheduled_principal, scheduled_interest, status FROM payment_schedule WHERE id = :id"),
            {"id": str(period_id)},
        )
        period = row.fetchone()
        if not period:
            return

        scheduled_total = period.scheduled_principal + period.scheduled_interest
        applied_total = waterfall.get("principal", Decimal("0")) + waterfall.get("interest", Decimal("0"))

        if applied_total >= scheduled_total:
            new_status = "paid"
        elif applied_total > 0:
            new_status = "partial"
        else:
            return

        await self.db.execute(
            text("UPDATE payment_schedule SET status = :status WHERE id = :id"),
            {"status": new_status, "id": str(period_id)},
        )

    async def _record_late_fee(
        self,
        loan: Loan,
        payment: Payment,
        late_fee: Decimal,
        posted_by: UUID,
    ) -> None:
        """Record a late fee assessment as a Fee record."""
        fee = Fee(
            loan_id=loan.id,
            fee_type="late",
            description=f"Late fee assessed — {payment.days_late} days past due",
            accrual_date=payment.effective_date,
            due_date=payment.effective_date,
            amount=late_fee,
            amount_waived=Decimal("0"),
            amount_paid=min(late_fee, payment.applied_to_fees),
            status="paid" if payment.applied_to_fees >= late_fee else "accrued",
            payment_id=payment.id,
            created_by=posted_by,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.db.add(fee)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _get_payable_loan(self, loan_id: UUID) -> Loan:
        result = await self.db.execute(select(Loan).where(Loan.id == loan_id))
        loan = result.scalar_one_or_none()
        if not loan:
            raise LoanNotFoundError(f"Loan {loan_id} not found")
        if loan.status not in PAYABLE_STATUSES:
            raise PaymentPostingError(
                f"Loan is in status '{loan.status}' and cannot accept payments.",
                detail={"payable_statuses": list(PAYABLE_STATUSES)},
            )
        return loan

    async def _load_account_map(self) -> dict[str, UUID]:
        """Load ledger account IDs by code. Cached per request."""
        if hasattr(self, "_account_map_cache"):
            return self._account_map_cache

        result = await self.db.execute(
            select(LedgerAccount.code, LedgerAccount.id).where(LedgerAccount.is_active == True)
        )
        self._account_map_cache = {row.code: row.id for row in result}
        return self._account_map_cache

    async def _generate_payment_number(self) -> str:
        result = await self.db.execute(select(func.count()).select_from(Payment))
        count = result.scalar_one()
        return f"{PAYMENT_NUMBER_PREFIX}-{(count + 1):08d}"

    async def _generate_entry_number(self) -> str:
        result = await self.db.execute(select(func.count()).select_from(JournalEntry))
        count = result.scalar_one()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"JE-{ts}-{(count + 1):06d}"
