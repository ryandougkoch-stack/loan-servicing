"""
app/schemas/conversion.py

Pydantic schemas for batch conversion intake (Excel upload of multiple
mid-term-boarded loans + their counterparties).

Wire flow:
  POST /conversions/batches               -> parse + validate (no writes)
        returns BatchValidationReport
  POST /conversions/batches/{id}/commit   -> async per-row commit
        returns BatchCommitAccepted; final state lives on conversion_batch row
  GET  /conversions/batches/{id}          -> BatchStatus
"""
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Per-row reporting
# ---------------------------------------------------------------------------

class RowIssue(BaseModel):
    """A validation problem on a specific (sheet, row, column)."""
    sheet: str
    row: int
    column: Optional[str] = None
    severity: str = "error"          # error | warning
    message: str
    suggestion: Optional[str] = None


class RowResult(BaseModel):
    """Per-row outcome from validate or commit."""
    sheet: str
    row: int
    external_ref: Optional[str] = None
    status: str                       # ok | error
    loan_id: Optional[UUID] = None    # populated post-commit on success
    counterparty_id: Optional[UUID] = None
    errors: list[str] = []


# ---------------------------------------------------------------------------
# Validate-then-commit responses
# ---------------------------------------------------------------------------

class BatchValidationReport(BaseModel):
    """Returned synchronously from POST /conversions/batches."""
    batch_id: UUID
    file_name: str
    sheets_detected: list[str]
    counterparty_rows: int
    loan_rows: int
    valid_loan_rows: int
    invalid_loan_rows: int
    issues: list[RowIssue]
    row_results: list[RowResult]


class BatchCommitAccepted(BaseModel):
    """Returned from POST /conversions/batches/{id}/commit (202)."""
    batch_id: UUID
    status: str
    message: str


class BatchStatus(BaseModel):
    """Returned from GET /conversions/batches/{id}."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    file_name: str
    uploaded_by: UUID
    uploaded_at: datetime
    status: str
    total_rows: int
    succeeded_rows: int
    failed_rows: int
    validation_report: Optional[dict[str, Any]] = None
    commit_report: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
