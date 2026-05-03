#!/bin/bash
# Package loan-servicing platform into a clean zip
# Run from: ~/loan-servicing

set -e
OUTDIR="/tmp/loan-servicing-v7"
ZIPFILE="/mnt/c/Users/Kochs/Downloads/loan-servicing-v7.zip"

echo "Packaging loan-servicing v7..."

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

# Copy full app
cp -r ~/loan-servicing/app "$OUTDIR/"
cp -r ~/loan-servicing/alembic "$OUTDIR/" 2>/dev/null || true
cp -r ~/loan-servicing/scripts "$OUTDIR/" 2>/dev/null || true
cp -r ~/loan-servicing/tests "$OUTDIR/" 2>/dev/null || true

# Copy config files
for f in requirements.txt pyproject.toml docker-compose.yml .env.example alembic.ini; do
    [ -f ~/loan-servicing/$f ] && cp ~/loan-servicing/$f "$OUTDIR/" && echo "  + $f"
done

# Copy dashboard UI
cp /mnt/c/Users/Kochs/Downloads/dashboard_3.html "$OUTDIR/dashboard.html" 2>/dev/null && echo "  + dashboard.html" || echo "  ! dashboard.html not found"

# Strip __pycache__ and .pyc files
find "$OUTDIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find "$OUTDIR" -name "*.pyc" -delete 2>/dev/null || true

# Create zip
cd /tmp
rm -f "$ZIPFILE"
zip -r "$ZIPFILE" loan-servicing-v7/ -q
echo ""
echo "Done! Saved to: $ZIPFILE"
echo ""
echo "Contents:"
ls -la "$OUTDIR/app/"
