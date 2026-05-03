"""
app/schemas/payment.py

Pydantic schemas for payment API serialisation.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


VALID_PAYMENT_TYPES = {"regular", "prepayment", "payoff", "partial", "fee_only", "escrow_only", "returned", "reversal"}
VALID_PAYMENT_METHODS = {"ach", "wire", "check", "lockbox", "manual", "internal_transfer"}


class PaymentCreate(BaseModel):
    loan_id: UUID
    payment_type: str
    payment_method: str
    received_date: date
    effective_date: date
    gross_amount: Decimal = Field(gt=0)
    reference_number: Optional[str] = None
    bank_account_last4: Optional[str] = Field(None, min_length=4, max_length=4)
    period_id: Optional[UUID] = None    # link to specific schedule period
    notes: Optional[str] = None

    @model_validator(mode="after")
    def validate_payment(self) -> "PaymentCreate":
        if self.payment_type not in VALID_PAYMENT_TYPES:
            raise ValueError(f"Invalid payment_type: {self.payment_type}")
        if self.payment_method not in VALID_PAYMENT_METHODS:
            raise ValueError(f"Invalid payment_method: {self.payment_method}")
        if self.effective_date < self.received_date:
            raise ValueError("effective_date cannot be before received_date")
        return self


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    loan_id: UUID
    payment_number: str
    payment_type: str
    payment_method: str
    received_date: date
    effective_date: date
    gross_amount: Decimal
    applied_to_fees: Decimal
    applied_to_interest: Decimal
    applied_to_principal: Decimal
    applied_to_escrow: Decimal
    applied_to_advances: Decimal
    held_in_suspense: Decimal
    status: str
    return_reason: Optional[str]
    late_fee_assessed: Decimal
    days_late: int
    reference_number: Optional[str]
    journal_entry_id: Optional[UUID]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime


class PaymentReversal(BaseModel):
    reason: str = Field(min_length=10, description="Reason for reversal — required for audit trail")
