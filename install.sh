#!/usr/bin/env bash
#
# One-click installer for the APIPaw Agent (macOS / Linux).
#
# Creates a local virtual environment, installs Python dependencies and
# generates agent.config.json with sensible defaults.
#
# NOTE: this script intentionally contains ASCII-only text so that it behaves
# identically under any locale.
#
# Usage:
#   bash install.sh
#   bash install.sh --no-venv
#   bash install.sh --systemd       # also register a systemd --user service
#   bash install.sh --start-on-boot  # also install a systemd --user service

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

NO_VENV=0
SYSTEMD=0
START_ON_BOOT=0

for arg in "$@"; do
  case "$arg" in
    --no-venv)      NO_VENV=1 ;;
    --systemd)      SYSTEMD=1 ;;
    --start-on-boot) START_ON_BOOT=1 ;;
    -h|--help)
      sed -n '2,15p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

step() { echo "==> $1"; }
ok()   { echo "    [OK] $1"; }
warn() { echo "    [WARN] $1"; }
err()  { echo "    [ERROR] $1" >&2; }

# ---------------------------------------------------------------------------
# 1. Locate a usable Python interpreter (3.10+)
# ---------------------------------------------------------------------------
step "Checking Python"

PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  err "Python 3.10+ was not found on PATH."
  echo "    Install it from https://www.python.org/downloads/ and re-run this script." >&2
  exit 1
fi
ok "Using '$PYTHON_BIN' ($("$PYTHON_BIN" -c 'import sys; print(sys.version.split()[0])'))"

# ---------------------------------------------------------------------------
# 2. Virtual environment
# ---------------------------------------------------------------------------
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

if [ "$NO_VENV" -eq 1 ]; then
  warn "Skipping virtual environment (--no-venv)."
  VENV_PYTHON="$PYTHON_BIN"
elif [ -x "$VENV_PYTHON" ]; then
  step "Reusing existing virtual environment at .venv"
  ok ".venv"
else
  step "Creating virtual environment at .venv"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  if [ ! -x "$VENV_PYTHON" ]; then
    err "Failed to create the virtual environment."
    exit 1
  fi
  ok "Virtual environment created"
fi

# ---------------------------------------------------------------------------
# 3. Dependencies
# ---------------------------------------------------------------------------
step "Installing Python dependencies"
"$VENV_PYTHON" -m pip install --upgrade pip --quiet
"$VENV_PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"
ok "Dependencies installed"

# ---------------------------------------------------------------------------
# 4. agent.config.json
# ---------------------------------------------------------------------------
CONFIG_PATH="$PROJECT_ROOT/agent.config.json"
if [ -f "$CONFIG_PATH" ]; then
  step "Configuration already exists, keeping it"
  ok "agent.config.json"
else
  step "Generating agent.config.json"
  (cd "$PROJECT_ROOT" && "$VENV_PYTHON" -c "from agent_config import get_config; get_config()")
  ok "agent.config.json created"
fi

# ---------------------------------------------------------------------------
# 5. Optional: systemd user service
# ---------------------------------------------------------------------------
if [ "$SYSTEMD" -eq 1 ] || [ "$START_ON_BOOT" -eq 1 ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemd is not available on this system, skipping service registration."
  else
    step "Registering systemd user service"
    UNIT_DIR="$HOME/.config/systemd/user"
    UNIT_PATH="$UNIT_DIR/apipaw-agent.service"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=APIPaw Agent (local OpenAI-compatible node)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
ExecStart=$VENV_PYTHON $PROJECT_ROOT/main.py --no-tray
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable apipaw-agent.service >/dev/null 2>&1 || true
    systemctl --user start apipaw-agent.service >/dev/null 2>&1 || true
    ok "Service registered: systemctl --user status apipaw-agent"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------
echo
echo "APIPaw Agent installation finished."
echo
echo "Start the agent:"
echo "    ./.venv/bin/python main.py"
echo
echo "Health check:"
echo "    curl http://127.0.0.1:52415/health"
echo
echo "Edit 'agent.config.json' to set node_name, hardware_type, wallet_address and node_secret."
exit 0
