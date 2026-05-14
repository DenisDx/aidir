#!/usr/bin/env bash
# install.sh – AI Director full install / re-install script
# Usage: ./install.sh [config-template]
# Can be run on a clean system or for updates (keeps existing data).
set -euo pipefail

# ── Helpers ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YLW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }

resolve_config_template_name() {
  local raw_name="${1:-config}"
  if [[ "$raw_name" == *.* ]]; then
    printf '%s\n' "$raw_name"
  else
    printf '%s.json5.example\n' "$raw_name"
  fi
}

rotate_config_backups() {
  local base_file="$1"
  local bak1="${base_file}.bak"
  local bak2="${bak1}.bak"
  local bak3="${bak2}.bak"

  [[ -f "$bak3" ]] && rm -f "$bak3"
  [[ -f "$bak2" ]] && mv "$bak2" "$bak3"
  [[ -f "$bak1" ]] && mv "$bak1" "$bak2"
  [[ -f "$base_file" ]] && mv "$base_file" "$bak1"
}

ensure_snap_path() {
  if [[ -d /snap/bin ]] && [[ ":$PATH:" != *":/snap/bin:"* ]]; then
    export PATH="/snap/bin:$PATH"
  fi
}

install_python_venv_pkg_apt() {
  local sudo_cmd=""
  local versioned_venv_pkg

  if [[ $EUID -ne 0 ]]; then
    command -v sudo &>/dev/null || return 1
    info "Trying to install python venv package via sudo (password may be requested)..."
    sudo -v || return 1
    sudo_cmd="sudo"
  fi

  $sudo_cmd apt-get update -y || return 1
  if $sudo_cmd apt-get install -y python3-venv; then
    return 0
  fi

  versioned_venv_pkg="$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")' 2>/dev/null || true)"
  [[ -n "$versioned_venv_pkg" ]] || return 1
  warn "python3-venv package unavailable, trying ${versioned_venv_pkg}..."
  $sudo_cmd apt-get install -y "$versioned_venv_pkg"
}

python_venv_works_with_pip() {
  local probe_root
  local probe_env
  probe_root="$(mktemp -d)"
  probe_env="$probe_root/venv-probe"

  if python3 -m venv "$probe_env" >/dev/null 2>&1 && [[ -x "$probe_env/bin/pip" ]]; then
    rm -rf "$probe_root"
    return 0
  fi

  rm -rf "$probe_root"
  return 1
}

ensure_python_venv_ready() {
  if python_venv_works_with_pip; then
    return
  fi

  warn "Python can import venv, but creating venv with pip failed (ensurepip may be missing)."

  if command -v apt-get &>/dev/null; then
    if install_python_venv_pkg_apt && python_venv_works_with_pip; then
      info "Python venv package installed and verified"
      return
    fi
  fi

  local hint_pkg
  hint_pkg="$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")' 2>/dev/null || echo 'python3-venv')"
  die "Python virtual environment creation failed (ensurepip unavailable). Install venv support package (for Debian/Ubuntu: sudo apt install ${hint_pkg}), then re-run ./install.sh"
}

ensure_docker_daemon() {
  if docker info &>/dev/null; then
    return
  fi

  warn "Docker daemon is not accessible, trying to start it..."

  if command -v systemctl &>/dev/null && systemctl list-unit-files 2>/dev/null | grep -q '^docker\.service'; then
    if [[ $EUID -eq 0 ]]; then
      systemctl enable --now docker || true
    elif command -v sudo &>/dev/null; then
      info "Trying to start docker.service via sudo (password may be requested)..."
      sudo systemctl enable --now docker || true
    fi
  fi

  if docker info &>/dev/null; then
    return
  fi

  if command -v snap &>/dev/null && snap list docker &>/dev/null; then
    if [[ $EUID -eq 0 ]]; then
      snap start docker || true
    elif command -v sudo &>/dev/null; then
      info "Trying to start snap docker service via sudo (password may be requested)..."
      sudo snap start docker || true
    fi
  fi

  if docker info &>/dev/null; then
    return
  fi

  # Common case after fresh Docker install: daemon is running, but user session
  # does not yet have docker group membership until re-login.
  if [[ $EUID -ne 0 ]] && command -v sudo &>/dev/null; then
    if sudo docker info &>/dev/null; then
      if command -v getent &>/dev/null && getent group docker >/dev/null; then
        if ! id -nG "$USER" 2>/dev/null | grep -qw docker; then
          if getent group docker | awk -F: '{print $4}' | tr ',' '\n' | grep -Fxq "$USER"; then
            die "Docker is running, but current session does not see docker group yet. Please log out and log in again, then re-run ./install.sh"
          fi
          info "Adding user '$USER' to docker group via sudo (password may be requested)..."
          sudo usermod -aG docker "$USER" || true
          die "User '$USER' was added to docker group. Please log out and log in again, then re-run ./install.sh"
        fi
      fi
      die "Docker daemon is running, but access is denied for user '$USER'. Re-login may be required after adding docker group membership."
    fi
  fi

  docker info &>/dev/null || die "Docker daemon is not running or not accessible for user '$USER'. Try: sudo systemctl enable --now docker (or sudo snap start docker), then ensure user is in docker group: sudo usermod -aG docker $USER"
}

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

diagnose_cron_failure() {
  warn "Cron diagnostics:"

  if ! command -v crontab &>/dev/null; then
    warn "  crontab command is not available in PATH"
    return
  fi

  local who
  who="$(id -un 2>/dev/null || echo unknown)"
  warn "  current user: $who"

  local cron_list_err
  cron_list_err="$(crontab -l 2>&1 >/dev/null || true)"
  if [[ -n "$cron_list_err" ]]; then
    warn "  crontab -l message: $cron_list_err"
  else
    warn "  crontab -l: readable"
  fi

  if [[ -f /etc/cron.allow ]]; then
    if ! grep -Fxq "$who" /etc/cron.allow; then
      warn "  /etc/cron.allow exists and user '$who' is not listed"
    fi
  fi

  if [[ -f /etc/cron.deny ]]; then
    if grep -Fxq "$who" /etc/cron.deny; then
      warn "  /etc/cron.deny contains user '$who'"
    fi
  fi

  if command -v systemctl &>/dev/null; then
    if systemctl list-unit-files 2>/dev/null | grep -q '^cron\.service'; then
      systemctl status cron --no-pager -n 20 >/dev/null 2>&1 \
        && warn "  cron.service: present" \
        || warn "  cron.service: present but status unavailable without elevated rights"
    elif systemctl list-unit-files 2>/dev/null | grep -q '^crond\.service'; then
      systemctl status crond --no-pager -n 20 >/dev/null 2>&1 \
        && warn "  crond.service: present" \
        || warn "  crond.service: present but status unavailable without elevated rights"
    fi
  fi
}

wait_for_http_status() {
  local url="$1"
  local expected_status="$2"
  local timeout_seconds="$3"
  local label="$4"
  local i
  local code

  for i in $(seq 1 "$timeout_seconds"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)"
    if [[ "$code" == "$expected_status" ]]; then
      info "$label is ready ($url -> $code)"
      return 0
    fi
    sleep 1
  done

  warn "$label is not ready ($url expected $expected_status, got ${code:-n/a})"
  return 1
}

print_service_diagnostics() {
  warn "Service diagnostics:"
  $SYSTEMCTL status "$SERVICE_NAME" --no-pager -n 80 || true

  if command -v journalctl &>/dev/null; then
    if [[ "$SERVICE_MODE" == "system" ]]; then
      journalctl -u "$SERVICE_NAME" -n 120 --no-pager || true
    else
      journalctl --user -u "$SERVICE_NAME" -n 120 --no-pager || true
    fi
  fi

  if docker ps --format '{{.Names}}' | grep -Fxq aidir_nginx; then
    warn "Last nginx logs:"
    docker logs --tail 120 aidir_nginx || true
  fi
}

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
ENV_FILE="$SCRIPT_DIR/.env"
ENV_EXAMPLE="$SCRIPT_DIR/.env.example"
CONFIG_FILE="$SCRIPT_DIR/config.json5"
CONFIG_TEMPLATE_ARG="${1:-config}"
CONFIG_TEMPLATE_NAME="$(resolve_config_template_name "$CONFIG_TEMPLATE_ARG")"
if [[ "$CONFIG_TEMPLATE_NAME" = /* ]]; then
  CONFIG_TEMPLATE_FILE="$CONFIG_TEMPLATE_NAME"
else
  CONFIG_TEMPLATE_FILE="$SCRIPT_DIR/$CONFIG_TEMPLATE_NAME"
fi
CONFIG_TEMPLATE_EXPLICIT=0
if [[ $# -gt 0 ]]; then
  CONFIG_TEMPLATE_EXPLICIT=1
fi
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
CORE_APP="$SCRIPT_DIR/core/app.py"
CRON_SCRIPT="$SCRIPT_DIR/core/cron.py"
SERVICE_NAME="aidir"
ENV_CREATED=0
VENV_CREATED=0
DOCKER_BUILT=0
DOCKER_STARTED=0
CRON_UPDATED=0
CRON_STATE="not-configured"

# ── Step 1: Prerequisites check ───────────────────────────────────────────────
info "Checking prerequisites…"

ensure_snap_path

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
ensure_python_venv_ready

# Docker daemon reachable?
ensure_docker_daemon

# Required project files
require_file "$ENV_EXAMPLE"
require_file "$COMPOSE_FILE"
require_file "$CORE_APP"
require_file "$CRON_SCRIPT"
require_file "$SCRIPT_DIR/requirements.txt"

if [[ "$CONFIG_TEMPLATE_EXPLICIT" -eq 1 || ! -f "$CONFIG_FILE" ]]; then
  require_file "$CONFIG_TEMPLATE_FILE"
fi

info "Prerequisites OK"

# ── Step 1b: Config template setup ───────────────────────────────────────────
if [[ "$CONFIG_TEMPLATE_EXPLICIT" -eq 1 || ! -f "$CONFIG_FILE" ]]; then
  if [[ -f "$CONFIG_FILE" ]]; then
    info "Rotating existing config.json5 backups…"
    rotate_config_backups "$CONFIG_FILE"
  else
    info "No active config.json5 found; creating it from template…"
  fi

  cp "$CONFIG_TEMPLATE_FILE" "$CONFIG_FILE"
  info "Active config created from template: $(basename "$CONFIG_TEMPLATE_FILE")"
fi

# ── Step 2: .env setup ────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  warn ".env not found – copying from .env.example"
  cp "$ENV_EXAMPLE" "$ENV_FILE"

  # Auto-fill required runtime values.
  set_env_var "$ENV_FILE" "AIDIR_ROOT" "$SCRIPT_DIR"

  # Ask for admin password twice and verify both entries match.
  ROOT_PASSWORD_INPUT=""
  while true; do
    echo
    read -r -s -p "Enter ROOT_PASSWORD for WebUI admin (leave empty to auto-generate): " ROOT_PASSWORD_FIRST
    echo
    read -r -s -p "Repeat ROOT_PASSWORD for WebUI admin: " ROOT_PASSWORD_SECOND
    echo

    if [[ "$ROOT_PASSWORD_FIRST" != "$ROOT_PASSWORD_SECOND" ]]; then
      warn "Passwords do not match. Please try again."
      continue
    fi

    if [[ -z "$ROOT_PASSWORD_FIRST" ]]; then
      ROOT_PASSWORD_INPUT="$(generate_password)"
      info "ROOT_PASSWORD auto-generated. Save it now: $ROOT_PASSWORD_INPUT"
    else
      ROOT_PASSWORD_INPUT="$ROOT_PASSWORD_FIRST"
    fi
    break
  done
  set_env_var "$ENV_FILE" "ROOT_PASSWORD" "$ROOT_PASSWORD_INPUT"

  chmod 600 "$ENV_FILE"
  info ".env created and initialized"
  ENV_CREATED=1
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
if [[ -d "$VENV_DIR" ]] && [[ ! -x "$VENV_DIR/bin/python" || ! -x "$VENV_DIR/bin/pip" ]]; then
  warn "Existing venv is incomplete (missing bin/python or bin/pip); recreating it..."
  rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating Python virtual environment…"
  python3 -m venv "$VENV_DIR"
  VENV_CREATED=1
fi

info "Installing / upgrading Python dependencies…"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
info "Python dependencies OK"

# ── Step 4: Docker images / containers ────────────────────────────────────────
info "Building Docker images…"
cd "$SCRIPT_DIR"
if ! $DOCKER_COMPOSE build; then
  warn "Docker build failed (possible containerd cache issue); retrying with --no-cache…"
  $DOCKER_COMPOSE build --no-cache || die "Docker build failed on retry. Check Docker/containerd state and re-run."
fi
DOCKER_BUILT=1

info "Starting Docker services (redis, nginx)…"
$DOCKER_COMPOSE up -d --remove-orphans
DOCKER_STARTED=1

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
# Ensure marker entry is present and up-to-date on every run.
CRON_ENTRY="$CRON_JOB $CRON_MARKER"
CURRENT_CRONTAB="$(crontab -l 2>/dev/null || true)"

if printf '%s\n' "$CURRENT_CRONTAB" | grep -Fqx "$CRON_ENTRY"; then
  info "Cron entry already configured"
  CRON_UPDATED=1
  CRON_STATE="already-configured"
else
  FILTERED_CRONTAB="$(printf '%s\n' "$CURRENT_CRONTAB" | grep -Fv "$CRON_MARKER" || true)"
  if [[ -n "$FILTERED_CRONTAB" ]]; then
    NEW_CRONTAB="$FILTERED_CRONTAB"
    NEW_CRONTAB+=$'\n'
    NEW_CRONTAB+="$CRON_ENTRY"
  else
    NEW_CRONTAB="$CRON_ENTRY"
  fi

  if printf '%s\n' "$NEW_CRONTAB" | crontab -; then
    info "Cron entry set: $CRON_JOB"
    CRON_UPDATED=1
    CRON_STATE="updated"
  else
    warn "Failed to configure cron entry; continuing without periodic cron maintenance"
    diagnose_cron_failure
    CRON_STATE="failed"
  fi
fi

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

DOCKER_COMPOSE_BIN="$(command -v docker) compose"
SERVICE_EXEC_START_PRE="ExecStartPre=$DOCKER_COMPOSE_BIN -f $SCRIPT_DIR/docker-compose.yml up -d"

SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME.service"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=$SERVICE_DESC
After=$SERVICE_AFTER
$SERVICE_REQUIRES_BLOCK

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
$SERVICE_EXEC_START_PRE
ExecStart=$SERVICE_EXEC
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
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

if [[ $EUID -eq 0 ]]; then
  SERVICE_MODE="system"
else
  SERVICE_MODE="user"
fi

HEALTH_OK=1
if command -v curl &>/dev/null; then
  info "Running post-install health checks..."

  wait_for_http_status "http://127.0.0.1:${WEBUI_PORT:-20082}/api/auth/me" "401" 30 "WebUI backend" || HEALTH_OK=0
  wait_for_http_status "http://127.0.0.1:${OPENAIX_ENDPOINT_PORT:-21434}/health" "200" 30 "OpenAIx endpoint" || HEALTH_OK=0
  wait_for_http_status "http://127.0.0.1:${MCP_ENDPOINT_PORT:-20001}/health" "200" 30 "MCP endpoint" || HEALTH_OK=0
  wait_for_http_status "http://127.0.0.1:${NGINX_HTTP_PORT:-8080}/api/auth/me" "401" 30 "nginx -> WebUI proxy" || HEALTH_OK=0
else
  warn "curl is not available; skipping HTTP health checks"
fi

if [[ "$HEALTH_OK" -ne 1 ]]; then
  print_service_diagnostics
  die "Post-install health checks failed. See diagnostics above; fix the root cause and re-run ./install.sh"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "╔═══════════════════════════════════════════════╗"
info "║  AI Director installed successfully!          ║"
info "╚═══════════════════════════════════════════════╝"
echo ""
info "Install report:"
if [[ "$ENV_CREATED" -eq 1 ]]; then
  info "  .env file      : created from .env.example"
else
  info "  .env file      : reused existing"
fi
if [[ "$VENV_CREATED" -eq 1 ]]; then
  info "  Python venv    : created"
else
  info "  Python venv    : reused existing"
fi
if [[ "$DOCKER_BUILT" -eq 1 ]]; then
  info "  Docker build   : completed"
fi
if [[ "$DOCKER_STARTED" -eq 1 ]]; then
  info "  Docker services: started (redis, nginx)"
fi
if [[ "$CRON_UPDATED" -eq 1 ]]; then
  info "  Cron schedule  : ${CRON_STATE} (${CRON_PERIOD}m)"
else
  info "  Cron schedule  : not configured (see warnings above)"
fi
info "  Systemd mode   : $SERVICE_MODE"
info "  Service file   : $SERVICE_FILE"

echo ""
  info "What is running and where:"
  info "  Web GUI (nginx)        : http://localhost:${NGINX_HTTP_PORT:-8080}"
  info "  OpenAIx/Ollama API     : http://localhost:${OPENAIX_ENDPOINT_PORT:-21434}"
  info "  MCP endpoint           : http://localhost:${MCP_ENDPOINT_PORT:-20001}/mcp"
  info "  WebUI backend (direct) : http://${WEBUI_HOST:-0.0.0.0}:${WEBUI_PORT:-20082}"
  info "  Redis                  : ${REDIS_HOST:-127.0.0.1}:${REDIS_PORT:-6379}"
  info "  Upstream Ollama        : ${OLLAMA_BASE_URL:-http://127.0.0.1:11434}"

  echo ""
  info "How to open Web GUI:"
  info "  1) Open in browser: http://localhost:${NGINX_HTTP_PORT:-8080}"
  info "  2) Login with ROOT_USER / ROOT_PASSWORD from ${ENV_FILE}"

  echo ""
  info "Key .env values to verify:"
  info "  AIDIR_ROOT             : project absolute path"
  info "  ROOT_USER / ROOT_PASSWORD : Web GUI credentials"
  info "  NGINX_HTTP_PORT        : public Web GUI port"
  info "  WEBUI_HOST / WEBUI_PORT: internal WebUI backend bind"
  info "  OPENAIX_ENDPOINT_PORT  : AI HTTP API port"
  info "  MCP_ENDPOINT_PORT      : MCP API port"
  info "  REDIS_HOST / REDIS_PORT: Redis connection"
  info "  OLLAMA_BASE_URL        : upstream Ollama URL"

  echo ""
  info "Useful paths:"
  info "  Main config            : $SCRIPT_DIR/config.json5"
  info "  Environment            : $ENV_FILE"
  info "  Compose file           : $COMPOSE_FILE"
  info "  Combined logs          : $SCRIPT_DIR/logs/all.log"
  info "  Restart service        : $SYSTEMCTL restart $SERVICE_NAME"
  info "  Reload config          : $SYSTEMCTL reload $SERVICE_NAME  (or kill -HUP <pid>)"

echo ""
info "Recommended checks after install:"
info "  1) Check open ports: ss -ltnp | grep -E ':(8080|20082|21434|20001|6379)\\b'"
info "  2) Check docker services: $DOCKER_COMPOSE ps"
info "  3) Check core service: $SYSTEMCTL status $SERVICE_NAME --no-pager -n 20"
info "  4) Check upstream Ollama: curl -fsS ${OLLAMA_BASE_URL:-http://127.0.0.1:11434}/api/tags"
  info "  5) Check app endpoint: curl -fsS http://localhost:${OPENAIX_ENDPOINT_PORT:-21434}/health"
