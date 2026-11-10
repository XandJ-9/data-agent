#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PID_DIR="${DATA_AGENT_PID_DIR:-/tmp/data-agent-pids}"
LOG_DIR="${DATA_AGENT_LOG_DIR:-$PWD/logs}"
SERVE_PID="$PID_DIR/serve.pid"
WORKER_PID="$PID_DIR/worker.pid"
SERVE_LOG="$LOG_DIR/serve.log"
WORKER_LOG="$LOG_DIR/worker.log"

cmd=${1:-}

_log() { echo "[$(date '+%H:%M:%S')] $*"; }

_running() {
  [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null
}

_start_serve() {
  if _running "$SERVE_PID"; then
    _log "serve 已在运行 (pid=$(cat "$SERVE_PID"))"
    return
  fi
  mkdir -p "$PID_DIR" "$LOG_DIR"
  nohup uv run data-agent serve >> "$SERVE_LOG" 2>&1 &
  echo $! > "$SERVE_PID"
  _log "serve 已启动 (pid=$! 日志: $SERVE_LOG)"
}

_start_worker() {
  if _running "$WORKER_PID"; then
    _log "worker 已在运行 (pid=$(cat "$WORKER_PID"))"
    return
  fi
  mkdir -p "$PID_DIR" "$LOG_DIR"
  nohup uv run data-agent worker >> "$WORKER_LOG" 2>&1 &
  echo $! > "$WORKER_PID"
  _log "worker 已启动 (pid=$! 日志: $WORKER_LOG)"
}

_kill() {
  local pid_file=$1 name=$2
  if [ -f "$pid_file" ]; then
    local pid
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      _log "$name 已停止 (pid=$pid)"
    fi
    rm -f "$pid_file"
  fi
  pkill -f "data-agent $name" 2>/dev/null || true
}

_start_fe() {
  if pgrep -f "vite.*data-agent" >/dev/null 2>&1; then
    _log "前端已在运行"
    return
  fi
  cd web && npm run dev &
  _log "前端已启动 (vite dev server)"
}

case "$cmd" in
  start)
    _log "启动 data-agent…"
    _log "初始化数据库…"
    uv run data-agent init-db
    _start_serve
    sleep 1
    _start_worker
    _log "全部就绪"
    ;;
  dev)
    _log "启动开发模式（后端热加载 + 前端 HMR）…"
    _log "初始化数据库…"
    uv run data-agent init-db
    # 后端使用 uvicorn --reload，监听 src/ 目录变更自动重启
    if _running "$SERVE_PID"; then
      _log "serve 已在运行 (pid=$(cat "$SERVE_PID"))"
    else
      mkdir -p "$PID_DIR" "$LOG_DIR"
      nohup uv run data-agent dev >> "$SERVE_LOG" 2>&1 &
      echo $! > "$SERVE_PID"
      _log "serve 已启动（热加载模式，pid=$! 日志: $SERVE_LOG)"
    fi
    sleep 1
    _start_worker
    _start_fe
    _log "开发模式就绪 · 后端热加载（src/） + 前端 HMR（web/src/）"
    _log "使用 ./run.sh stop 停止所有服务"
    ;;
  stop)
    _log "停止 data-agent…"
    _kill "$SERVE_PID" serve
    pkill -f "data-agent dev" 2>/dev/null || true
    _kill "$WORKER_PID" worker
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  fe)
    _start_fe
    ;;
  status)
    if _running "$SERVE_PID"; then
      _log "serve  运行中 (pid=$(cat "$SERVE_PID"))"
    else
      _log "serve  未运行"
    fi
    if _running "$WORKER_PID"; then
      _log "worker 运行中 (pid=$(cat "$WORKER_PID"))"
    else
      _log "worker 未运行"
    fi
    ;;
  logs)
    tail -f "$SERVE_LOG" "$WORKER_LOG"
    ;;
  *)
    echo "用法: ./run.sh {start|stop|restart|status|logs|fe|dev}"
    exit 1
    ;;
esac