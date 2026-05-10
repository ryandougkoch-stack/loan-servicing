"""
app/web/router.py

Server-rendered HTMX UI for the loan-servicing platform.

This router handles the human-facing pages — login, loan list, single-loan
boarding form, batch conversion intake. It does NOT replace the JSON API
under /api/v1; both can run side-by-side.

Auth: cookie-based (lsp_access). Web pages call the same service layer the
API uses, so business logic stays in one place.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.core.security import TokenPayload, create_access_token, create_refresh_token, decode_token
from app.db.session import get_shared_session
from app.models.conversion import ConversionBatch
from app.models.loan import Loan
from app.models.portfolio import Counterparty, Portfolio
from app.schemas.auth import LoginRequest
from app.schemas.loan import LoanCreate, LoanConversionPayload
from app.services.activity_service import ActivityService
from app.services.auth_service import AuthService
from app.services.batch_conversion_service import BatchConversionService
from app.services.loan_service import LoanService
from app.web.dependencies import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    WebAuthRedirect,
    get_web_db,
    get_web_user,
    get_web_user_id,
    get_web_user_optional,
)

logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/web", include_in_schema=False)


# Cookie lifetimes — match JWT settings so they expire in lockstep.
def _cookie_kwargs(max_age: int) -> dict:
    return dict(
        httponly=True,
        samesite="lax",
        secure=False,        # set True behind HTTPS in production
        max_age=max_age,
        path="/",
    )


# ---------------------------------------------------------------------------
# Root + health redirect
# ---------------------------------------------------------------------------

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def web_root(user: Optional[TokenPayload] = Depends(get_web_user_optional)):
    return RedirectResponse(url="/web/loans" if user else "/web/login",
                            status_code=status.HTTP_302_FOUND)


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    user: Optional[TokenPayload] = Depends(get_web_user_optional),
    error: Optional[str] = None,
):
    if user:
        return RedirectResponse(url="/web/loans", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    tenant_slug: str = Form(...),
):
    """Authenticate via the same AuthService the API uses; set HttpOnly cookies."""
    from app.core.config import settings

    async for shared_session in get_shared_session():
        svc = AuthService(shared_session)
        try:
            token_pair, _profile = await svc.login(
                LoginRequest(email=email, password=password, tenant_slug=tenant_slug),
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
        except Exception as e:
            logger.warning("web_login_failed", email=email, error=str(e))
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Login failed. Check email, password, and tenant."},
                status_code=400,
            )

    response = RedirectResponse(url="/web/loans", status_code=303)
    response.set_cookie(ACCESS_COOKIE, token_pair.access_token,
                        **_cookie_kwargs(settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60))
    response.set_cookie(REFRESH_COOKIE, token_pair.refresh_token,
                        **_cookie_kwargs(settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400))
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/web/login", status_code=303)
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")
    return response


# ---------------------------------------------------------------------------
# Loans list
# ---------------------------------------------------------------------------

@router.get("/loans", response_class=HTMLResponse)
async def loans_page(
    request: Request,
    user: TokenPayload = Depends(get_web_user),
    db: AsyncSession = Depends(get_web_db),
):
    svc = LoanService(db)
    loans, total = await svc.list_loans(page=1, page_size=100)
    return templates.TemplateResponse(
        "loans/list.html",
        {"request": request, "user": user, "loans": loans, "total": total},
    )


# ---------------------------------------------------------------------------
# Single-loan boarding (with optional conversion)
# ---------------------------------------------------------------------------

@router.get("/loans/new", response_class=HTMLResponse)
async def new_loan_page(
    request: Request,
    user: TokenPayload = Depends(get_web_user),
    db: AsyncSession = Depends(get_web_db),
):
    portfolios = (await db.execute(select(Portfolio).order_by(Portfolio.code))).scalars().all()
    borrowers = (await db.execute(
        select(Counterparty).where(Counterparty.type == "borrower").order_by(Counterparty.legal_name)
    )).scalars().all()
    return templates.TemplateResponse(
        "loans/new.html",
        {"request": request, "user": user, "portfolios": portfolios, "borrowers": borrowers},
    )


def _decimal(v: Optional[str]) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    return Decimal(v)


def _rate(v: Optional[str]) -> Optional[Decimal]:
    """Form input: '8.5' → 0.085, '0.085' → 0.085 (anything > 1 treated as percent)."""
    d = _decimal(v)
    if d is None:
        return None
    return d / 100 if d > 1 else d


def _date(v: Optional[str]) -> Optional[date]:
    if not v:
        return None
    return date.fromisoformat(v)


@router.post("/loans", response_class=HTMLResponse)
async def create_loan_submit(
    request: Request,
    user: TokenPayload = Depends(get_web_user),
    user_id: UUID = Depends(get_web_user_id),
    db: AsyncSession = Depends(get_web_db),
    # Loan core
    portfolio_id: UUID = Form(...),
    primary_borrower_id: UUID = Form(...),
    loan_number: Optional[str] = Form(None),
    loan_name: Optional[str] = Form(None),
    original_balance: str = Form(...),
    rate_type: str = Form(...),
    coupon_rate: Optional[str] = Form(None),
    day_count: str = Form("ACT/360"),
    origination_date: str = Form(...),
    maturity_date: str = Form(...),
    payment_frequency: str = Form("QUARTERLY"),
    amortization_type: str = Form("bullet"),
    grace_period_days: int = Form(5),
    # Conversion (all optional; presence of conversion_enabled flips path)
    conversion_enabled: Optional[str] = Form(None),
    as_of_date: Optional[str] = Form(None),
    current_principal: Optional[str] = Form(None),
    accrued_interest: Optional[str] = Form(None),
    accrued_fees: Optional[str] = Form(None),
    last_payment_date: Optional[str] = Form(None),
    last_payment_amount: Optional[str] = Form(None),
    next_due_date: Optional[str] = Form(None),
    paid_to_date_principal: Optional[str] = Form(None),
    paid_to_date_interest: Optional[str] = Form(None),
    paid_to_date_fees: Optional[str] = Form(None),
    prior_servicer_name: Optional[str] = Form(None),
    prior_servicer_loan_id: Optional[str] = Form(None),
):
    """Single-loan boarding form submit. Renders the result fragment."""
    conv_payload: Optional[LoanConversionPayload] = None
    if conversion_enabled == "on":
        try:
            conv_payload = LoanConversionPayload(
                as_of_date=_date(as_of_date),
                current_principal=_decimal(current_principal) or Decimal("0"),
                accrued_interest=_decimal(accrued_interest) or Decimal("0"),
                accrued_fees=_decimal(accrued_fees) or Decimal("0"),
                last_payment_date=_date(last_payment_date),
                last_payment_amount=_decimal(last_payment_amount),
                next_due_date=_date(next_due_date),
                paid_to_date_principal=_decimal(paid_to_date_principal) or Decimal("0"),
                paid_to_date_interest=_decimal(paid_to_date_interest) or Decimal("0"),
                paid_to_date_fees=_decimal(paid_to_date_fees) or Decimal("0"),
                prior_servicer_name=prior_servicer_name or None,
                prior_servicer_loan_id=prior_servicer_loan_id or None,
            )
        except Exception as e:
            return templates.TemplateResponse(
                "loans/_create_result.html",
                {"request": request, "ok": False, "error": f"Invalid conversion fields: {e}"},
                status_code=400,
            )

    try:
        payload = LoanCreate(
            portfolio_id=portfolio_id,
            primary_borrower_id=primary_borrower_id,
            loan_number=loan_number or None,
            loan_name=loan_name or None,
            original_balance=_decimal(original_balance),
            rate_type=rate_type,
            coupon_rate=_rate(coupon_rate),
            day_count=day_count,
            origination_date=_date(origination_date),
            maturity_date=_date(maturity_date),
            payment_frequency=payment_frequency,
            amortization_type=amortization_type,
            grace_period_days=grace_period_days,
            conversion=conv_payload,
        )
    except Exception as e:
        return templates.TemplateResponse(
            "loans/_create_result.html",
            {"request": request, "ok": False, "error": f"Validation: {e}"},
            status_code=400,
        )

    svc = LoanService(db)
    activity_svc = ActivityService(db)
    try:
        if payload.conversion is not None:
            loan = await svc.create_converted_loan(payload, created_by=user_id)
            await activity_svc.log(
                loan_id=loan.id, event_type="converted",
                event_summary=f"Loan converted from prior servicer — {loan.loan_number}",
                field_changes={
                    "as_of_date": {"new": str(payload.conversion.as_of_date)},
                    "current_principal": {"new": str(payload.conversion.current_principal)},
                    "boarding_type": {"new": "converted"},
                }, user_id=user_id,
            )
        else:
            loan = await svc.create_loan(payload, created_by=user_id)
            await activity_svc.log(
                loan_id=loan.id, event_type="boarded",
                event_summary=f"Loan boarded — {loan.loan_number}",
                field_changes={"original_balance": {"new": str(loan.original_balance)}},
                user_id=user_id,
            )
        await db.flush()
    except (ConflictError, ValidationError) as e:
        return templates.TemplateResponse(
            "loans/_create_result.html",
            {"request": request, "ok": False, "error": str(e)},
            status_code=400,
        )

    return templates.TemplateResponse(
        "loans/_create_result.html",
        {"request": request, "ok": True, "loan": loan, "converted": conv_payload is not None},
    )


# ---------------------------------------------------------------------------
# Batch conversion intake
# ---------------------------------------------------------------------------

@router.get("/conversions", response_class=HTMLResponse)
async def conversions_page(
    request: Request,
    user: TokenPayload = Depends(get_web_user),
    db: AsyncSession = Depends(get_web_db),
):
    batches = (await db.execute(
        select(ConversionBatch).order_by(ConversionBatch.uploaded_at.desc()).limit(20)
    )).scalars().all()
    return templates.TemplateResponse(
        "conversions/upload.html",
        {"request": request, "user": user, "batches": batches},
    )


@router.post("/conversions/validate", response_class=HTMLResponse)
async def conversions_validate(
    request: Request,
    file: UploadFile = File(...),
    user: TokenPayload = Depends(get_web_user),
    user_id: UUID = Depends(get_web_user_id),
    db: AsyncSession = Depends(get_web_db),
):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return templates.TemplateResponse(
            "conversions/_validation_report.html",
            {"request": request, "error": "Only .xlsx files are supported"},
            status_code=400,
        )
    body = await file.read()
    if not body:
        return templates.TemplateResponse(
            "conversions/_validation_report.html",
            {"request": request, "error": "Empty file"},
            status_code=400,
        )

    svc = BatchConversionService(db)
    try:
        report = await svc.parse_and_validate(body, file.filename, uploaded_by=user_id)
        await db.flush()
    except ValidationError as e:
        return templates.TemplateResponse(
            "conversions/_validation_report.html",
            {"request": request, "error": str(e)},
            status_code=400,
        )

    return templates.TemplateResponse(
        "conversions/_validation_report.html",
        {"request": request, "report": report},
    )


@router.post("/conversions/{batch_id}/commit", response_class=HTMLResponse)
async def conversions_commit(
    request: Request,
    batch_id: UUID,
    user: TokenPayload = Depends(get_web_user),
    db: AsyncSession = Depends(get_web_db),
):
    batch = (await db.execute(
        select(ConversionBatch).where(ConversionBatch.id == batch_id)
    )).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, "batch not found")
    if batch.status != "validated":
        return templates.TemplateResponse(
            "conversions/_batch_status.html",
            {"request": request, "batch": batch, "error":
             f"batch is in status '{batch.status}', expected 'validated'"},
            status_code=409,
        )
    batch.status = "committing"
    await db.flush()

    from app.workers.tasks.batch_conversion import run_batch_commit
    run_batch_commit.delay(user.tenant_slug, str(batch_id))

    return templates.TemplateResponse(
        "conversions/_batch_status.html",
        {"request": request, "batch": batch, "polling": True},
    )


@router.get("/conversions/{batch_id}/status", response_class=HTMLResponse)
async def conversions_status(
    request: Request,
    batch_id: UUID,
    user: TokenPayload = Depends(get_web_user),
    db: AsyncSession = Depends(get_web_db),
):
    batch = (await db.execute(
        select(ConversionBatch).where(ConversionBatch.id == batch_id)
    )).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, "batch not found")
    polling = batch.status in ("committing",)
    return templates.TemplateResponse(
        "conversions/_batch_status.html",
        {"request": request, "batch": batch, "polling": polling},
    )
