#!/usr/bin/env bash
# claude-code-vitals installer — one-line setup
# Usage: curl -fsSL https://raw.githubusercontent.com/jatinmayekar/claude-code-vitals/main/install.sh | bash
#
# What this does:
#   1. Installs claude-code-vitals via pip
#   2. Runs `ccvitals init` to configure Claude Code statusLine
#   3. You're done — restart Claude Code

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[32m'
YELLOW='\033[33m'
CYAN='\033[36m'
RED='\033[31m'
RESET='\033[0m'

echo ""
echo -e "${CYAN}${BOLD}⚡ claude-code-vitals installer${RESET}"
echo -e "${CYAN}   Know your limits before they know you.${RESET}"
echo ""

# Check Python version
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}Error: python3 not found. Please install Python 3.10+.${RESET}"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]); then
    echo -e "${RED}Error: Python 3.10+ required (found $PY_VERSION).${RESET}"
    exit 1
fi

echo -e "  ${GREEN}✓${RESET} Python $PY_VERSION detected"

# Check if Claude Code is available
if [ -d "$HOME/.claude" ]; then
    echo -e "  ${GREEN}✓${RESET} Claude Code config directory found"
else
    echo -e "  ${YELLOW}⚠${RESET} No ~/.claude directory — Claude Code may not be installed"
    echo "    claude-code-vitals will still install, but the status bar won't work until Claude Code is set up."
fi

# Install via pip
echo ""
echo -e "${BOLD}Installing claude-code-vitals...${RESET}"

if pip install claude-code-vitals 2>/dev/null; then
    echo -e "  ${GREEN}✓${RESET} Installed via pip"
elif pip install claude-code-vitals --user 2>/dev/null; then
    echo -e "  ${GREEN}✓${RESET} Installed via pip --user"
elif pip install claude-code-vitals --break-system-packages 2>/dev/null; then
    echo -e "  ${GREEN}✓${RESET} Installed via pip --break-system-packages"
else
    echo -e "  ${RED}✗${RESET} pip install failed"
    echo "  Try: pip install claude-code-vitals --user"
    exit 1
fi

# Run init
echo ""
ccvitals init

echo ""
echo -e "${GREEN}${BOLD}Done!${RESET} Restart Claude Code to see the status bar."
echo -e "  Run ${CYAN}ccvitals status${RESET} to check current state."
echo -e "  Run ${CYAN}ccvitals report${RESET} to generate trend charts."
echo ""
