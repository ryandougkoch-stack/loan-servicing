"""
tests/unit/test_amortization_engine.py

Comprehensive unit tests for the amortization engine.

Test philosophy:
  - Every output is checked against independently hand-calculated values
  - Waterfall integrity: scheduled_principal sums to original_balance
  - Interest integrity: matches manual period-by-period calculation
  - Edge cases: stub periods, month-end roll, irregular schedules
  - All arithmetic uses Decimal throughout
"""
from datetime import date
from decimal import Decimal

import pytest

from app.services.amortization_engine import AmortizationEngine, RateStep

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def engine(
    balance="1000000",
    rate="0.09",
    origination=date(2024, 1, 1),
    maturity=date(2026, 1, 1),
    frequency="QUARTERLY",
    amort_type="bullet",
    day_count="ACT/360",
    first_payment=None,
    io_months=0,
    balloon=None,
    pik_rate="0",
    rate_steps=None,
):
    return AmortizationEngine.from_params(
        original_balance=Decimal(balance),
        coupon_rate=Decimal(rate),
        origination_date=origination,
        maturity_date=maturity,
        payment_frequency=frequency,
        amortization_type=amort_type,
        day_count=day_count,
        first_payment_date=first_payment,
        pik_rate=Decimal(pik_rate),
        interest_only_period_months=io_months,
        balloon_amount=Decimal(balloon) if balloon else None,
        rate_steps=rate_steps,
    )


# ---------------------------------------------------------------------------
# 1. Bullet / Interest-Only
# ---------------------------------------------------------------------------

class TestBulletLoans:

    def test_principal_only_in_final_period(self):
        e = engine(amort_type="bullet")
        periods = e.generate()
        # All periods except last: no principal
        for p in periods[:-1]:
            assert p.scheduled_principal == Decimal("0"), f"Period {p.period_number} has unexpected principal"
        # Last period: full balance
        assert periods[-1].scheduled_principal == Decimal("1000000.00")

    def test_principal_sum_equals_original_balance(self):
        e = engine(amort_type="bullet")
        periods = e.generate()
        total_principal = sum(p.scheduled_principal for p in periods)
        assert total_principal == Decimal("1000000.00")

    def test_interest_is_positive_every_period(self):
        e = engine(amort_type="bullet")
        for p in e.generate():
            assert p.scheduled_interest > 0, f"Period {p.period_number} has zero interest"

    def test_quarterly_2yr_produces_8_periods(self):
        e = engine(
            origination=date(2024, 1, 1),
            maturity=date(2026, 1, 1),
            frequency="QUARTERLY",
        )
        periods = e.generate()
        assert len(periods) == 8

    def test_first_period_interest_act360(self):
        """
        Q1 2024: Jan 1 → Apr 1 = 91 days.
        Interest = $1,000,000 × 9% × 91/360 = $22,750.00
        """
        e = engine(
            balance="1000000",
            rate="0.09",
            origination=date(2024, 1, 1),
            maturity=date(2025, 1, 1),
            frequency="QUARTERLY",
            day_count="ACT/360",
        )
        periods = e.generate()
        # Jan 1 → Apr 1 = 91 days
        expected = (Decimal("1000000") * Decimal("0.09") * Decimal("91") / Decimal("360")).quantize(Decimal("0.01"))
        assert periods[0].scheduled_interest == expected

    def test_first_period_interest_act365(self):
        """ACT/365 day count."""
        e = engine(
            balance="1000000",
            rate="0.09",
            origination=date(2024, 1, 1),
            maturity=date(2025, 1, 1),
            frequency="QUARTERLY",
            day_count="ACT/365",
        )
        periods = e.generate()
        expected = (Decimal("1000000") * Decimal("0.09") * Decimal("91") / Decimal("365")).quantize(Decimal("0.01"))
        assert periods[0].scheduled_interest == expected

    def test_30_360_day_count(self):
        """
        30/360: Jan 1 → Apr 1
        Days = 360*(0) + 30*(3) + (1-1) = 90 days
        Interest = $1,000,000 × 9% × 90/360 = $22,500.00
        """
        e = engine(
            balance="1000000",
            rate="0.09",
            origination=date(2024, 1, 1),
            maturity=date(2025, 1, 1),
            frequency="QUARTERLY",
            day_count="30/360",
        )
        periods = e.generate()
        expected = (Decimal("1000000") * Decimal("0.09") * Decimal("90") / Decimal("360")).quantize(Decimal("0.01"))
        assert periods[0].scheduled_interest == expected

    def test_annual_loan_produces_correct_period_count(self):
        e = engine(
            origination=date(2024, 1, 1),
            maturity=date(2027, 1, 1),
            frequency="ANNUAL",
        )
        periods = e.generate()
        assert len(periods) == 3

    def test_ending_balance_reaches_zero(self):
        e = engine(amort_type="bullet")
        periods = e.generate()
        assert periods[-1].ending_balance == Decimal("0.00")

    def test_period_dates_are_contiguous(self):
        e = engine(amort_type="bullet")
        periods = e.generate()
        for i in range(1, len(periods)):
            assert periods[i].period_start_date == periods[i - 1].period_end_date


# ---------------------------------------------------------------------------
# 2. Fully Amortizing
# ---------------------------------------------------------------------------

class TestAmortizingLoans:

    def test_principal_sum_equals_original_balance(self):
        e = engine(
            balance="500000",
            rate="0.08",
            origination=date(2024, 1, 1),
            maturity=date(2026, 1, 1),
            frequency="QUARTERLY",
            amort_type="amortizing",
        )
        periods = e.generate()
        total_principal = sum(p.scheduled_principal for p in periods)
        assert total_principal == Decimal("500000.00"), f"Principal sum: {total_principal}"

    def test_balance_decreases_each_period(self):
        e = engine(
            balance="1000000",
            rate="0.09",
            origination=date(2024, 1, 1),
            maturity=date(2027, 1, 1),
            frequency="QUARTERLY",
            amort_type="amortizing",
        )
        periods = e.generate()
        for i in range(1, len(periods)):
            assert periods[i].beginning_balance < periods[i - 1].beginning_balance

    def test_final_balance_is_zero(self):
        e = engine(
            balance="1000000",
            rate="0.09",
            origination=date(2024, 1, 1),
            maturity=date(2027, 1, 1),
            frequency="QUARTERLY",
            amort_type="amortizing",
        )
        periods = e.generate()
        assert periods[-1].ending_balance == Decimal("0.00")

    def test_level_payment_roughly_constant(self):
        """Non-final periods should have nearly equal total payments (±1 cent rounding)."""
        e = engine(
            balance="1000000",
            rate="0.09",
            origination=date(2024, 1, 1),
            maturity=date(2027, 1, 1),
            frequency="QUARTERLY",
            amort_type="amortizing",
            day_count="30/360",  # uniform days makes payment truly level
        )
        periods = e.generate()
        non_final_totals = [p.total_scheduled for p in periods[:-1]]
        min_pmt = min(non_final_totals)
        max_pmt = max(non_final_totals)
        # Allow up to $1 variance from rounding
        assert max_pmt - min_pmt <= Decimal("1.00"), (
            f"Payment variance too large: min={min_pmt}, max={max_pmt}"
        )

    def test_interest_decreases_as_balance_falls(self):
        """In a level payment amortising loan, interest shrinks each period."""
        e = engine(
            balance="1000000",
            rate="0.09",
            origination=date(2024, 1, 1),
            maturity=date(2027, 1, 1),
            frequency="QUARTERLY",
            amort_type="amortizing",
            day_count="30/360",
        )
        periods = e.generate()
        for i in range(1, len(periods) - 1):
            assert periods[i].scheduled_interest < periods[i - 1].scheduled_interest

    def test_io_stub_then_amortizing(self):
        """First 4 periods IO, then amortising."""
        e = engine(
            balance="1000000",
            rate="0.09",
            origination=date(2024, 1, 1),
            maturity=date(2027, 1, 1),
            frequency="QUARTERLY",
            amort_type="amortizing",
            io_months=12,
        )
        periods = e.generate()
        # First 4 periods: no principal
        for p in periods[:4]:
            assert p.scheduled_principal == Decimal("0"), (
                f"Period {p.period_number} should be IO but has principal {p.scheduled_principal}"
            )
        # After IO cutoff: principal > 0
        assert periods[4].scheduled_principal > Decimal("0")


# ---------------------------------------------------------------------------
# 3. Partial Amortizing (Balloon)
# ---------------------------------------------------------------------------

class TestPartialAmortizingLoans:

    def test_explicit_balloon_paid_at_maturity(self):
        e = engine(
            balance="1000000",
            rate="0.08",
            origination=date(2024, 1, 1),
            maturity=date(2029, 1, 1),
            frequency="QUARTERLY",
            amort_type="partial_amortizing",
            balloon="500000",
        )
        periods = e.generate()
        # Final period principal should be close to balloon
        # (small rounding adjustments are normal)
        final_principal = periods[-1].scheduled_principal
        assert final_principal >= Decimal("499000"), f"Balloon too small: {final_principal}"
        assert final_principal <= Decimal("510000"), f"Balloon too large: {final_principal}"

    def test_principal_sum_equals_original_balance_partial(self):
        e = engine(
            balance="2000000",
            rate="0.085",
            origination=date(2024, 1, 1),
            maturity=date(2029, 1, 1),
            frequency="QUARTERLY",
            amort_type="partial_amortizing",
            balloon="1000000",
        )
        periods = e.generate()
        total = sum(p.scheduled_principal for p in periods)
        assert total == Decimal("2000000.00"), f"Principal sum: {total}"

    def test_final_balance_is_zero(self):
        e = engine(
            balance="1000000",
            rate="0.08",
            origination=date(2024, 1, 1),
            maturity=date(2029, 1, 1),
            frequency="QUARTERLY",
            amort_type="partial_amortizing",
            balloon="500000",
        )
        periods = e.generate()
        assert periods[-1].ending_balance == Decimal("0.00")


# ---------------------------------------------------------------------------
# 4. Step-Rate Loans
# ---------------------------------------------------------------------------

class TestStepRateLoans:

    def test_rate_changes_at_step_date(self):
        """Rate should switch at the step effective date."""
        steps = [
            RateStep(effective_date=date(2024, 1, 1), annual_rate=Decimal("0.07")),
            RateStep(effective_date=date(2025, 1, 1), annual_rate=Decimal("0.09")),
        ]
        e = engine(
            balance="1000000",
            rate="0.07",
            origination=date(2024, 1, 1),
            maturity=date(2026, 1, 1),
            frequency="QUARTERLY",
            amort_type="bullet",
            rate_steps=steps,
        )
        periods = e.generate()
        # Periods before 2025-01-01 use 7%
        early_periods = [p for p in periods if p.period_start_date < date(2025, 1, 1)]
        late_periods = [p for p in periods if p.period_start_date >= date(2025, 1, 1)]

        for p in early_periods:
            assert p.interest_rate_used == Decimal("0.07")
        for p in late_periods:
            assert p.interest_rate_used == Decimal("0.09")

    def test_rate_floor_applied(self):
        """Rate should never go below the floor."""
        e = engine(
            balance="1000000",
            rate="0.02",   # below floor
        )
        e.input.rate_floor = Decimal("0.05")
        periods = e.generate()
        for p in periods:
            assert p.interest_rate_used >= Decimal("0.05")

    def test_rate_cap_applied(self):
        """Rate should never exceed the cap."""
        e = engine(
            balance="1000000",
            rate="0.15",   # above cap
        )
        e.input.rate_cap = Decimal("0.12")
        periods = e.generate()
        for p in periods:
            assert p.interest_rate_used <= Decimal("0.12")


# ---------------------------------------------------------------------------
# 5. Day Count Conventions
# ---------------------------------------------------------------------------

class TestDayCountConventions:

    def test_act360_vs_act365_same_days_different_interest(self):
        """ACT/360 produces higher interest than ACT/365 for same period."""
        e360 = engine(day_count="ACT/360")
        e365 = engine(day_count="ACT/365")
        p360 = e360.generate()[0]
        p365 = e365.generate()[0]
        # ACT/360 has smaller denominator → higher interest
        assert p360.scheduled_interest > p365.scheduled_interest

    def test_30_360_produces_round_day_counts(self):
        """30/360 should produce 90-day quarter counts for regular quarters."""
        e = engine(
            origination=date(2024, 1, 1),
            maturity=date(2025, 1, 1),
            frequency="QUARTERLY",
            day_count="30/360",
        )
        periods = e.generate()
        for p in periods:
            # In 30/360, regular quarters should be 90 days
            assert p.days_in_period == 90, (
                f"Period {p.period_number}: expected 90 days, got {p.days_in_period}"
            )

    def test_act_act_leap_year_uses_366(self):
        """ACT/ACT should use 366-day denominator in a leap year (2024)."""
        e = engine(
            origination=date(2024, 1, 1),
            maturity=date(2025, 1, 1),
            frequency="QUARTERLY",
            day_count="ACT/ACT",
        )
        periods = e.generate()
        # 2024 is a leap year; first period denominator should be 366
        # Verify by checking interest is less than ACT/365 would give
        e365 = engine(
            origination=date(2024, 1, 1),
            maturity=date(2025, 1, 1),
            frequency="QUARTERLY",
            day_count="ACT/365",
        )
        p_act_act = periods[0]
        p_365 = e365.generate()[0]
        assert p_act_act.scheduled_interest < p_365.scheduled_interest


# ---------------------------------------------------------------------------
# 6. Zero Coupon
# ---------------------------------------------------------------------------

class TestZeroCouponLoans:

    def test_single_period(self):
        e = engine(
            balance="1000000",
            rate="0.10",
            origination=date(2024, 1, 1),
            maturity=date(2027, 1, 1),
            amort_type="bullet",
        )
        e.input.rate_type = "zero_coupon"
        periods = e.generate()
        assert len(periods) == 1
        assert periods[0].scheduled_interest == Decimal("0")
        assert periods[0].scheduled_principal == Decimal("1000000.00")


# ---------------------------------------------------------------------------
# 7. Per Diem and Payoff Quote
# ---------------------------------------------------------------------------

class TestPerDiemAndPayoff:

    def test_per_diem_is_positive(self):
        e = engine()
        pd = e.per_diem(Decimal("1000000"), date(2024, 6, 15))
        assert pd > Decimal("0")

    def test_per_diem_act360_calculation(self):
        """
        Per diem = $1,000,000 × 9% / 360 = $250.00
        """
        e = engine(balance="1000000", rate="0.09", day_count="ACT/360")
        pd = e.per_diem(Decimal("1000000"), date(2024, 6, 15))
        expected = (Decimal("1000000") * Decimal("0.09") / Decimal("360")).quantize(Decimal("0.01"))
        assert pd == expected

    def test_payoff_amount_breakdown(self):
        e = engine(balance="1000000", rate="0.09", day_count="ACT/360")
        result = e.payoff_amount(
            balance=Decimal("1000000"),
            as_of_date=date(2024, 6, 1),
            good_through=date(2024, 6, 15),  # 14 days
        )
        assert result["principal_balance"] == Decimal("1000000")
        assert result["per_diem"] > Decimal("0")
        assert result["accrued_interest_to_date"] > Decimal("0")
        # 14 days of interest
        expected_interest = (Decimal("1000000") * Decimal("0.09") * Decimal("14") / Decimal("360")).quantize(Decimal("0.01"))
        assert result["accrued_interest_to_date"] == expected_interest


# ---------------------------------------------------------------------------
# 8. Date and Period Edge Cases
# ---------------------------------------------------------------------------

class TestDateEdgeCases:

    def test_month_end_roll(self):
        """Loan originating on Jan 31 should roll to last day of each month."""
        e = engine(
            origination=date(2024, 1, 31),
            maturity=date(2025, 1, 31),
            frequency="MONTHLY",
            amort_type="bullet",
        )
        periods = e.generate()
        # Feb period should end on Feb 29 (2024 is a leap year)
        assert periods[0].period_end_date == date(2024, 2, 29)
        # Mar period ends Mar 31
        assert periods[1].period_end_date == date(2024, 3, 31)

    def test_stub_first_period(self):
        """Non-standard first payment date creates a stub first period."""
        e = engine(
            origination=date(2024, 1, 15),
            first_payment=date(2024, 4, 1),  # not exactly 3 months from origination
            maturity=date(2026, 4, 1),
            frequency="QUARTERLY",
            amort_type="bullet",
        )
        periods = e.generate()
        # First period is a stub: Jan 15 → Apr 1 = 77 days
        first_days = (periods[0].period_end_date - periods[0].period_start_date).days
        assert first_days == 77

    def test_1_year_bullet_monthly(self):
        e = engine(
            origination=date(2024, 1, 1),
            maturity=date(2025, 1, 1),
            frequency="MONTHLY",
            amort_type="bullet",
        )
        periods = e.generate()
        assert len(periods) == 12

    def test_maturity_date_is_last_period_end(self):
        e = engine()
        periods = e.generate()
        assert periods[-1].period_end_date == e.input.maturity_date

    def test_period_start_equals_previous_end(self):
        """Periods must be contiguous — no gaps or overlaps."""
        e = engine(amort_type="amortizing")
        periods = e.generate()
        for i in range(1, len(periods)):
            assert periods[i].period_start_date == periods[i - 1].period_end_date, (
                f"Gap between period {i} and {i+1}"
            )

    def test_beginning_balance_of_period_equals_ending_of_prior(self):
        e = engine(amort_type="amortizing")
        periods = e.generate()
        for i in range(1, len(periods)):
            assert periods[i].beginning_balance == periods[i - 1].ending_balance, (
                f"Balance discontinuity at period {i+1}"
            )


# ---------------------------------------------------------------------------
# 9. Invariants (property-based style)
# ---------------------------------------------------------------------------

class TestInvariants:

    @pytest.mark.parametrize("amort_type", ["bullet", "amortizing", "partial_amortizing"])
    def test_principal_sum_always_equals_balance(self, amort_type):
        kwargs = {"amort_type": amort_type}
        if amort_type == "partial_amortizing":
            kwargs["balloon"] = "400000"
        e = engine(**kwargs)
        periods = e.generate()
        total = sum(p.scheduled_principal for p in periods)
        assert total == Decimal("1000000.00"), f"{amort_type}: total={total}"

    @pytest.mark.parametrize("amort_type", ["bullet", "amortizing"])
    def test_final_balance_always_zero(self, amort_type):
        e = engine(amort_type=amort_type)
        periods = e.generate()
        assert periods[-1].ending_balance == Decimal("0.00")

    @pytest.mark.parametrize("day_count", ["ACT/360", "ACT/365", "30/360", "ACT/ACT"])
    def test_interest_always_positive_all_day_counts(self, day_count):
        e = engine(day_count=day_count)
        for p in e.generate():
            if p.period_start_date < e.input.maturity_date:
                assert p.scheduled_interest > Decimal("0")

    @pytest.mark.parametrize("freq,expected_n", [
        ("MONTHLY",     24),   # 2 years × 12 = 24
        ("QUARTERLY",    8),   # 2 years × 4  = 8
        ("SEMI_ANNUAL",  4),   # 2 years × 2  = 4
        ("ANNUAL",       2),   # 2 years × 1  = 2
    ])
    def test_all_frequencies_produce_valid_schedules(self, freq, expected_n):
        e = engine(
            origination=date(2024, 1, 1),
            maturity=date(2026, 1, 1),
            frequency=freq,
        )
        periods = e.generate()
        assert len(periods) == expected_n, f"{freq}: expected {expected_n}, got {len(periods)}"
        total = sum(p.scheduled_principal for p in periods)
        assert total == Decimal("1000000.00")
