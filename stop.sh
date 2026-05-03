#!/bin/bash
cd ~/loan-servicing
source venv/bin/activate

echo "Stopping Celery..."
celery -A app.workers.celery_app control shutdown 2>/dev/null || true
[ -f /tmp/celery_beat.pid ] && kill $(cat /tmp/celery_beat.pid) 2>/dev/null || true

echo "Stopping Docker services..."
docker-compose stop

echo "Done."
