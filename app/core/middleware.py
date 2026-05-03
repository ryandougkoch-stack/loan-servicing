"""
app/core/middleware.py

ASGI middleware stack:

1. RequestIDMiddleware  — generates a unique request_id for every request
                          and binds it to the structlog context
2. TenantContextMiddleware — binds tenant_slug to the structlog context
                             so it appears in all log lines for the request
3. AuditMiddleware      — logs every state-changing request (POST/PUT/PATCH/DELETE)
                          to the audit log (lightweight version; full field-level
                          audit happens at the service layer)
"""
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from structlog.contextvars import bind_contextvars, clear_contextvars

logger = structlog.get_logger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Attach a unique X-Request-ID to every request and response.
    Bind it to the structlog context so it appears in all log lines.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        clear_contextvars()

        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        bind_contextvars(request_id=request_id)

        start_time = time.perf_counter()

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )

        return response


class TenantContextMiddleware(BaseHTTPMiddleware):
    """
    Extract tenant_slug from the JWT (already validated by dependencies.py)
    and bind it to structlog context for log correlation.

    We don't re-validate the token here — that's the dependency layer's job.
    We only parse the payload for logging context, so failures are silent.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                from app.core.security import decode_token
                payload = decode_token(auth_header.split(" ", 1)[1])
                bind_contextvars(
                    tenant_slug=payload.tenant_slug,
                    user_id=payload.sub,
                    role=payload.role,
                )
            except Exception:
                pass  # Don't fail the request on logging context errors

        return await call_next(request)
