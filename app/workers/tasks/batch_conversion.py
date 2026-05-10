"""
app/workers/tasks/batch_conversion.py

Celery task that commits a previously-validated conversion batch.

Triggered from POST /conversions/batches/{id}/commit. The endpoint persists
nothing beyond enqueuing — this worker does all the per-loan inserts.

Per-loan transactions are owned by BatchConversionService.commit_batch:
one bad row doesn't roll back the rest.
"""
import asyncio
from uuid import UUID

import structlog

from app.services.batch_conversion_service import commit_batch
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(
    name="app.workers.tasks.batch_conversion.run_batch_commit",
    bind=True,
    max_retries=0,   # per-row failures don't retry the whole batch
)
def run_batch_commit(self, tenant_slug: str, batch_id_str: str):
    """
    Commit batch `batch_id_str` for tenant `tenant_slug`.
    Failures within individual loan inserts are recorded in
    conversion_batch.commit_report; only catastrophic errors raise here.
    """
    try:
        result = asyncio.run(commit_batch(tenant_slug, UUID(batch_id_str)))
        logger.info("batch_commit_completed", tenant=tenant_slug,
                    batch_id=batch_id_str,
                    succeeded=result["succeeded"], failed=result["failed"])
    except Exception:
        logger.exception("batch_commit_task_failed", tenant=tenant_slug, batch_id=batch_id_str)
        # Surface as a hard failure on the batch row so the UI can show it.
        from sqlalchemy import select
        from app.db.session import get_tenant_session_context
        from app.models.conversion import ConversionBatch

        async def _mark_failed():
            async with get_tenant_session_context(tenant_slug) as session:
                batch = (await session.execute(
                    select(ConversionBatch).where(ConversionBatch.id == UUID(batch_id_str))
                )).scalar_one_or_none()
                if batch is not None:
                    batch.status = "failed"
        try:
            asyncio.run(_mark_failed())
        except Exception:
            logger.exception("batch_failed_status_update_also_failed")
        raise
