"""
app/workers/celery_app.py

Celery application configuration.

Background tasks in this system:
  - Daily interest accrual (runs at midnight per tenant)
  - Delinquency aging (runs nightly)
  - Payment schedule generation (triggered on loan boarding)
  - Investran GL export (runs at 2am daily)
  - Statement generation (triggered by billing cycle)
  - Maturity / covenant / insurance alerts (runs nightly)

Each task is tenant-aware: it receives a tenant_slug argument and
sets search_path before doing any database work.
"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "loan_servicing",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.tasks.accrual",
        "app.workers.tasks.delinquency",
        "app.workers.tasks.batch_conversion",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,            # re-queue on worker crash
    worker_prefetch_multiplier=1,   # one task at a time per worker (financial safety)
    task_routes={
        "app.workers.tasks.accrual.*":          {"queue": "accrual"},
        "app.workers.tasks.delinquency.*":      {"queue": "delinquency"},
        "app.workers.tasks.batch_conversion.*": {"queue": "conversion"},
    },
)

# ---------------------------------------------------------------------------
# Periodic schedule
# ---------------------------------------------------------------------------
celery_app.conf.beat_schedule = {
    # Interest accrual — midnight UTC every day
    "daily-interest-accrual": {
        "task": "app.workers.tasks.accrual.run_daily_accrual_all_tenants",
        "schedule": crontab(hour=0, minute=5),
    },
    # Delinquency aging — 1am UTC every day
    "daily-delinquency-aging": {
        "task": "app.workers.tasks.delinquency.run_aging_all_tenants",
        "schedule": crontab(hour=1, minute=0),
    },
}
