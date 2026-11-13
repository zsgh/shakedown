#!/usr/bin/env bash
set -euo pipefail

# 用法：SHAKEDOWN_DATABASE_URL=postgresql://... ./scripts/backup_db.sh [输出目录]
# 依赖 pg_dump。默认从 backend 配置或环境变量读取 DATABASE_URL。
OUT_DIR="${1:-./backups}"
mkdir -p "$OUT_DIR"
DSN="${SHAKEDOWN_DATABASE_URL:-${DATABASE_URL:-postgresql://shakedown:shakedown@localhost:5432/shakedown}}"
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="$OUT_DIR/shakedown-$STAMP.dump"
pg_dump --format=custom --no-owner --no-privileges --dbname="$DSN" --file="$FILE"
sha256sum "$FILE" > "$FILE.sha256" 2>/dev/null || shasum -a 256 "$FILE" > "$FILE.sha256"
echo "$FILE"
