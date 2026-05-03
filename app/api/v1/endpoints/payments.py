"""
app/api/v1/endpoints/payments.py

Payment posting and retrieval endpoints.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_id, require_min_role
from app.core.security import TokenPayload
from app.schemas.payment import PaymentCreate, PaymentRead, PaymentReversal
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("", response_model=dict)
async def list_payments(
    loan_id: Optional[UUID] = Query(None),
    portfolio_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    service = PaymentService(db)
    payments, total = await service.list_payments(
        loan_id=loan_id,
        portfolio_id=portfolio_id,
        status_filter=status,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [PaymentRead.model_validate(p) for p in payments],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
async def post_payment(
    payload: PaymentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Post a payment to a loan.

    The service layer will:
    1. Validate the loan is in a payable status
    2. Calculate days late and assess late fees if applicable
    3. Apply the configurable waterfall (fees → interest → principal → escrow)
    4. Create a double-entry journal entry
    5. Update loan balances
    6. Update payment schedule period status
    """
    service = PaymentService(db)
    payment = await service.post_payment(payload, posted_by=user_id)

    from app.services.activity_service import ActivityService
    activity_svc = ActivityService(db)
    await activity_svc.log(
        loan_id=payment.loan_id,
        event_type="payment_posted",
        event_summary=f"Payment {payment.payment_number} posted — ${payment.gross_amount} ({payment.payment_method})",
        field_changes={
            "applied_to_interest": {"new": str(payment.applied_to_interest)},
            "applied_to_principal": {"new": str(payment.applied_to_principal)},
            "applied_to_fees": {"new": str(payment.applied_to_fees)},
        },
        user_id=user_id,
    )
    return PaymentRead.model_validate(payment)


@router.get("/{payment_id}", response_model=PaymentRead)
async def get_payment(
    payment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    service = PaymentService(db)
    payment = await service.get_payment_or_404(payment_id)
    return PaymentRead.model_validate(payment)


@router.post("/{payment_id}/reverse", response_model=PaymentRead)
async def reverse_payment(
    payment_id: UUID,
    payload: PaymentReversal,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("finance")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Reverse a posted payment. Creates offsetting journal entries.
    Requires finance role or above.
    Reversals are themselves audited and cannot be reversed.
    """
    service = PaymentService(db)
    reversed_payment = await service.reverse_payment(
        payment_id, reason=payload.reason, reversed_by=user_id
    )
    return PaymentRead.model_validate(reversed_payment)
