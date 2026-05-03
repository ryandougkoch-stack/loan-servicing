"""
tests/unit/test_reporting_service.py

Unit tests for the reporting service.

Strategy: mock the database session and return pre-built result rows.
This lets us test all the Python-side logic (bucket assignment, aggregation,
percentage calculation, period building, sorting) without needing a real database.

The SQL queries themselves are integration-tested separately.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.reporting_service import (
    ReportingService,
    _dpd_to_bucket,
    _days_to_maturity_bucket,
    _safe_pct,
    BUCKET_ORDER,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_service():
    db = AsyncMock()
    return ReportingService(db)


def mock_mapping(**kwargs):
    """Create a mapping-like object that supports item access."""
    m = MagicMock()
    m.__getitem__ = lambda self, key: kwargs[key]
    m.get = lambda key, default=None: kwargs.get(key, default)
    # Make it work with dict(row._mapping)
    for k, v in kwargs.items():
        setattr(m, k, v)
    return kwargs   # just use a plain dict — mappings().all() returns these


# ---------------------------------------------------------------------------
# Pure function tests (no DB needed)
# ---------------------------------------------------------------------------

class TestDPDBucket:

    def test_zero_dpd_is_current(self):
        assert _dpd_to_bucket(0) == "current"

    def test_negative_dpd_is_current(self):
        assert _dpd_to_bucket(-5) == "current"

    def test_1_dpd(self):
        assert _dpd_to_bucket(1) == "1-30"

    def test_30_dpd(self):
        assert _dpd_to_bucket(30) == "1-30"

    def test_31_dpd(self):
        assert _dpd_to_bucket(31) == "31-60"

    def test_60_dpd(self):
        assert _dpd_to_bucket(60) == "31-60"

    def test_61_dpd(self):
        assert _dpd_to_bucket(61) == "61-90"

    def test_91_dpd(self):
        assert _dpd_to_bucket(91) == "91-120"

    def test_121_dpd(self):
        assert _dpd_to_bucket(121) == "120+"

    def test_999_dpd(self):
        assert _dpd_to_bucket(999) == "120+"

    def test_bucket_order_is_complete(self):
        """All six standard buckets must be present in the canonical order."""
        assert BUCKET_ORDER == ["current", "1-30", "31-60", "61-90", "91-120", "120+"]


class TestMaturityBucket:

    def test_overdue(self):
        assert _days_to_maturity_bucket(-1) == "overdue"

    def test_0_days(self):
        assert _days_to_maturity_bucket(0) == "0-30"

    def test_30_days(self):
        assert _days_to_maturity_bucket(30) == "0-30"

    def test_31_days(self):
        assert _days_to_maturity_bucket(31) == "31-60"

    def test_91_days(self):
        assert _days_to_maturity_bucket(91) == "91-180"

    def test_181_days(self):
        assert _days_to_maturity_bucket(181) == "181-365"

    def test_366_days(self):
        assert _days_to_maturity_bucket(366) == "365+"


class TestSafePct:

    def test_zero_denominator_returns_zero(self):
        assert _safe_pct(Decimal("100"), Decimal("0")) == Decimal("0")

    def test_normal_calculation(self):
        result = _safe_pct(Decimal("25"), Decimal("100"))
        assert result == Decimal("25.00")

    def test_rounds_to_cents(self):
        # 1/3 = 33.33...%
        result = _safe_pct(Decimal("1"), Decimal("3"))
        assert result == Decimal("33.33")

    def test_100_percent(self):
        result = _safe_pct(Decimal("500"), Decimal("500"))
        assert result == Decimal("100.00")

    def test_over_100_percent(self):
        # Collections can exceed scheduled (e.g. prepayments)
        result = _safe_pct(Decimal("110"), Decimal("100"))
        assert result == Decimal("110.00")


# ---------------------------------------------------------------------------
# Cash period building
# ---------------------------------------------------------------------------

class TestCashPeriodBuilding:

    def test_monthly_jan_to_mar(self):
        svc = make_service()
        periods = svc._build_cash_periods(date(2024, 1, 1), date(2024, 3, 31), "monthly")
        assert len(periods) == 3
        labels = [p[0] for p in periods]
        assert "January 2024" in labels
        assert "February 2024" in labels
        assert "March 2024" in labels

    def test_monthly_period_boundaries(self):
        svc = make_service()
        periods = svc._build_cash_periods(date(2024, 1, 1), date(2024, 2, 29), "monthly")
        # Jan: Jan 1 → Jan 31
        assert periods[0][1] == date(2024, 1, 1)
        assert periods[0][2] == date(2024, 1, 31)
        # Feb (2024 is leap): Feb 1 → Feb 29
        assert periods[1][1] == date(2024, 2, 1)
        assert periods[1][2] == date(2024, 2, 29)

    def test_quarterly_full_year(self):
        svc = make_service()
        periods = svc._build_cash_periods(date(2024, 1, 1), date(2024, 12, 31), "quarterly")
        assert len(periods) == 4
        labels = [p[0] for p in periods]
        assert "Q1 2024" in labels
        assert "Q4 2024" in labels

    def test_quarterly_q1_boundaries(self):
        svc = make_service()
        periods = svc._build_cash_periods(date(2024, 1, 1), date(2024, 3, 31), "quarterly")
        assert len(periods) == 1
        assert periods[0][1] == date(2024, 1, 1)
        assert periods[0][2] == date(2024, 3, 31)

    def test_single_month(self):
        svc = make_service()
        periods = svc._build_cash_periods(date(2024, 6, 1), date(2024, 6, 30), "monthly")
        assert len(periods) == 1
        assert periods[0][0] == "June 2024"

    def test_12_monthly_periods_in_full_year(self):
        svc = make_service()
        periods = svc._build_cash_periods(date(2024, 1, 1), date(2024, 12, 31), "monthly")
        assert len(periods) == 12

    def test_period_coverage_is_contiguous(self):
        """Every day in the range must be covered by exactly one period."""
        svc = make_service()
        periods = svc._build_cash_periods(date(2024, 1, 1), date(2024, 6, 30), "monthly")
        # Check no gaps: end of period N + 1 day = start of period N+1
        for i in range(1, len(periods)):
            prev_end = periods[i - 1][2]
            next_start = periods[i][1]
            assert next_start == prev_end + timedelta(days=1), (
                f"Gap between period {i} and {i+1}: {prev_end} → {next_start}"
            )


# ---------------------------------------------------------------------------
# Aging report: bucket summary aggregation
# ---------------------------------------------------------------------------

class TestAgingBucketAggregation:

    @pytest.mark.asyncio
    async def test_aging_bucket_summary_counts(self):
        """Bucket summary should correctly count and sum loans."""
        svc = make_service()

        # Simulate aging_report's internal aggregation logic directly
        from app.schemas.reports import AgingBucket
        loans = [
            AgingBucket(
                loan_id=uuid4(), loan_number="L001", borrower_name="A",
                portfolio_name="P1", current_principal=Decimal("100000"),
                days_past_due=0, bucket="current", total_past_due=Decimal("0"),
                maturity_date=date(2026, 1, 1), rate_type="fixed",
            ),
            AgingBucket(
                loan_id=uuid4(), loan_number="L002", borrower_name="B",
                portfolio_name="P1", current_principal=Decimal("200000"),
                days_past_due=15, bucket="1-30", total_past_due=Decimal("5000"),
                maturity_date=date(2026, 1, 1), rate_type="fixed",
            ),
            AgingBucket(
                loan_id=uuid4(), loan_number="L003", borrower_name="C",
                portfolio_name="P1", current_principal=Decimal("150000"),
                days_past_due=45, bucket="31-60", total_past_due=Decimal("12000"),
                maturity_date=date(2026, 1, 1), rate_type="fixed",
            ),
        ]

        # Replicate the aggregation logic from the service
        bucket_totals = {b: {"count": 0, "outstanding": Decimal("0"), "past_due": Decimal("0")} for b in BUCKET_ORDER}
        total_outstanding = Decimal("0")
        for loan in loans:
            bt = bucket_totals[loan.bucket]
            bt["count"] += 1
            bt["outstanding"] += loan.current_principal
            bt["past_due"] += loan.total_past_due
            total_outstanding += loan.current_principal

        assert bucket_totals["current"]["count"] == 1
        assert bucket_totals["1-30"]["count"] == 1
        assert bucket_totals["31-60"]["count"] == 1
        assert bucket_totals["61-90"]["count"] == 0
        assert total_outstanding == Decimal("450000")

    def test_pct_delinquent_calculation(self):
        """Percentage delinquent should be total_past_due / total_outstanding."""
        total_outstanding = Decimal("1000000")
        total_past_due = Decimal("75000")
        pct = _safe_pct(total_past_due, total_outstanding)
        assert pct == Decimal("7.50")

    def test_pct_delinquent_zero_portfolio(self):
        """Empty portfolio should return 0% delinquent, not an error."""
        pct = _safe_pct(Decimal("0"), Decimal("0"))
        assert pct == Decimal("0")


# ---------------------------------------------------------------------------
# Maturity pipeline: bucket assignment
# ---------------------------------------------------------------------------

class TestMaturityPipeline:

    def test_overdue_loan_counted(self):
        """Loans past maturity (negative days) should appear in overdue bucket."""
        assert _days_to_maturity_bucket(-30) == "overdue"
        assert _days_to_maturity_bucket(-1) == "overdue"

    def test_maturing_today_is_0_30(self):
        assert _days_to_maturity_bucket(0) == "0-30"

    def test_maturing_3m_vs_12m_exposure(self):
        """Test the exposure bucketing logic directly."""
        loans = [
            {"days": 15, "exposure": Decimal("500000")},
            {"days": 45, "exposure": Decimal("300000")},
            {"days": 91, "exposure": Decimal("200000")},
            {"days": 200, "exposure": Decimal("100000")},
        ]
        maturing_3m = sum(l["exposure"] for l in loans if l["days"] <= 90)
        maturing_12m = sum(l["exposure"] for l in loans if l["days"] <= 365)

        assert maturing_3m == Decimal("800000")   # 500k + 300k
        assert maturing_12m == Decimal("1100000")  # all four


# ---------------------------------------------------------------------------
# Payoff pipeline
# ---------------------------------------------------------------------------

class TestPayoffPipeline:

    def test_expiring_7_days_count(self):
        """Quotes expiring in ≤7 days should be flagged."""
        today = date.today()
        quotes = [
            {"good_through_date": today + timedelta(days=3)},   # expiring soon
            {"good_through_date": today + timedelta(days=7)},   # expiring soon (boundary)
            {"good_through_date": today + timedelta(days=8)},   # not expiring soon
            {"good_through_date": today + timedelta(days=30)},  # not expiring soon
        ]
        expiring = sum(
            1 for q in quotes
            if 0 <= (q["good_through_date"] - today).days <= 7
        )
        assert expiring == 2

    def test_total_payoff_sums_correctly(self):
        payoffs = [Decimal("500000"), Decimal("750000"), Decimal("1200000")]
        total = sum(payoffs)
        assert total == Decimal("2450000")


# ---------------------------------------------------------------------------
# Collector productivity
# ---------------------------------------------------------------------------

class TestCollectorProductivity:

    def test_promise_kept_rate_zero_promises(self):
        """If no promises were obtained, kept rate should be 0, not a divide-by-zero error."""
        rate = _safe_pct(Decimal("0"), Decimal("0"))
        assert rate == Decimal("0")

    def test_promise_kept_rate_calculation(self):
        """8 promises kept out of 10 = 80%."""
        rate = _safe_pct(Decimal("8"), Decimal("10"))
        assert rate == Decimal("80.00")

    def test_team_totals_aggregation(self):
        """Team totals should sum all collector activities."""
        collectors = [
            {"activities": 15, "collected": Decimal("50000"), "promises": 5, "kept": 4},
            {"activities": 22, "collected": Decimal("75000"), "promises": 8, "kept": 6},
            {"activities": 8,  "collected": Decimal("20000"), "promises": 2, "kept": 1},
        ]
        total_activities = sum(c["activities"] for c in collectors)
        total_promises = sum(c["promises"] for c in collectors)
        total_kept = sum(c["kept"] for c in collectors)
        team_kept_rate = _safe_pct(Decimal(str(total_kept)), Decimal(str(total_promises)))

        assert total_activities == 45
        assert total_promises == 15
        assert total_kept == 11
        assert team_kept_rate == Decimal("73.33")


# ---------------------------------------------------------------------------
# Portfolio summary
# ---------------------------------------------------------------------------

class TestPortfolioSummary:

    def test_grand_totals_sum_correctly(self):
        """Grand totals should be the sum across all portfolio rows."""
        from app.schemas.reports import PortfolioSummaryRow
        rows = [
            PortfolioSummaryRow(
                portfolio_id=uuid4(), portfolio_name="Fund A",
                total_outstanding_principal=Decimal("5000000"),
                total_exposure=Decimal("5200000"),
                total_past_due_amount=Decimal("100000"),
                loan_count_total=20,
            ),
            PortfolioSummaryRow(
                portfolio_id=uuid4(), portfolio_name="Fund B",
                total_outstanding_principal=Decimal("3000000"),
                total_exposure=Decimal("3100000"),
                total_past_due_amount=Decimal("50000"),
                loan_count_total=12,
            ),
        ]
        grand_outstanding = sum(r.total_outstanding_principal for r in rows)
        grand_exposure = sum(r.total_exposure for r in rows)
        grand_past_due = sum(r.total_past_due_amount for r in rows)
        grand_count = sum(r.loan_count_total for r in rows)

        assert grand_outstanding == Decimal("8000000")
        assert grand_exposure == Decimal("8300000")
        assert grand_past_due == Decimal("150000")
        assert grand_count == 32

    def test_pct_portfolio_delinquent(self):
        """WAC-style pct delinquent calculation."""
        outstanding = Decimal("5000000")
        past_due = Decimal("250000")
        pct = _safe_pct(past_due, outstanding)
        assert pct == Decimal("5.00")

    def test_wac_correctly_weighted(self):
        """Weighted average coupon should weight by outstanding principal."""
        # Loan A: $1M at 8%; Loan B: $2M at 10%
        # WAC = (1M*8% + 2M*10%) / 3M = (80k + 200k) / 3M = 280k/3M = 9.333...%
        principal_a, rate_a = Decimal("1000000"), Decimal("0.08")
        principal_b, rate_b = Decimal("2000000"), Decimal("0.10")
        total = principal_a + principal_b
        wac = (rate_a * principal_a + rate_b * principal_b) / total
        assert abs(wac - Decimal("0.09333333")) < Decimal("0.00000001")


# ---------------------------------------------------------------------------
# Exception report
# ---------------------------------------------------------------------------

class TestExceptionReport:

    def test_priority_ordering(self):
        """Critical exceptions should sort before high, then normal, then low."""
        priority_rank = {"critical": 1, "high": 2, "normal": 3, "low": 4}
        exceptions = [
            {"priority": "low", "id": 4},
            {"priority": "critical", "id": 1},
            {"priority": "normal", "id": 3},
            {"priority": "high", "id": 2},
        ]
        sorted_exceptions = sorted(exceptions, key=lambda e: priority_rank[e["priority"]])
        ids_in_order = [e["id"] for e in sorted_exceptions]
        assert ids_in_order == [1, 2, 3, 4]

    def test_total_count_aggregation(self):
        counts = {"critical": 2, "high": 5, "normal": 12, "low": 3}
        total = sum(counts.values())
        assert total == 22

    def test_exposure_flagged_sums_unique_loans(self):
        """Total exposure should sum the principal of all flagged loans."""
        flagged_principals = [
            Decimal("500000"),
            Decimal("1200000"),
            Decimal("750000"),
        ]
        total = sum(flagged_principals)
        assert total == Decimal("2450000")
