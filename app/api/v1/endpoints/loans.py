"""
app/api/v1/endpoints/loans.py

Loan CRUD endpoints.
All endpoints are tenant-scoped via the get_db dependency.
"""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import get_db, get_current_user_id, require_min_role
from app.core.exceptions import LoanNotFoundError
from app.core.security import TokenPayload
from app.models.loan import Loan
from app.schemas.loan import LoanCreate, LoanRead, LoanSummary, LoanStatusUpdate
from app.services.loan_service import LoanService

router = APIRouter(prefix="/loans", tags=["loans"])


@router.get("", response_model=dict)
async def list_loans(
    portfolio_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    borrower_id: Optional[UUID] = Query(None),
    client_id: Optional[UUID] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """
    List loans with optional filters. Paginated.
    Returns total count for pagination UI.
    """
    service = LoanService(db)
    loans, total = await service.list_loans(
        portfolio_id=portfolio_id,
        status_filter=status,
        borrower_id=borrower_id,
        client_id=client_id,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [LoanSummary.model_validate(loan) for loan in loans],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.post("", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
async def create_loan(
    payload: LoanCreate,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Board a new loan. Status starts as 'boarding'.
    Triggers validation rules before persisting.
    """
    service = LoanService(db)
    loan = await service.create_loan(payload, created_by=user_id)

    from app.services.activity_service import ActivityService
    activity_svc = ActivityService(db)
    await activity_svc.log(
        loan_id=loan.id,
        event_type="boarded",
        event_summary=f"Loan boarded — {loan.loan_number} ${loan.original_balance} @ {float(loan.coupon_rate or 0)*100:.3f}%",
        field_changes={
            "original_balance": {"new": str(loan.original_balance)},
            "coupon_rate": {"new": str(loan.coupon_rate)},
            "maturity_date": {"new": str(loan.maturity_date)},
            "rate_type": {"new": loan.rate_type},
        },
        user_id=user_id,
    )
    return LoanRead.model_validate(loan)


@router.get("/{loan_id}", response_model=LoanRead)
async def get_loan(
    loan_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """Retrieve a single loan with all related data."""
    service = LoanService(db)
    loan = await service.get_loan_or_404(loan_id)
    return LoanRead.model_validate(loan)


@router.patch("/{loan_id}/status", response_model=LoanRead)
async def update_loan_status(
    loan_id: UUID,
    payload: LoanStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Transition a loan to a new status.
    Validates allowed status transitions (e.g. boarding → funded, not funded → boarding).
    """
    service = LoanService(db)
    loan_before = await service.get_loan_or_404(loan_id)
    prior_status = loan_before.status
    await service.update_status(loan_id, payload.status, updated_by=user_id)
    loan = await service.get_loan_or_404(loan_id)

    from app.services.activity_service import ActivityService
    activity_svc = ActivityService(db)
    await activity_svc.log(
        loan_id=loan_id,
        event_type="status_changed",
        event_summary=f"Status: {prior_status} -> {payload.status}",
        field_changes={"status": {"prior": prior_status, "new": payload.status}},
        user_id=user_id,
    )

    return LoanRead.model_validate(loan)


@router.get("/{loan_id}/payment-schedule", response_model=List[dict])
async def get_payment_schedule(
    loan_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    """Return the amortization schedule for a loan."""
    service = LoanService(db)
    await service.get_loan_or_404(loan_id)
    schedule = await service.get_payment_schedule(loan_id)
    return schedule


@router.post("/{loan_id}/payment-schedule/generate", response_model=dict)
async def generate_payment_schedule(
    loan_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    (Re)generate the payment schedule for a loan using the amortization engine.
    Existing open periods are superseded; paid periods are preserved.
    """
    service = LoanService(db)
    loan = await service.get_loan_or_404(loan_id)
    count = await service.generate_schedule(loan)
    return {"periods_generated": count, "loan_id": str(loan_id)}


@router.post("/{loan_id}/modifications", response_model=dict)
async def create_modification(
    loan_id: UUID,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """Apply a loan modification — rate change or maturity extension."""
    from datetime import date as date_type
    from decimal import Decimal as Dec

    service = LoanService(db)

    mod_type = payload.get("modification_type")
    eff_date = date_type.fromisoformat(payload["effective_date"])
    new_rate = Dec(str(payload["new_rate"])) if payload.get("new_rate") else None
    new_maturity = date_type.fromisoformat(payload["new_maturity_date"]) if payload.get("new_maturity_date") else None
    new_freq = payload.get("new_payment_frequency")

    mod = await service.create_modification(
        loan_id=loan_id,
        modification_type=mod_type,
        effective_date=eff_date,
        created_by=user_id,
        new_rate=new_rate,
        new_maturity_date=new_maturity,
        new_payment_frequency=new_freq,
        description=payload.get("description"),
        notes=payload.get("notes"),
    )

    from app.services.activity_service import ActivityService
    activity_svc = ActivityService(db)
    changes = {}
    if mod_type == "rate_change" and new_rate is not None:
        changes["coupon_rate"] = {"new": str(new_rate)}
        summary = f"Rate change to {float(new_rate)*100:.3f}% (effective {eff_date})"
    elif mod_type == "maturity_extension" and new_maturity is not None:
        changes["maturity_date"] = {"new": str(new_maturity)}
        if new_freq:
            changes["payment_frequency"] = {"new": new_freq}
        summary = f"Maturity extended to {new_maturity}"
    else:
        summary = f"Modification applied: {mod_type}"
    await activity_svc.log(
        loan_id=loan_id,
        event_type="modification_applied",
        event_summary=summary,
        field_changes=changes,
        user_id=user_id,
    )

    return {
        "modification_id": str(mod.id),
        "loan_id": str(loan_id),
        "modification_type": mod_type,
        "status": mod.status,
        "effective_date": str(eff_date),
    }


@router.post("/{loan_id}/payoff-quote", response_model=dict)
async def generate_payoff_quote(
    loan_id: UUID,
    payload: dict = None,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """Generate a payoff quote with breakdown of all components."""
    from datetime import date as date_type
    from app.services.payoff_service import PayoffService
    payload = payload or {}
    payoff_date = None
    if payload.get("payoff_date"):
        payoff_date = date_type.fromisoformat(payload["payoff_date"])
    svc = PayoffService(db)
    return await svc.generate_quote(loan_id, payoff_date)


@router.post("/{loan_id}/payoff", response_model=dict)
async def process_payoff(
    loan_id: UUID,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """Process a payoff payment and close the loan."""
    from datetime import date as date_type
    from decimal import Decimal as Dec
    from app.services.payoff_service import PayoffService
    payoff_date = date_type.fromisoformat(payload["payoff_date"])
    amount = Dec(str(payload["amount_received"]))
    svc = PayoffService(db)
    result = await svc.process_payoff(loan_id, payoff_date, amount, user_id)

    from app.services.activity_service import ActivityService
    activity_svc = ActivityService(db)
    await activity_svc.log(
        loan_id=loan_id,
        event_type="payoff_processed",
        event_summary=f"Loan paid off — ${amount} received on {payoff_date}",
        field_changes={"status": {"prior": "funded/modified", "new": "paid_off"},
                       "paid_off_at": {"new": str(payoff_date)},
                       "amount_received": {"new": str(amount)}},
        user_id=user_id,
    )
    return result


@router.get("/{loan_id}/activity", response_model=list[dict])
async def get_loan_activity(
    loan_id: UUID,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """Get the activity log for a loan."""
    from app.services.activity_service import ActivityService
    svc = ActivityService(db)
    return await svc.get_activity(loan_id, limit)
