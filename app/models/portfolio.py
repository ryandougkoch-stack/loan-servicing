"""
app/models/portfolio.py

ORM models for portfolio, counterparty, and all supporting entities
that were in schema.sql but didn't have Python ORM representations yet.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer,
    Numeric, String, Text, text,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, AuditMixin


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

class Portfolio(Base, AuditMixin):
    __tablename__ = "portfolio"

    code: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    fund_type: Mapped[Optional[str]] = mapped_column(String(50))
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    inception_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    investran_entity_id: Mapped[Optional[str]] = mapped_column(String(100))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    client_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("client.id"))
    loans: Mapped[list["Loan"]] = relationship("Loan", back_populates="portfolio")
    client: Mapped[Optional["Client"]] = relationship("Client", back_populates="portfolios")


# ---------------------------------------------------------------------------
# Counterparty
# ---------------------------------------------------------------------------

class Counterparty(Base, AuditMixin):
    __tablename__ = "counterparty"

    type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    legal_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    short_name: Mapped[Optional[str]] = mapped_column(String(100))
    tax_id: Mapped[Optional[str]] = mapped_column(String(50))
    entity_type: Mapped[Optional[str]] = mapped_column(String(50))
    jurisdiction: Mapped[Optional[str]] = mapped_column(String(2))
    state_of_formation: Mapped[Optional[str]] = mapped_column(String(2))
    address_line1: Mapped[Optional[str]] = mapped_column(String(200))
    address_line2: Mapped[Optional[str]] = mapped_column(String(200))
    city: Mapped[Optional[str]] = mapped_column(String(100))
    state: Mapped[Optional[str]] = mapped_column(String(50))
    postal_code: Mapped[Optional[str]] = mapped_column(String(20))
    country: Mapped[str] = mapped_column(String(2), default="US")
    primary_contact_name: Mapped[Optional[str]] = mapped_column(String(200))
    primary_contact_email: Mapped[Optional[str]] = mapped_column(String(200))
    primary_contact_phone: Mapped[Optional[str]] = mapped_column(String(50))
    kyc_status: Mapped[str] = mapped_column(String(20), default="pending")
    kyc_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    kyc_reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(100))
    notes: Mapped[Optional[str]] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Loan supporting entities
# ---------------------------------------------------------------------------

class LoanGuarantor(Base):
    __tablename__ = "loan_guarantor"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False)
    guarantor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("counterparty.id"), nullable=False)
    guarantee_type: Mapped[Optional[str]] = mapped_column(String(30))
    guarantee_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    effective_date: Mapped[Optional[date]] = mapped_column(Date)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    loan: Mapped["Loan"] = relationship("Loan", back_populates="guarantors")
    guarantor: Mapped["Counterparty"] = relationship("Counterparty")


class Collateral(Base, AuditMixin):
    __tablename__ = "collateral"

    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    collateral_type: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    valuation_date: Mapped[Optional[date]] = mapped_column(Date)
    valuation_source: Mapped[Optional[str]] = mapped_column(String(100))
    lien_position: Mapped[Optional[int]] = mapped_column(Integer)
    ucc_filed: Mapped[bool] = mapped_column(Boolean, default=False)
    ucc_filing_number: Mapped[Optional[str]] = mapped_column(String(100))
    ucc_filed_date: Mapped[Optional[date]] = mapped_column(Date)
    ucc_expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    insurance_required: Mapped[bool] = mapped_column(Boolean, default=False)
    insurance_provider: Mapped[Optional[str]] = mapped_column(String(200))
    insurance_policy_number: Mapped[Optional[str]] = mapped_column(String(100))
    insurance_expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    insurance_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    loan: Mapped["Loan"] = relationship("Loan", back_populates="collaterals")


class Covenant(Base, AuditMixin):
    __tablename__ = "covenant"

    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    covenant_type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    metric: Mapped[Optional[str]] = mapped_column(String(100))
    threshold_operator: Mapped[Optional[str]] = mapped_column(String(5))
    threshold_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    measurement_frequency: Mapped[Optional[str]] = mapped_column(String(20))
    next_test_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    last_test_date: Mapped[Optional[date]] = mapped_column(Date)
    last_test_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    last_test_result: Mapped[Optional[str]] = mapped_column(String(20))
    waiver_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    waiver_expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    loan: Mapped["Loan"] = relationship("Loan", back_populates="covenants")


class RateReset(Base):
    __tablename__ = "rate_reset"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    reset_date: Mapped[date] = mapped_column(Date, nullable=False)
    index_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    spread: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    new_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    floor_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    cap_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    loan: Mapped["Loan"] = relationship("Loan", back_populates="rate_resets")


class LoanModification(Base):
    __tablename__ = "loan_modification"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    modification_type: Mapped[str] = mapped_column(String(30), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    prior_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    new_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6))
    prior_maturity: Mapped[Optional[date]] = mapped_column(Date)
    new_maturity: Mapped[Optional[date]] = mapped_column(Date)
    prior_payment_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    new_payment_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    principal_adjusted: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    description: Mapped[Optional[str]] = mapped_column(Text)
    document_reference: Mapped[Optional[str]] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    loan: Mapped["Loan"] = relationship("Loan", back_populates="modifications")


# ---------------------------------------------------------------------------
# Collections & workout
# ---------------------------------------------------------------------------

class DelinquencyRecord(Base):
    __tablename__ = "delinquency_record"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    days_past_due: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delinquency_bucket: Mapped[str] = mapped_column(String(10), nullable=False)
    principal_past_due: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    interest_past_due: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    fees_past_due: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkoutPlan(Base, AuditMixin):
    __tablename__ = "workout_plan"

    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False)
    plan_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    start_date: Mapped[Optional[date]] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date)
    terms_description: Mapped[Optional[str]] = mapped_column(Text)
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


# ---------------------------------------------------------------------------
# Payoff quote
# ---------------------------------------------------------------------------

class PayoffQuote(Base):
    __tablename__ = "payoff_quote"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), nullable=False, index=True)
    quote_date: Mapped[date] = mapped_column(Date, nullable=False)
    good_through_date: Mapped[date] = mapped_column(Date, nullable=False)
    principal_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    accrued_interest: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    per_diem: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fees_outstanding: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    prepayment_penalty: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    escrow_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    total_payoff: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="active")
    generated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Workflow task
# ---------------------------------------------------------------------------

class WorkflowTask(Base, AuditMixin):
    __tablename__ = "workflow_task"

    loan_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), index=True)
    portfolio_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("portfolio.id"))
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="normal")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    sla_hours: Mapped[Optional[int]] = mapped_column(Integer)
    escalated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    escalated_to: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------

class Document(Base):
    __tablename__ = "document"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("loan.id"), index=True)
    counterparty_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("counterparty.id"))
    portfolio_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("portfolio.id"))
    document_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    file_name: Mapped[str] = mapped_column(String(300), nullable=False)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_provider: Mapped[str] = mapped_column(String(20), default="s3")
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current_version: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    prior_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("document.id"))
    effective_date: Mapped[Optional[date]] = mapped_column(Date)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    requires_esign: Mapped[bool] = mapped_column(Boolean, default=False)
    esign_status: Mapped[Optional[str]] = mapped_column(String(20))
    esign_provider: Mapped[Optional[str]] = mapped_column(String(50))
    esign_envelope_id: Mapped[Optional[str]] = mapped_column(String(200))
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)



class Client(Base, AuditMixin):
    __tablename__ = "client"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[Optional[str]] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    notes: Mapped[Optional[str]] = mapped_column(Text)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    portfolios: Mapped[list["Portfolio"]] = relationship("Portfolio", back_populates="client")
