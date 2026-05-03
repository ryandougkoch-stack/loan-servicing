from datetime import datetime
"""
app/api/v1/endpoints/portfolios.py
Portfolio CRUD with KPI rollup at the portfolio level.
Supports filtering by client_id and active/closed status.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenPayload
from app.api.dependencies import get_current_user_id, get_db, require_min_role

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


@router.get("", response_model=list[dict])
async def list_portfolios(
    client_id: Optional[UUID] = None,
    include_closed: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """List portfolios, optionally filtered by client and status, with KPI rollups."""
    where_clauses = []
    params = {}
    if client_id:
        where_clauses.append("p.client_id = :cid")
        params["cid"] = client_id
    if not include_closed:
        where_clauses.append("p.status = 'active'")
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Phase 2: KPIs roll up via loan_allocation. Dollar fields are prorated by
    # ownership_pct; loan counts stay un-prorated (each loan counts once per fund
    # it's allocated to). Active allocations only — uses end_date IS NULL.
    sql = f"""
        SELECT
            p.id, p.code, p.name, p.fund_type, p.base_currency, p.inception_date,
            p.status, p.notes, p.closed_at, p.client_id,
            c.name AS client_name,
            COUNT(DISTINCT l.id) FILTER (WHERE l.status NOT IN ('paid_off','written_off')) AS active_loan_count,
            COUNT(DISTINCT l.id) AS total_loan_count,
            COALESCE(SUM(l.commitment_amount * a.ownership_pct / 100) FILTER (WHERE l.status NOT IN ('paid_off','written_off')), 0) AS total_commitment,
            COALESCE(SUM(l.current_principal * a.ownership_pct / 100), 0) AS total_principal_outstanding,
            COALESCE(SUM(l.accrued_interest * a.ownership_pct / 100), 0) AS total_accrued_interest,
            COALESCE(SUM(l.accrued_fees * a.ownership_pct / 100), 0) AS total_accrued_fees,
            COUNT(DISTINCT l.id) FILTER (WHERE l.status = 'delinquent') AS delinquent_loan_count,
            COUNT(DISTINCT l.id) FILTER (WHERE l.status = 'default') AS default_loan_count
        FROM portfolio p
        LEFT JOIN client c ON c.id = p.client_id
        LEFT JOIN loan_allocation a ON a.portfolio_id = p.id AND a.end_date IS NULL
        LEFT JOIN loan l ON l.id = a.loan_id
        {where}
        GROUP BY p.id, p.code, p.name, p.fund_type, p.base_currency, p.inception_date,
                 p.status, p.notes, p.closed_at, p.client_id, c.name
        ORDER BY p.status, p.name
    """
    result = await db.execute(text(sql), params)
    rows = result.fetchall()
    return [
        {
            "id": str(r.id),
            "code": r.code,
            "name": r.name,
            "fund_type": r.fund_type,
            "base_currency": r.base_currency,
            "inception_date": str(r.inception_date) if r.inception_date else None,
            "status": r.status,
            "notes": r.notes,
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            "client_id": str(r.client_id) if r.client_id else None,
            "client_name": r.client_name,
            "active_loan_count": r.active_loan_count,
            "total_loan_count": r.total_loan_count,
            "total_commitment": str(r.total_commitment),
            "total_principal_outstanding": str(r.total_principal_outstanding),
            "total_accrued_interest": str(r.total_accrued_interest),
            "total_accrued_fees": str(r.total_accrued_fees),
            "delinquent_loan_count": r.delinquent_loan_count,
            "default_loan_count": r.default_loan_count,
        }
        for r in rows
    ]


@router.get("/{portfolio_id}", response_model=dict)
async def get_portfolio(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """Single portfolio with KPI rollup."""
    result = await db.execute(
        text("""
            SELECT
                p.id, p.code, p.name, p.fund_type, p.base_currency, p.inception_date,
                p.status, p.notes, p.closed_at, p.client_id,
                c.name AS client_name,
                COUNT(DISTINCT l.id) FILTER (WHERE l.status NOT IN ('paid_off','written_off')) AS active_loan_count,
                COUNT(DISTINCT l.id) AS total_loan_count,
                COALESCE(SUM(l.commitment_amount * a.ownership_pct / 100) FILTER (WHERE l.status NOT IN ('paid_off','written_off')), 0) AS total_commitment,
                COALESCE(SUM(l.current_principal * a.ownership_pct / 100), 0) AS total_principal_outstanding,
                COALESCE(SUM(l.accrued_interest * a.ownership_pct / 100), 0) AS total_accrued_interest,
                COALESCE(SUM(l.accrued_fees * a.ownership_pct / 100), 0) AS total_accrued_fees,
                COUNT(DISTINCT l.id) FILTER (WHERE l.status = 'delinquent') AS delinquent_loan_count,
                COUNT(DISTINCT l.id) FILTER (WHERE l.status = 'default') AS default_loan_count
            FROM portfolio p
            LEFT JOIN client c ON c.id = p.client_id
            LEFT JOIN loan_allocation a ON a.portfolio_id = p.id AND a.end_date IS NULL
            LEFT JOIN loan l ON l.id = a.loan_id
            WHERE p.id = :pid
            GROUP BY p.id, p.code, p.name, p.fund_type, p.base_currency, p.inception_date,
                     p.status, p.notes, p.closed_at, p.client_id, c.name
        """),
        {"pid": portfolio_id}
    )
    r = result.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return {
        "id": str(r.id),
        "code": r.code,
        "name": r.name,
        "fund_type": r.fund_type,
        "base_currency": r.base_currency,
        "inception_date": str(r.inception_date) if r.inception_date else None,
        "status": r.status,
        "notes": r.notes,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
        "client_id": str(r.client_id) if r.client_id else None,
        "client_name": r.client_name,
        "active_loan_count": r.active_loan_count,
        "total_loan_count": r.total_loan_count,
        "total_commitment": str(r.total_commitment),
        "total_principal_outstanding": str(r.total_principal_outstanding),
        "total_accrued_interest": str(r.total_accrued_interest),
        "total_accrued_fees": str(r.total_accrued_fees),
        "delinquent_loan_count": r.delinquent_loan_count,
        "default_loan_count": r.default_loan_count,
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_portfolio(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """Create a new portfolio under a client."""
    code = payload.get("code")
    name = payload.get("name")
    client_id = payload.get("client_id")
    if not (code and name and client_id):
        raise HTTPException(status_code=400, detail="code, name, and client_id are required")
    result = await db.execute(
        text("""
            INSERT INTO portfolio (code, name, fund_type, base_currency, inception_date, notes, client_id)
            VALUES (:code, :name, :fund_type, :base_currency, :inception_date, :notes, :client_id)
            RETURNING id
        """),
        {
            "code": code,
            "name": name,
            "fund_type": payload.get("fund_type"),
            "base_currency": payload.get("base_currency", "USD"),
            "inception_date": (datetime.fromisoformat(payload["inception_date"]).date() if payload.get("inception_date") else None),
            "notes": payload.get("notes"),
            "client_id": client_id,
        }
    )
    new_id = result.scalar()
    await db.commit()
    return {"id": str(new_id), "code": code, "name": name, "status": "active"}


@router.post("/{portfolio_id}/close", response_model=dict)
async def close_portfolio(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Close a portfolio. Requires no active allocations to active loans.
    Uses loan_allocation, not loan.portfolio_id — a fund that's transferred all
    its allocations away should be able to close even if it was the lead fund.
    """
    result = await db.execute(
        text("""
            SELECT COUNT(*) AS active_count
            FROM loan_allocation a
            JOIN loan l ON l.id = a.loan_id
            WHERE a.portfolio_id = :pid
              AND a.end_date IS NULL
              AND l.status NOT IN ('paid_off','written_off')
        """),
        {"pid": portfolio_id}
    )
    active_count = result.scalar()
    if active_count and active_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot close portfolio: {active_count} active loan(s) remain."
        )
    await db.execute(
        text("""
            UPDATE portfolio SET status='closed', closed_at=now(), updated_at=now()
            WHERE id = :pid
        """),
        {"pid": portfolio_id}
    )
    await db.commit()
    return {"id": str(portfolio_id), "status": "closed"}


@router.post("/{portfolio_id}/reopen", response_model=dict)
async def reopen_portfolio(
    portfolio_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """Reopen a closed portfolio."""
    await db.execute(
        text("""
            UPDATE portfolio SET status='active', closed_at=NULL, updated_at=now()
            WHERE id = :pid
        """),
        {"pid": portfolio_id}
    )
    await db.commit()
    return {"id": str(portfolio_id), "status": "active"}
