"""
app/services/payoff_service.py
Payoff quote calculation and payoff processing.
"""
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.loan import Loan
from app.schemas.payment import PaymentCreate
from app.services.payment_service import PaymentService

logger = structlog.get_logger(__name__)


class PayoffService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_loan(self, loan_id: UUID) -> Loan:
        result = await self.db.execute(select(Loan).where(Loan.id == loan_id))
        loan = result.scalar_one_or_none()
        if not loan:
            raise ValueError(f"Loan {loan_id} not found")
        return loan

    def _calculate_per_diem(self, loan: Loan) -> Decimal:
        if not loan.coupon_rate or not loan.current_principal:
            return Decimal("0.00")
        days = Decimal("360") if loan.day_count == "ACT/360" else Decimal("365")
        per_diem = (loan.current_principal * loan.coupon_rate) / days
        return per_diem.quantize(Decimal("0.01"))

    def _calculate_prepayment_penalty(self, loan: Loan, payoff_date: date):
        if loan.prepayment_penalty_type == "none" or not loan.prepayment_penalty_type:
            return (Decimal("0.00"), "No prepayment penalty")
        if loan.prepayment_penalty_type == "flat_pct":
            if not loan.prepayment_penalty_pct:
                return (Decimal("0.00"), "No penalty rate set")
            penalty = (loan.current_principal * loan.prepayment_penalty_pct).quantize(Decimal("0.01"))
            pct = float(loan.prepayment_penalty_pct) * 100
            return (penalty, f"Flat {pct:.2f}% prepayment penalty on outstanding principal")
        if loan.prepayment_penalty_type == "step_down":
            schedule = loan.prepayment_penalty_schedule or []
            if not schedule:
                return (Decimal("0.00"), "No step-down schedule defined")
            months_since = ((payoff_date.year - loan.origination_date.year) * 12
                            + (payoff_date.month - loan.origination_date.month))
            applicable_pct = Decimal("0")
            tier_desc = "after final tier"
            for tier in schedule:
                if months_since < tier["months"]:
                    applicable_pct = Decimal(str(tier["pct"])) / Decimal("100")
                    tier_desc = f"month {months_since} (within {tier['months']}-month tier @ {tier['pct']}%)"
                    break
            penalty = (loan.current_principal * applicable_pct).quantize(Decimal("0.01"))
            return (penalty, f"Step-down penalty: {tier_desc}")
        return (Decimal("0.00"), "Unknown penalty type")

    async def generate_quote(self, loan_id: UUID, payoff_date: Optional[date] = None) -> dict:
        loan = await self.get_loan(loan_id)
        today = date.today()
        if payoff_date is None:
            payoff_date = today
        if payoff_date < today:
            raise ValueError("Payoff date cannot be in the past")
        days_to_payoff = (payoff_date - today).days
        per_diem = self._calculate_per_diem(loan)
        additional_interest = (per_diem * Decimal(days_to_payoff)).quantize(Decimal("0.01"))
        principal = loan.current_principal or Decimal("0.00")
        accrued_interest = (loan.accrued_interest or Decimal("0.00")) + additional_interest
        accrued_fees = loan.accrued_fees or Decimal("0.00")
        penalty, penalty_desc = self._calculate_prepayment_penalty(loan, payoff_date)
        total = principal + accrued_interest + accrued_fees + penalty
        return {
            "loan_id": str(loan.id),
            "loan_number": loan.loan_number,
            "borrower_id": str(loan.primary_borrower_id),
            "quote_date": today.isoformat(),
            "payoff_date": payoff_date.isoformat(),
            "days_to_payoff": days_to_payoff,
            "per_diem_interest": str(per_diem),
            "components": {
                "outstanding_principal": str(principal),
                "accrued_interest_today": str(loan.accrued_interest or Decimal("0.00")),
                "additional_interest_to_payoff": str(additional_interest),
                "accrued_interest_at_payoff": str(accrued_interest),
                "accrued_fees": str(accrued_fees),
                "prepayment_penalty": str(penalty),
                "prepayment_penalty_description": penalty_desc,
            },
            "total_payoff_amount": str(total),
            "good_through_date": payoff_date.isoformat(),
            "warning": "After the good-through date, add the per-diem amount per day.",
        }

    async def process_payoff(self, loan_id: UUID, payoff_date: date,
                             amount_received: Decimal, user_id: UUID) -> dict:
        loan = await self.get_loan(loan_id)
        quote = await self.generate_quote(loan_id, payoff_date)
        expected = Decimal(quote["total_payoff_amount"])
        if abs(amount_received - expected) > Decimal("1.00"):
            raise ValueError(f"Payoff amount mismatch. Expected ${expected}, received ${amount_received}.")

        # Route the payoff cash through PaymentService so it lands in the GL
        # (allocation-aware split, Payment record for audit + reporting, plus the
        # waterfall correctly clears principal/interest/fees and recognizes the
        # prepayment penalty as income via account 4040). Compute the penalty
        # here — payoff_service owns the penalty schedule logic.
        penalty, _ = self._calculate_prepayment_penalty(loan, payoff_date)

        payment_payload = PaymentCreate(
            loan_id=loan.id,
            payment_type="payoff",
            payment_method="wire",  # standard for payoffs; future: pass through from API
            received_date=payoff_date,
            effective_date=payoff_date,
            gross_amount=amount_received,
        )
        payment = await PaymentService(self.db).post_payment(
            payload=payment_payload,
            posted_by=user_id,
            prepayment_penalty=penalty,
        )

        # post_payment already cleared principal/interest/fees via the waterfall;
        # flip the loan to paid_off after that's persisted.
        loan.status = "paid_off"
        loan.paid_off_at = payoff_date
        await self.db.flush()
        logger.info("loan_paid_off", loan_id=str(loan.id),
                    payoff_date=payoff_date.isoformat(),
                    amount_received=str(amount_received),
                    penalty=str(penalty),
                    payment_id=str(payment.id))
        return {
            "loan_id": str(loan.id),
            "loan_number": loan.loan_number,
            "status": "paid_off",
            "paid_off_at": payoff_date.isoformat(),
            "amount_received": str(amount_received),
            "payment_id": str(payment.id),
            "payment_number": payment.payment_number,
            "prepayment_penalty": str(penalty),
        }
