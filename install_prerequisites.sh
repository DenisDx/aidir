#!/usr/bin/env bash
# install_prerequisites.sh - checks and installs Python, Docker, Docker Compose
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YLW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }

SUDO=""
SUDO_VERIFIED=0

# Return 0 if command exists in PATH.
has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

# Verify sudo access once before privileged operations.
ensure_sudo_access() {
  if [[ -z "$SUDO" ]]; then
    return
  fi
  if [[ "$SUDO_VERIFIED" -eq 1 ]]; then
    return
  fi

  info "Privileged operation required. sudo may ask for your password..."
  sudo -v || die "sudo authentication failed"
  SUDO_VERIFIED=1
}

# Ensure /snap/bin is in PATH if available (for snap-installed docker tools).
ensure_snap_path() {
  if [[ -d /snap/bin ]] && [[ ":$PATH:" != *":/snap/bin:"* ]]; then
    export PATH="/snap/bin:$PATH"
  fi
}

# Detect supported package manager and print its id.
detect_pm() {
  if has_cmd apt-get; then
    echo "apt"
    return
  fi
  if has_cmd dnf; then
    echo "dnf"
    return
  fi
  if has_cmd yum; then
    echo "yum"
    return
  fi
  if has_cmd pacman; then
    echo "pacman"
    return
  fi
  if has_cmd zypper; then
    echo "zypper"
    return
  fi
  if has_cmd apk; then
    echo "apk"
    return
  fi
  echo ""
}

# Install OS packages for the detected package manager.
install_packages() {
  local pm="$1"
  shift
  local packages=("$@")

  ensure_sudo_access

  case "$pm" in
    apt)
      $SUDO apt-get update -y
      $SUDO apt-get install -y "${packages[@]}"
      ;;
    dnf)
      $SUDO dnf install -y "${packages[@]}"
      ;;
    yum)
      $SUDO yum install -y "${packages[@]}"
      ;;
    pacman)
      $SUDO pacman -Sy --noconfirm "${packages[@]}"
      ;;
    zypper)
      $SUDO zypper --non-interactive install "${packages[@]}"
      ;;
    apk)
      $SUDO apk add --no-cache "${packages[@]}"
      ;;
    *)
      die "Unsupported package manager: $pm"
      ;;
  esac
}

# Install Python 3 if missing.
ensure_python() {
  if has_cmd python3; then
    info "Python found: $(python3 --version 2>/dev/null || true)"
    return
  fi

  info "Python 3 is missing, installing..."
  case "$PM" in
    apt) install_packages "$PM" python3 python3-venv python3-pip ;;
    dnf|yum) install_packages "$PM" python3 python3-pip ;;
    pacman) install_packages "$PM" python python-pip ;;
    zypper) install_packages "$PM" python3 python3-pip ;;
    apk) install_packages "$PM" python3 py3-pip ;;
    *) die "Cannot install Python automatically for package manager: $PM" ;;
  esac

  has_cmd python3 || die "Python installation failed: python3 command not found"
  info "Python installed: $(python3 --version 2>/dev/null || true)"
}

# Install Docker engine/CLI if missing.
ensure_docker() {
  ensure_snap_path

  if ! has_cmd docker && [[ -x /snap/bin/docker ]]; then
    export PATH="/snap/bin:$PATH"
  fi

  if has_cmd docker; then
    info "Docker found: $(docker --version 2>/dev/null || true)"
    return
  fi

  info "Docker is missing, installing..."
  case "$PM" in
    apt) install_packages "$PM" docker.io ;;
    dnf|yum) install_packages "$PM" docker ;;
    pacman) install_packages "$PM" docker ;;
    zypper) install_packages "$PM" docker ;;
    apk) install_packages "$PM" docker docker-cli ;;
    *) die "Cannot install Docker automatically for package manager: $PM" ;;
  esac

  has_cmd docker || die "Docker installation failed: docker command not found"
  info "Docker installed: $(docker --version 2>/dev/null || true)"
}

# Install Docker Compose plugin/standalone if both variants are missing.
ensure_compose() {
  ensure_snap_path

  if docker compose version >/dev/null 2>&1; then
    info "Docker Compose plugin found: $(docker compose version 2>/dev/null | head -n1 || true)"
    return
  fi

  if ! has_cmd docker-compose && [[ -x /snap/bin/docker-compose ]]; then
    export PATH="/snap/bin:$PATH"
  fi

  if has_cmd docker-compose; then
    info "Docker Compose standalone found: $(docker-compose --version 2>/dev/null || true)"
    return
  fi

  info "Docker Compose is missing, installing..."
  case "$PM" in
    apt)
      # Ubuntu/Debian package names differ across releases.
      ensure_sudo_access
      $SUDO apt-get update -y
      if $SUDO apt-get install -y docker-compose-plugin; then
        :
      elif $SUDO apt-get install -y docker-compose-v2; then
        warn "Installed docker-compose-v2 fallback package"
      else
        warn "Plugin package is unavailable, trying standalone docker-compose..."
        $SUDO apt-get install -y docker-compose
      fi
      ;;
    dnf|yum)
      install_packages "$PM" docker-compose-plugin
      ;;
    pacman)
      install_packages "$PM" docker-compose
      ;;
    zypper)
      install_packages "$PM" docker-compose
      ;;
    apk)
      install_packages "$PM" docker-cli-compose
      ;;
    *)
      die "Cannot install Docker Compose automatically for package manager: $PM"
      ;;
  esac

  if docker compose version >/dev/null 2>&1; then
    info "Docker Compose plugin installed"
    return
  fi
  if has_cmd docker-compose; then
    info "Docker Compose standalone installed"
    return
  fi

  die "Docker Compose installation failed"
}

# Try to make Docker daemon available.
ensure_docker_daemon() {
  if docker info >/dev/null 2>&1; then
    info "Docker daemon is accessible"
    return
  fi

  warn "Docker daemon is not accessible, trying to start service..."

  if has_cmd systemctl && (systemctl list-unit-files 2>/dev/null | grep -q '^docker\.service'); then
    ensure_sudo_access
    $SUDO systemctl enable --now docker || true
  fi

  if docker info >/dev/null 2>&1; then
    info "Docker daemon is now accessible"
    return
  fi

  warn "Docker command exists, but daemon is still unavailable."
  warn "If Docker is installed via snap, ensure snap service is running: sudo snap start docker"
  warn "If permission is denied, add your user to docker group and re-login: sudo usermod -aG docker $USER"
}

main() {
  ensure_snap_path

  if [[ $EUID -eq 0 ]]; then
    SUDO=""
  else
    has_cmd sudo || die "This script needs root privileges via sudo"
    SUDO="sudo"
  fi

  PM="$(detect_pm)"
  [[ -n "$PM" ]] || die "No supported package manager found (apt, dnf, yum, pacman, zypper, apk)"

  info "Detected package manager: $PM"

  ensure_python
  ensure_docker
  ensure_compose
  ensure_docker_daemon

  echo
  info "All prerequisites are installed and checked"
  info "Detected binaries:"
  info "  python3        -> $(command -v python3 || echo 'not found')"
  info "  docker         -> $(command -v docker || echo 'not found')"
  if docker compose version >/dev/null 2>&1; then
    info "  docker compose -> plugin"
  elif has_cmd docker-compose; then
    info "  docker-compose -> standalone"
  else
    info "  docker compose -> not found"
  fi
}

main "$@"
