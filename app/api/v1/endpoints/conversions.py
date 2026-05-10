"""
app/api/v1/endpoints/conversions.py

Batch loan conversion intake — validate-then-commit Excel upload flow.

Endpoints:
  POST /conversions/batches             upload + validate (sync, no writes
                                        beyond the conversion_batch row itself)
  POST /conversions/batches/{id}/commit dispatch async per-loan commit (202)
  GET  /conversions/batches             list batches (latest first)
  GET  /conversions/batches/{id}        full status incl. validation +
                                        commit reports

Single-loan conversion stays on POST /loans (with conversion block on
LoanCreate) — that's a separate path. This module is only for batch.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_id, require_min_role
from app.core.exceptions import ValidationError
from app.core.security import TokenPayload
from app.models.conversion import ConversionBatch
from app.schemas.conversion import (
    BatchCommitAccepted,
    BatchStatus,
    BatchValidationReport,
)
from app.services.batch_conversion_service import BatchConversionService

router = APIRouter(prefix="/conversions", tags=["conversions"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024     # 25 MB cap on intake xlsx


@router.post("/batches", response_model=BatchValidationReport, status_code=status.HTTP_201_CREATED)
async def create_batch(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Upload a multi-sheet xlsx of loans-to-convert. Synchronously parses,
    validates, and persists a conversion_batch row plus its validation
    report. NO loans are inserted — call /commit when the report looks right.
    """
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Only .xlsx files are supported")

    body = await file.read()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max {MAX_UPLOAD_BYTES // 1024 // 1024} MB",
        )
    if not body:
        raise HTTPException(status_code=400, detail="Empty file")

    svc = BatchConversionService(db)
    try:
        report = await svc.parse_and_validate(body, file.filename, uploaded_by=user_id)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return report


@router.post("/batches/{batch_id}/commit", response_model=BatchCommitAccepted, status_code=status.HTTP_202_ACCEPTED)
async def commit_batch(
    batch_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """
    Dispatch the async commit. Per-row failures don't roll back the batch;
    poll GET /batches/{id} for the commit_report.
    """
    batch = (await db.execute(
        select(ConversionBatch).where(ConversionBatch.id == batch_id)
    )).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if batch.status != "validated":
        raise HTTPException(
            status_code=409,
            detail=f"batch status is '{batch.status}', expected 'validated'",
        )

    # Mark intent so concurrent commit attempts can be rejected.
    batch.status = "committing"
    await db.flush()

    # Hand the worker an explicit tenant_slug — Celery tasks have no request context.
    from app.workers.tasks.batch_conversion import run_batch_commit
    run_batch_commit.delay(current_user.tenant_slug, str(batch_id))

    return BatchCommitAccepted(
        batch_id=batch_id,
        status="committing",
        message="Commit dispatched. Poll GET /conversions/batches/{id} for status.",
    )


@router.get("/batches", response_model=list[BatchStatus])
async def list_batches(
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
    limit: int = 50,
):
    """List recent batches. Latest first."""
    rows = (await db.execute(
        select(ConversionBatch).order_by(ConversionBatch.uploaded_at.desc()).limit(limit)
    )).scalars().all()
    return [BatchStatus.model_validate(r) for r in rows]


@router.get("/batches/{batch_id}", response_model=BatchStatus)
async def get_batch(
    batch_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: TokenPayload = Depends(require_min_role("ops")),
):
    """Full status incl. validation_report and commit_report (when committed)."""
    batch = (await db.execute(
        select(ConversionBatch).where(ConversionBatch.id == batch_id)
    )).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return BatchStatus.model_validate(batch)
