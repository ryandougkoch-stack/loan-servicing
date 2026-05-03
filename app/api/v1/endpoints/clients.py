"""
app/api/v1/endpoints/clients.py
Client CRUD with KPI rollup at the client level.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenPayload
from app.api.dependencies import get_current_user_id, get_db, require_min_role

router = APIRouter(prefix="/clients", tags=["clients"])


@router.get("", response_model=list[dict])
async def list_clients(
    include_closed: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """List clients with KPI rollups across all their portfolios/loans."""
    where = "" if include_closed else "WHERE c.status = 'active'"
    # Phase 2: dollar fields prorated through loan_allocation; loan counts un-prorated.
    # Portfolio counts unchanged (driven by client→portfolio FK).
    sql = f"""
        SELECT
            c.id, c.name, c.legal_name, c.status, c.notes, c.closed_at, c.created_at,
            COUNT(DISTINCT p.id) FILTER (WHERE p.status = 'active') AS active_portfolio_count,
            COUNT(DISTINCT p.id) AS total_portfolio_count,
            COUNT(DISTINCT l.id) FILTER (WHERE l.status NOT IN ('paid_off','written_off')) AS active_loan_count,
            COUNT(DISTINCT l.id) AS total_loan_count,
            COALESCE(SUM(l.commitment_amount * a.ownership_pct / 100) FILTER (WHERE l.status NOT IN ('paid_off','written_off')), 0) AS total_commitment,
            COALESCE(SUM(l.current_principal * a.ownership_pct / 100), 0) AS total_principal_outstanding,
            COALESCE(SUM(l.accrued_interest * a.ownership_pct / 100), 0) AS total_accrued_interest,
            COALESCE(SUM(l.accrued_fees * a.ownership_pct / 100), 0) AS total_accrued_fees,
            COUNT(DISTINCT l.id) FILTER (WHERE l.status = 'delinquent') AS delinquent_loan_count,
            COUNT(DISTINCT l.id) FILTER (WHERE l.status = 'default') AS default_loan_count
        FROM client c
        LEFT JOIN portfolio p ON p.client_id = c.id
        LEFT JOIN loan_allocation a ON a.portfolio_id = p.id AND a.end_date IS NULL
        LEFT JOIN loan l ON l.id = a.loan_id
        {where}
        GROUP BY c.id, c.name, c.legal_name, c.status, c.notes, c.closed_at, c.created_at
        ORDER BY c.status, c.name
    """
    result = await db.execute(text(sql))
    rows = result.fetchall()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "legal_name": r.legal_name,
            "status": r.status,
            "notes": r.notes,
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "active_portfolio_count": r.active_portfolio_count,
            "total_portfolio_count": r.total_portfolio_count,
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


@router.get("/{client_id}", response_model=dict)
async def get_client(
    client_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """Get a single client with full KPI rollup."""
    result = await db.execute(
        text("""
            SELECT
                c.id, c.name, c.legal_name, c.status, c.notes, c.closed_at, c.created_at,
                COUNT(DISTINCT p.id) FILTER (WHERE p.status = 'active') AS active_portfolio_count,
                COUNT(DISTINCT p.id) AS total_portfolio_count,
                COUNT(DISTINCT l.id) FILTER (WHERE l.status NOT IN ('paid_off','written_off')) AS active_loan_count,
                COUNT(DISTINCT l.id) AS total_loan_count,
                COALESCE(SUM(l.commitment_amount * a.ownership_pct / 100) FILTER (WHERE l.status NOT IN ('paid_off','written_off')), 0) AS total_commitment,
                COALESCE(SUM(l.current_principal * a.ownership_pct / 100), 0) AS total_principal_outstanding,
                COALESCE(SUM(l.accrued_interest * a.ownership_pct / 100), 0) AS total_accrued_interest,
                COALESCE(SUM(l.accrued_fees * a.ownership_pct / 100), 0) AS total_accrued_fees,
                COUNT(DISTINCT l.id) FILTER (WHERE l.status = 'delinquent') AS delinquent_loan_count,
                COUNT(DISTINCT l.id) FILTER (WHERE l.status = 'default') AS default_loan_count
            FROM client c
            LEFT JOIN portfolio p ON p.client_id = c.id
            LEFT JOIN loan_allocation a ON a.portfolio_id = p.id AND a.end_date IS NULL
            LEFT JOIN loan l ON l.id = a.loan_id
            WHERE c.id = :cid
            GROUP BY c.id, c.name, c.legal_name, c.status, c.notes, c.closed_at, c.created_at
        """),
        {"cid": client_id}
    )
    r = result.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Client not found")
    return {
        "id": str(r.id),
        "name": r.name,
        "legal_name": r.legal_name,
        "status": r.status,
        "notes": r.notes,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "active_portfolio_count": r.active_portfolio_count,
        "total_portfolio_count": r.total_portfolio_count,
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
async def create_client(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """Create a new client."""
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    result = await db.execute(
        text("""
            INSERT INTO client (name, legal_name, notes)
            VALUES (:name, :legal_name, :notes)
            RETURNING id
        """),
        {
            "name": name,
            "legal_name": payload.get("legal_name"),
            "notes": payload.get("notes"),
        }
    )
    new_id = result.scalar()
    await db.commit()
    return {"id": str(new_id), "name": name, "status": "active"}


@router.post("/{client_id}/close", response_model=dict)
async def close_client(
    client_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Close a client. Requires all loans under this client to be paid_off or written_off.
    """
    # Validate: no active allocations to active loans under this client's portfolios.
    # Uses loan_allocation rather than loan.portfolio_id — captures real economic
    # exposure (a fund that bought into a loan via allocation counts; a fund that
    # transferred its share away does not).
    result = await db.execute(
        text("""
            SELECT COUNT(*) AS active_count
            FROM loan_allocation a
            JOIN loan l      ON l.id = a.loan_id
            JOIN portfolio p ON p.id = a.portfolio_id
            WHERE p.client_id = :cid
              AND a.end_date IS NULL
              AND l.status NOT IN ('paid_off','written_off')
        """),
        {"cid": client_id}
    )
    active_count = result.scalar()
    if active_count and active_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot close client: {active_count} active loan(s) remain. Pay off or write off all loans first."
        )
    # Mark client closed
    await db.execute(
        text("""
            UPDATE client SET status='closed', closed_at=now(), updated_at=now()
            WHERE id = :cid
        """),
        {"cid": client_id}
    )
    # Cascade close all portfolios under this client
    await db.execute(
        text("""
            UPDATE portfolio SET status='closed', closed_at=now(), updated_at=now()
            WHERE client_id = :cid AND status = 'active'
        """),
        {"cid": client_id}
    )
    await db.commit()
    return {"id": str(client_id), "status": "closed"}


@router.post("/{client_id}/reopen", response_model=dict)
async def reopen_client(
    client_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """Reopen a closed client."""
    await db.execute(
        text("""
            UPDATE client SET status='active', closed_at=NULL, updated_at=now()
            WHERE id = :cid
        """),
        {"cid": client_id}
    )
    await db.commit()
    return {"id": str(client_id), "status": "active"}
