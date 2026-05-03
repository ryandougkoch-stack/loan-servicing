"""
app/models/ledger.py

ORM models for the double-entry ledger, payments, fees, and interest accruals.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime,
    ForeignKey, Integer, Numeric, String, Text, ARRAY
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class LedgerAccount(Base):
    __tablename__ = "ledger_account"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type: Mapped[str] = mapped_column(String(15), nullable=False)
    normal_balance: Mapped[str] = mapped_column(String(6), nullable=False)
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("ledger_account.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    gl_account_code: Mapped[Optional[str]] = mapped_column(String(50))  # Investran mapping


class JournalEntry(Base):
    __tablename__ = "journal_entry"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_number: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    loan_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("portfolio.id"), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    reference_type: Mapped[Optional[str]] = mapped_column(String(30))
    is_reversed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reversed_by_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("journal_entry.id"))
    reversal_of_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("journal_entry.id"))
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="posted")
    posted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    investran_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    investran_batch_id: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    lines: Mapped[list["JournalLine"]] = relationship("JournalLine", back_populates="entry", cascade="all, delete-orphan")


class JournalLine(Base):
    __tablename__ = "journal_line"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("journal_entry.id"), nullable=False, index=True)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ledger_account.id"), nullable=False, index=True)
    debit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    memo: Mapped[Optional[str]] = mapped_column(Text)

    entry: Mapped["JournalEntry"] = relationship("JournalEntry", back_populates="lines")
    account: Mapped["LedgerAccount"] = relationship("LedgerAccount")


class Payment(Base):
    __tablename__ = "payment"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    payment_number: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    payment_type: Mapped[str] = mapped_column(String(20), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    applied_to_fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    applied_to_interest: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    applied_to_principal: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    applied_to_escrow: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    applied_to_advances: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    held_in_suspense: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    period_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("payment_schedule.id"))
    reference_number: Mapped[Optional[str]] = mapped_column(String(100))
    bank_account_last4: Mapped[Optional[str]] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="pending", index=True)
    return_reason: Mapped[Optional[str]] = mapped_column(String(50))
    late_fee_assessed: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    days_late: Mapped[int] = mapped_column(Integer, default=0)
    posted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    journal_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("journal_entry.id"))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    loan: Mapped["Loan"] = relationship("Loan", back_populates="payments")
    journal_entry: Mapped[Optional["JournalEntry"]] = relationship("JournalEntry")


class Fee(Base):
    __tablename__ = "fee"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    fee_type: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    accrual_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    amount_waived: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="accrued")
    waiver_reason: Mapped[Optional[str]] = mapped_column(Text)
    waived_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    waived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    payment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("payment.id"))
    journal_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("journal_entry.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    loan: Mapped["Loan"] = relationship("Loan", back_populates="fees")


class InterestAccrual(Base):
    __tablename__ = "interest_accrual"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    accrual_date: Mapped[date] = mapped_column(Date, nullable=False)
    beginning_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    daily_rate: Mapped[Decimal] = mapped_column(Numeric(12, 10), nullable=False)
    accrued_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    pik_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    day_count_used: Mapped[str] = mapped_column(String(10), nullable=False)
    rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    journal_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("journal_entry.id"))
    is_reversed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
