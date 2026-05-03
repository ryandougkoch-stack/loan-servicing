"""
app/api/v1/endpoints/reports.py

Reporting endpoints. All are GET-only — no writes here.

Every endpoint:
  - Requires at minimum the 'reporting' role
  - Accepts an optional portfolio_id filter
  - Accepts an optional as_of_date parameter (defaults to today)
  - Returns a typed response schema with a meta block

Response caching note:
  These queries can be expensive on large portfolios. In production,
  consider adding a Redis cache layer (e.g. 5-minute TTL for intraday,
  longer for historical as_of queries). Not implemented here to keep
  the MVP scope clean, but the cache key pattern would be:
      f"report:{report_type}:{tenant_slug}:{portfolio_id}:{as_of_date}"
"""
from datetime import date, timedelta
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_min_role
from app.core.security import TokenPayload
from app.schemas.reports import (
    AgingReport,
    CashPositionReport,
    CollectorProductivityReport,
    ExceptionReport,
    MaturityPipelineReport,
    PayoffPipelineReport,
    PortfolioSummaryReport,
)
from app.services.reporting_service import ReportingService

router = APIRouter(prefix="/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# 1. Portfolio Summary
# ---------------------------------------------------------------------------

@router.get("/portfolio-summary", response_model=PortfolioSummaryReport)
async def portfolio_summary(
    as_of_date: Optional[date] = Query(None, description="Defaults to today"),
    portfolio_id: Optional[UUID] = Query(None, description="Filter to a single portfolio"),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """
    High-level AUM and portfolio health summary.

    Returns one row per portfolio with:
    - Loan counts by status
    - Outstanding principal, accrued interest, total exposure
    - Weighted average coupon (WAC) and maturity (WAM)
    - Delinquency % of portfolio
    - Grand totals across all portfolios
    """
    svc = ReportingService(db)
    return await svc.portfolio_summary(
        as_of_date=as_of_date,
        portfolio_id=portfolio_id,
    )


# ---------------------------------------------------------------------------
# 2. Aging / Delinquency
# ---------------------------------------------------------------------------

@router.get("/aging", response_model=AgingReport)
async def aging_report(
    as_of_date: Optional[date] = Query(None),
    portfolio_id: Optional[UUID] = Query(None),
    bucket: Optional[str] = Query(None, description="Filter to a single bucket: current | 1-30 | 31-60 | 61-90 | 91-120 | 120+"),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """
    Delinquency aging report.

    Returns loan-level detail plus bucket summary totals.
    Ordered by days past due descending (worst first).

    Bucket filter is additive with portfolio_id — you can ask for
    'all 91-120 day loans in Portfolio A'.
    """
    svc = ReportingService(db)
    return await svc.aging_report(
        as_of_date=as_of_date,
        portfolio_id=portfolio_id,
        bucket_filter=bucket,
    )


# ---------------------------------------------------------------------------
# 3. Cash Position
# ---------------------------------------------------------------------------

@router.get("/cash-position", response_model=CashPositionReport)
async def cash_position_report(
    period_start: date = Query(..., description="Start of reporting period"),
    period_end: date = Query(..., description="End of reporting period"),
    portfolio_id: Optional[UUID] = Query(None),
    frequency: str = Query("monthly", description="monthly | quarterly"),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """
    Scheduled vs. actual collections over a date range.

    Shows:
    - Scheduled P&I and fees per period (from payment_schedule)
    - Actual collections per period (from posted payments)
    - Variance and collection rate %
    - Forward look: expected collections in next 30/60/90 days
    """
    svc = ReportingService(db)
    return await svc.cash_position_report(
        period_start=period_start,
        period_end=period_end,
        portfolio_id=portfolio_id,
        frequency=frequency,
    )


# ---------------------------------------------------------------------------
# 4. Maturity Pipeline
# ---------------------------------------------------------------------------

@router.get("/maturity-pipeline", response_model=MaturityPipelineReport)
async def maturity_pipeline(
    as_of_date: Optional[date] = Query(None),
    portfolio_id: Optional[UUID] = Query(None),
    horizon_days: int = Query(365, ge=1, le=1825, description="Look-ahead window in days (default 365, max 5 years)"),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """
    Loans approaching maturity within the horizon window.

    Returns loan-level detail bucketed by 0-30 / 31-60 / 61-90 / 91-180 /
    181-365 / 365+ days to maturity, plus overdue (past maturity, not paid off).

    Useful for:
    - Portfolio management review meetings
    - Extension/refinance pipeline tracking
    - Concentration risk by maturity date
    """
    svc = ReportingService(db)
    return await svc.maturity_pipeline(
        as_of_date=as_of_date,
        portfolio_id=portfolio_id,
        horizon_days=horizon_days,
    )


# ---------------------------------------------------------------------------
# 5. Payoff Pipeline
# ---------------------------------------------------------------------------

@router.get("/payoff-pipeline", response_model=PayoffPipelineReport)
async def payoff_pipeline(
    as_of_date: Optional[date] = Query(None),
    portfolio_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """
    Active payoff quotes and their expiry status.

    Flags:
    - Quotes expiring in ≤7 days (need follow-up or refresh)
    - Total potential cash inflow if all active quotes close
    """
    svc = ReportingService(db)
    return await svc.payoff_pipeline(
        as_of_date=as_of_date,
        portfolio_id=portfolio_id,
    )


# ---------------------------------------------------------------------------
# 6. Collector Productivity
# ---------------------------------------------------------------------------

@router.get("/collector-productivity", response_model=CollectorProductivityReport)
async def collector_productivity(
    period_start: date = Query(...),
    period_end: date = Query(...),
    portfolio_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),  # ops+ only; not for borrowers/reporting-only roles
):
    """
    Collections team activity and outcome metrics for a period.

    Shows per-collector:
    - Call / email / letter counts
    - Promises obtained and kept (with keep rate %)
    - Loans contacted and resolved
    - Amount promised vs. collected

    Team aggregate totals included at bottom of response.
    """
    svc = ReportingService(db)
    return await svc.collector_productivity(
        period_start=period_start,
        period_end=period_end,
        portfolio_id=portfolio_id,
    )


# ---------------------------------------------------------------------------
# 7. Exceptions / Watchlist
# ---------------------------------------------------------------------------

@router.get("/exceptions", response_model=ExceptionReport)
async def exception_report(
    as_of_date: Optional[date] = Query(None),
    portfolio_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """
    Open exception items from the workflow queue.

    Covers: missing insurance, covenant breaches, UCC expiry, delinquency
    milestones, returned ACH, payoff requests, modification requests,
    and any other open workflow task.

    Ordered by priority (critical → high → normal → low) then due date.
    """
    svc = ReportingService(db)
    return await svc.exception_report(
        as_of_date=as_of_date,
        portfolio_id=portfolio_id,
    )


# ---------------------------------------------------------------------------
# Convenience: today's dashboard summary
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_model=dict)
async def dashboard_summary(
    portfolio_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """
    Composite endpoint that returns the key numbers for a management dashboard
    in a single API call. Runs portfolio summary, aging summary, maturity
    pipeline (90-day), and exception counts in parallel.
    """
    import asyncio
    svc = ReportingService(db)
    today = date.today()

    # Run all four queries concurrently
    portfolio, aging, maturity, exceptions = await asyncio.gather(
        svc.portfolio_summary(as_of_date=today, portfolio_id=portfolio_id),
        svc.aging_report(as_of_date=today, portfolio_id=portfolio_id),
        svc.maturity_pipeline(as_of_date=today, portfolio_id=portfolio_id, horizon_days=90),
        svc.exception_report(as_of_date=today, portfolio_id=portfolio_id),
    )

    return {
        "as_of_date": today.isoformat(),
        "portfolio": {
            "total_outstanding": str(portfolio.grand_total_outstanding),
            "total_exposure": str(portfolio.grand_total_exposure),
            "loan_count": portfolio.grand_loan_count,
            "total_past_due": str(portfolio.grand_total_past_due),
        },
        "aging": {
            "total_past_due": str(aging.total_past_due),
            "pct_delinquent": str(aging.pct_delinquent),
            "buckets": {b.bucket: {"count": b.loan_count, "past_due": str(b.total_past_due)} for b in aging.bucket_summary},
        },
        "maturity_90_days": {
            "loan_count": len(maturity.loans),
            "total_exposure": str(maturity.total_maturing_3m),
            "overdue_count": maturity.count_overdue,
        },
        "exceptions": {
            "critical": exceptions.critical_count,
            "high": exceptions.high_count,
            "total": exceptions.total_count,
            "exposure_flagged": str(exceptions.total_exposure_flagged),
        },
    }
