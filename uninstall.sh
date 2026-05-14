#!/usr/bin/env bash
# uninstall.sh - stop and remove AI Director runtime integrations.
# Usage:
#   ./uninstall.sh           # disable services/integrations, keep data
#   ./uninstall.sh --purge   # additionally remove local runtime artifacts (.env, venv, logs, redis data)
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YLW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }

run_root() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
    return
  fi
  return 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

resolve_compose_cmd() {
  if command_exists docker && docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return 0
  fi
  if command_exists docker-compose; then
    echo "docker-compose"
    return 0
  fi
  return 1
}

remove_user_service() {
  local service_name="$1"
  local user_service_file="$HOME/.config/systemd/user/${service_name}.service"

  if [[ ! -f "$user_service_file" ]]; then
    info "User service file not found: $user_service_file"
    return
  fi

  if command_exists systemctl && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user stop "$service_name" >/dev/null 2>&1 || true
    systemctl --user disable "$service_name" >/dev/null 2>&1 || true
  else
    warn "systemctl --user is unavailable in this session; removing user service file only"
  fi

  rm -f "$user_service_file"

  if command_exists systemctl && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user reset-failed "$service_name" >/dev/null 2>&1 || true
  fi

  info "Removed user service: $service_name"
}

remove_system_service() {
  local service_name="$1"
  local system_service_file="/etc/systemd/system/${service_name}.service"

  if [[ ! -f "$system_service_file" ]]; then
    info "System service file not found: $system_service_file"
    return
  fi

  run_root systemctl stop "$service_name" >/dev/null 2>&1 || true
  run_root systemctl disable "$service_name" >/dev/null 2>&1 || true
  run_root rm -f "$system_service_file" || die "Failed to remove system service file: $system_service_file"
  run_root systemctl daemon-reload >/dev/null 2>&1 || true
  run_root systemctl reset-failed "$service_name" >/dev/null 2>&1 || true

  info "Removed system service: $service_name"
}

remove_cron_marker() {
  local marker="$1"
  local current
  current="$(crontab -l 2>/dev/null || true)"

  if [[ -z "$current" ]]; then
    info "No user crontab entries found"
    return
  fi

  if echo "$current" | grep -Fq "$marker"; then
    echo "$current" | grep -Fv "$marker" | crontab -
    info "Removed cron entries with marker: $marker"
  else
    info "No cron entries with marker found: $marker"
  fi
}

stop_compose_stack() {
  local compose_cmd="$1"
  local compose_file="$2"

  if [[ -z "$compose_cmd" ]]; then
    warn "Docker Compose command not found; skipping container shutdown"
    return
  fi

  if ! command_exists docker; then
    warn "Docker CLI not found; skipping container shutdown"
    return
  fi

  if ! docker info >/dev/null 2>&1; then
    warn "Docker daemon is not accessible; skipping container shutdown"
    return
  fi

  # shellcheck disable=SC2086
  $compose_cmd -f "$compose_file" down --remove-orphans || warn "Failed to stop compose stack"
  info "Docker compose stack stopped"
}

purge_runtime_artifacts() {
  local script_dir="$1"

  rm -rf "$script_dir/venv"
  rm -f "$script_dir/.env"
  rm -f "$script_dir/logs"/*.log "$script_dir/logs"/*.jsonl 2>/dev/null || true
  rm -f "$script_dir/docker/volumes/redis"/*.rdb 2>/dev/null || true

  info "Purged local runtime artifacts (.env, venv, logs, redis dump files)"
}

print_help() {
  cat <<'EOF'
uninstall.sh - AI Director uninstall helper

Usage:
  ./uninstall.sh [--purge] [--help]

Options:
  --purge   Remove local runtime artifacts in project directory:
            .env, venv, logs/*.log, logs/*.jsonl, docker/volumes/redis/*.rdb
  --help    Show this message
EOF
}

SERVICE_NAME="aidir"
CRON_MARKER="# aidir-cron"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --purge)
      PURGE=1
      ;;
    --help|-h)
      print_help
      exit 0
      ;;
    *)
      die "Unknown argument: $arg"
      ;;
  esac
done

info "Starting AI Director uninstall..."

remove_user_service "$SERVICE_NAME"
remove_system_service "$SERVICE_NAME"
remove_cron_marker "$CRON_MARKER"

COMPOSE_CMD="$(resolve_compose_cmd || true)"
stop_compose_stack "$COMPOSE_CMD" "$COMPOSE_FILE"

if [[ "$PURGE" -eq 1 ]]; then
  purge_runtime_artifacts "$SCRIPT_DIR"
fi

echo ""
info "Uninstall completed"
info "Service removed: $SERVICE_NAME (user and/or system if present)"
info "Cron marker removed: $CRON_MARKER"
info "Compose stack: stopped (if Docker was accessible)"
if [[ "$PURGE" -eq 1 ]]; then
  info "Runtime artifacts: purged"
else
  info "Runtime artifacts: kept (use --purge to remove)"
fi
