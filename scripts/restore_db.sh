#!/usr/bin/env bash
set -euo pipefail

# 用法：SHAKEDOWN_DATABASE_URL=postgresql://... ./scripts/restore_db.sh backups/shakedown-xxx.dump
FILE="${1:?用法: restore_db.sh <dump 文件>}"
DSN="${SHAKEDOWN_DATABASE_URL:-${DATABASE_URL:-postgresql://shakedown:shakedown@localhost:5432/shakedown}}"
if [ -f "$FILE.sha256" ]; then
  (cd "$(dirname "$FILE")" && shasum -a 256 -c "$(basename "$FILE").sha256" 2>/dev/null || sha256sum -c "$(basename "$FILE").sha256")
fi
pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$DSN" "$FILE"
echo "restored: $FILE"
