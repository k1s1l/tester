#!/bin/sh
# PostgreSQL backup with rotation.
#   DATABASE_URL=postgresql://user:pass@host:5432/db sh scripts/backup_db.sh ./backups [keep_days]
# Restore:
#   gunzip -c backups/sneaker_bot_YYYYmmdd_HHMMSS.sql.gz | psql "$DATABASE_URL"
# Backups contain personal data: store them privately and never commit them to git.
set -eu

TARGET_DIR="${1:-./backups}"
KEEP_DAYS="${2:-14}"
: "${DATABASE_URL:?DATABASE_URL is required}"

# SQLAlchemy-style URLs are not understood by pg_dump
URL=$(echo "$DATABASE_URL" | sed 's#^postgresql+asyncpg://#postgresql://#; s#^postgres://#postgresql://#')

mkdir -p "$TARGET_DIR"
FILE="$TARGET_DIR/sneaker_bot_$(date -u +%Y%m%d_%H%M%S).sql.gz"
pg_dump --no-owner --no-privileges "$URL" | gzip -9 > "$FILE"
echo "Backup written: $FILE"

find "$TARGET_DIR" -name 'sneaker_bot_*.sql.gz' -mtime +"$KEEP_DAYS" -delete
