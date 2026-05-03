"""app/api/v1/endpoints/counterparties.py"""
from uuid import UUID
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_min_role
from app.core.security import TokenPayload

router = APIRouter(prefix="/counterparties", tags=["counterparties"])


class CounterpartyCreate(BaseModel):
    type: str = "borrower"
    legal_name: str
    entity_type: Optional[str] = None
    state_of_formation: Optional[str] = None
    kyc_status: str = "pending"


class CounterpartyRead(BaseModel):
    id: UUID
    type: str
    legal_name: str
    entity_type: Optional[str]
    kyc_status: str


@router.post("", response_model=CounterpartyRead)
async def create_counterparty(
    payload: CounterpartyCreate,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    now = datetime.now(timezone.utc)
    result = await db.execute(text("""
        INSERT INTO counterparty (type, legal_name, entity_type, state_of_formation,
                                  kyc_status, is_active, created_at, updated_at)
        VALUES (:type, :legal_name, :entity_type, :state, :kyc, true, :now, :now)
        RETURNING id, type, legal_name, entity_type, kyc_status
    """), {
        "type": payload.type,
        "legal_name": payload.legal_name,
        "entity_type": payload.entity_type,
        "state": payload.state_of_formation,
        "kyc": payload.kyc_status,
        "now": now,
    })
    await db.commit()
    row = result.mappings().one()
    return CounterpartyRead(**row)


@router.get("")
async def list_counterparties(
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("reporting")),
):
    result = await db.execute(text("""
        SELECT id, type, legal_name, entity_type, kyc_status
        FROM counterparty
        WHERE is_active = true
        ORDER BY legal_name
        LIMIT 100
    """))
    rows = result.mappings().all()
    return [CounterpartyRead(**r) for r in rows]
