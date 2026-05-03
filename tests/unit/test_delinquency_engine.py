"""
tests/unit/test_delinquency_engine.py

Unit tests for the delinquency engine.

All tests are pure Python — no database, no async, no mocks.
The engine is a pure calculation function; we test it like one.

Test categories:
  1. DPD calculation (grace periods, exact day counts)
  2. Bucket assignment
  3. Past-due amount calculation
  4. Partial payment handling
  5. Multiple overdue periods
  6. Milestone detection
  7. Status transition recommendations
  8. Workflow task generation
  9. Edge cases and invariants
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.delinquency_engine import (
    DelinquencyEngine,
    ScheduledPeriod,
    PaymentRecord,
    tasks_for_milestones,
    _milestone_task,
    MILESTONE_10,
    MILESTONE_30,
    MILESTONE_60,
    MILESTONE_90,
    MILESTONE_120,
)

# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------

TODAY = date(2024, 6, 15)
LOAN_ID = "loan-001"

def make_engine(
    loan_status="funded",
    grace_period_days=5,
    current_principal="1000000",
    accrued_interest="25000",
    accrued_fees="500",
):
    return DelinquencyEngine(
        loan_id=LOAN_ID,
        loan_status=loan_status,
        grace_period_days=grace_period_days,
        current_principal=Decimal(current_principal),
        accrued_interest=Decimal(accrued_interest),
        accrued_fees=Decimal(accrued_fees),
    )


def make_period(
    period_number=1,
    due_date=None,
    principal="25000",
    interest="22500",
    fees="0",
    status="open",
):
    return ScheduledPeriod(
        period_number=period_number,
        due_date=due_date or TODAY - timedelta(days=30),
        scheduled_principal=Decimal(principal),
        scheduled_interest=Decimal(interest),
        scheduled_fees=Decimal(fees),
        status=status,
    )


def make_payment(
    effective_date=None,
    principal="0",
    interest="0",
    fees="0",
    status="posted",
):
    return PaymentRecord(
        effective_date=effective_date or TODAY,
        applied_to_principal=Decimal(principal),
        applied_to_interest=Decimal(interest),
        applied_to_fees=Decimal(fees),
        status=status,
    )


# ---------------------------------------------------------------------------
# 1. DPD calculation
# ---------------------------------------------------------------------------

class TestDPDCalculation:

    def test_no_overdue_periods_dpd_zero(self):
        engine = make_engine()
        # Future period — not overdue
        schedule = [make_period(due_date=TODAY + timedelta(days=10))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 0

    def test_due_today_is_not_overdue(self):
        """Due today means strictly TODAY — not yet past due."""
        engine = make_engine()
        schedule = [make_period(due_date=TODAY)]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 0

    def test_due_yesterday_within_grace(self):
        """1 DPD with grace_period_days=5 → effective DPD = 0."""
        engine = make_engine(grace_period_days=5)
        schedule = [make_period(due_date=TODAY - timedelta(days=1))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 0

    def test_grace_period_exact_boundary(self):
        """DPD = grace_period → effective DPD = 0 (last day of grace)."""
        grace = 5
        engine = make_engine(grace_period_days=grace)
        schedule = [make_period(due_date=TODAY - timedelta(days=grace))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 0

    def test_one_day_past_grace(self):
        """DPD = grace_period + 1 → effective DPD = 1."""
        grace = 5
        engine = make_engine(grace_period_days=grace)
        schedule = [make_period(due_date=TODAY - timedelta(days=grace + 1))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 1

    def test_dpd_exact_calculation(self):
        """30 raw DPD - 5 grace = 25 effective DPD."""
        engine = make_engine(grace_period_days=5)
        schedule = [make_period(due_date=TODAY - timedelta(days=30))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 25

    def test_zero_grace_period(self):
        """With grace_period_days=0, DPD = raw days past due."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=15))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 15

    def test_dpd_uses_oldest_period(self):
        """With multiple overdue periods, DPD is calculated from the oldest."""
        engine = make_engine(grace_period_days=0)
        schedule = [
            make_period(period_number=1, due_date=TODAY - timedelta(days=60)),
            make_period(period_number=2, due_date=TODAY - timedelta(days=30)),
        ]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 60   # oldest is 60 days ago

    def test_paid_period_not_counted(self):
        """A 'paid' period does not contribute to DPD."""
        engine = make_engine(grace_period_days=0)
        schedule = [
            make_period(due_date=TODAY - timedelta(days=30), status="paid"),
        ]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 0

    def test_waived_period_not_counted(self):
        """A 'waived' period is excluded from delinquency."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=30), status="waived")]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 0

    def test_deferred_period_not_counted(self):
        """A 'deferred' period is excluded from delinquency."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=30), status="deferred")]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 0

    def test_partial_period_is_overdue(self):
        """A 'partial' period counts as overdue."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=20), status="partial")]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 20


# ---------------------------------------------------------------------------
# 2. Bucket assignment
# ---------------------------------------------------------------------------

class TestBucketAssignment:

    def _result_for_dpd(self, dpd: int):
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=dpd))] if dpd > 0 else []
        return engine.calculate(TODAY, schedule, [])

    def test_current(self):
        assert self._result_for_dpd(0).bucket == "current"

    def test_1_dpd(self):
        assert self._result_for_dpd(1).bucket == "1-30"

    def test_30_dpd(self):
        assert self._result_for_dpd(30).bucket == "1-30"

    def test_31_dpd(self):
        assert self._result_for_dpd(31).bucket == "31-60"

    def test_61_dpd(self):
        assert self._result_for_dpd(61).bucket == "61-90"

    def test_91_dpd(self):
        assert self._result_for_dpd(91).bucket == "91-120"

    def test_121_dpd(self):
        assert self._result_for_dpd(121).bucket == "120+"


# ---------------------------------------------------------------------------
# 3. Past-due amounts
# ---------------------------------------------------------------------------

class TestPastDueAmounts:

    def test_open_period_fully_past_due(self):
        """An open period's full amounts are past due."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(
            due_date=TODAY - timedelta(days=30),
            principal="25000",
            interest="22500",
            fees="500",
            status="open",
        )]
        result = engine.calculate(TODAY, schedule, [])
        assert result.principal_past_due == Decimal("25000")
        assert result.interest_past_due == Decimal("22500")
        assert result.fees_past_due == Decimal("500")
        assert result.total_past_due == Decimal("48000")

    def test_multiple_overdue_periods_summed(self):
        """Multiple overdue periods should sum correctly."""
        engine = make_engine(grace_period_days=0)
        schedule = [
            make_period(period_number=1, due_date=TODAY - timedelta(days=60),
                        principal="25000", interest="22500", fees="0"),
            make_period(period_number=2, due_date=TODAY - timedelta(days=30),
                        principal="25000", interest="22000", fees="0"),
        ]
        result = engine.calculate(TODAY, schedule, [])
        assert result.principal_past_due == Decimal("50000")
        assert result.interest_past_due == Decimal("44500")

    def test_no_overdue_amounts_when_current(self):
        """Current loans have zero past-due amounts."""
        engine = make_engine()
        result = engine.calculate(TODAY, [], [])
        assert result.principal_past_due == Decimal("0")
        assert result.interest_past_due == Decimal("0")
        assert result.fees_past_due == Decimal("0")
        assert result.total_past_due == Decimal("0")

    def test_total_is_sum_of_components(self):
        """total_past_due must always equal sum of components."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(
            due_date=TODAY - timedelta(days=45),
            principal="30000",
            interest="18000",
            fees="750",
        )]
        result = engine.calculate(TODAY, schedule, [])
        assert result.total_past_due == (
            result.principal_past_due +
            result.interest_past_due +
            result.fees_past_due
        )


# ---------------------------------------------------------------------------
# 4. Milestone detection
# ---------------------------------------------------------------------------

class TestMilestoneDetection:

    def test_no_milestones_when_current(self):
        engine = make_engine()
        result = engine.calculate(TODAY, [], [], prior_dpd=0)
        assert result.milestones_triggered == []

    def test_crossing_10_dpd_triggers_milestone(self):
        """Moving from 8 DPD to 12 DPD should trigger the 10 DPD milestone."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=12))]
        result = engine.calculate(TODAY, schedule, [], prior_dpd=8)
        assert MILESTONE_10 in result.milestones_triggered

    def test_already_past_milestone_not_retriggered(self):
        """If prior_dpd=15, the 10 DPD milestone should not re-trigger."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=25))]
        result = engine.calculate(TODAY, schedule, [], prior_dpd=15)
        assert MILESTONE_10 not in result.milestones_triggered

    def test_crossing_30_dpd(self):
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=32))]
        result = engine.calculate(TODAY, schedule, [], prior_dpd=25)
        assert MILESTONE_30 in result.milestones_triggered

    def test_crossing_90_dpd_triggers_default_milestone(self):
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=95))]
        result = engine.calculate(TODAY, schedule, [], prior_dpd=85)
        assert MILESTONE_90 in result.milestones_triggered

    def test_multiple_milestones_in_one_run(self):
        """A loan going from 0 to 95 DPD crosses 10, 30, 60, 90 milestones."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=95))]
        result = engine.calculate(TODAY, schedule, [], prior_dpd=0)
        assert MILESTONE_10 in result.milestones_triggered
        assert MILESTONE_30 in result.milestones_triggered
        assert MILESTONE_60 in result.milestones_triggered
        assert MILESTONE_90 in result.milestones_triggered
        assert MILESTONE_120 not in result.milestones_triggered   # not yet

    def test_120_dpd_milestone(self):
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=125))]
        result = engine.calculate(TODAY, schedule, [], prior_dpd=115)
        assert MILESTONE_120 in result.milestones_triggered

    def test_exactly_at_milestone_triggers_it(self):
        """Exactly 30 DPD should trigger the 30 DPD milestone (inclusive)."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=30))]
        result = engine.calculate(TODAY, schedule, [], prior_dpd=29)
        assert MILESTONE_30 in result.milestones_triggered


# ---------------------------------------------------------------------------
# 5. Status transition recommendations
# ---------------------------------------------------------------------------

class TestStatusRecommendations:

    def test_funded_loan_goes_delinquent_at_1_dpd(self):
        """A funded loan with any DPD should be recommended for delinquent status."""
        engine = make_engine(loan_status="funded", grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=5))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.recommended_status == "delinquent"

    def test_funded_current_no_transition(self):
        """A funded, current loan needs no status change."""
        engine = make_engine(loan_status="funded")
        result = engine.calculate(TODAY, [], [])
        assert result.recommended_status is None

    def test_delinquent_loan_cures_to_funded(self):
        """A delinquent loan with DPD=0 should recommend returning to funded."""
        engine = make_engine(loan_status="delinquent")
        result = engine.calculate(TODAY, [], [])
        assert result.recommended_status == "funded"

    def test_at_90_dpd_recommend_default(self):
        """90+ DPD on a delinquent loan should recommend default."""
        engine = make_engine(loan_status="delinquent", grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=95))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.recommended_status == "default"

    def test_already_default_no_recommendation(self):
        """A loan already in default doesn't need another recommendation."""
        engine = make_engine(loan_status="default", grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=100))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.recommended_status is None

    def test_workout_loan_no_recommendation(self):
        """A loan in workout is being managed — no auto-recommendation."""
        engine = make_engine(loan_status="workout", grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=100))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.recommended_status is None


# ---------------------------------------------------------------------------
# 6. Workflow task generation
# ---------------------------------------------------------------------------

class TestWorkflowTaskGeneration:

    def test_10_dpd_task_priority_normal(self):
        task = _milestone_task(MILESTONE_10, "L001", "Acme Corp", 12, Decimal("47500"))
        assert task is not None
        assert task.priority == "normal"
        assert "10 DPD" in task.title
        assert "L001" in task.title

    def test_30_dpd_task_priority_high(self):
        task = _milestone_task(MILESTONE_30, "L001", "Acme Corp", 32, Decimal("95000"))
        assert task.priority == "high"
        assert "30 DPD" in task.title

    def test_90_dpd_task_priority_critical(self):
        task = _milestone_task(MILESTONE_90, "L001", "Acme Corp", 92, Decimal("142500"))
        assert task.priority == "critical"
        assert "DEFAULT" in task.title.upper()

    def test_120_dpd_task_priority_critical(self):
        task = _milestone_task(MILESTONE_120, "L001", "Acme Corp", 122, Decimal("190000"))
        assert task.priority == "critical"
        assert "charge-off" in task.description.lower()

    def test_task_includes_borrower_name(self):
        task = _milestone_task(MILESTONE_30, "L001", "Globex Inc", 35, Decimal("50000"))
        assert "Globex Inc" in task.description

    def test_task_includes_past_due_amount(self):
        task = _milestone_task(MILESTONE_30, "L001", "Acme", 35, Decimal("47500.00"))
        assert "47,500.00" in task.description

    def test_tasks_for_milestones_returns_one_per_milestone(self):
        milestones = [MILESTONE_10, MILESTONE_30]
        tasks = tasks_for_milestones(
            loan_id="loan-1",
            loan_number="L001",
            borrower_name="Acme",
            portfolio_id="port-1",
            milestones=milestones,
            dpd=35,
            total_past_due=Decimal("50000"),
        )
        assert len(tasks) == 2

    def test_no_milestones_no_tasks(self):
        tasks = tasks_for_milestones(
            loan_id="loan-1", loan_number="L001",
            borrower_name="Acme", portfolio_id="port-1",
            milestones=[], dpd=0, total_past_due=Decimal("0"),
        )
        assert tasks == []


# ---------------------------------------------------------------------------
# 7. Invariants
# ---------------------------------------------------------------------------

class TestInvariants:

    def test_total_past_due_never_negative(self):
        """Past-due amounts should never be negative."""
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=45))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.total_past_due >= Decimal("0")
        assert result.principal_past_due >= Decimal("0")
        assert result.interest_past_due >= Decimal("0")
        assert result.fees_past_due >= Decimal("0")

    def test_dpd_never_negative(self):
        """Effective DPD should always be >= 0."""
        engine = make_engine(grace_period_days=5)
        # Loan due yesterday — within grace period
        schedule = [make_period(due_date=TODAY - timedelta(days=1))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due >= 0

    def test_current_loan_has_no_past_due(self):
        """If DPD is 0, past-due amounts must be 0."""
        engine = make_engine()
        result = engine.calculate(TODAY, [], [])
        assert result.days_past_due == 0
        assert result.total_past_due == Decimal("0")

    def test_bucket_consistent_with_dpd(self):
        """Bucket should always be consistent with DPD value."""
        engine = make_engine(grace_period_days=0)
        test_cases = [
            (0, "current"), (15, "1-30"), (45, "31-60"),
            (75, "61-90"), (100, "91-120"), (150, "120+"),
        ]
        for dpd, expected_bucket in test_cases:
            schedule = [make_period(due_date=TODAY - timedelta(days=dpd))] if dpd > 0 else []
            result = engine.calculate(TODAY, schedule, [])
            assert result.bucket == expected_bucket, (
                f"DPD={result.days_past_due} → expected bucket {expected_bucket}, "
                f"got {result.bucket}"
            )

    def test_milestones_are_subset_of_known_milestones(self):
        """Milestones returned should only be known milestone values."""
        known = {MILESTONE_10, MILESTONE_30, MILESTONE_60, MILESTONE_90, MILESTONE_120}
        engine = make_engine(grace_period_days=0)
        schedule = [make_period(due_date=TODAY - timedelta(days=130))]
        result = engine.calculate(TODAY, schedule, [], prior_dpd=0)
        for m in result.milestones_triggered:
            assert m in known, f"Unknown milestone: {m}"

    @pytest.mark.parametrize("grace", [0, 3, 5, 10, 15])
    def test_grace_period_respected(self, grace):
        """Loan due exactly grace_period_days ago should have DPD=0."""
        engine = make_engine(grace_period_days=grace)
        schedule = [make_period(due_date=TODAY - timedelta(days=grace))]
        result = engine.calculate(TODAY, schedule, [])
        assert result.days_past_due == 0, (
            f"grace={grace}: expected DPD=0, got {result.days_past_due}"
        )
