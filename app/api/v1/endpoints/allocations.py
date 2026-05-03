"""
app/api/v1/endpoints/allocations.py

Loan-allocation endpoints: read/write the syndication % set for a loan,
and look up a portfolio's current loan holdings.
"""
from datetime import date as date_type
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user_id, get_db, require_min_role
from app.core.security import TokenPayload
from app.schemas.allocation import (
    AllocationUpdate,
    LoanAllocationRead,
    PortfolioAllocationRead,
)
from app.services.loan_allocation_service import LoanAllocationService

router = APIRouter(tags=["allocations"])


@router.get("/loans/{loan_id}/allocations", response_model=list[LoanAllocationRead])
async def list_loan_allocations(
    loan_id: UUID,
    as_of: Optional[date_type] = Query(
        None, description="Default: today. Show allocations active on this date."
    ),
    include_history: bool = Query(
        False, description="If true, return all rows (current + historical)."
    ),
    db: AsyncSession = Depends(get_db),
    _user: TokenPayload = Depends(require_min_role("reporting")),
):
    """Allocations for a loan. Defaults to active-as-of-today."""
    svc = LoanAllocationService(db)
    rows = (
        await svc.get_history(loan_id)
        if include_history
        else await svc.get_active_allocations(loan_id, as_of_date=as_of)
    )
    return [LoanAllocationRead.model_validate(r) for r in rows]


@router.put("/loans/{loan_id}/allocations", response_model=list[LoanAllocationRead])
async def update_loan_allocations(
    loan_id: UUID,
    payload: AllocationUpdate,
    db: AsyncSession = Depends(get_db),
    _user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Replace allocations effective from payload.effective_date.
    Body: {effective_date, allocations: [{portfolio_id, ownership_pct}], notes?}
    Allocations must sum to 100. End-dates current rows; supersedes any
    future-dated changes whose effective_date >= payload.effective_date.
    """
    svc = LoanAllocationService(db)
    rows = await svc.update_allocations(
        loan_id=loan_id,
        new_set=payload.allocations,
        effective_date=payload.effective_date,
        user_id=user_id,
        notes=payload.notes,
    )
    return [LoanAllocationRead.model_validate(r) for r in rows]


@router.get(
    "/portfolios/{portfolio_id}/allocations",
    response_model=list[PortfolioAllocationRead],
)
async def list_portfolio_allocations(
    portfolio_id: UUID,
    as_of: Optional[date_type] = Query(None),
    db: AsyncSession = Depends(get_db),
    _user: TokenPayload = Depends(require_min_role("reporting")),
):
    """Loans + ownership_pct held by this portfolio, active on as_of (default today)."""
    svc = LoanAllocationService(db)
    return await svc.get_loans_for_portfolio(portfolio_id, as_of_date=as_of)
