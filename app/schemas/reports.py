"""
app/schemas/reports.py

Pydantic response schemas for all reporting endpoints.

Design principles:
  - Every report has a typed schema — no bare dicts in API responses
  - Monetary amounts are always Decimal, never float
  - Dates are always date objects, never strings in internal logic
  - Each report includes metadata (as_of_date, generated_at, filters applied)
    so the consumer knows exactly what they're looking at
  - All counts and amounts default to zero (never None) for safe arithmetic
    on the frontend
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Shared metadata block
# ---------------------------------------------------------------------------

class ReportMeta(BaseModel):
    as_of_date: date
    generated_at: datetime
    portfolio_id: Optional[UUID] = None
    portfolio_name: Optional[str] = None


# ---------------------------------------------------------------------------
# 1. Portfolio Summary Report
# ---------------------------------------------------------------------------

class PortfolioSummaryRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    portfolio_id: UUID
    portfolio_name: str
    fund_type: Optional[str] = None
    base_currency: str = "USD"

    # Loan counts
    loan_count_total: int = 0
    loan_count_funded: int = 0
    loan_count_delinquent: int = 0
    loan_count_default: int = 0
    loan_count_workout: int = 0
    loan_count_paid_off: int = 0

    # Balances
    total_committed: Decimal = Decimal("0")
    total_outstanding_principal: Decimal = Decimal("0")
    total_accrued_interest: Decimal = Decimal("0")
    total_accrued_fees: Decimal = Decimal("0")
    total_exposure: Decimal = Decimal("0")       # principal + interest + fees

    # Performance
    weighted_avg_coupon: Optional[Decimal] = None      # WAC
    weighted_avg_maturity_years: Optional[Decimal] = None  # WAM

    # Delinquency summary
    total_past_due_amount: Decimal = Decimal("0")
    pct_portfolio_delinquent: Decimal = Decimal("0")   # % of outstanding that is past due


class PortfolioSummaryReport(BaseModel):
    meta: ReportMeta
    portfolios: list[PortfolioSummaryRow]

    # Aggregate across all portfolios
    grand_total_outstanding: Decimal = Decimal("0")
    grand_total_exposure: Decimal = Decimal("0")
    grand_total_past_due: Decimal = Decimal("0")
    grand_loan_count: int = 0


# ---------------------------------------------------------------------------
# 2. Aging / Delinquency Report
# ---------------------------------------------------------------------------

class AgingBucket(BaseModel):
    """One delinquency bucket for a single loan."""
    model_config = ConfigDict(from_attributes=True)

    loan_id: UUID
    loan_number: str
    loan_name: Optional[str] = None
    borrower_name: str
    portfolio_name: str

    current_principal: Decimal = Decimal("0")
    days_past_due: int = 0
    bucket: str = "current"           # current | 1-30 | 31-60 | 61-90 | 91-120 | 120+

    principal_past_due: Decimal = Decimal("0")
    interest_past_due: Decimal = Decimal("0")
    fees_past_due: Decimal = Decimal("0")
    total_past_due: Decimal = Decimal("0")

    last_payment_date: Optional[date] = None
    last_payment_amount: Optional[Decimal] = None
    maturity_date: date
    rate_type: str
    effective_rate: Optional[Decimal] = None


class AgingBucketSummary(BaseModel):
    """Aggregate totals for one delinquency bucket across all loans."""
    bucket: str
    loan_count: int = 0
    total_outstanding: Decimal = Decimal("0")
    total_past_due: Decimal = Decimal("0")
    pct_of_portfolio: Decimal = Decimal("0")


class AgingReport(BaseModel):
    meta: ReportMeta
    loans: list[AgingBucket]
    bucket_summary: list[AgingBucketSummary]

    # Totals
    total_loans: int = 0
    total_outstanding: Decimal = Decimal("0")
    total_past_due: Decimal = Decimal("0")
    pct_delinquent: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# 3. Cash Position Report
# ---------------------------------------------------------------------------

class CashPositionPeriod(BaseModel):
    """Collections and expected payments for a given date range."""
    period_label: str             # e.g. "March 2025"
    period_start: date
    period_end: date

    # Expected (from payment schedule)
    scheduled_principal: Decimal = Decimal("0")
    scheduled_interest: Decimal = Decimal("0")
    scheduled_fees: Decimal = Decimal("0")
    total_scheduled: Decimal = Decimal("0")

    # Actual (from posted payments)
    collected_principal: Decimal = Decimal("0")
    collected_interest: Decimal = Decimal("0")
    collected_fees: Decimal = Decimal("0")
    total_collected: Decimal = Decimal("0")

    # Variance
    variance: Decimal = Decimal("0")             # collected - scheduled
    collection_rate: Decimal = Decimal("0")      # collected / scheduled (%)

    # Payment counts
    payments_received: int = 0
    payments_returned: int = 0


class CashPositionReport(BaseModel):
    meta: ReportMeta
    periods: list[CashPositionPeriod]

    # Rolling totals
    total_scheduled_ytd: Decimal = Decimal("0")
    total_collected_ytd: Decimal = Decimal("0")
    ytd_collection_rate: Decimal = Decimal("0")

    # Current month forward look
    next_30_days_expected: Decimal = Decimal("0")
    next_60_days_expected: Decimal = Decimal("0")
    next_90_days_expected: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# 4. Maturity Pipeline Report
# ---------------------------------------------------------------------------

class MaturityItem(BaseModel):
    """A loan approaching maturity."""
    loan_id: UUID
    loan_number: str
    loan_name: Optional[str] = None
    borrower_name: str
    portfolio_name: str

    maturity_date: date
    days_to_maturity: int
    maturity_bucket: str     # 0-30 | 31-60 | 61-90 | 91-180 | 181-365 | 365+

    current_principal: Decimal = Decimal("0")
    accrued_interest: Decimal = Decimal("0")
    total_exposure: Decimal = Decimal("0")

    loan_status: str
    rate_type: str
    effective_rate: Optional[Decimal] = None

    # Extension / payoff signals
    has_extension_option: bool = False
    payoff_quote_active: bool = False
    workout_plan_active: bool = False


class MaturityBucketSummary(BaseModel):
    bucket: str
    loan_count: int = 0
    total_exposure: Decimal = Decimal("0")


class MaturityPipelineReport(BaseModel):
    meta: ReportMeta
    loans: list[MaturityItem]
    bucket_summary: list[MaturityBucketSummary]

    total_maturing_12m: Decimal = Decimal("0")  # total exposure maturing in 12 months
    total_maturing_3m: Decimal = Decimal("0")
    count_overdue: int = 0                       # past maturity, not yet paid off


# ---------------------------------------------------------------------------
# 5. Payoff Pipeline Report
# ---------------------------------------------------------------------------

class PayoffPipelineItem(BaseModel):
    loan_id: UUID
    loan_number: str
    loan_name: Optional[str] = None
    borrower_name: str
    portfolio_name: str

    quote_id: UUID
    quote_date: date
    good_through_date: date
    quote_expires_in_days: int

    principal_balance: Decimal = Decimal("0")
    accrued_interest: Decimal = Decimal("0")
    fees_outstanding: Decimal = Decimal("0")
    prepayment_penalty: Decimal = Decimal("0")
    total_payoff: Decimal = Decimal("0")
    per_diem: Decimal = Decimal("0")

    quote_status: str   # active | expired | used | cancelled
    loan_status: str


class PayoffPipelineReport(BaseModel):
    meta: ReportMeta
    items: list[PayoffPipelineItem]

    active_quote_count: int = 0
    total_payoff_if_all_close: Decimal = Decimal("0")
    expiring_in_7_days: int = 0


# ---------------------------------------------------------------------------
# 6. Collector Productivity Report
# ---------------------------------------------------------------------------

class CollectorStats(BaseModel):
    """Activity summary for one servicing user."""
    user_id: UUID
    user_name: str

    period_start: date
    period_end: date

    # Activity counts
    total_activities: int = 0
    calls_made: int = 0
    emails_sent: int = 0
    letters_sent: int = 0
    promises_obtained: int = 0
    promises_kept: int = 0
    promise_kept_rate: Decimal = Decimal("0")   # %

    # Loans touched
    loans_contacted: int = 0
    loans_resolved: int = 0    # moved from delinquent → current

    # Dollar impact
    amount_promised: Decimal = Decimal("0")
    amount_collected_post_promise: Decimal = Decimal("0")


class CollectorProductivityReport(BaseModel):
    meta: ReportMeta
    period_start: date
    period_end: date
    collectors: list[CollectorStats]

    # Team totals
    team_total_activities: int = 0
    team_total_collected: Decimal = Decimal("0")
    team_promise_kept_rate: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# 7. Exception / Watchlist Report
# ---------------------------------------------------------------------------

class ExceptionItem(BaseModel):
    """A loan flagged for an exception condition."""
    loan_id: UUID
    loan_number: str
    loan_name: Optional[str] = None
    borrower_name: str
    portfolio_name: str
    current_principal: Decimal = Decimal("0")
    loan_status: str

    exception_type: str   # e.g. missing_insurance | covenant_breach | ucc_expiring | etc.
    exception_detail: str
    days_open: int = 0
    priority: str = "normal"   # low | normal | high | critical
    assigned_to: Optional[str] = None
    due_date: Optional[date] = None


class ExceptionReport(BaseModel):
    meta: ReportMeta
    exceptions: list[ExceptionItem]

    critical_count: int = 0
    high_count: int = 0
    normal_count: int = 0
    total_count: int = 0
    total_exposure_flagged: Decimal = Decimal("0")
