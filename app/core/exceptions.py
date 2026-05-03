"""
app/core/exceptions.py

Domain exception hierarchy and FastAPI exception handlers.

Using named exceptions (rather than raw HTTPException everywhere) keeps
business logic clean and makes error handling consistent across the API.
"""
from typing import Any, Optional

import structlog
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Base exceptions
# ---------------------------------------------------------------------------

class LoanServicingError(Exception):
    """Base class for all domain exceptions."""
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, detail: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# 400-range
# ---------------------------------------------------------------------------

class ValidationError(LoanServicingError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_code = "VALIDATION_ERROR"


class NotFoundError(LoanServicingError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "NOT_FOUND"


class ConflictError(LoanServicingError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "CONFLICT"


class PermissionDeniedError(LoanServicingError):
    status_code = status.HTTP_403_FORBIDDEN
    error_code = "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Domain-specific exceptions
# ---------------------------------------------------------------------------

class LoanNotFoundError(NotFoundError):
    error_code = "LOAN_NOT_FOUND"


class PaymentPostingError(LoanServicingError):
    """Raised when a payment cannot be posted (e.g. wrong loan status, bad waterfall)."""
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_code = "PAYMENT_POSTING_ERROR"


class LedgerImbalanceError(LoanServicingError):
    """Raised when a journal entry does not balance. This is a critical error."""
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "LEDGER_IMBALANCE"


class TenantNotFoundError(NotFoundError):
    error_code = "TENANT_NOT_FOUND"


class InvestranSyncError(LoanServicingError):
    error_code = "INVESTRAN_SYNC_ERROR"


class PayoffQuoteExpiredError(LoanServicingError):
    status_code = status.HTTP_410_GONE
    error_code = "PAYOFF_QUOTE_EXPIRED"


class ModificationConflictError(ConflictError):
    error_code = "MODIFICATION_CONFLICT"


# ---------------------------------------------------------------------------
# Exception handlers — register on the FastAPI app
# ---------------------------------------------------------------------------

def _error_response(
    request: Request,
    exc: LoanServicingError,
) -> JSONResponse:
    logger.warning(
        "domain_exception",
        error_code=exc.error_code,
        message=exc.message,
        path=request.url.path,
        method=request.method,
    )
    body = {
        "error": exc.error_code,
        "message": exc.message,
    }
    if exc.detail is not None:
        body["detail"] = exc.detail
    return JSONResponse(status_code=exc.status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoanServicingError)
    async def domain_exception_handler(request: Request, exc: LoanServicingError):
        return _error_response(request, exc)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "INTERNAL_ERROR", "message": "An unexpected error occurred."},
        )
