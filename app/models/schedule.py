"""
app/models/schedule.py

ORM model for payment_schedule and related tables.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PaymentSchedule(Base):
    __tablename__ = "payment_schedule"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    loan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True
    )
    period_number: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scheduled_principal: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    scheduled_interest: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    scheduled_fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    scheduled_escrow: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    days_in_period: Mapped[Optional[int]] = mapped_column(Integer)
    interest_rate_used: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    beginning_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    ending_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="open", index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    loan: Mapped["Loan"] = relationship("Loan", back_populates="payment_schedule")

    def __repr__(self) -> str:
        return f"<PaymentSchedule loan={self.loan_id} period={self.period_number} due={self.due_date} status={self.status}>"
