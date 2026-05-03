"""
app/models/loan.py

SQLAlchemy ORM model for the loan table.
Mirrors the schema defined in schema.sql.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey,
    Integer, Numeric, String, Text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, AuditMixin


class Loan(Base, AuditMixin):
    __tablename__ = "loan"

    # Identity
    portfolio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("portfolio.id"), nullable=False, index=True)
    loan_number: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    loan_name: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="boarding", index=True)

    # Parties
    primary_borrower_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("counterparty.id"), nullable=False, index=True)

    # Economics
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    original_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    commitment_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    current_principal: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    accrued_interest: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    accrued_fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))

    # Rate
    rate_type: Mapped[str] = mapped_column(String(20), nullable=False)
    coupon_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    pik_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    rate_floor: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    rate_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    spread: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    index_code: Mapped[Optional[str]] = mapped_column(String(20), )
    day_count: Mapped[str] = mapped_column(String(10), nullable=False, default="ACT/360")

    # Schedule
    origination_date: Mapped[date] = mapped_column(Date, nullable=False)
    first_payment_date: Mapped[Optional[date]] = mapped_column(Date)
    maturity_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    payment_frequency: Mapped[str] = mapped_column(String(15), nullable=False, default="QUARTERLY")
    amortization_type: Mapped[str] = mapped_column(String(25), nullable=False, default="bullet")
    interest_only_period_months: Mapped[Optional[int]] = mapped_column(Integer)
    balloon_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))

    # Late charges
    grace_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    late_fee_type: Mapped[Optional[str]] = mapped_column(String(30))
    late_fee_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    default_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    # Prepayment penalty
    prepayment_penalty_type: Mapped[str] = mapped_column(String(20), default="none")
    prepayment_penalty_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    prepayment_penalty_schedule: Mapped[Optional[dict]] = mapped_column(JSONB)
    default_triggered_at: Mapped[Optional[date]] = mapped_column(Date)

    # Investran
    investran_loan_id: Mapped[Optional[str]] = mapped_column(String(100))
    investran_last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Admin
    boarding_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    funded_at: Mapped[Optional[date]] = mapped_column(Date)
    paid_off_at: Mapped[Optional[date]] = mapped_column(Date)
    servicer_notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))

    # Relationships
    portfolio: Mapped["Portfolio"] = relationship("Portfolio", back_populates="loans")
    primary_borrower: Mapped["Counterparty"] = relationship("Counterparty", foreign_keys=[primary_borrower_id])
    guarantors: Mapped[list["LoanGuarantor"]] = relationship("LoanGuarantor", back_populates="loan", cascade="all, delete-orphan")
    collaterals: Mapped[list["Collateral"]] = relationship("Collateral", back_populates="loan", cascade="all, delete-orphan")
    covenants: Mapped[list["Covenant"]] = relationship("Covenant", back_populates="loan", cascade="all, delete-orphan")
    payment_schedule: Mapped[list["PaymentSchedule"]] = relationship("PaymentSchedule", back_populates="loan", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship("Payment", back_populates="loan")
    fees: Mapped[list["Fee"]] = relationship("Fee", back_populates="loan")
    modifications: Mapped[list["LoanModification"]] = relationship("LoanModification", back_populates="loan")
    rate_resets: Mapped[list["RateReset"]] = relationship("RateReset", back_populates="loan")

    def __repr__(self) -> str:
        return f"<Loan {self.loan_number} | {self.status} | ${self.current_principal:,.2f}>"
