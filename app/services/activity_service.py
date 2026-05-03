"""
app/services/activity_service.py
Logs material loan events for audit and operational visibility.

Design:
- Material events only (boarding, status, modifications, payments, payoffs, manual edits)
- One event per logical action with a JSON payload of field changes
- Captures user, timestamp, IP, user agent
- Daily accruals are NOT logged here - that table would flood the log
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class ActivityService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def log(
        self,
        loan_id: UUID,
        event_type: str,
        event_summary: str,
        field_changes: Optional[dict] = None,
        user_id: Optional[UUID] = None,
        user_email: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        """
        Record a material event on a loan.
        event_type examples: boarded, status_changed, modification_applied,
                             payment_posted, payoff_processed, manual_edit
        """
        import json
        await self.db.execute(
            text("""
                INSERT INTO loan_activity
                  (loan_id, event_type, event_summary, field_changes,
                   user_id, user_email, ip_address, user_agent)
                VALUES
                  (:loan_id, :event_type, :event_summary,
                   CAST(:field_changes AS JSONB),
                   :user_id, :user_email, :ip_address, :user_agent)
            """),
            {
                "loan_id": loan_id,
                "event_type": event_type,
                "event_summary": event_summary,
                "field_changes": json.dumps(field_changes) if field_changes else None,
                "user_id": user_id,
                "user_email": user_email,
                "ip_address": ip_address,
                "user_agent": user_agent,
            }
        )
        logger.info(
            "loan_activity_logged",
            loan_id=str(loan_id),
            event_type=event_type,
        )

    async def get_activity(self, loan_id: UUID, limit: int = 100) -> list[dict]:
        """Fetch activity log for a loan, most recent first."""
        result = await self.db.execute(
            text("""
                SELECT la.id, la.event_type, la.event_summary, la.field_changes,
                       la.user_id, COALESCE(u.email, la.user_email) as user_email,
                       la.ip_address, la.created_at
                FROM loan_activity la
                LEFT JOIN shared.users u ON u.id = la.user_id
                WHERE la.loan_id = :loan_id
                ORDER BY la.created_at DESC
                LIMIT :limit
            """),
            {"loan_id": loan_id, "limit": limit}
        )
        rows = result.fetchall()
        return [
            {
                "id": str(r.id),
                "event_type": r.event_type,
                "event_summary": r.event_summary,
                "field_changes": r.field_changes,
                "user_email": r.user_email,
                "ip_address": r.ip_address,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
