"""
tests/unit/test_payment_waterfall.py

Unit tests for the payment waterfall logic in PaymentService.
These tests are pure Python — no database required.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.services.payment_service import PaymentService
from app.core.exceptions import LedgerImbalanceError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_loan(
    current_principal=Decimal("100000.00"),
    accrued_interest=Decimal("2500.00"),
    accrued_fees=Decimal("500.00"),
    late_fee_type=None,
    late_fee_amount=None,
    grace_period_days=5,
):
    loan = MagicMock()
    loan.current_principal = current_principal
    loan.accrued_interest = accrued_interest
    loan.accrued_fees = accrued_fees
    loan.late_fee_type = late_fee_type
    loan.late_fee_amount = late_fee_amount
    loan.grace_period_days = grace_period_days
    return loan


def make_service():
    db = AsyncMock()
    return PaymentService(db)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPaymentWaterfall:

    def test_full_payment_covers_fees_interest_principal(self):
        """A payment that exceeds all outstanding amounts should be fully applied."""
        loan = make_loan(
            current_principal=Decimal("100000.00"),
            accrued_interest=Decimal("2500.00"),
            accrued_fees=Decimal("500.00"),
        )
        service = make_service()

        gross = Decimal("103000.00")  # exactly covers all
        result = service._apply_waterfall(gross, loan, Decimal("0"))

        assert result["fees"] == Decimal("500.00")
        assert result["interest"] == Decimal("2500.00")
        assert result["principal"] == Decimal("100000.00")
        assert result["suspense"] == Decimal("0.00")
        assert sum(result.values()) == gross

    def test_partial_payment_covers_fees_and_partial_interest(self):
        """A partial payment should fill fees first, then interest."""
        loan = make_loan(
            accrued_interest=Decimal("2500.00"),
            accrued_fees=Decimal("500.00"),
        )
        service = make_service()

        gross = Decimal("1000.00")
        result = service._apply_waterfall(gross, loan, Decimal("0"))

        assert result["fees"] == Decimal("500.00")
        assert result["interest"] == Decimal("500.00")
        assert result["principal"] == Decimal("0.00")
        assert result["suspense"] == Decimal("0.00")
        assert sum(result.values()) == gross

    def test_overpayment_goes_to_suspense(self):
        """Any amount beyond outstanding balances should go to suspense."""
        loan = make_loan(
            current_principal=Decimal("10000.00"),
            accrued_interest=Decimal("500.00"),
            accrued_fees=Decimal("0.00"),
        )
        service = make_service()

        gross = Decimal("11000.00")  # $500 overpayment
        result = service._apply_waterfall(gross, loan, Decimal("0"))

        assert result["principal"] == Decimal("10000.00")
        assert result["interest"] == Decimal("500.00")
        assert result["suspense"] == Decimal("500.00")
        assert sum(result.values()) == gross

    def test_late_fee_added_to_fees_bucket(self):
        """Late fee should be added to the fees bucket in the waterfall."""
        loan = make_loan(
            accrued_fees=Decimal("0.00"),
            accrued_interest=Decimal("1000.00"),
        )
        service = make_service()
        late_fee = Decimal("250.00")

        gross = Decimal("1250.00")
        result = service._apply_waterfall(gross, loan, late_fee)

        assert result["fees"] == Decimal("250.00")
        assert result["interest"] == Decimal("1000.00")
        assert result["suspense"] == Decimal("0.00")
        assert sum(result.values()) == gross

    def test_zero_payment_raises_or_returns_zero(self):
        """A zero gross amount should result in all-zero allocations."""
        loan = make_loan()
        service = make_service()

        # Note: PaymentCreate schema enforces gt=0, so this would be caught
        # before reaching service. But we test the waterfall logic directly.
        gross = Decimal("0.00")
        result = service._apply_waterfall(gross, loan, Decimal("0"))

        assert all(v == Decimal("0") for v in result.values())

    def test_waterfall_always_sums_to_gross(self):
        """Property test: allocations must always sum to gross amount."""
        test_cases = [
            (Decimal("50.00"), Decimal("100000.00"), Decimal("2500.00"), Decimal("250.00")),
            (Decimal("5000.00"), Decimal("100000.00"), Decimal("2500.00"), Decimal("0.00")),
            (Decimal("200000.00"), Decimal("100000.00"), Decimal("0.00"), Decimal("0.00")),
            (Decimal("0.01"), Decimal("100000.00"), Decimal("2500.00"), Decimal("500.00")),
        ]
        service = make_service()

        for gross, principal, interest, fees in test_cases:
            loan = make_loan(
                current_principal=principal,
                accrued_interest=interest,
                accrued_fees=fees,
            )
            result = service._apply_waterfall(gross, loan, Decimal("0"))
            assert sum(result.values()) == gross, (
                f"Imbalance for gross={gross}: {result}"
            )

    def test_fees_priority_over_interest(self):
        """Fees must be applied before interest — waterfall order is enforced."""
        loan = make_loan(
            accrued_fees=Decimal("300.00"),
            accrued_interest=Decimal("1000.00"),
            current_principal=Decimal("50000.00"),
        )
        service = make_service()

        # Payment that can cover fees but not full interest
        gross = Decimal("800.00")
        result = service._apply_waterfall(gross, loan, Decimal("0"))

        assert result["fees"] == Decimal("300.00")
        assert result["interest"] == Decimal("500.00")
        assert result["principal"] == Decimal("0.00")

    def test_penny_rounding_does_not_cause_imbalance(self):
        """Rounding to cents should not cause waterfall to not sum to gross."""
        loan = make_loan(
            current_principal=Decimal("99999.99"),
            accrued_interest=Decimal("1234.56"),
            accrued_fees=Decimal("0.01"),
        )
        service = make_service()

        gross = Decimal("101234.56")
        result = service._apply_waterfall(gross, loan, Decimal("0"))
        assert sum(result.values()) == gross


class TestLoanBalanceUpdates:

    @pytest.mark.asyncio
    async def test_balance_update_reduces_principal(self):
        """Applying principal should reduce loan.current_principal."""
        loan = make_loan(current_principal=Decimal("100000.00"))
        service = make_service()

        waterfall = {
            "fees": Decimal("0"),
            "interest": Decimal("0"),
            "principal": Decimal("5000.00"),
            "escrow": Decimal("0"),
            "advances": Decimal("0"),
            "suspense": Decimal("0"),
        }
        await service._update_loan_balances(loan, waterfall)
        assert loan.current_principal == Decimal("95000.00")

    @pytest.mark.asyncio
    async def test_balance_cannot_go_negative(self):
        """Overpayment of principal should floor at zero, not go negative."""
        loan = make_loan(current_principal=Decimal("1000.00"))
        service = make_service()

        waterfall = {
            "fees": Decimal("0"),
            "interest": Decimal("0"),
            "principal": Decimal("5000.00"),  # more than balance
            "escrow": Decimal("0"),
            "advances": Decimal("0"),
            "suspense": Decimal("0"),
        }
        await service._update_loan_balances(loan, waterfall)
        assert loan.current_principal == Decimal("0.00")

    @pytest.mark.asyncio
    async def test_reversal_increases_balances(self):
        """A reversal (negative waterfall) should restore balances."""
        loan = make_loan(
            current_principal=Decimal("95000.00"),
            accrued_interest=Decimal("0.00"),
        )
        service = make_service()

        # Reversing a $5000 principal payment
        reversal_waterfall = {
            "fees": Decimal("0"),
            "interest": Decimal("0"),
            "principal": Decimal("-5000.00"),
            "escrow": Decimal("0"),
            "advances": Decimal("0"),
            "suspense": Decimal("0"),
        }
        await service._update_loan_balances(loan, reversal_waterfall)
        assert loan.current_principal == Decimal("100000.00")
