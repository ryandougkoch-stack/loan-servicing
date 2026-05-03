"""
app/schemas/allocation.py

Pydantic v2 schemas for loan_allocation API request/response serialisation.
A loan can be held across multiple portfolios; each row carries an effective-dated
ownership_pct that sums to 100 across the active set for a given loan.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AllocationItem(BaseModel):
    """One (portfolio, ownership_pct) entry in a new allocation set."""
    portfolio_id: UUID
    ownership_pct: Decimal = Field(gt=0, le=100, max_digits=9, decimal_places=6)


class AllocationUpdate(BaseModel):
    """Request body for PUT /loans/{id}/allocations."""
    effective_date: date
    allocations: list[AllocationItem] = Field(min_length=1)
    notes: Optional[str] = None

    @field_validator("allocations")
    @classmethod
    def _no_duplicate_portfolios(cls, v: list[AllocationItem]) -> list[AllocationItem]:
        ids = [a.portfolio_id for a in v]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate portfolio_id in allocations list")
        return v


class LoanAllocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    loan_id: UUID
    portfolio_id: UUID
    ownership_pct: Decimal
    effective_date: date
    end_date: Optional[date] = None
    notes: Optional[str] = None
    created_by: Optional[UUID] = None
    created_at: datetime


class PortfolioAllocationRead(BaseModel):
    """View from the portfolio side: which loans this portfolio holds and at what %."""
    allocation_id: UUID
    loan_id: UUID
    loan_number: str
    loan_name: Optional[str] = None
    ownership_pct: Decimal
    effective_date: date
    end_date: Optional[date] = None
