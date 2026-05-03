"""
app/services/reporting_service.py

Query engine for all reports.

Design principles:
  - SQL-first for aggregations: push grouping, filtering, and summing to
    Postgres rather than loading rows into Python and aggregating there.
    At 1,000+ loans this matters a lot.
  - Every query is parameterised — no string interpolation in SQL.
  - All monetary arithmetic uses Decimal throughout.
  - Each report method is independently testable; no shared mutable state.
  - Queries use CTEs for readability; SQLAlchemy core (text()) for complex
    aggregations where the ORM would produce awkward SQL.
  - Reports run in READ-ONLY mode: no writes, no flushes, no commits.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.reports import (
    AgingBucket,
    AgingBucketSummary,
    AgingReport,
    CashPositionPeriod,
    CashPositionReport,
    CollectorProductivityReport,
    CollectorStats,
    ExceptionItem,
    ExceptionReport,
    MaturityBucketSummary,
    MaturityItem,
    MaturityPipelineReport,
    PayoffPipelineItem,
    PayoffPipelineReport,
    PortfolioSummaryReport,
    PortfolioSummaryRow,
    ReportMeta,
)

logger = structlog.get_logger(__name__)

CENTS = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")


# ---------------------------------------------------------------------------
# Delinquency bucket helpers
# ---------------------------------------------------------------------------

def _dpd_to_bucket(days: int) -> str:
    if days <= 0:
        return "current"
    elif days <= 30:
        return "1-30"
    elif days <= 60:
        return "31-60"
    elif days <= 90:
        return "61-90"
    elif days <= 120:
        return "91-120"
    return "120+"


BUCKET_ORDER = ["current", "1-30", "31-60", "61-90", "91-120", "120+"]
MATURITY_BUCKETS = ["0-30", "31-60", "61-90", "91-180", "181-365", "365+"]


def _days_to_maturity_bucket(days: int) -> str:
    if days < 0:
        return "overdue"
    elif days <= 30:
        return "0-30"
    elif days <= 60:
        return "31-60"
    elif days <= 90:
        return "61-90"
    elif days <= 180:
        return "91-180"
    elif days <= 365:
        return "181-365"
    return "365+"


def _safe_pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == ZERO:
        return ZERO
    return (numerator / denominator * HUNDRED).quantize(CENTS, rounding=ROUND_HALF_UP)


def _meta(as_of: date, portfolio_id=None, portfolio_name=None) -> ReportMeta:
    return ReportMeta(
        as_of_date=as_of,
        generated_at=datetime.now(timezone.utc),
        portfolio_id=portfolio_id,
        portfolio_name=portfolio_name,
    )


# ===========================================================================
# Reporting Service
# ===========================================================================

class ReportingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # -------------------------------------------------------------------------
    # 1. Portfolio Summary
    # -------------------------------------------------------------------------

    async def portfolio_summary(
        self,
        as_of_date: Optional[date] = None,
        portfolio_id: Optional[UUID] = None,
    ) -> PortfolioSummaryReport:
        as_of = as_of_date or date.today()

        # Phase 2: rollup is allocation-aware.
        # - loan/delinquency stats join through loan_allocation (active as-of :as_of)
        # - dollar SUMs are prorated by ownership_pct / 100
        # - weighted-avg numerator and denominator both use ownership_pct (the /100 cancels)
        # - filter pf_loan keys off a.portfolio_id (the allocating fund), not l.portfolio_id
        pf_loan = f"AND a.portfolio_id = '{portfolio_id}'" if portfolio_id else ""
        pf_port = f"AND p.id = '{portfolio_id}'" if portfolio_id else ""

        sql = text(f"""
            WITH loan_stats AS (
                SELECT
                    a.portfolio_id,
                    COUNT(*)                                                FILTER (WHERE l.status NOT IN ('boarding','paid_off','charged_off','transferred'))  AS loan_count_total,
                    COUNT(*)                                                FILTER (WHERE l.status = 'funded')          AS loan_count_funded,
                    COUNT(*)                                                FILTER (WHERE l.status = 'delinquent')      AS loan_count_delinquent,
                    COUNT(*)                                                FILTER (WHERE l.status = 'default')         AS loan_count_default,
                    COUNT(*)                                                FILTER (WHERE l.status = 'workout')         AS loan_count_workout,
                    COUNT(*)                                                FILTER (WHERE l.status = 'paid_off')        AS loan_count_paid_off,
                    COALESCE(SUM(l.commitment_amount * a.ownership_pct / 100), 0) AS total_committed,
                    COALESCE(SUM(l.current_principal * a.ownership_pct / 100), 0) AS total_outstanding_principal,
                    COALESCE(SUM(l.accrued_interest  * a.ownership_pct / 100), 0) AS total_accrued_interest,
                    COALESCE(SUM(l.accrued_fees      * a.ownership_pct / 100), 0) AS total_accrued_fees,
                    COALESCE(SUM((l.current_principal + l.accrued_interest + l.accrued_fees) * a.ownership_pct / 100), 0) AS total_exposure,
                    CASE WHEN SUM(l.current_principal * a.ownership_pct) > 0
                         THEN SUM(l.coupon_rate * l.current_principal * a.ownership_pct) / SUM(l.current_principal * a.ownership_pct)
                         ELSE NULL END                                      AS weighted_avg_coupon,
                    CASE WHEN SUM(l.current_principal * a.ownership_pct) > 0
                         THEN SUM((l.maturity_date - :as_of) * l.current_principal * a.ownership_pct) / SUM(l.current_principal * a.ownership_pct) / 365.25
                         ELSE NULL END                                      AS weighted_avg_maturity_years
                FROM loan l
                JOIN loan_allocation a
                  ON a.loan_id = l.id
                 AND a.effective_date <= :as_of
                 AND (a.end_date IS NULL OR a.end_date > :as_of)
                WHERE l.status NOT IN ('boarding', 'transferred')
                  {pf_loan}
                GROUP BY a.portfolio_id
            ),
            delinquency_stats AS (
                SELECT
                    a.portfolio_id,
                    COALESCE(SUM(dr.total_past_due * a.ownership_pct / 100), 0)  AS total_past_due_amount
                FROM loan l
                JOIN loan_allocation a
                  ON a.loan_id = l.id
                 AND a.effective_date <= :as_of
                 AND (a.end_date IS NULL OR a.end_date > :as_of)
                JOIN LATERAL (
                    SELECT total_past_due
                    FROM delinquency_record
                    WHERE loan_id = l.id
                      AND as_of_date <= :as_of
                    ORDER BY as_of_date DESC
                    LIMIT 1
                ) dr ON true
                WHERE 1=1 {pf_loan}
                GROUP BY a.portfolio_id
            )
            SELECT
                p.id                            AS portfolio_id,
                p.name                          AS portfolio_name,
                p.fund_type,
                p.base_currency,
                COALESCE(ls.loan_count_total,       0)  AS loan_count_total,
                COALESCE(ls.loan_count_funded,      0)  AS loan_count_funded,
                COALESCE(ls.loan_count_delinquent,  0)  AS loan_count_delinquent,
                COALESCE(ls.loan_count_default,     0)  AS loan_count_default,
                COALESCE(ls.loan_count_workout,     0)  AS loan_count_workout,
                COALESCE(ls.loan_count_paid_off,    0)  AS loan_count_paid_off,
                COALESCE(ls.total_committed,        0)  AS total_committed,
                COALESCE(ls.total_outstanding_principal, 0) AS total_outstanding_principal,
                COALESCE(ls.total_accrued_interest, 0)  AS total_accrued_interest,
                COALESCE(ls.total_accrued_fees,     0)  AS total_accrued_fees,
                COALESCE(ls.total_exposure,         0)  AS total_exposure,
                ls.weighted_avg_coupon,
                ls.weighted_avg_maturity_years,
                COALESCE(ds.total_past_due_amount,  0)  AS total_past_due_amount
            FROM portfolio p
            LEFT JOIN loan_stats ls         ON ls.portfolio_id = p.id
            LEFT JOIN delinquency_stats ds  ON ds.portfolio_id = p.id
            WHERE p.status = 'active'
              {pf_port}
            ORDER BY p.name
        """)

        result = await self.db.execute(sql, {"as_of": as_of})
        rows = result.mappings().all()

        portfolios = []
        for row in rows:
            outstanding = Decimal(str(row["total_outstanding_principal"]))
            past_due = Decimal(str(row["total_past_due_amount"]))
            port = PortfolioSummaryRow(
                portfolio_id=row["portfolio_id"],
                portfolio_name=row["portfolio_name"],
                fund_type=row["fund_type"],
                base_currency=row["base_currency"] or "USD",
                loan_count_total=row["loan_count_total"] or 0,
                loan_count_funded=row["loan_count_funded"] or 0,
                loan_count_delinquent=row["loan_count_delinquent"] or 0,
                loan_count_default=row["loan_count_default"] or 0,
                loan_count_workout=row["loan_count_workout"] or 0,
                loan_count_paid_off=row["loan_count_paid_off"] or 0,
                total_committed=Decimal(str(row["total_committed"])),
                total_outstanding_principal=outstanding,
                total_accrued_interest=Decimal(str(row["total_accrued_interest"])),
                total_accrued_fees=Decimal(str(row["total_accrued_fees"])),
                total_exposure=Decimal(str(row["total_exposure"])),
                weighted_avg_coupon=Decimal(str(row["weighted_avg_coupon"])).quantize(Decimal("0.000001")) if row["weighted_avg_coupon"] else None,
                weighted_avg_maturity_years=Decimal(str(row["weighted_avg_maturity_years"])).quantize(CENTS) if row["weighted_avg_maturity_years"] else None,
                total_past_due_amount=past_due,
                pct_portfolio_delinquent=_safe_pct(past_due, outstanding),
            )
            portfolios.append(port)

        grand_outstanding = sum(p.total_outstanding_principal for p in portfolios)
        grand_exposure = sum(p.total_exposure for p in portfolios)
        grand_past_due = sum(p.total_past_due_amount for p in portfolios)
        grand_count = sum(p.loan_count_total for p in portfolios)

        return PortfolioSummaryReport(
            meta=_meta(as_of, portfolio_id),
            portfolios=portfolios,
            grand_total_outstanding=grand_outstanding,
            grand_total_exposure=grand_exposure,
            grand_total_past_due=grand_past_due,
            grand_loan_count=grand_count,
        )

    # -------------------------------------------------------------------------
    # 2. Aging / Delinquency Report
    # -------------------------------------------------------------------------

    async def aging_report(
        self,
        as_of_date: Optional[date] = None,
        portfolio_id: Optional[UUID] = None,
        bucket_filter: Optional[str] = None,
    ) -> AgingReport:
        as_of = as_of_date or date.today()

        # Allocation-aware: one row per (loan × active allocation). Dollar columns
        # are pro-rated by ownership_pct/100. portfolio_name is the allocating fund's
        # name (a.portfolio_id), not the lead-fund pointer (l.portfolio_id).
        pf_alloc = f"AND a.portfolio_id = '{portfolio_id}'" if portfolio_id else ""

        sql = text(f"""
            SELECT
                l.id                        AS loan_id,
                l.loan_number,
                l.loan_name,
                (l.current_principal * a.ownership_pct / 100)  AS current_principal,
                l.maturity_date,
                l.rate_type,
                l.coupon_rate               AS effective_rate,
                c.legal_name                AS borrower_name,
                p.name                      AS portfolio_name,
                -- Latest delinquency record (pro-rated)
                COALESCE(dr.days_past_due, 0)                                   AS days_past_due,
                COALESCE(dr.principal_past_due * a.ownership_pct / 100, 0)      AS principal_past_due,
                COALESCE(dr.interest_past_due  * a.ownership_pct / 100, 0)      AS interest_past_due,
                COALESCE(dr.fees_past_due      * a.ownership_pct / 100, 0)      AS fees_past_due,
                COALESCE(dr.total_past_due     * a.ownership_pct / 100, 0)      AS total_past_due,
                -- Last payment (loan-level, not pro-rated — the wire was for the full amount)
                lp.received_date            AS last_payment_date,
                lp.gross_amount             AS last_payment_amount
            FROM loan l
            JOIN loan_allocation a
              ON a.loan_id = l.id
             AND a.effective_date <= :as_of
             AND (a.end_date IS NULL OR a.end_date > :as_of)
            JOIN portfolio p        ON p.id = a.portfolio_id
            JOIN counterparty c     ON c.id = l.primary_borrower_id
            LEFT JOIN LATERAL (
                SELECT days_past_due, principal_past_due, interest_past_due,
                       fees_past_due, total_past_due
                FROM delinquency_record
                WHERE loan_id = l.id
                  AND as_of_date <= :as_of
                ORDER BY as_of_date DESC
                LIMIT 1
            ) dr ON true
            LEFT JOIN LATERAL (
                SELECT received_date, gross_amount
                FROM payment
                WHERE loan_id = l.id
                  AND status = 'posted'
                ORDER BY received_date DESC
                LIMIT 1
            ) lp ON true
            WHERE l.status IN ('funded','modified','delinquent','default','workout','payoff_pending')
              {pf_alloc}
            ORDER BY COALESCE(dr.days_past_due, 0) DESC, l.current_principal DESC
        """)

        result = await self.db.execute(sql, {
            "as_of": as_of,
        })
        rows = result.mappings().all()

        loans = []
        for row in rows:
            dpd = int(row["days_past_due"] or 0)
            bucket = _dpd_to_bucket(dpd)
            if bucket_filter and bucket != bucket_filter:
                continue
            loans.append(AgingBucket(
                loan_id=row["loan_id"],
                loan_number=row["loan_number"],
                loan_name=row["loan_name"],
                borrower_name=row["borrower_name"],
                portfolio_name=row["portfolio_name"],
                current_principal=Decimal(str(row["current_principal"])),
                days_past_due=dpd,
                bucket=bucket,
                principal_past_due=Decimal(str(row["principal_past_due"])),
                interest_past_due=Decimal(str(row["interest_past_due"])),
                fees_past_due=Decimal(str(row["fees_past_due"])),
                total_past_due=Decimal(str(row["total_past_due"])),
                last_payment_date=row["last_payment_date"],
                last_payment_amount=Decimal(str(row["last_payment_amount"])) if row["last_payment_amount"] else None,
                maturity_date=row["maturity_date"],
                rate_type=row["rate_type"],
                effective_rate=Decimal(str(row["effective_rate"])) if row["effective_rate"] else None,
            ))

        # Bucket summary
        bucket_totals: dict[str, dict] = {b: {"count": 0, "outstanding": ZERO, "past_due": ZERO} for b in BUCKET_ORDER}
        total_outstanding = ZERO
        total_past_due = ZERO

        for loan in loans:
            bt = bucket_totals[loan.bucket]
            bt["count"] += 1
            bt["outstanding"] += loan.current_principal
            bt["past_due"] += loan.total_past_due
            total_outstanding += loan.current_principal
            total_past_due += loan.total_past_due

        bucket_summary = [
            AgingBucketSummary(
                bucket=b,
                loan_count=bucket_totals[b]["count"],
                total_outstanding=bucket_totals[b]["outstanding"],
                total_past_due=bucket_totals[b]["past_due"],
                pct_of_portfolio=_safe_pct(bucket_totals[b]["outstanding"], total_outstanding),
            )
            for b in BUCKET_ORDER
        ]

        return AgingReport(
            meta=_meta(as_of, portfolio_id),
            loans=loans,
            bucket_summary=bucket_summary,
            total_loans=len(loans),
            total_outstanding=total_outstanding,
            total_past_due=total_past_due,
            pct_delinquent=_safe_pct(total_past_due, total_outstanding),
        )

    # -------------------------------------------------------------------------
    # 3. Cash Position Report
    # -------------------------------------------------------------------------

    async def cash_position_report(
        self,
        period_start: date,
        period_end: date,
        portfolio_id: Optional[UUID] = None,
        frequency: str = "monthly",   # monthly | quarterly
    ) -> CashPositionReport:
        as_of = date.today()

        # Build period buckets
        periods_meta = self._build_cash_periods(period_start, period_end, frequency)

        # Allocation-aware: scheduled and collected cash get pro-rated by
        # ownership_pct/100 against the portfolio's share. Active allocations
        # only — uses end_date IS NULL (period end is a single point in time
        # but the allocation set we care about is "what's active now"; for
        # a historical cash-position report this would need as-of refinement).
        pf_alloc = f"AND a.portfolio_id = '{portfolio_id}'" if portfolio_id else ""

        scheduled_sql = text(f"""
            SELECT
                ps.due_date,
                COALESCE(SUM(ps.scheduled_principal * a.ownership_pct / 100), 0) AS sched_principal,
                COALESCE(SUM(ps.scheduled_interest  * a.ownership_pct / 100), 0) AS sched_interest,
                COALESCE(SUM(ps.scheduled_fees      * a.ownership_pct / 100), 0) AS sched_fees
            FROM payment_schedule ps
            JOIN loan l ON l.id = ps.loan_id
            JOIN loan_allocation a
              ON a.loan_id = l.id AND a.end_date IS NULL
            WHERE ps.is_current = true
              AND ps.due_date BETWEEN :start AND :end
              {pf_alloc}
            GROUP BY ps.due_date
            ORDER BY ps.due_date
        """)

        collected_sql = text(f"""
            SELECT
                py.effective_date,
                COALESCE(SUM(py.applied_to_principal * a.ownership_pct / 100), 0) AS coll_principal,
                COALESCE(SUM(py.applied_to_interest  * a.ownership_pct / 100), 0) AS coll_interest,
                COALESCE(SUM(py.applied_to_fees      * a.ownership_pct / 100), 0) AS coll_fees,
                COUNT(DISTINCT py.id) FILTER (WHERE py.status = 'posted')   AS payments_received,
                COUNT(DISTINCT py.id) FILTER (WHERE py.status = 'returned') AS payments_returned
            FROM payment py
            JOIN loan l ON l.id = py.loan_id
            JOIN loan_allocation a
              ON a.loan_id = l.id AND a.end_date IS NULL
            WHERE py.effective_date BETWEEN :start AND :end
              AND py.status IN ('posted','returned')
              {pf_alloc}
            GROUP BY py.effective_date
            ORDER BY py.effective_date
        """)

        params = {
            "start": period_start,
            "end": period_end,
        }

        sched_result = await self.db.execute(scheduled_sql, params)
        coll_result = await self.db.execute(collected_sql, params)

        sched_by_date = {
            row["due_date"]: row for row in sched_result.mappings().all()
        }
        coll_by_date = {
            row["effective_date"]: row for row in coll_result.mappings().all()
        }

        # Build period rows
        report_periods = []
        total_scheduled_ytd = ZERO
        total_collected_ytd = ZERO

        for label, pstart, pend in periods_meta:
            # Aggregate scheduled amounts for all due dates in this period
            sched_p = ZERO
            sched_i = ZERO
            sched_f = ZERO
            coll_p = ZERO
            coll_i = ZERO
            coll_f = ZERO
            pmts_received = 0
            pmts_returned = 0

            current_day = pstart
            while current_day <= pend:
                if current_day in sched_by_date:
                    r = sched_by_date[current_day]
                    sched_p += Decimal(str(r["sched_principal"]))
                    sched_i += Decimal(str(r["sched_interest"]))
                    sched_f += Decimal(str(r["sched_fees"]))
                if current_day in coll_by_date:
                    r = coll_by_date[current_day]
                    coll_p += Decimal(str(r["coll_principal"]))
                    coll_i += Decimal(str(r["coll_interest"]))
                    coll_f += Decimal(str(r["coll_fees"]))
                    pmts_received += int(r["payments_received"] or 0)
                    pmts_returned += int(r["payments_returned"] or 0)
                current_day += timedelta(days=1)

            total_sched = sched_p + sched_i + sched_f
            total_coll = coll_p + coll_i + coll_f
            variance = total_coll - total_sched

            total_scheduled_ytd += total_sched
            total_collected_ytd += total_coll

            report_periods.append(CashPositionPeriod(
                period_label=label,
                period_start=pstart,
                period_end=pend,
                scheduled_principal=sched_p,
                scheduled_interest=sched_i,
                scheduled_fees=sched_f,
                total_scheduled=total_sched,
                collected_principal=coll_p,
                collected_interest=coll_i,
                collected_fees=coll_f,
                total_collected=total_coll,
                variance=variance,
                collection_rate=_safe_pct(total_coll, total_sched),
                payments_received=pmts_received,
                payments_returned=pmts_returned,
            ))

        # Forward look: expected in 30/60/90 days
        fwd_30, fwd_60, fwd_90 = await self._forward_expected(as_of, portfolio_id)

        return CashPositionReport(
            meta=_meta(as_of, portfolio_id),
            periods=report_periods,
            total_scheduled_ytd=total_scheduled_ytd,
            total_collected_ytd=total_collected_ytd,
            ytd_collection_rate=_safe_pct(total_collected_ytd, total_scheduled_ytd),
            next_30_days_expected=fwd_30,
            next_60_days_expected=fwd_60,
            next_90_days_expected=fwd_90,
        )

    async def _forward_expected(
        self,
        as_of: date,
        portfolio_id: Optional[UUID],
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Sum of scheduled payments due in next 30/60/90 days, pro-rated by allocation."""
        pf_alloc = f"AND a.portfolio_id = '{portfolio_id}'" if portfolio_id else ""
        sql = text(f"""
            SELECT
                COALESCE(SUM(ps.total_scheduled * a.ownership_pct / 100) FILTER (WHERE ps.due_date <= :d30), 0) AS fwd_30,
                COALESCE(SUM(ps.total_scheduled * a.ownership_pct / 100) FILTER (WHERE ps.due_date <= :d60), 0) AS fwd_60,
                COALESCE(SUM(ps.total_scheduled * a.ownership_pct / 100) FILTER (WHERE ps.due_date <= :d90), 0) AS fwd_90
            FROM payment_schedule ps
            JOIN loan l ON l.id = ps.loan_id
            JOIN loan_allocation a
              ON a.loan_id = l.id AND a.end_date IS NULL
            WHERE ps.is_current = true
              AND ps.status IN ('open','partial')
              AND ps.due_date > :as_of
              {pf_alloc}
        """)
        result = await self.db.execute(sql, {
            "as_of": as_of,
            "d30": as_of + timedelta(days=30),
            "d60": as_of + timedelta(days=60),
            "d90": as_of + timedelta(days=90),
        })
        row = result.mappings().one()
        return (
            Decimal(str(row["fwd_30"])),
            Decimal(str(row["fwd_60"])),
            Decimal(str(row["fwd_90"])),
        )

    # -------------------------------------------------------------------------
    # 4. Maturity Pipeline Report
    # -------------------------------------------------------------------------

    async def maturity_pipeline(
        self,
        as_of_date: Optional[date] = None,
        portfolio_id: Optional[UUID] = None,
        horizon_days: int = 365,
    ) -> MaturityPipelineReport:
        as_of = as_of_date or date.today()
        cutoff = as_of + timedelta(days=horizon_days)

        # Allocation-aware: one row per (loan × active allocation). Principal,
        # accrued interest, and total exposure are pro-rated by ownership_pct/100.
        # portfolio_name is the allocating fund (a.portfolio_id), not the lead.
        pf_alloc = f"AND a.portfolio_id = '{portfolio_id}'" if portfolio_id else ""
        sql = text(f"""
            SELECT
                l.id                AS loan_id,
                l.loan_number,
                l.loan_name,
                l.maturity_date,
                (l.current_principal * a.ownership_pct / 100)  AS current_principal,
                (l.accrued_interest  * a.ownership_pct / 100)  AS accrued_interest,
                ((l.current_principal + l.accrued_interest + l.accrued_fees) * a.ownership_pct / 100)  AS total_exposure,
                l.status            AS loan_status,
                l.rate_type,
                l.coupon_rate       AS effective_rate,
                (l.maturity_date - :as_of)  AS days_to_maturity,
                c.legal_name        AS borrower_name,
                p.name              AS portfolio_name,
                EXISTS (
                    SELECT 1 FROM payoff_quote pq
                    WHERE pq.loan_id = l.id
                      AND pq.status = 'active'
                      AND pq.good_through_date >= :as_of
                ) AS payoff_quote_active,
                EXISTS (
                    SELECT 1 FROM workout_plan wp
                    WHERE wp.loan_id = l.id AND wp.status = 'active'
                ) AS workout_plan_active
            FROM loan l
            JOIN loan_allocation a
              ON a.loan_id = l.id
             AND a.effective_date <= :as_of
             AND (a.end_date IS NULL OR a.end_date > :as_of)
            JOIN portfolio p    ON p.id = a.portfolio_id
            JOIN counterparty c ON c.id = l.primary_borrower_id
            WHERE l.status IN ('funded','modified','delinquent','default','workout')
              AND l.maturity_date <= :cutoff
              {pf_alloc}
            ORDER BY l.maturity_date ASC
        """)

        result = await self.db.execute(sql, {
            "as_of": as_of,
            "cutoff": cutoff,
        })
        rows = result.mappings().all()

        loans = []
        overdue_count = 0
        maturing_3m = ZERO
        maturing_12m = ZERO

        for row in rows:
            dtm = int(row["days_to_maturity"] or 0)
            bucket = _days_to_maturity_bucket(dtm)
            if dtm < 0:
                overdue_count += 1
            exposure = Decimal(str(row["total_exposure"]))
            if dtm <= 90:
                maturing_3m += exposure
            if dtm <= 365:
                maturing_12m += exposure

            loans.append(MaturityItem(
                loan_id=row["loan_id"],
                loan_number=row["loan_number"],
                loan_name=row["loan_name"],
                borrower_name=row["borrower_name"],
                portfolio_name=row["portfolio_name"],
                maturity_date=row["maturity_date"],
                days_to_maturity=dtm,
                maturity_bucket=bucket,
                current_principal=Decimal(str(row["current_principal"])),
                accrued_interest=Decimal(str(row["accrued_interest"])),
                total_exposure=exposure,
                loan_status=row["loan_status"],
                rate_type=row["rate_type"],
                effective_rate=Decimal(str(row["effective_rate"])) if row["effective_rate"] else None,
                payoff_quote_active=bool(row["payoff_quote_active"]),
                workout_plan_active=bool(row["workout_plan_active"]),
            ))

        # Bucket summary
        bucket_totals = {b: {"count": 0, "exposure": ZERO} for b in ["overdue"] + list(MATURITY_BUCKETS)}
        for loan in loans:
            b = loan.maturity_bucket
            if b in bucket_totals:
                bucket_totals[b]["count"] += 1
                bucket_totals[b]["exposure"] += loan.total_exposure

        bucket_summary = [
            MaturityBucketSummary(
                bucket=b,
                loan_count=bucket_totals[b]["count"],
                total_exposure=bucket_totals[b]["exposure"],
            )
            for b in (["overdue"] + list(MATURITY_BUCKETS))
            if bucket_totals[b]["count"] > 0
        ]

        return MaturityPipelineReport(
            meta=_meta(as_of, portfolio_id),
            loans=loans,
            bucket_summary=bucket_summary,
            total_maturing_12m=maturing_12m,
            total_maturing_3m=maturing_3m,
            count_overdue=overdue_count,
        )

    # -------------------------------------------------------------------------
    # 5. Payoff Pipeline Report
    # -------------------------------------------------------------------------

    async def payoff_pipeline(
        self,
        as_of_date: Optional[date] = None,
        portfolio_id: Optional[UUID] = None,
    ) -> PayoffPipelineReport:
        as_of = as_of_date or date.today()

        # Allocation-aware: one row per (active payoff quote × active allocation).
        # Quote dollar columns (principal/interest/fees/penalty/total/per_diem) are
        # pro-rated by ownership_pct/100 — that's each fund's share of the payoff cash.
        pf_alloc = f"AND a.portfolio_id = '{portfolio_id}'" if portfolio_id else ""
        sql = text(f"""
            SELECT
                l.id            AS loan_id,
                l.loan_number,
                l.loan_name,
                l.status        AS loan_status,
                c.legal_name    AS borrower_name,
                p.name          AS portfolio_name,
                pq.id           AS quote_id,
                pq.quote_date,
                pq.good_through_date,
                (pq.good_through_date - :as_of)     AS expires_in_days,
                (pq.principal_balance   * a.ownership_pct / 100) AS principal_balance,
                (pq.accrued_interest    * a.ownership_pct / 100) AS accrued_interest,
                (pq.fees_outstanding    * a.ownership_pct / 100) AS fees_outstanding,
                (pq.prepayment_penalty  * a.ownership_pct / 100) AS prepayment_penalty,
                (pq.total_payoff        * a.ownership_pct / 100) AS total_payoff,
                (pq.per_diem            * a.ownership_pct / 100) AS per_diem,
                pq.status       AS quote_status
            FROM payoff_quote pq
            JOIN loan l         ON l.id = pq.loan_id
            JOIN loan_allocation a
              ON a.loan_id = l.id
             AND a.effective_date <= :as_of
             AND (a.end_date IS NULL OR a.end_date > :as_of)
            JOIN portfolio p    ON p.id = a.portfolio_id
            JOIN counterparty c ON c.id = l.primary_borrower_id
            WHERE pq.status = 'active'
              {pf_alloc}
            ORDER BY pq.good_through_date ASC
        """)

        result = await self.db.execute(sql, {
            "as_of": as_of,
        })
        rows = result.mappings().all()

        items = []
        total_payoff = ZERO
        expiring_7 = 0

        for row in rows:
            exp_days = int(row["expires_in_days"] or 0)
            payoff = Decimal(str(row["total_payoff"]))
            total_payoff += payoff
            if 0 <= exp_days <= 7:
                expiring_7 += 1

            items.append(PayoffPipelineItem(
                loan_id=row["loan_id"],
                loan_number=row["loan_number"],
                loan_name=row["loan_name"],
                borrower_name=row["borrower_name"],
                portfolio_name=row["portfolio_name"],
                quote_id=row["quote_id"],
                quote_date=row["quote_date"],
                good_through_date=row["good_through_date"],
                quote_expires_in_days=exp_days,
                principal_balance=Decimal(str(row["principal_balance"])),
                accrued_interest=Decimal(str(row["accrued_interest"])),
                fees_outstanding=Decimal(str(row["fees_outstanding"])),
                prepayment_penalty=Decimal(str(row["prepayment_penalty"])),
                total_payoff=payoff,
                per_diem=Decimal(str(row["per_diem"])),
                quote_status=row["quote_status"],
                loan_status=row["loan_status"],
            ))

        return PayoffPipelineReport(
            meta=_meta(as_of, portfolio_id),
            items=items,
            active_quote_count=len(items),
            total_payoff_if_all_close=total_payoff,
            expiring_in_7_days=expiring_7,
        )

    # -------------------------------------------------------------------------
    # 6. Collector Productivity Report
    # -------------------------------------------------------------------------

    async def collector_productivity(
        self,
        period_start: date,
        period_end: date,
        portfolio_id: Optional[UUID] = None,
    ) -> CollectorProductivityReport:
        as_of = date.today()

        # Operational report — collector activity counts per user. Filter-only:
        # when portfolio_id is given, restrict to loans with an active allocation
        # in that fund. Activity counts are loan-level human work (no pro-ration).
        pf_exists = (
            "AND EXISTS ("
            " SELECT 1 FROM loan_allocation la"
            " WHERE la.loan_id = l.id"
            f" AND la.portfolio_id = '{portfolio_id}'"
            " AND la.end_date IS NULL"
            ")"
        ) if portfolio_id else ""

        sql = text(f"""
            WITH activity_stats AS (
                SELECT
                    ca.performed_by                                         AS user_id,
                    COUNT(*)                                                AS total_activities,
                    COUNT(*) FILTER (WHERE ca.activity_type = 'call')      AS calls_made,
                    COUNT(*) FILTER (WHERE ca.activity_type = 'email')     AS emails_sent,
                    COUNT(*) FILTER (WHERE ca.activity_type = 'letter')    AS letters_sent,
                    COUNT(*) FILTER (WHERE ca.activity_type = 'promise_to_pay') AS promises_obtained,
                    COUNT(*) FILTER (WHERE ca.activity_type = 'promise_to_pay' AND ca.promise_kept = true) AS promises_kept,
                    COUNT(DISTINCT ca.loan_id)                              AS loans_contacted,
                    COALESCE(SUM(ca.promise_amount), 0)                    AS amount_promised
                FROM collections_activity ca
                JOIN loan l ON l.id = ca.loan_id
                WHERE ca.activity_date BETWEEN :start AND :end
                  {pf_exists}
                GROUP BY ca.performed_by
            ),
            resolutions AS (
                SELECT
                    ca.performed_by     AS user_id,
                    COUNT(DISTINCT ca.loan_id) AS loans_resolved
                FROM collections_activity ca
                JOIN loan l ON l.id = ca.loan_id
                WHERE ca.activity_date BETWEEN :start AND :end
                  AND l.status IN ('funded', 'modified')
                  {pf_exists}
                GROUP BY ca.performed_by
            )
            SELECT
                a.user_id,
                a.total_activities,
                a.calls_made,
                a.emails_sent,
                a.letters_sent,
                a.promises_obtained,
                a.promises_kept,
                a.loans_contacted,
                a.amount_promised,
                COALESCE(r.loans_resolved, 0)   AS loans_resolved
            FROM activity_stats a
            LEFT JOIN resolutions r ON r.user_id = a.user_id
            ORDER BY a.total_activities DESC
        """)

        result = await self.db.execute(sql, {
            "start": datetime.combine(period_start, datetime.min.time()),
            "end": datetime.combine(period_end, datetime.max.time()),
        })
        rows = result.mappings().all()

        # We don't have user names in the tenant schema (they're in shared.users)
        # so we load them separately
        user_ids = [row["user_id"] for row in rows]
        user_names = await self._load_user_names(user_ids)

        collectors = []
        team_activities = 0
        team_collected = ZERO
        total_promises = 0
        total_kept = 0

        for row in rows:
            uid = row["user_id"]
            promises = int(row["promises_obtained"] or 0)
            kept = int(row["promises_kept"] or 0)
            kept_rate = _safe_pct(Decimal(str(kept)), Decimal(str(promises))) if promises > 0 else ZERO

            collectors.append(CollectorStats(
                user_id=uid,
                user_name=user_names.get(str(uid), str(uid)[:8] + "..."),
                period_start=period_start,
                period_end=period_end,
                total_activities=int(row["total_activities"] or 0),
                calls_made=int(row["calls_made"] or 0),
                emails_sent=int(row["emails_sent"] or 0),
                letters_sent=int(row["letters_sent"] or 0),
                promises_obtained=promises,
                promises_kept=kept,
                promise_kept_rate=kept_rate,
                loans_contacted=int(row["loans_contacted"] or 0),
                loans_resolved=int(row["loans_resolved"] or 0),
                amount_promised=Decimal(str(row["amount_promised"])),
            ))
            team_activities += int(row["total_activities"] or 0)
            total_promises += promises
            total_kept += kept

        team_kept_rate = _safe_pct(Decimal(str(total_kept)), Decimal(str(total_promises))) if total_promises > 0 else ZERO

        return CollectorProductivityReport(
            meta=_meta(as_of, portfolio_id),
            period_start=period_start,
            period_end=period_end,
            collectors=collectors,
            team_total_activities=team_activities,
            team_total_collected=team_collected,
            team_promise_kept_rate=team_kept_rate,
        )

    # -------------------------------------------------------------------------
    # 7. Exception / Watchlist Report
    # -------------------------------------------------------------------------

    async def exception_report(
        self,
        as_of_date: Optional[date] = None,
        portfolio_id: Optional[UUID] = None,
    ) -> ExceptionReport:
        as_of = as_of_date or date.today()

        # Operational report — one row per workflow_task, NOT per allocation.
        # Filter-only: when portfolio_id is given, restrict to loans with an
        # active allocation in that fund. Dollar columns are not pro-rated;
        # current_principal here is the loan's full balance (operational context).
        # portfolio_name is the lead-fund pointer (l.portfolio_id) for stable display.
        pf_exists = (
            "AND EXISTS ("
            " SELECT 1 FROM loan_allocation a"
            " WHERE a.loan_id = l.id"
            f" AND a.portfolio_id = '{portfolio_id}'"
            " AND a.effective_date <= :as_of"
            " AND (a.end_date IS NULL OR a.end_date > :as_of)"
            ")"
        ) if portfolio_id else ""

        sql = text(f"""
            SELECT
                wt.id               AS task_id,
                wt.task_type        AS exception_type,
                wt.title            AS exception_detail,
                wt.priority,
                wt.due_date,
                wt.created_at,
                (CURRENT_DATE - wt.created_at::date)    AS days_open,
                l.id                AS loan_id,
                l.loan_number,
                l.loan_name,
                l.status            AS loan_status,
                l.current_principal,
                c.legal_name        AS borrower_name,
                p.name              AS portfolio_name,
                NULL::text          AS assigned_to
            FROM workflow_task wt
            JOIN loan l         ON l.id = wt.loan_id
            JOIN counterparty c ON c.id = l.primary_borrower_id
            JOIN portfolio p    ON p.id = l.portfolio_id
            WHERE wt.status IN ('open','in_progress','escalated')
              {pf_exists}
            ORDER BY
                CASE wt.priority
                    WHEN 'critical' THEN 1
                    WHEN 'high'     THEN 2
                    WHEN 'normal'   THEN 3
                    ELSE 4
                END,
                wt.due_date NULLS LAST
        """)

        result = await self.db.execute(sql, {
            "as_of": as_of,
        })
        rows = result.mappings().all()

        exceptions = []
        counts = {"critical": 0, "high": 0, "normal": 0, "low": 0}
        total_exposure = ZERO

        for row in rows:
            priority = row["priority"] or "normal"
            counts[priority] = counts.get(priority, 0) + 1
            principal = Decimal(str(row["current_principal"] or 0))
            total_exposure += principal

            exceptions.append(ExceptionItem(
                loan_id=row["loan_id"],
                loan_number=row["loan_number"],
                loan_name=row["loan_name"],
                borrower_name=row["borrower_name"],
                portfolio_name=row["portfolio_name"],
                current_principal=principal,
                loan_status=row["loan_status"],
                exception_type=row["exception_type"],
                exception_detail=row["exception_detail"],
                days_open=int(row["days_open"] or 0),
                priority=priority,
                assigned_to=row["assigned_to"],
                due_date=row["due_date"],
            ))

        return ExceptionReport(
            meta=_meta(as_of, portfolio_id),
            exceptions=exceptions,
            critical_count=counts.get("critical", 0),
            high_count=counts.get("high", 0),
            normal_count=counts.get("normal", 0),
            total_count=len(exceptions),
            total_exposure_flagged=total_exposure,
        )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _build_cash_periods(
        self,
        start: date,
        end: date,
        frequency: str,
    ) -> list[tuple[str, date, date]]:
        """
        Build a list of (label, period_start, period_end) tuples.
        """
        from app.services.amortization_engine import AmortizationEngine
        import calendar

        periods = []
        current = start

        if frequency == "monthly":
            while current <= end:
                last_day = calendar.monthrange(current.year, current.month)[1]
                pend = min(date(current.year, current.month, last_day), end)
                label = current.strftime("%B %Y")
                periods.append((label, current, pend))
                # Next month
                if current.month == 12:
                    current = date(current.year + 1, 1, 1)
                else:
                    current = date(current.year, current.month + 1, 1)
        elif frequency == "quarterly":
            while current <= end:
                q = (current.month - 1) // 3 + 1
                q_end_month = q * 3
                last_day = calendar.monthrange(current.year, q_end_month)[1]
                pend = min(date(current.year, q_end_month, last_day), end)
                label = f"Q{q} {current.year}"
                periods.append((label, current, pend))
                # Next quarter
                if q_end_month + 1 > 12:
                    current = date(current.year + 1, 1, 1)
                else:
                    current = date(current.year, q_end_month + 1, 1)
        else:
            periods.append((f"{start} – {end}", start, end))

        return periods

    async def _load_user_names(self, user_ids: list) -> dict[str, str]:
        """
        Load user display names from the shared schema.
        Returns a dict of {user_id_str: full_name}.
        """
        if not user_ids:
            return {}
        try:
            result = await self.db.execute(
                text("""
                    SELECT id::text, full_name
                    FROM shared.users
                    WHERE id = ANY(:ids::uuid[])
                """),
                {"ids": [str(uid) for uid in user_ids]},
            )
            return {row["id"]: row["full_name"] for row in result.mappings().all()}
        except Exception:
            # If shared schema query fails (e.g. permissions), return empty
            return {}
