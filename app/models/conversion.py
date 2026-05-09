"""
app/models/conversion.py

ORM models for mid-term loan boarding (loan conversion).

Two tables:
  - LoanConversion: one row per converted loan, captures opening balances
    + prior-servicer references at the cutover date (as_of_date).
  - ConversionBatch: one row per uploaded file in the batch import flow.
    A LoanConversion row may reference its batch via batch_id (nullable —
    single-loan conversions have no batch).

Schema lives in scripts/migrations/0003_loan_conversion.sql.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sqlalchemy import func

from app.db.base import Base, AuditMixin


class ConversionBatch(Base, AuditMixin):
    __tablename__ = "conversion_batch"

    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[Optional[str]] = mapped_column(Text)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validation_report: Mapped[Optional[dict]] = mapped_column(JSONB)
    commit_report: Mapped[Optional[dict]] = mapped_column(JSONB)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    conversions: Mapped[list["LoanConversion"]] = relationship("LoanConversion", back_populates="batch")


class LoanConversion(Base, AuditMixin):
    __tablename__ = "loan_conversion"

    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, unique=True)
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("conversion_batch.id"))

    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)

    current_principal: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    accrued_interest: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    accrued_fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))

    last_payment_date: Mapped[Optional[date]] = mapped_column(Date)
    last_payment_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    next_due_date: Mapped[Optional[date]] = mapped_column(Date)

    paid_to_date_principal: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    paid_to_date_interest: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    paid_to_date_fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))

    prior_servicer_name: Mapped[Optional[str]] = mapped_column(Text)
    prior_servicer_loan_id: Mapped[Optional[str]] = mapped_column(Text)
    conversion_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))

    suspense_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("ledger_account.id"))
    # Not an FK — syndicated conversions post N journal entries (one per
    # allocation). Holds the JE id only when the conversion is non-syndicated.
    opening_journal_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))

    posted_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    batch: Mapped[Optional["ConversionBatch"]] = relationship("ConversionBatch", back_populates="conversions")

    def __repr__(self) -> str:
        return f"<LoanConversion loan={self.loan_id} as_of={self.as_of_date} principal=${self.current_principal:,.2f}>"
