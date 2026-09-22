#!/bin/bash
#
# Plexus device setup
#
# Usage:
#   curl -sL https://app.plexus.company/setup | bash -s -- --key plx_abc123 --name my-device-01
#
# Note: The canonical version of this script is served from the frontend
# at app/setup/route.ts. This copy is for reference/offline use.
#

set -e

# ─────────────────────────────────────────────────────────────────────────────
# Styling
# ─────────────────────────────────────────────────────────────────────────────

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

# Symbols
CHECK="✓"
CROSS="✗"
BULLET="•"
SPINNER_FRAMES=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")

# Layout
WIDTH=45

header() {
    echo ""
    echo -e "  ${DIM}┌$(printf '─%.0s' $(seq 1 $((WIDTH-2))))┐${NC}"
    echo -e "  ${DIM}│  $1$(printf ' %.0s' $(seq 1 $((WIDTH-${#1}-5))))│${NC}"
    echo -e "  ${DIM}└$(printf '─%.0s' $(seq 1 $((WIDTH-2))))┘${NC}"
    echo ""
}

divider() {
    echo -e "  ${DIM}$(printf '─%.0s' $(seq 1 $((WIDTH-2))))${NC}"
}

success() {
    echo -e "  ${GREEN}$CHECK $1${NC}"
}

error() {
    echo -e "  ${RED}$CROSS $1${NC}"
}

warn() {
    echo -e "  ${YELLOW}$BULLET $1${NC}"
}

info() {
    echo "  $1"
}

dim() {
    echo -e "  ${DIM}$1${NC}"
}

hint() {
    echo -e "  ${CYAN}$1${NC}"
}

label() {
    printf "  %-12s %s\n" "$1" "$2"
}

# Spinner function
spin() {
    local msg="$1"
    local pid=$2
    local i=0
    while kill -0 $pid 2>/dev/null; do
        printf "\r  ${SPINNER_FRAMES[$((i % 10))]} %s" "$msg"
        i=$((i + 1))
        sleep 0.08
    done
    printf "\r%*s\r" $((WIDTH + 10)) ""
}

# Run command with spinner
run_with_spinner() {
    local msg="$1"
    shift
    "$@" &>/dev/null &
    local pid=$!
    spin "$msg" $pid
    wait $pid
    return $?
}

# ─────────────────────────────────────────────────────────────────────────────
# Parse Arguments
# ─────────────────────────────────────────────────────────────────────────────

API_KEY=""
DEVICE_NAME=""
DEVICE_SLUG=""
ORG_ID=""
ENDPOINT="https://app.plexus.company"

while [[ $# -gt 0 ]]; do
    case $1 in
        --api-key|--key|-k)
            API_KEY="$2"
            shift 2
            ;;
        --name|-n)
            DEVICE_NAME="$2"
            shift 2
            ;;
        --device-id|-s|--slug)
            DEVICE_SLUG="$2"
            shift 2
            ;;
        --org)
            ORG_ID="$2"
            shift 2
            ;;
        --endpoint)
            ENDPOINT="$2"
            shift 2
            ;;
        # Accepted and ignored: this script no longer installs a service.
        --no-service)
            shift
            ;;
        *)
            shift
            ;;
    esac
done

# The wire slug rule — gateway/validate.go sourceIDPattern, mirrored by the
# SDK's _validate_source_id. Reject here so a bad name fails in setup rather
# than as an opaque rejection on the device's first connect.
if [ -n "$DEVICE_SLUG" ] && ! echo "$DEVICE_SLUG" | grep -Eq '^[a-z0-9][a-z0-9._-]*$'; then
    error "Invalid --slug: $DEVICE_SLUG"
    hint "  Must match ^[a-z0-9][a-z0-9._-]*$ (lowercase, digits, . _ -)"
    echo ""
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────

header "Plexus Setup"

# Detect system
OS=$(uname -s)
ARCH=$(uname -m)
label "System" "$OS $ARCH"

# Check for Python — auto-install if missing
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    warn "Python not found — installing..."
    if command -v apt-get &> /dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip
    elif command -v dnf &> /dev/null; then
        sudo dnf install -y -q python3 python3-pip
    elif command -v yum &> /dev/null; then
        sudo yum install -y -q python3 python3-pip
    elif command -v brew &> /dev/null; then
        brew install python3
    else
        error "Could not install Python automatically"
        hint "  Install Python 3.8+ manually, then re-run this script"
        echo ""
        exit 1
    fi

    # Re-detect after install
    if command -v python3 &> /dev/null; then
        PYTHON=python3
    elif command -v python &> /dev/null; then
        PYTHON=python
    else
        error "Python installation failed"
        echo ""
        exit 1
    fi
    success "Python installed"
fi

PYTHON_VERSION=$($PYTHON --version 2>&1 | cut -d' ' -f2)
label "Python" "$PYTHON_VERSION"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Install the SDK
# ─────────────────────────────────────────────────────────────────────────────

divider
echo ""

# Check if python3-venv is available (needed on Debian/Ubuntu)
if [ "$OS" = "Linux" ] && ! $PYTHON -c "import venv" 2>/dev/null; then
    if command -v apt-get &> /dev/null; then
        if [ "$EUID" -eq 0 ]; then
            run_with_spinner "Installing python3-venv..." apt-get update -qq
            run_with_spinner "Installing python3-venv..." apt-get install -y -qq python3-venv
        elif sudo -n true 2>/dev/null; then
            run_with_spinner "Installing python3-venv..." sudo apt-get update -qq
            run_with_spinner "Installing python3-venv..." sudo apt-get install -y -qq python3-venv
        else
            error "python3-venv is required but not installed"
            hint "  Run: sudo apt install python3-venv"
            echo ""
            exit 1
        fi
    fi
fi

# Use a virtual environment to avoid PEP 668 issues on modern Python/Debian
VENV_DIR="/opt/plexus/venv"
PLEXUS_BIN_DIR="/opt/plexus/bin"

if [ "$OS" = "Linux" ]; then
    if [ "$EUID" -eq 0 ]; then
        mkdir -p /opt/plexus
    elif sudo -n true 2>/dev/null; then
        sudo mkdir -p /opt/plexus
        sudo chown $USER:$USER /opt/plexus
    else
        # Fall back to user directory if no sudo
        VENV_DIR="$HOME/.plexus/venv"
        PLEXUS_BIN_DIR="$HOME/.plexus/bin"
        mkdir -p "$HOME/.plexus"
    fi
else
    VENV_DIR="$HOME/.plexus/venv"
    PLEXUS_BIN_DIR="$HOME/.plexus/bin"
    mkdir -p "$HOME/.plexus"
fi

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    if ! run_with_spinner "Creating virtual environment..." $PYTHON -m venv "$VENV_DIR"; then
        error "Failed to create virtual environment"
        exit 1
    fi
fi

VENV_PIP="$VENV_DIR/bin/pip"

# Install the SDK into the venv
run_with_spinner "Upgrading pip..." "$VENV_PIP" install --upgrade pip --quiet

IS_PI=false
if [ -f /proc/device-tree/model ] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
fi

# Install system packages for camera support on Pi
if [ "$IS_PI" = true ] && command -v apt-get &> /dev/null; then
    PKGS=""
    dpkg -s libcap-dev &> /dev/null 2>&1 || PKGS="libcap-dev"
    if [ -n "$PKGS" ]; then
        if [ "$EUID" -eq 0 ]; then
            run_with_spinner "Installing system packages..." apt-get install -y -qq $PKGS
        elif sudo -n true 2>/dev/null; then
            run_with_spinner "Installing system packages..." sudo apt-get install -y -qq $PKGS
        fi
    fi
fi

# Add user to i2c group for sensor access without sudo
if [ "$OS" = "Linux" ] && getent group i2c &> /dev/null && ! id -nG | grep -qw i2c; then
    if [ "$EUID" -eq 0 ]; then
        usermod -aG i2c "$USER"
    elif sudo -n true 2>/dev/null; then
        sudo usermod -aG i2c "$USER"
    fi
fi

# The 0.2.0 thin-SDK rewrite dropped [sensors], [picamera] and the rest. The
# only runtime extra left is [video], which telemetry does not need. Asking for
# a removed one makes pip warn and fall back, so ask for the package plainly.
if run_with_spinner "Installing plexus-python..." "$VENV_PIP" install --upgrade plexus-python --quiet; then
    success "SDK installed"
else
    error "Installation failed"
    exit 1
fi

# Make 'plexus' command available system-wide
VENV_PLEXUS="$VENV_DIR/bin/plexus"
if [ -f "$VENV_PLEXUS" ]; then
    mkdir -p "$PLEXUS_BIN_DIR"
    ln -sf "$VENV_PLEXUS" "$PLEXUS_BIN_DIR/plexus"

    if [ "$OS" = "Linux" ]; then
        # Add to PATH via bashrc
        PROFILE_FILE="$HOME/.bashrc"
        if ! grep -q "$PLEXUS_BIN_DIR" "$PROFILE_FILE" 2>/dev/null; then
            echo "" >> "$PROFILE_FILE"
            echo "# Plexus" >> "$PROFILE_FILE"
            echo "export PATH=\"$PLEXUS_BIN_DIR:\$PATH\"" >> "$PROFILE_FILE"
        fi
        export PATH="$PLEXUS_BIN_DIR:$PATH"

        # Also symlink to /usr/local/bin if possible
        if [ "$EUID" -eq 0 ]; then
            ln -sf "$VENV_PLEXUS" /usr/local/bin/plexus
        elif sudo -n true 2>/dev/null; then
            sudo ln -sf "$VENV_PLEXUS" /usr/local/bin/plexus
        fi
    fi
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Configure Device
# ─────────────────────────────────────────────────────────────────────────────

divider
echo ""

if [ -n "$API_KEY" ]; then
    # API key flow — write config and skip pairing
    mkdir -p "$HOME/.plexus"

    # Build config JSON
    CONFIG="{\"api_key\":\"$API_KEY\",\"endpoint\":\"$ENDPOINT\""
    if [ -n "$DEVICE_SLUG" ]; then
        CONFIG="$CONFIG,\"source_id\":\"$DEVICE_SLUG\""
    fi
    if [ -n "$DEVICE_NAME" ]; then
        CONFIG="$CONFIG,\"source_name\":\"$DEVICE_NAME\""
        # Derive slug from name if no explicit slug
        if [ -z "$DEVICE_SLUG" ]; then
            SOURCE_ID=$(echo "$DEVICE_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9._-]/-/g' | sed 's/--*/-/g' | sed 's/^-//;s/-$//')
            # A name made entirely of punctuation slugifies to nothing, and a
            # leading . or _ survives the strip — both are rejected on the wire.
            if ! echo "$SOURCE_ID" | grep -Eq '^[a-z0-9][a-z0-9._-]*$'; then
                error "Could not derive a valid device id from --name \"$DEVICE_NAME\""
                hint "  Pass one explicitly: --slug my-device-01"
                echo ""
                exit 1
            fi
            CONFIG="$CONFIG,\"source_id\":\"$SOURCE_ID\""
        fi
    fi
    if [ -n "$ORG_ID" ]; then
        CONFIG="$CONFIG,\"org_id\":\"$ORG_ID\""
    fi
    CONFIG="$CONFIG}"

    echo "$CONFIG" > "$HOME/.plexus/config.json"
    export PLEXUS_API_KEY="$API_KEY"
    success "API key configured"
    if [ -n "$DEVICE_SLUG" ]; then
        success "Device ID: $DEVICE_SLUG"
    fi
    if [ -n "$DEVICE_NAME" ]; then
        success "Device name: $DEVICE_NAME"
    fi
    if [ -n "$ORG_ID" ]; then
        success "Organization: $ORG_ID"
    fi
    echo ""
else
    warn "No API key provided"
    echo ""
    dim "To connect this device, authorize it in a browser:"
    echo ""
    hint "  $PLEXUS_BIN_DIR/plexus init"
    echo ""
    dim "Or re-run this installer with --key plx_xxx --name my-device-01"
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Verify the device can reach Plexus
#
# There is no Plexus agent to install. plexus-python is a library: you write
# the script that reads your sensors and calls px.send(). So instead of
# installing a service around a binary that does not exist, prove the
# credentials and the network path work, and hand back a runnable starter.
# ─────────────────────────────────────────────────────────────────────────────

if [ -n "$API_KEY" ]; then
    divider
    echo ""

    VERIFY_OUT=$("$VENV_DIR/bin/python" - <<'PYEOF' 2>&1
import sys
try:
    from plexus import Plexus
    px = Plexus()
    px.send("setup.check", 1)
    px.close()
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}")
    sys.exit(1)
PYEOF
    ) && VERIFY_OK=true || VERIFY_OK=false

    if [ "$VERIFY_OK" = true ]; then
        success "Connected — sent a test reading"
        dim "  This device is now visible at https://app.plexus.company"
    else
        error "Could not send a reading"
        echo ""
        dim "$VERIFY_OUT"
        echo ""
        hint "  Check the API key is active at https://app.plexus.company/api,"
        hint "  and that this host can reach $ENDPOINT"
        echo ""
        exit 1
    fi
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────

divider
echo ""
success "Setup complete"
echo ""
dim "Next — write the script that reads your sensors:"
echo ""
info "  $VENV_DIR/bin/python - <<'EOF'"
info "  from plexus import Plexus"
info "  px = Plexus()                 # reads ~/.plexus/config.json"
info "  px.send(\"temperature\", 21.5)"
info "  EOF"
echo ""
dim "Recipes for MAVLink, CAN, MQTT, Modbus and I2C:"
hint "  https://github.com/plexus-oss/plexus-python/tree/main/examples"
echo ""
dim "To run it on boot, point a systemd unit at your own script —"
dim "there is no Plexus daemon to start."
echo ""
hint "Dashboard: https://app.plexus.company"
echo ""
divider
echo ""
dim "What was installed:"
info "  Virtual env:  $VENV_DIR"
info "  Config:       $HOME/.plexus/config.json"
info "  CLI:          $PLEXUS_BIN_DIR/plexus  (init, login, logout, whoami)"
echo ""
dim "To uninstall:"
info "  rm -rf $VENV_DIR $PLEXUS_BIN_DIR $HOME/.plexus"
echo ""
