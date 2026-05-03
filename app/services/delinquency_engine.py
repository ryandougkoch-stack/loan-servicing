"""
app/services/delinquency_engine.py

Delinquency calculation engine.

Responsibilities:
  1. Given a loan and its payment schedule, determine as of a date:
     - How many days past due is the loan?
     - Which bucket does it fall in?
     - How much principal / interest / fees are past due?

  2. Determine whether a loan status transition is warranted
     (e.g. funded → delinquent → default) based on DPD thresholds.

  3. Generate workflow tasks for delinquency milestones
     (first notice at 10 DPD, second notice at 30 DPD, default at 90 DPD, etc.)

Design: pure functions only in this module. No database access.
The worker (tasks/delinquency.py) owns the DB reads/writes.
This module is fully unit-testable without a database.

Thresholds (configurable via settings in production; hardcoded here for MVP):
  - Grace period:       per loan (loan.grace_period_days, typically 5)
  - First notice:       10 DPD
  - Second notice:      30 DPD
  - Third notice:       60 DPD
  - Default trigger:    90 DPD (unless loan has a different threshold)
  - Charge-off review: 120 DPD
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

ZERO = Decimal("0")
CENTS = Decimal("0.01")

# ---------------------------------------------------------------------------
# DPD thresholds for milestone events
# ---------------------------------------------------------------------------
MILESTONE_10  = 10
MILESTONE_30  = 30
MILESTONE_60  = 60
MILESTONE_90  = 90    # default trigger
MILESTONE_120 = 120   # charge-off review

# Status transition thresholds (DPD → status)
DPD_TO_DELINQUENT  = 1    # any DPD beyond grace period
DPD_TO_DEFAULT     = 90


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScheduledPeriod:
    """Lightweight representation of a payment_schedule row."""
    period_number: int
    due_date: date
    scheduled_principal: Decimal
    scheduled_interest: Decimal
    scheduled_fees: Decimal
    status: str   # open | paid | partial | overdue | waived | deferred

    @property
    def total_scheduled(self) -> Decimal:
        return self.scheduled_principal + self.scheduled_interest + self.scheduled_fees


@dataclass
class PaymentRecord:
    """Lightweight representation of a posted payment."""
    effective_date: date
    applied_to_principal: Decimal
    applied_to_interest: Decimal
    applied_to_fees: Decimal
    status: str   # posted | returned | reversed


@dataclass
class DelinquencyResult:
    """Output of the delinquency engine for a single loan on a single date."""
    loan_id: object          # UUID
    as_of_date: date
    days_past_due: int
    bucket: str
    principal_past_due: Decimal
    interest_past_due: Decimal
    fees_past_due: Decimal
    total_past_due: Decimal
    # The specific overdue period driving the DPD calculation
    oldest_overdue_due_date: Optional[date]
    # Milestone flags — used by workflow task generator
    milestones_triggered: list[int] = field(default_factory=list)
    # Recommended status transition (None if no change warranted)
    recommended_status: Optional[str] = None


@dataclass
class MilestoneTask:
    """A workflow task to be created for a delinquency milestone."""
    task_type: str
    title: str
    description: str
    priority: str
    days_past_due: int


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

class DelinquencyEngine:
    """
    Stateless engine: instantiate with a loan's attributes and call calculate().
    """

    def __init__(
        self,
        loan_id: object,
        loan_status: str,
        grace_period_days: int,
        current_principal: Decimal,
        accrued_interest: Decimal,
        accrued_fees: Decimal,
    ):
        self.loan_id = loan_id
        self.loan_status = loan_status
        self.grace_period_days = grace_period_days
        self.current_principal = current_principal
        self.accrued_interest = accrued_interest
        self.accrued_fees = accrued_fees

    def calculate(
        self,
        as_of_date: date,
        schedule: list[ScheduledPeriod],
        payments: list[PaymentRecord],
        prior_dpd: int = 0,
    ) -> DelinquencyResult:
        """
        Calculate delinquency status as of as_of_date.

        Algorithm:
          1. Find the oldest unpaid (open/partial) period whose due date
             has passed as_of_date.
          2. DPD = (as_of_date - oldest_overdue_due_date) - grace_period_days
             (floored at 0)
          3. Past-due amounts = sum of unpaid amounts on all overdue periods.
          4. Determine bucket, milestone events, recommended status transition.

        Args:
            as_of_date:  The date we're calculating delinquency for.
            schedule:    All current (is_current=True) payment schedule rows.
            payments:    All posted payments for this loan.
            prior_dpd:   DPD from the previous calculation (for milestone detection).
        """
        # Step 1: find overdue periods
        overdue_periods = self._overdue_periods(schedule, as_of_date)

        if not overdue_periods:
            # Loan is current
            return DelinquencyResult(
                loan_id=self.loan_id,
                as_of_date=as_of_date,
                days_past_due=0,
                bucket="current",
                principal_past_due=ZERO,
                interest_past_due=ZERO,
                fees_past_due=ZERO,
                total_past_due=ZERO,
                oldest_overdue_due_date=None,
                milestones_triggered=[],
                recommended_status=self._recommended_status_current(),
            )

        # Step 2: DPD from oldest overdue period
        oldest_due = min(p.due_date for p in overdue_periods)
        raw_dpd = (as_of_date - oldest_due).days
        dpd = max(0, raw_dpd - self.grace_period_days)

        # Step 3: past-due amounts
        principal_pd = ZERO
        interest_pd = ZERO
        fees_pd = ZERO

        for period in overdue_periods:
            unpaid_ratio = self._unpaid_ratio(period, payments)
            principal_pd += (period.scheduled_principal * unpaid_ratio).quantize(CENTS)
            interest_pd  += (period.scheduled_interest  * unpaid_ratio).quantize(CENTS)
            fees_pd      += (period.scheduled_fees       * unpaid_ratio).quantize(CENTS)

        total_pd = principal_pd + interest_pd + fees_pd

        # Step 4: bucket, milestones, recommended status
        bucket = self._dpd_to_bucket(dpd)
        milestones = self._new_milestones(prior_dpd, dpd)
        rec_status = self._recommended_status(dpd)

        return DelinquencyResult(
            loan_id=self.loan_id,
            as_of_date=as_of_date,
            days_past_due=dpd,
            bucket=bucket,
            principal_past_due=principal_pd,
            interest_past_due=interest_pd,
            fees_past_due=fees_pd,
            total_past_due=total_pd,
            oldest_overdue_due_date=oldest_due,
            milestones_triggered=milestones,
            recommended_status=rec_status,
        )

    # -------------------------------------------------------------------------
    # Overdue period detection
    # -------------------------------------------------------------------------

    def _overdue_periods(
        self,
        schedule: list[ScheduledPeriod],
        as_of_date: date,
    ) -> list[ScheduledPeriod]:
        """
        Return schedule periods that are past-due as of as_of_date.

        A period is overdue if:
          - Its due_date < as_of_date (strictly before, not same day)
          - Its status is 'open' or 'partial' (not fully paid)
          - It is not waived or deferred
        """
        return [
            p for p in schedule
            if p.due_date < as_of_date
            and p.status in ("open", "partial", "overdue")
        ]

    def _unpaid_ratio(
        self,
        period: ScheduledPeriod,
        payments: list[PaymentRecord],
    ) -> Decimal:
        """
        Estimate the unpaid fraction of a period.

        For 'open' periods: 1.0 (fully unpaid)
        For 'partial' periods: we approximate by checking how much
        total cash has been applied vs. total scheduled for this period.

        Note: In a full implementation, payments would be linked to periods
        via payment.period_id. For now we use status as the signal.
        """
        if period.status == "open":
            return Decimal("1")
        elif period.status == "overdue":
            return Decimal("1")
        elif period.status == "partial":
            # Approximate: assume 50% paid for partial periods
            # In production, join to payment table via period_id for exact amount
            return Decimal("0.5")
        return ZERO

    # -------------------------------------------------------------------------
    # Bucket, milestone, status helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _dpd_to_bucket(dpd: int) -> str:
        if dpd <= 0:    return "current"
        if dpd <= 30:   return "1-30"
        if dpd <= 60:   return "31-60"
        if dpd <= 90:   return "61-90"
        if dpd <= 120:  return "91-120"
        return "120+"

    @staticmethod
    def _new_milestones(prior_dpd: int, current_dpd: int) -> list[int]:
        """
        Return milestones that have been newly crossed since prior_dpd.
        e.g. prior=8, current=12 → [10] (crossed 10 DPD milestone)
        """
        milestones = [MILESTONE_10, MILESTONE_30, MILESTONE_60, MILESTONE_90, MILESTONE_120]
        return [m for m in milestones if prior_dpd < m <= current_dpd]

    def _recommended_status(self, dpd: int) -> Optional[str]:
        """
        Return a recommended loan status transition based on DPD.
        Returns None if no change is warranted from current status.
        """
        if dpd >= DPD_TO_DEFAULT and self.loan_status not in ("default", "workout", "payoff_pending"):
            return "default"
        elif dpd >= DPD_TO_DELINQUENT and self.loan_status == "funded":
            return "delinquent"
        elif dpd == 0 and self.loan_status == "delinquent":
            return "funded"  # cured
        return None

    def _recommended_status_current(self) -> Optional[str]:
        """Status recommendation when loan is current (DPD=0)."""
        if self.loan_status == "delinquent":
            return "funded"   # cured
        return None


# ---------------------------------------------------------------------------
# Workflow task generation
# ---------------------------------------------------------------------------

def tasks_for_milestones(
    loan_id: object,
    loan_number: str,
    borrower_name: str,
    portfolio_id: object,
    milestones: list[int],
    dpd: int,
    total_past_due: Decimal,
) -> list[MilestoneTask]:
    """
    Generate workflow tasks for newly-crossed DPD milestones.
    One task per milestone event.
    """
    tasks = []
    for milestone in milestones:
        task = _milestone_task(
            milestone, loan_number, borrower_name, dpd, total_past_due
        )
        if task:
            tasks.append(task)
    return tasks


def _milestone_task(
    milestone: int,
    loan_number: str,
    borrower_name: str,
    dpd: int,
    total_past_due: Decimal,
) -> Optional[MilestoneTask]:
    past_due_str = f"${total_past_due:,.2f}"

    if milestone == MILESTONE_10:
        return MilestoneTask(
            task_type="delinquency_milestone",
            title=f"{loan_number} — 10 DPD: First notice required",
            description=(
                f"{borrower_name} is {dpd} days past due. "
                f"Amount past due: {past_due_str}. "
                f"Send initial past-due notice per servicing procedures."
            ),
            priority="normal",
            days_past_due=dpd,
        )
    elif milestone == MILESTONE_30:
        return MilestoneTask(
            task_type="delinquency_milestone",
            title=f"{loan_number} — 30 DPD: Second notice + call required",
            description=(
                f"{borrower_name} is {dpd} days past due. "
                f"Amount past due: {past_due_str}. "
                f"Issue second demand notice and document outreach attempt."
            ),
            priority="high",
            days_past_due=dpd,
        )
    elif milestone == MILESTONE_60:
        return MilestoneTask(
            task_type="delinquency_milestone",
            title=f"{loan_number} — 60 DPD: Escalate to senior servicing",
            description=(
                f"{borrower_name} is {dpd} days past due. "
                f"Amount past due: {past_due_str}. "
                f"Escalate to senior team. Review collateral and consider workout options."
            ),
            priority="high",
            days_past_due=dpd,
        )
    elif milestone == MILESTONE_90:
        return MilestoneTask(
            task_type="delinquency_milestone",
            title=f"{loan_number} — 90 DPD: DEFAULT THRESHOLD — immediate action",
            description=(
                f"{borrower_name} is {dpd} days past due. "
                f"Amount past due: {past_due_str}. "
                f"Loan qualifies for default status. "
                f"Initiate default interest, notify investors, begin legal review."
            ),
            priority="critical",
            days_past_due=dpd,
        )
    elif milestone == MILESTONE_120:
        return MilestoneTask(
            task_type="delinquency_milestone",
            title=f"{loan_number} — 120 DPD: Charge-off review required",
            description=(
                f"{borrower_name} is {dpd} days past due. "
                f"Amount past due: {past_due_str}. "
                f"Loan has reached 120 DPD. Initiate charge-off review process."
            ),
            priority="critical",
            days_past_due=dpd,
        )
    return None
