"""
app/schemas/loan.py

Pydantic v2 schemas for loan API request/response serialisation.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------

VALID_STATUSES = {
    "boarding", "approved", "funded", "modified", "delinquent",
    "default", "workout", "payoff_pending", "paid_off", "charged_off", "transferred"
}

VALID_RATE_TYPES = {"fixed", "floating", "mixed", "pik", "step", "zero_coupon"}

VALID_AMORTIZATION_TYPES = {"bullet", "interest_only", "amortizing", "partial_amortizing", "custom"}

# Allowed status transitions: {from_status: {allowed_to_statuses}}
STATUS_TRANSITIONS = {
    "boarding":       {"approved", "funded"},
    "approved":       {"funded", "boarding"},
    "funded":         {"modified", "delinquent", "default", "payoff_pending", "paid_off", "transferred"},
    "modified":       {"funded", "delinquent", "default", "payoff_pending", "paid_off"},
    "delinquent":     {"funded", "default", "workout", "payoff_pending", "paid_off", "charged_off"},
    "default":        {"workout", "payoff_pending", "charged_off", "transferred"},
    "workout":        {"funded", "modified", "paid_off", "charged_off"},
    "payoff_pending": {"paid_off"},
    "paid_off":       set(),
    "charged_off":    {"funded"},   # recovery scenario
    "transferred":    set(),
}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class LoanCreate(BaseModel):
    portfolio_id: UUID
    loan_number: Optional[str] = None          # auto-generated if omitted
    loan_name: Optional[str] = None
    primary_borrower_id: UUID
    currency: str = "USD"

    # Economics
    original_balance: Decimal = Field(gt=0)
    commitment_amount: Optional[Decimal] = Field(None, gt=0)

    # Rate
    rate_type: str
    coupon_rate: Optional[Decimal] = Field(None, ge=0, le=1)
    pik_rate: Optional[Decimal] = Field(None, ge=0, le=1)
    rate_floor: Optional[Decimal] = Field(None, ge=0)
    rate_cap: Optional[Decimal] = Field(None, ge=0)
    spread: Optional[Decimal] = Field(None, ge=0)
    index_code: Optional[str] = None
    day_count: str = "ACT/360"

    # Schedule
    origination_date: date
    first_payment_date: Optional[date] = None
    maturity_date: date
    payment_frequency: str = "QUARTERLY"
    amortization_type: str = "bullet"
    interest_only_period_months: Optional[int] = Field(None, ge=0)
    balloon_amount: Optional[Decimal] = Field(None, gt=0)

    # Late charges
    grace_period_days: int = Field(5, ge=0, le=30)
    late_fee_type: Optional[str] = None
    late_fee_amount: Optional[Decimal] = Field(None, ge=0)
    default_rate: Optional[Decimal] = Field(None, ge=0)

    # References
    investran_loan_id: Optional[str] = None
    servicer_notes: Optional[str] = None

    @model_validator(mode="after")
    def validate_loan(self) -> "LoanCreate":
        if self.rate_type not in VALID_RATE_TYPES:
            raise ValueError(f"Invalid rate_type: {self.rate_type}")
        if self.amortization_type not in VALID_AMORTIZATION_TYPES:
            raise ValueError(f"Invalid amortization_type: {self.amortization_type}")
        if self.maturity_date <= self.origination_date:
            raise ValueError("maturity_date must be after origination_date")
        if self.rate_type == "fixed" and self.coupon_rate is None:
            raise ValueError("coupon_rate is required for fixed rate loans")
        if self.rate_type == "floating" and (self.spread is None or self.index_code is None):
            raise ValueError("spread and index_code are required for floating rate loans")
        if self.rate_cap and self.rate_floor and self.rate_cap < self.rate_floor:
            raise ValueError("rate_cap must be >= rate_floor")
        return self


# ---------------------------------------------------------------------------
# Read (API response)
# ---------------------------------------------------------------------------

class LoanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    loan_number: str
    loan_name: Optional[str]
    status: str
    portfolio_id: UUID
    primary_borrower_id: UUID
    currency: str
    original_balance: Decimal
    commitment_amount: Optional[Decimal]
    current_principal: Decimal
    accrued_interest: Decimal
    accrued_fees: Decimal
    rate_type: str
    coupon_rate: Optional[Decimal]
    pik_rate: Optional[Decimal]
    spread: Optional[Decimal]
    index_code: Optional[str]
    day_count: str
    origination_date: date
    first_payment_date: Optional[date]
    maturity_date: date
    payment_frequency: str
    amortization_type: str
    grace_period_days: int
    late_fee_type: Optional[str]
    late_fee_amount: Optional[Decimal]
    default_rate: Optional[Decimal]
    default_triggered_at: Optional[date]
    prepayment_penalty_type: Optional[str] = None
    prepayment_penalty_pct: Optional[Decimal] = None
    prepayment_penalty_schedule: Optional[list] = None
    investran_loan_id: Optional[str]
    investran_last_sync_at: Optional[datetime]
    funded_at: Optional[date]
    paid_off_at: Optional[date]
    created_at: datetime
    updated_at: datetime


class LoanSummary(BaseModel):
    """Lightweight projection for list views and dashboards."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    loan_number: str
    loan_name: Optional[str]
    status: str
    portfolio_id: UUID
    primary_borrower_id: UUID
    currency: str
    current_principal: Decimal
    accrued_interest: Decimal
    rate_type: str
    coupon_rate: Optional[Decimal]
    maturity_date: date
    payment_frequency: str


class LoanStatusUpdate(BaseModel):
    status: str
    notes: Optional[str] = None

    @model_validator(mode="after")
    def validate_status(self) -> "LoanStatusUpdate":
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {self.status}")
        return self
