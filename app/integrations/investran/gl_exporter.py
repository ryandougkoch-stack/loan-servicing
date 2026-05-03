"""
app/integrations/investran/gl_exporter.py

Investran GL export integration.

Investran (SS&C) is the fund accounting platform used by private credit
fund administrators. It does not have a real-time API for most deployments;
the standard integration pattern is a scheduled file-based export.

This module:
  1. Queries journal entries not yet synced to Investran
  2. Transforms them into Investran's expected CSV format
  3. Writes the file to S3 (for audit) and delivers via SFTP
  4. Marks entries as synced and logs the batch

Investran import format varies by client configuration. This implements
a generic GL journal import format; your SS&C implementation team will
provide the exact field mapping for each client.
"""
import csv
import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.exceptions import InvestranSyncError
from app.models.ledger import JournalEntry, JournalLine, LedgerAccount
from app.db.session import get_tenant_session_context

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Column mapping for Investran GL import
# Adjust field names to match your SS&C implementation's template
# ---------------------------------------------------------------------------
INVESTRAN_COLUMNS = [
    "JournalEntryNumber",
    "EntryDate",
    "EffectiveDate",
    "EntityCode",           # maps to portfolio.investran_entity_id
    "AccountCode",          # maps to ledger_account.gl_account_code
    "DebitAmount",
    "CreditAmount",
    "Description",
    "LoanReference",        # loan.investran_loan_id
    "InternalReference",    # our entry_number
    "EntryType",
    "Currency",
]


class InvestranGLExporter:
    """
    Builds and delivers a GL export file for one tenant.
    One instance per export run.
    """

    def __init__(self, tenant_slug: str, export_date: date):
        self.tenant_slug = tenant_slug
        self.export_date = export_date
        self.batch_id = f"INV-{export_date.strftime('%Y%m%d')}-{str(uuid.uuid4())[:8].upper()}"

    async def run(self) -> dict:
        """
        Execute the full export pipeline.
        Returns a summary dict with record_count, file_name, batch_id.
        """
        async with get_tenant_session_context(self.tenant_slug) as session:
            # 1. Load unsynced posted journal entries
            entries = await self._load_unsynced_entries(session)

            if not entries:
                logger.info(
                    "investran_export_skipped",
                    tenant=self.tenant_slug,
                    reason="no_unsynced_entries",
                )
                return {"record_count": 0, "batch_id": self.batch_id}

            # 2. Build CSV
            csv_content, row_count = self._build_csv(entries)

            # 3. Write to S3
            file_name = f"gl_export_{self.tenant_slug}_{self.batch_id}.csv"
            s3_path = await self._upload_to_s3(csv_content, file_name)

            # 4. Deliver via SFTP (if configured)
            sftp_delivered = False
            if settings.INVESTRAN_SFTP_HOST:
                await self._deliver_via_sftp(csv_content, file_name)
                sftp_delivered = True

            # 5. Mark entries as synced
            entry_ids = [e.id for e in entries]
            now = datetime.now(timezone.utc)
            await session.execute(
                update(JournalEntry)
                .where(JournalEntry.id.in_(entry_ids))
                .values(
                    investran_synced_at=now,
                    investran_batch_id=self.batch_id,
                )
            )

            logger.info(
                "investran_export_completed",
                tenant=self.tenant_slug,
                batch_id=self.batch_id,
                record_count=row_count,
                s3_path=s3_path,
                sftp_delivered=sftp_delivered,
            )

            return {
                "batch_id": self.batch_id,
                "record_count": row_count,
                "file_name": file_name,
                "s3_path": s3_path,
                "sftp_delivered": sftp_delivered,
            }

    async def _load_unsynced_entries(self, session) -> list[JournalEntry]:
        result = await session.execute(
            select(JournalEntry)
            .where(
                JournalEntry.status == "posted",
                JournalEntry.investran_synced_at.is_(None),
                JournalEntry.entry_date <= self.export_date,
            )
            .options(
                selectinload(JournalEntry.lines).selectinload(JournalLine.account)
            )
            .order_by(JournalEntry.effective_date, JournalEntry.entry_number)
        )
        return list(result.scalars().all())

    def _build_csv(self, entries: list[JournalEntry]) -> tuple[str, int]:
        """
        Transform journal entries into Investran CSV format.
        Each journal LINE becomes one CSV row.
        """
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=INVESTRAN_COLUMNS)
        writer.writeheader()

        row_count = 0
        for entry in entries:
            for line in entry.lines:
                gl_code = (
                    line.account.gl_account_code
                    if line.account and line.account.gl_account_code
                    else line.account.code if line.account else ""
                )

                writer.writerow({
                    "JournalEntryNumber": entry.entry_number,
                    "EntryDate": entry.entry_date.strftime("%Y-%m-%d"),
                    "EffectiveDate": entry.effective_date.strftime("%Y-%m-%d"),
                    "EntityCode": "",          # populated from portfolio in full impl
                    "AccountCode": gl_code,
                    "DebitAmount": str(line.debit_amount) if line.debit_amount > 0 else "",
                    "CreditAmount": str(line.credit_amount) if line.credit_amount > 0 else "",
                    "Description": (entry.description or "")[:100],
                    "LoanReference": "",       # populated from loan.investran_loan_id in full impl
                    "InternalReference": entry.entry_number,
                    "EntryType": entry.entry_type,
                    "Currency": line.currency,
                })
                row_count += 1

        return output.getvalue(), row_count

    async def _upload_to_s3(self, content: str, file_name: str) -> str:
        """Upload export file to S3. Returns the S3 path."""
        if not settings.AWS_ACCESS_KEY_ID:
            logger.warning("s3_upload_skipped", reason="AWS credentials not configured")
            return f"s3://{settings.S3_BUCKET_EXPORTS}/{file_name}"

        try:
            import boto3
            s3 = boto3.client(
                "s3",
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION,
            )
            key = f"investran/gl/{self.tenant_slug}/{file_name}"
            s3.put_object(
                Bucket=settings.S3_BUCKET_EXPORTS,
                Key=key,
                Body=content.encode("utf-8"),
                ContentType="text/csv",
                ServerSideEncryption="AES256",
            )
            return f"s3://{settings.S3_BUCKET_EXPORTS}/{key}"
        except Exception as e:
            logger.error("s3_upload_failed", error=str(e))
            raise InvestranSyncError(f"S3 upload failed: {e}")

    async def _deliver_via_sftp(self, content: str, file_name: str) -> None:
        """
        Deliver the export file to Investran via SFTP.
        Requires paramiko: pip install paramiko
        """
        try:
            import paramiko

            transport = paramiko.Transport(
                (settings.INVESTRAN_SFTP_HOST, settings.INVESTRAN_SFTP_PORT)
            )
            transport.connect(
                username=settings.INVESTRAN_SFTP_USER,
                password=settings.INVESTRAN_SFTP_PASSWORD,
            )
            sftp = paramiko.SFTPClient.from_transport(transport)
            remote_path = f"{settings.INVESTRAN_SFTP_REMOTE_PATH}/{file_name}"

            with sftp.open(remote_path, "w") as f:
                f.write(content)

            sftp.close()
            transport.close()

            logger.info("sftp_delivered", remote_path=remote_path)

        except Exception as e:
            logger.error("sftp_delivery_failed", error=str(e))
            raise InvestranSyncError(f"SFTP delivery failed: {e}")
