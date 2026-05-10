#!/bin/bash
# Loan Servicing Platform - Full startup script
# Run this instead of uvicorn directly

cd ~/loan-servicing
source venv/bin/activate

echo "Starting Docker services..."
docker-compose up -d postgres redis

echo "Waiting for postgres..."
sleep 3

echo "Starting Celery worker..."
celery -A app.workers.celery_app worker \
  --loglevel=info \
  --queues=accrual,delinquency,conversion \
  --concurrency=2 \
  --logfile=/tmp/celery_worker.log \
  --detach

echo "Starting Celery Beat scheduler..."
celery -A app.workers.celery_app beat \
  --loglevel=info \
  --logfile=/tmp/celery_beat.log \
  --pidfile=/tmp/celery_beat.pid \
  --detach

echo "Starting API server..."
uvicorn app.main:app --reload --host 0.0.0.0

