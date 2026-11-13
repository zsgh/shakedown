#!/usr/bin/env bash
# Shakedown 本地开发服务管理：start / stop / restart / status / logs
#
#   ./scripts/dev.sh start     # 后台启动后端(8000) + 前端(5173)
#   ./scripts/dev.sh status    # 看状态
#   ./scripts/dev.sh logs      # 跟日志
#   ./scripts/dev.sh stop      # 停掉
#
# 说明：
# - 后端会自动跑 alembic 迁移（建表/升级）
# - 用 nohup + disown 脱离当前终端，关掉终端也不会被杀
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
BACKEND_PORT="${BACKEND_BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
LOG_DIR="${SHAKEDOWN_LOG_DIR:-/tmp/shakedown}"
PID_DIR="$LOG_DIR/pids"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

mkdir -p "$PID_DIR"

is_alive() {  # $1 = pid 文件
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid; pid="$(cat "$pid_file" 2>/dev/null)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

listening() {  # $1 = 端口：该端口已有监听（可能由外部启动，如手工 uvicorn / vite）
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

ensure_postgres() {
  # PG 是独立的 brew 服务；机器休眠唤醒后偶尔会被杀掉，这里做自愈
  if pg_isready -q 2>/dev/null; then
    echo "→ PostgreSQL 已就绪"
    return
  fi
  echo "→ PostgreSQL 未就绪，启动中…"
  # launchd 里可能残留失败状态（Bootstrap error），先卸载再启动
  launchctl bootout "gui/$(id -u)/sh.brew.postgresql@18" 2>/dev/null || true
  brew services start postgresql@18 >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    pg_isready -q 2>/dev/null && { echo "  ✓ PostgreSQL 就绪"; return; }
    sleep 1
  done
  echo "  ✗ PostgreSQL 启动失败（看日志：/opt/homebrew/var/log/postgresql@18.log）"
  return 1
}

stop_one() {  # $1=名字 $2=pid文件
  local name="$1" pid_file="$2"
  if is_alive "$pid_file"; then
    local pid; pid="$(cat "$pid_file")"
    echo "→ 停止 $name (pid $pid)"
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null
  fi
  rm -f "$pid_file"
}

start_backend() {
  if is_alive "$PID_DIR/backend.pid"; then
    echo "→ 后端已在运行 (pid $(cat "$PID_DIR/backend.pid"))"
    return
  fi
  if listening "$BACKEND_PORT"; then
    echo "→ 端口 $BACKEND_PORT 已有服务监听（外部启动的后端？），跳过"
    return
  fi
  echo "→ 启动后端 :$BACKEND_PORT"
  cd "$ROOT/backend" || exit 1
  nohup "$VENV/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" \
    >"$BACKEND_LOG" 2>&1 &
  echo $! >"$PID_DIR/backend.pid"
  disown 2>/dev/null || true
}

start_frontend() {
  if is_alive "$PID_DIR/frontend.pid"; then
    echo "→ 前端已在运行 (pid $(cat "$PID_DIR/frontend.pid"))"
    return
  fi
  if listening "$FRONTEND_PORT"; then
    echo "→ 端口 $FRONTEND_PORT 已有服务监听（外部启动的前端？），跳过"
    return
  fi
  echo "→ 启动前端 :$FRONTEND_PORT"
  cd "$ROOT/frontend" || exit 1
  [[ -d node_modules ]] || npm install --no-audit --no-fund
  nohup npx vite --port "$FRONTEND_PORT" >"$FRONTEND_LOG" 2>&1 &
  echo $! >"$PID_DIR/frontend.pid"
  disown 2>/dev/null || true
}

wait_http() {  # $1=url $2=名字
  for _ in $(seq 1 40); do
    if curl -fsS -m 2 "$1" >/dev/null 2>&1; then
      echo "  ✓ $2 就绪"
      return 0
    fi
    sleep 0.5
  done
  echo "  ✗ $2 没起来（看日志：${LOG_DIR}）"
  return 1
}

case "${1:-start}" in
  start)
    ensure_postgres
    start_backend
    start_frontend
    # 等后端起来后跑迁移（幂等）
    for _ in $(seq 1 40); do
      curl -fsS -m 2 "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1 && break
      sleep 0.5
    done
    ( cd "$ROOT/backend" && "$VENV/bin/python" -m alembic upgrade head >/dev/null 2>&1 && echo "  ✓ 数据库迁移已到最新" )
    wait_http "http://127.0.0.1:$BACKEND_PORT/api/health" "后端 :$BACKEND_PORT"
    wait_http "http://127.0.0.1:$FRONTEND_PORT/" "前端 :$FRONTEND_PORT"
    echo
    echo "看板:   http://127.0.0.1:$FRONTEND_PORT"
    echo "API:    http://127.0.0.1:$BACKEND_PORT/docs"
    ;;
  stop)
    stop_one "后端" "$PID_DIR/backend.pid"
    stop_one "前端" "$PID_DIR/frontend.pid"
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    if pg_isready -q 2>/dev/null; then
      echo "✓ postgres 运行中"
    else
      echo "✗ postgres 未运行（start 时会自动拉起）"
    fi
    for name in backend frontend; do
      pid_file="$PID_DIR/$name.pid"
      case "$name" in
        backend) port="$BACKEND_PORT" ;;
        frontend) port="$FRONTEND_PORT" ;;
      esac
      if is_alive "$pid_file"; then
        echo "✓ $name 运行中 (pid $(cat "$pid_file"))"
      elif listening "${port}"; then
        echo "✓ $name 运行中（外部启动，端口 ${port}）"
      else
        echo "✗ $name 未运行"
      fi
    done
    ;;
  logs)
    tail -n 60 -f "$BACKEND_LOG" "$FRONTEND_LOG"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
