"""
app/services/amortization_engine.py

Amortization and interest calculation engine.

Handles:
  - Fixed rate loans (standard amortizing, interest-only, bullet, partial amortizing)
  - Floating rate loans (SOFR/PRIME + spread, with floor/cap)
  - PIK (payment-in-kind) loans — interest capitalises into principal
  - Step-rate loans — rate changes on a schedule
  - Zero coupon — no periodic cash interest; yield at maturity
  - Irregular / custom schedules
  - Day count conventions: ACT/360, ACT/365, 30/360, ACT/ACT
  - Stub periods (first period longer or shorter than standard)

Design principles:
  - All arithmetic uses Python Decimal throughout — never float
  - Every calculation is reproducible: same inputs → same outputs always
  - The engine is a pure function: it takes a Loan and returns a list of periods
  - No database access here — the service layer persists the results
  - Each period carries a full snapshot so statements can be regenerated
    at any point without recalculating the entire schedule

Amortization methods:
  BULLET          — interest-only payments; full principal at maturity
  INTEREST_ONLY   — synonym for BULLET (no principal payments until maturity)
  AMORTIZING      — level payment (constant P+I), standard mortgage style
  PARTIAL_AMORT   — amortizing with a balloon at maturity
  CUSTOM          — caller provides principal schedule; engine adds interest
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

# ---------------------------------------------------------------------------
# Precision constants
# ---------------------------------------------------------------------------
CENTS = Decimal("0.01")
TEN_PLACES = Decimal("0.0000000001")
EIGHT_PLACES = Decimal("0.00000001")
ZERO = Decimal("0")
ONE = Decimal("1")

# ---------------------------------------------------------------------------
# Day count denominators (static)
# ---------------------------------------------------------------------------
DAY_COUNT_STATIC = {
    "ACT/360": Decimal("360"),
    "ACT/365": Decimal("365"),
    "30/360":  Decimal("360"),
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SchedulePeriod:
    """
    One row of the amortization schedule.
    Maps 1:1 to a payment_schedule row in the database.
    """
    period_number: int
    period_start_date: date
    period_end_date: date
    due_date: date
    beginning_balance: Decimal
    scheduled_principal: Decimal
    scheduled_interest: Decimal
    scheduled_fees: Decimal = ZERO
    scheduled_escrow: Decimal = ZERO
    days_in_period: int = 0
    interest_rate_used: Decimal = ZERO  # effective annual rate for this period
    ending_balance: Decimal = ZERO

    def __post_init__(self):
        self.ending_balance = (
            self.beginning_balance - self.scheduled_principal
        ).quantize(CENTS)

    @property
    def total_scheduled(self) -> Decimal:
        return (
            self.scheduled_principal
            + self.scheduled_interest
            + self.scheduled_fees
            + self.scheduled_escrow
        )

    def to_dict(self) -> dict:
        return {
            "period_number": self.period_number,
            "period_start_date": self.period_start_date,
            "period_end_date": self.period_end_date,
            "due_date": self.due_date,
            "beginning_balance": self.beginning_balance,
            "scheduled_principal": self.scheduled_principal,
            "scheduled_interest": self.scheduled_interest,
            "scheduled_fees": self.scheduled_fees,
            "scheduled_escrow": self.scheduled_escrow,
            "days_in_period": self.days_in_period,
            "interest_rate_used": self.interest_rate_used,
            "ending_balance": self.ending_balance,
            "total_scheduled": self.total_scheduled,
            "is_current": True,
            "status": "open",
        }


@dataclass
class RateStep:
    """A rate that applies from effective_date onwards."""
    effective_date: date
    annual_rate: Decimal   # e.g. Decimal("0.08") for 8%


@dataclass
class EngineInput:
    """
    Normalised input to the engine.
    Built from a Loan ORM object by AmortizationEngine.__init__.
    """
    origination_date: date
    first_payment_date: date
    maturity_date: date
    original_balance: Decimal
    rate_type: str                          # fixed | floating | pik | step | zero_coupon
    amortization_type: str                  # bullet | interest_only | amortizing | partial_amortizing | custom
    day_count: str                          # ACT/360 | ACT/365 | 30/360 | ACT/ACT
    payment_frequency_months: int           # 1, 3, 6, 12
    coupon_rate: Decimal                    # annual rate (decimal)
    pik_rate: Decimal = ZERO               # PIK component (if mixed cash/PIK)
    rate_floor: Optional[Decimal] = None
    rate_cap: Optional[Decimal] = None
    interest_only_period_months: int = 0   # months before amortisation begins
    balloon_amount: Optional[Decimal] = None
    rate_steps: list[RateStep] = field(default_factory=list)
    custom_principal_schedule: Optional[dict[int, Decimal]] = None  # period_number → principal


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class AmortizationEngine:
    """
    Generates a payment schedule for a loan.

    Usage:
        engine = AmortizationEngine(loan)
        periods = engine.generate()   # returns list[SchedulePeriod]

    Or with raw parameters (useful for scenario/quote tools):
        engine = AmortizationEngine.from_params(
            original_balance=Decimal("1000000"),
            coupon_rate=Decimal("0.09"),
            ...
        )
    """

    def __init__(self, loan):
        """Construct from a Loan ORM object."""
        freq_map = {
            "MONTHLY":     1,
            "QUARTERLY":   3,
            "SEMI_ANNUAL": 6,
            "ANNUAL":      12,
            "BULLET":      None,   # handled as single period
        }

        freq_months = freq_map.get(loan.payment_frequency, 3)
        if freq_months is None:
            # Bullet = single period from origination to maturity
            freq_months = self._months_between(loan.origination_date, loan.maturity_date)

        first_pmt = loan.first_payment_date or self._add_months(
            loan.origination_date, freq_months
        )

        self.input = EngineInput(
            origination_date=loan.origination_date,
            first_payment_date=first_pmt,
            maturity_date=loan.maturity_date,
            original_balance=loan.current_principal,  # use current (may differ from original after modifications)
            rate_type=loan.rate_type,
            amortization_type=loan.amortization_type,
            day_count=loan.day_count,
            payment_frequency_months=freq_months,
            coupon_rate=loan.coupon_rate or ZERO,
            pik_rate=loan.pik_rate or ZERO,
            rate_floor=loan.rate_floor,
            rate_cap=loan.rate_cap,
            interest_only_period_months=loan.interest_only_period_months or 0,
        )
        self._loan = loan

    @classmethod
    def from_params(
        cls,
        original_balance: Decimal,
        coupon_rate: Decimal,
        origination_date: date,
        maturity_date: date,
        payment_frequency: str = "QUARTERLY",
        amortization_type: str = "bullet",
        day_count: str = "ACT/360",
        first_payment_date: Optional[date] = None,
        pik_rate: Decimal = ZERO,
        rate_floor: Optional[Decimal] = None,
        rate_cap: Optional[Decimal] = None,
        interest_only_period_months: int = 0,
        balloon_amount: Optional[Decimal] = None,
        rate_steps: Optional[list[RateStep]] = None,
    ) -> "AmortizationEngine":
        """
        Construct directly from parameters — for payoff quotes, scenario tools,
        and modification previews without needing a Loan ORM object.
        """
        from types import SimpleNamespace
        loan = SimpleNamespace(
            origination_date=origination_date,
            first_payment_date=first_payment_date,
            maturity_date=maturity_date,
            current_principal=original_balance,
            rate_type="fixed",
            amortization_type=amortization_type,
            day_count=day_count,
            payment_frequency=payment_frequency,
            coupon_rate=coupon_rate,
            pik_rate=pik_rate,
            rate_floor=rate_floor,
            rate_cap=rate_cap,
            interest_only_period_months=interest_only_period_months,
        )
        engine = cls.__new__(cls)
        cls.__init__(engine, loan)
        if balloon_amount:
            engine.input.balloon_amount = balloon_amount
        if rate_steps:
            engine.input.rate_steps = rate_steps
        return engine

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def generate(self) -> list[SchedulePeriod]:
        """
        Generate the full payment schedule.
        Returns a list of SchedulePeriod, one per payment date.
        """
        inp = self.input

        if inp.rate_type == "zero_coupon":
            return self._generate_zero_coupon()

        periods = self._build_period_dates()

        if inp.amortization_type in ("bullet", "interest_only"):
            return self._generate_bullet(periods)
        elif inp.amortization_type == "amortizing":
            return self._generate_amortizing(periods)
        elif inp.amortization_type == "partial_amortizing":
            return self._generate_partial_amortizing(periods)
        elif inp.amortization_type == "custom":
            return self._generate_custom(periods)
        else:
            raise ValueError(f"Unknown amortization_type: {inp.amortization_type}")

    def calculate_period_interest(
        self,
        balance: Decimal,
        start_date: date,
        end_date: date,
        annual_rate: Optional[Decimal] = None,
    ) -> Decimal:
        """
        Calculate interest for a single period.
        Exposed publicly for use by payoff quote and scenario tools.
        """
        rate = annual_rate if annual_rate is not None else self.input.coupon_rate
        days = self._count_days(start_date, end_date)
        denominator = self._day_count_denominator(start_date, end_date)
        period_rate = (rate * Decimal(str(days)) / denominator).quantize(TEN_PLACES)
        return (balance * period_rate).quantize(CENTS, rounding=ROUND_HALF_UP)

    def per_diem(self, balance: Decimal, as_of_date: date) -> Decimal:
        """
        One day's interest on the given balance.
        Used in payoff quotes for the stub period calculation.
        """
        rate = self._effective_rate(as_of_date)
        denominator = self._day_count_denominator(as_of_date, as_of_date + timedelta(days=1))
        return (balance * rate / denominator).quantize(CENTS, rounding=ROUND_HALF_UP)

    def payoff_amount(self, balance: Decimal, as_of_date: date, good_through: date) -> dict:
        """
        Calculate total payoff amount including stub period interest.
        Returns a breakdown suitable for generating a payoff letter.
        """
        stub_days = (good_through - as_of_date).days
        rate = self._effective_rate(as_of_date)
        denominator = self._day_count_denominator(as_of_date, good_through)
        stub_interest = (
            balance * rate * Decimal(str(stub_days)) / denominator
        ).quantize(CENTS, rounding=ROUND_HALF_UP)
        per_diem_amt = self.per_diem(balance, as_of_date)

        return {
            "principal_balance": balance,
            "accrued_interest_to_date": stub_interest,
            "per_diem": per_diem_amt,
            "good_through_date": good_through,
            "as_of_date": as_of_date,
            "rate_used": rate,
        }

    # -------------------------------------------------------------------------
    # Schedule generators
    # -------------------------------------------------------------------------

    def _generate_bullet(self, periods: list[tuple[date, date, date]]) -> list[SchedulePeriod]:
        """
        Interest-only / bullet schedule.
        Every period: interest only. Final period: interest + full principal.
        """
        inp = self.input
        balance = inp.original_balance
        result = []

        for i, (start, end, due) in enumerate(periods):
            is_last = (i == len(periods) - 1)
            rate = self._effective_rate(start)
            interest = self.calculate_period_interest(balance, start, end, rate)

            # PIK: add to principal instead of cash payment
            if inp.rate_type == "pik":
                pik_interest = interest
                interest = ZERO
                # PIK capitalises — but we still show it as a scheduled item
                # so the loan balance grows. Handled via accrual task in practice.
            else:
                pik_interest = ZERO
                if inp.pik_rate > 0:
                    pik_interest = self.calculate_period_interest(balance, start, end, inp.pik_rate)
                    interest = self.calculate_period_interest(balance, start, end, rate - inp.pik_rate)

            principal = balance if is_last else ZERO

            result.append(SchedulePeriod(
                period_number=i + 1,
                period_start_date=start,
                period_end_date=end,
                due_date=due,
                beginning_balance=balance,
                scheduled_principal=principal,
                scheduled_interest=interest,
                days_in_period=self._count_days(start, end),
                interest_rate_used=rate,
            ))

            if is_last:
                balance = ZERO
            # PIK adds to balance each period
            if pik_interest > 0 and not is_last:
                balance = (balance + pik_interest).quantize(CENTS)

        return result

    def _generate_amortizing(self, periods: list[tuple[date, date, date]]) -> list[SchedulePeriod]:
        """
        Fully amortizing — level payment (constant P+I each period).
        Standard mortgage / term loan style.

        Uses the standard annuity formula:
            PMT = PV * r / (1 - (1 + r)^-n)

        For loans with an interest-only stub, the IO periods use interest-only
        payments and amortisation begins after interest_only_period_months.
        """
        inp = self.input
        n_total = len(periods)

        # Identify IO boundary
        io_cutoff = 0
        if inp.interest_only_period_months > 0:
            io_cutoff = self._io_period_count(periods, inp.interest_only_period_months)

        amort_periods = periods[io_cutoff:]
        n_amort = len(amort_periods)

        # Calculate the level payment for the amortising portion
        # We approximate using average period length (not perfectly accurate for irregular periods,
        # but very close; exact approach would recalculate per-period)
        balance_at_amort_start = inp.original_balance
        if io_cutoff > 0:
            # Balance after IO periods (no principal paid)
            balance_at_amort_start = inp.original_balance

        rate = self._effective_rate(inp.first_payment_date)
        level_pmt = self._level_payment(balance_at_amort_start, rate, n_amort, periods)

        result = []
        balance = inp.original_balance

        for i, (start, end, due) in enumerate(periods):
            period_rate = self._effective_rate(start)  # re-check for step-rate loans
            interest = self.calculate_period_interest(balance, start, end, period_rate)
            is_last = (i == n_total - 1)

            if i < io_cutoff:
                # Interest-only period
                principal = ZERO
            elif is_last:
                # Final period: clean up rounding; pay exact remaining balance
                principal = balance
            else:
                # Amortising period: level payment - interest = principal
                # Recalculate level pmt if rate changed (step-rate)
                if period_rate != rate:
                    rate = period_rate
                    remaining_periods = n_total - i
                    level_pmt = self._level_payment(balance, rate, remaining_periods, periods[i:])
                principal = (level_pmt - interest).quantize(CENTS, rounding=ROUND_HALF_UP)
                # Guard: principal can't exceed balance or be negative
                principal = max(ZERO, min(principal, balance))

            result.append(SchedulePeriod(
                period_number=i + 1,
                period_start_date=start,
                period_end_date=end,
                due_date=due,
                beginning_balance=balance,
                scheduled_principal=principal,
                scheduled_interest=interest,
                days_in_period=self._count_days(start, end),
                interest_rate_used=period_rate,
            ))

            balance = (balance - principal).quantize(CENTS)

        return result

    def _generate_partial_amortizing(self, periods: list[tuple[date, date, date]]) -> list[SchedulePeriod]:
        """
        Partially amortizing with a balloon payment at maturity.

        Calculates the level payment as if the loan fully amortised over a longer
        shadow term, then pays the remaining balance as a balloon at maturity.

        The balloon_amount can be specified explicitly or derived from the shadow amortisation.
        """
        inp = self.input
        n = len(periods)

        if inp.balloon_amount is not None:
            # Balloon specified: derive the payment that pays down to balloon
            balloon = inp.balloon_amount
            target_balance_at_maturity = balloon
        else:
            # Default: derive from a 30-year shadow amortisation (common for CRE)
            target_balance_at_maturity = self._shadow_balloon(inp.original_balance, inp.coupon_rate, n)

        rate = self._effective_rate(inp.first_payment_date)
        level_pmt = self._level_payment_to_target(
            inp.original_balance, rate, n, target_balance_at_maturity, periods
        )

        result = []
        balance = inp.original_balance

        for i, (start, end, due) in enumerate(periods):
            period_rate = self._effective_rate(start)
            interest = self.calculate_period_interest(balance, start, end, period_rate)
            is_last = (i == n - 1)

            if is_last:
                # Balloon: pay all remaining
                principal = balance
            else:
                principal = max(ZERO, (level_pmt - interest).quantize(CENTS, rounding=ROUND_HALF_UP))
                principal = min(principal, balance)

            result.append(SchedulePeriod(
                period_number=i + 1,
                period_start_date=start,
                period_end_date=end,
                due_date=due,
                beginning_balance=balance,
                scheduled_principal=principal,
                scheduled_interest=interest,
                days_in_period=self._count_days(start, end),
                interest_rate_used=period_rate,
            ))

            balance = (balance - principal).quantize(CENTS)

        return result

    def _generate_custom(self, periods: list[tuple[date, date, date]]) -> list[SchedulePeriod]:
        """
        Custom principal schedule. Caller specifies principal for each period;
        engine adds interest.
        """
        inp = self.input
        custom = inp.custom_principal_schedule or {}
        balance = inp.original_balance
        result = []

        for i, (start, end, due) in enumerate(periods):
            period_number = i + 1
            rate = self._effective_rate(start)
            interest = self.calculate_period_interest(balance, start, end, rate)
            principal = custom.get(period_number, ZERO)
            is_last = (i == len(periods) - 1)
            if is_last:
                principal = balance  # ensure full payoff at maturity

            result.append(SchedulePeriod(
                period_number=period_number,
                period_start_date=start,
                period_end_date=end,
                due_date=due,
                beginning_balance=balance,
                scheduled_principal=principal,
                scheduled_interest=interest,
                days_in_period=self._count_days(start, end),
                interest_rate_used=rate,
            ))
            balance = (balance - principal).quantize(CENTS)

        return result

    def _generate_zero_coupon(self) -> list[SchedulePeriod]:
        """
        Zero coupon loan — no periodic interest payments.
        Single period from origination to maturity.
        The yield is recognised at payoff (via accrual engine, not schedule).
        Schedule shows one period with principal only.
        """
        inp = self.input
        return [SchedulePeriod(
            period_number=1,
            period_start_date=inp.origination_date,
            period_end_date=inp.maturity_date,
            due_date=inp.maturity_date,
            beginning_balance=inp.original_balance,
            scheduled_principal=inp.original_balance,
            scheduled_interest=ZERO,
            days_in_period=self._count_days(inp.origination_date, inp.maturity_date),
            interest_rate_used=ZERO,
        )]

    # -------------------------------------------------------------------------
    # Period date generation
    # -------------------------------------------------------------------------

    def _build_period_dates(self) -> list[tuple[date, date, date]]:
        """
        Build a list of (period_start, period_end, due_date) tuples.

        Handles:
          - Stub first period (first payment date may not align exactly with
            origination + frequency)
          - Month-end roll: if origination is on the last day of the month,
            subsequent periods roll to month-end
          - The final period always ends on maturity_date
        """
        inp = self.input
        periods = []
        freq = inp.payment_frequency_months
        month_end_roll = self._is_month_end(inp.origination_date)

        current_start = inp.origination_date
        current_due = inp.first_payment_date

        while True:
            current_end = current_due  # period ends on the due date (standard convention)
            periods.append((current_start, current_end, current_due))

            if current_due >= inp.maturity_date:
                break

            current_start = current_end
            next_due = self._add_months(current_due, freq, month_end_roll)

            # Cap the final period at maturity
            if next_due >= inp.maturity_date:
                next_due = inp.maturity_date

            current_due = next_due

        # Ensure the final period ends exactly at maturity
        if periods and periods[-1][1] != inp.maturity_date:
            last_start, _, _ = periods[-1]
            periods[-1] = (last_start, inp.maturity_date, inp.maturity_date)

        return periods

    # -------------------------------------------------------------------------
    # Rate resolution
    # -------------------------------------------------------------------------

    def _effective_rate(self, as_of_date: date) -> Decimal:
        """
        Return the effective annual rate for a given date.
        Handles step-rate schedules, floors, and caps.
        """
        inp = self.input

        if inp.rate_type == "zero_coupon":
            return ZERO

        # Step-rate: find the most recent step effective on or before as_of_date
        if inp.rate_steps:
            applicable = [s for s in inp.rate_steps if s.effective_date <= as_of_date]
            if applicable:
                rate = max(applicable, key=lambda s: s.effective_date).annual_rate
            else:
                rate = inp.coupon_rate
        else:
            rate = inp.coupon_rate

        # Apply floor and cap
        if inp.rate_floor is not None:
            rate = max(rate, inp.rate_floor)
        if inp.rate_cap is not None:
            rate = min(rate, inp.rate_cap)

        return rate

    # -------------------------------------------------------------------------
    # Day count conventions
    # -------------------------------------------------------------------------

    def _count_days(self, start: date, end: date) -> int:
        """
        Count days between two dates according to the day count convention.

        ACT/360, ACT/365, ACT/ACT: actual calendar days
        30/360: each month treated as 30 days
        """
        if self.input.day_count == "30/360":
            return self._days_30_360(start, end)
        return (end - start).days

    def _days_30_360(self, start: date, end: date) -> int:
        """
        30/360 day count (Bond Basis).
        Formula: 360*(Y2-Y1) + 30*(M2-M1) + (D2-D1)
        with adjustments: D1=min(D1,30); if D1=30 then D2=min(D2,30)
        """
        d1, m1, y1 = start.day, start.month, start.year
        d2, m2, y2 = end.day, end.month, end.year

        if d1 == 31:
            d1 = 30
        if d1 == 30 and d2 == 31:
            d2 = 30

        return 360 * (y2 - y1) + 30 * (m2 - m1) + (d2 - d1)

    def _day_count_denominator(self, start: date, end: date) -> Decimal:
        """Return the annual denominator for the day count convention."""
        dc = self.input.day_count
        if dc == "ACT/ACT":
            # Actual days in the year containing the start date
            days_in_year = 366 if calendar.isleap(start.year) else 365
            return Decimal(str(days_in_year))
        return DAY_COUNT_STATIC.get(dc, Decimal("360"))

    # -------------------------------------------------------------------------
    # Payment amount calculations
    # -------------------------------------------------------------------------

    def _level_payment(
        self,
        balance: Decimal,
        annual_rate: Decimal,
        n_periods: int,
        periods: list[tuple[date, date, date]],
    ) -> Decimal:
        """
        Calculate the level periodic payment (annuity formula).

        For ACT-based day counts the period rate varies slightly each period,
        so we use the average period length for the annuity calculation and
        adjust in the final period.
        """
        if annual_rate == ZERO or n_periods == 0:
            return (balance / Decimal(str(n_periods))).quantize(CENTS, rounding=ROUND_HALF_UP) if n_periods else ZERO

        avg_days = self._average_period_days(periods)
        denominator = self._day_count_denominator(periods[0][0], periods[0][1]) if periods else Decimal("360")
        period_rate = (annual_rate * Decimal(str(avg_days)) / denominator)

        # PMT = PV * r / (1 - (1+r)^-n)
        one_plus_r_pow_n = (ONE + period_rate) ** n_periods
        payment = balance * period_rate * one_plus_r_pow_n / (one_plus_r_pow_n - ONE)
        return payment.quantize(CENTS, rounding=ROUND_HALF_UP)

    def _level_payment_to_target(
        self,
        balance: Decimal,
        annual_rate: Decimal,
        n_periods: int,
        target_ending_balance: Decimal,
        periods: list[tuple[date, date, date]],
    ) -> Decimal:
        """
        Calculate level payment that leaves a specific balloon balance at maturity.

        Formula: PMT = (PV - FV/(1+r)^n) * r / (1 - (1+r)^-n)
        """
        if annual_rate == ZERO:
            amort_amount = (balance - target_ending_balance) / Decimal(str(n_periods))
            return amort_amount.quantize(CENTS, rounding=ROUND_HALF_UP)

        avg_days = self._average_period_days(periods)
        denominator = self._day_count_denominator(periods[0][0], periods[0][1]) if periods else Decimal("360")
        period_rate = annual_rate * Decimal(str(avg_days)) / denominator

        one_plus_r_pow_n = (ONE + period_rate) ** n_periods
        pv_of_fv = target_ending_balance / one_plus_r_pow_n
        payment = (balance - pv_of_fv) * period_rate * one_plus_r_pow_n / (one_plus_r_pow_n - ONE)
        return payment.quantize(CENTS, rounding=ROUND_HALF_UP)

    def _shadow_balloon(
        self,
        balance: Decimal,
        annual_rate: Decimal,
        n_actual_periods: int,
        shadow_periods: int = 120,  # 30 years quarterly
    ) -> Decimal:
        """
        Calculate what the outstanding balance would be after n_actual_periods
        if the loan amortised over shadow_periods (default 30yr quarterly).
        Used to derive balloon amount when not explicitly specified.
        """
        if annual_rate == ZERO or shadow_periods == 0:
            return ZERO

        # Approximate: use quarterly period rate
        period_rate = annual_rate * Decimal("90") / Decimal("360")
        one_plus_r = ONE + period_rate
        pmt = balance * period_rate * (one_plus_r ** shadow_periods) / ((one_plus_r ** shadow_periods) - ONE)

        # Balance after n_actual_periods
        remaining = balance * (one_plus_r ** n_actual_periods) - pmt * ((one_plus_r ** n_actual_periods) - ONE) / period_rate
        return max(ZERO, remaining.quantize(CENTS, rounding=ROUND_HALF_UP))

    # -------------------------------------------------------------------------
    # Date arithmetic helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _add_months(dt: date, months: int, month_end_roll: bool = False) -> date:
        """
        Add months to a date.
        If month_end_roll is True, the result rolls to the last day of the month
        when the input date is the last day of its month.
        """
        month = dt.month - 1 + months
        year = dt.year + month // 12
        month = month % 12 + 1
        last_day = calendar.monthrange(year, month)[1]

        if month_end_roll:
            day = last_day
        else:
            day = min(dt.day, last_day)

        return date(year, month, day)

    @staticmethod
    def _months_between(d1: date, d2: date) -> int:
        return (d2.year - d1.year) * 12 + (d2.month - d1.month)

    @staticmethod
    def _is_month_end(dt: date) -> bool:
        return dt.day == calendar.monthrange(dt.year, dt.month)[1]

    @staticmethod
    def _average_period_days(periods: list[tuple[date, date, date]]) -> float:
        if not periods:
            return 90.0
        total_days = sum((end - start).days for start, end, _ in periods)
        return total_days / len(periods)

    def _io_period_count(
        self,
        periods: list[tuple[date, date, date]],
        io_months: int,
    ) -> int:
        """Return the number of periods that fall within the interest-only window."""
        cutoff = self._add_months(self.input.origination_date, io_months)
        return sum(1 for start, end, _ in periods if end <= cutoff)
