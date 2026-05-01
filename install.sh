#!/usr/bin/env bash
# install.sh – AI Director full install / re-install script
# Usage: ./install.sh
# Can be run on a clean system or for updates (keeps existing data).
set -euo pipefail

# ── Helpers ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YLW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }

require_file() {
  [[ -f "$1" ]] || die "Required file not found: $1"
}

set_env_var() {
  local file="$1"
  local key="$2"
  local value="$3"
  local escaped_value
  escaped_value=$(printf '%s' "$value" | sed 's/[&|\\]/\\&/g')
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${escaped_value}|" "$file"
  else
    echo "${key}=${value}" >> "$file"
  fi
}

generate_password() {
  tr -dc 'A-Za-z0-9!@#%^*_' < /dev/urandom | head -c 20
}

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
ENV_FILE="$SCRIPT_DIR/.env"
ENV_EXAMPLE="$SCRIPT_DIR/.env.example"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
CORE_APP="$SCRIPT_DIR/core/app.py"
CRON_SCRIPT="$SCRIPT_DIR/core/cron.py"
SERVICE_NAME="aidir"

# ── Step 1: Prerequisites check ───────────────────────────────────────────────
info "Checking prerequisites…"

check_cmd() {
  command -v "$1" &>/dev/null || die "Required command not found: $1. Please install it and re-run."
}

check_cmd docker
check_cmd python3
check_cmd crontab

# docker compose v2 (plugin) or v1 (standalone)
if docker compose version &>/dev/null 2>&1; then
  DOCKER_COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  DOCKER_COMPOSE="docker-compose"
else
  die "Docker Compose not found. Install Docker Compose plugin or standalone docker-compose."
fi
info "Docker Compose: $DOCKER_COMPOSE"

# Check python3-venv
python3 -c "import venv" 2>/dev/null || die "python3-venv not available. Install python3-venv package."

# Docker daemon reachable?
docker info &>/dev/null || die "Docker daemon not running or not accessible. Start Docker and check permissions."

# Required project files
require_file "$ENV_EXAMPLE"
require_file "$COMPOSE_FILE"
require_file "$CORE_APP"
require_file "$CRON_SCRIPT"
require_file "$SCRIPT_DIR/requirements.txt"

info "Prerequisites OK"

# ── Step 2: .env setup ────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  warn ".env not found – copying from .env.example"
  cp "$ENV_EXAMPLE" "$ENV_FILE"

  # Auto-fill required runtime values.
  set_env_var "$ENV_FILE" "AIDIR_ROOT" "$SCRIPT_DIR"

  # Ask only for essential interactive value required by SPEC.
  echo
  read -r -s -p "Enter ROOT_PASSWORD for WebUI admin (leave empty to auto-generate): " ROOT_PASSWORD_INPUT
  echo
  if [[ -z "$ROOT_PASSWORD_INPUT" ]]; then
    ROOT_PASSWORD_INPUT="$(generate_password)"
    info "ROOT_PASSWORD auto-generated. Save it now: $ROOT_PASSWORD_INPUT"
  fi
  set_env_var "$ENV_FILE" "ROOT_PASSWORD" "$ROOT_PASSWORD_INPUT"

  chmod 600 "$ENV_FILE"
  info ".env created and initialized"
else
  # Keep existing env file, but ensure required root path stays correct.
  set_env_var "$ENV_FILE" "AIDIR_ROOT" "$SCRIPT_DIR"
fi

# Load .env into current shell (for compose variable substitution)
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Ensure nginx public port is configured and does not conflict with backend port.
if [[ -z "${NGINX_HTTP_PORT:-}" ]]; then
  NGINX_HTTP_PORT=8080
  set_env_var "$ENV_FILE" "NGINX_HTTP_PORT" "$NGINX_HTTP_PORT"
  info "NGINX_HTTP_PORT not set; defaulted to $NGINX_HTTP_PORT"
fi

if [[ "${NGINX_HTTP_PORT}" == "${WEBUI_PORT}" ]]; then
  if [[ "${WEBUI_PORT}" != "8080" ]]; then
    NGINX_HTTP_PORT=8080
  else
    NGINX_HTTP_PORT=8081
  fi
  set_env_var "$ENV_FILE" "NGINX_HTTP_PORT" "$NGINX_HTTP_PORT"
  warn "WEBUI_PORT and NGINX_HTTP_PORT were equal; NGINX_HTTP_PORT changed to $NGINX_HTTP_PORT"
fi

# ── Step 3: venv + dependencies ───────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating Python virtual environment…"
  python3 -m venv "$VENV_DIR"
fi

info "Installing / upgrading Python dependencies…"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
info "Python dependencies OK"

# ── Step 4: Docker images / containers ────────────────────────────────────────
info "Building Docker images…"
cd "$SCRIPT_DIR"
$DOCKER_COMPOSE build

info "Starting Docker services (redis, nginx)…"
$DOCKER_COMPOSE up -d --remove-orphans

# Wait for Redis
info "Waiting for Redis to be healthy…"
REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
for i in $(seq 1 20); do
  if docker exec aidir_redis redis-cli ping 2>/dev/null | grep -q PONG; then
    info "Redis is up"
    break
  fi
  [[ $i -eq 20 ]] && die "Redis did not become healthy in time"
  sleep 1
done

# ── Step 5: Cron entry ────────────────────────────────────────────────────────
CRON_PERIOD="${CRON_PERIOD:-1}"
if ! [[ "$CRON_PERIOD" =~ ^[0-9]+$ ]] || [[ "$CRON_PERIOD" -le 0 ]]; then
  warn "Invalid CRON_PERIOD=$CRON_PERIOD; fallback to 1 minute"
  CRON_PERIOD=1
fi

# Run each minute and execute cron.py only on exact period boundaries.
# This supports any positive CRON_PERIOD in minutes without extra helper scripts.
CRON_JOB="* * * * * [ \$(( \$(date +\\%s) / 60 % $CRON_PERIOD )) -eq 0 ] && $VENV_DIR/bin/python $CRON_SCRIPT"
CRON_MARKER="# aidir-cron"

info "Configuring cron job (CRON_PERIOD=${CRON_PERIOD}m)…"
# Remove old aidir cron entries, add new one
( crontab -l 2>/dev/null | grep -v "$CRON_MARKER" ; \
  echo "$CRON_JOB $CRON_MARKER" ) | crontab -
info "Cron entry set: $CRON_JOB"

# ── Step 6: Systemd service ───────────────────────────────────────────────────
info "Registering systemd service ($SERVICE_NAME)…"

SERVICE_EXEC="$VENV_DIR/bin/python $CORE_APP"
SERVICE_DESC="AI Director core service"

if [[ $EUID -eq 0 ]]; then
  SYSTEMD_DIR="/etc/systemd/system"
  SYSTEMCTL="systemctl"
  WANTED_BY="multi-user.target"
  SERVICE_AFTER="network.target docker.service"
  SERVICE_REQUIRES_BLOCK="Requires=docker.service"
else
  SYSTEMD_DIR="$HOME/.config/systemd/user"
  SYSTEMCTL="systemctl --user"
  WANTED_BY="default.target"
  SERVICE_AFTER="network.target"
  SERVICE_REQUIRES_BLOCK=""
  mkdir -p "$SYSTEMD_DIR"
  # Enable linger so user service survives logout
  loginctl enable-linger "$USER" 2>/dev/null || true
fi

SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME.service"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=$SERVICE_DESC
After=$SERVICE_AFTER
$SERVICE_REQUIRES_BLOCK

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SERVICE_EXEC
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=$WANTED_BY
EOF

if [[ $EUID -ne 0 ]]; then
  # systemctl --user requires a user session bus.
  $SYSTEMCTL show-environment >/dev/null 2>&1 || die "systemctl --user is unavailable (no user DBus session). Login with a regular user session and re-run install.sh, or run as root for a system service."
fi

$SYSTEMCTL daemon-reload || die "Failed to reload systemd daemon"
$SYSTEMCTL enable "$SERVICE_NAME" || die "Failed to enable $SERVICE_NAME"
$SYSTEMCTL restart "$SERVICE_NAME" || die "Failed to restart $SERVICE_NAME"

# Verify service is present and active.
if [[ $EUID -eq 0 ]]; then
  $SYSTEMCTL status "$SERVICE_NAME" --no-pager -n 20 || die "Service registered but not active"
else
  $SYSTEMCTL status "$SERVICE_NAME" --no-pager -n 20 || die "User service registered but not active"
fi

info "Service $SERVICE_NAME started"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "╔═══════════════════════════════════════════════╗"
info "║  AI Director installed successfully!          ║"
info "╚═══════════════════════════════════════════════╝"
echo ""
info "Ollama endpoint : http://localhost:${OLLAMA_ENDPOINT_PORT:-21434}"
info "WebUI           : http://localhost:${NGINX_HTTP_PORT:-20080}"
echo ""
info "Test:"
info "  curl http://localhost:\${OLLAMA_ENDPOINT_PORT:-21434}/api/chat \\"
info "    -d '{\"model\":\"llama3\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"stream\":false}'"
echo ""
info "Logs: $SCRIPT_DIR/logs/all.log"
info "Restart service: $SYSTEMCTL restart $SERVICE_NAME"
info "Reload config  : $SYSTEMCTL reload $SERVICE_NAME  (or kill -HUP <pid>)"
