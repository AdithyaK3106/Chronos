#!/usr/bin/env bash
# ==============================================================================
# Chronos Complete One-Line Installer & Environment Setup
#
# Supports: Linux (x86_64, aarch64), macOS (Intel, Apple Silicon)
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/chronos-agent/chronos/main/install.sh | bash
#   ./install.sh [OPTIONS]
#
# Options:
#   --yes, -y            Non-interactive unattended mode (uses sensible defaults)
#   --with-slm           Enable OpenHands CodeScout-1.7B SLM localization
#   --no-slm             Disable SLM localization (pure deterministic AST + BM25)
#   --slm-backend=...    SLM backend: openai_compatible, ollama, agy (default: openai_compatible)
#   --slm-endpoint=...   SLM endpoint URL (default: http://localhost:8000/v1)
#   --no-mcp             Skip automatic IDE MCP configuration
#   --dir=PATH           Target installation directory (default: ~/.chronos/repo or current dir)
# ==============================================================================

set -euo pipefail

# Text formatting
BOLD="\033[1m"
GREEN="\033[32m"
CYAN="\033[36m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

echo -e "${CYAN}${BOLD}"
echo "  ██████╗██╗  ██╗██████╗  ██████╗ ███╗   ██╗ ██████╗ ███████╗"
echo " ██╔════╝██║  ██║██╔══██╗██╔═══██╗████╗  ██║██╔═══██╗██╔════╝"
echo " ██║     ███████║██████╔╝██║   ██║██╔██╗ ██║██║   ██║███████╗"
echo " ██║     ██╔══██║██╔══██╗██║   ██║██║╚██╗██║██║   ██║╚════██║"
echo " ╚██████╗██║  ██║██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝███████║"
echo "  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝"
echo -e "${RESET}"
echo -e "${BOLD}⏳ Chronos Universal Installer (2-Minute Quickstart)${RESET}\n"

# Defaults
NON_INTERACTIVE=false
OPT_SLM=""
SLM_BACKEND="openai_compatible"
SLM_ENDPOINT="http://localhost:8000/v1"
CONFIGURE_MCP=true
TARGET_DIR=""

# Parse CLI arguments
for arg in "$@"; do
    case "$arg" in
        --yes|-y)
            NON_INTERACTIVE=true
            ;;
        --with-slm)
            OPT_SLM=true
            ;;
        --no-slm)
            OPT_SLM=false
            ;;
        --slm-backend=*)
            SLM_BACKEND="${arg#*=}"
            OPT_SLM=true
            ;;
        --slm-endpoint=*)
            SLM_ENDPOINT="${arg#*=}"
            ;;
        --no-mcp)
            CONFIGURE_MCP=false
            ;;
        --dir=*)
            TARGET_DIR="${arg#*=}"
            ;;
        --help|-h)
            echo "Usage: ./install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -y, --yes          Unattended mode (uses defaults, no prompts)"
            echo "  --with-slm         Enable CodeScout-1.7B SLM localization"
            echo "  --no-slm           Disable SLM (default: fast AST + BM25)"
            echo "  --slm-backend=...  Backend: openai_compatible (default), ollama, agy"
            echo "  --slm-endpoint=... Base URL for SLM server"
            echo "  --no-mcp           Skip IDE MCP configuration"
            echo "  --dir=PATH         Installation directory"
            exit 0
            ;;
        *)
            echo -e "${YELLOW}Warning: Unknown option '$arg' ignored.${RESET}"
            ;;
    esac
done

# Step 1: Detect Platform & Prerequisites
OS="$(uname -s)"
ARCH="$(uname -m)"
echo -e "${CYAN}==>${RESET} Detected System: ${BOLD}${OS} (${ARCH})${RESET}"

# Determine directory
if [ -z "$TARGET_DIR" ]; then
    if [ -f "pyproject.toml" ] && grep -q "name = \"chronos\"" pyproject.toml 2>/dev/null; then
        TARGET_DIR="$(pwd)"
        echo -e "${CYAN}==>${RESET} Using existing Chronos repository: ${BOLD}${TARGET_DIR}${RESET}"
    else
        TARGET_DIR="${HOME}/.chronos/repo"
        mkdir -p "${TARGET_DIR}"
        echo -e "${CYAN}==>${RESET} Cloning Chronos into: ${BOLD}${TARGET_DIR}${RESET}"
        if [ ! -d "${TARGET_DIR}/.git" ]; then
            git clone --depth 1 https://github.com/chronos-agent/chronos.git "${TARGET_DIR}"
        else
            echo -e "${CYAN}==>${RESET} Repository already exists at ${TARGET_DIR}, pulling latest changes..."
            git -C "${TARGET_DIR}" pull --ff-only || true
        fi
    fi
fi

cd "${TARGET_DIR}"

# Step 2: Ensure fast package manager (uv or python3 venv)
USE_UV=false
if command -v uv >/dev/null 2>&1; then
    USE_UV=true
    echo -e "${GREEN}✓${RESET} Found ${BOLD}uv${RESET} fast Python package manager."
elif command -v curl >/dev/null 2>&1; then
    echo -e "${CYAN}==>${RESET} Installing standalone ${BOLD}uv${RESET} for ultra-fast setup..."
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
    if [ -f "${HOME}/.cargo/bin/uv" ]; then
        export PATH="${HOME}/.cargo/bin:${PATH}"
        USE_UV=true
        echo -e "${GREEN}✓${RESET} Installed ${BOLD}uv${RESET} successfully."
    elif [ -f "${HOME}/.local/bin/uv" ]; then
        export PATH="${HOME}/.local/bin:${PATH}"
        USE_UV=true
        echo -e "${GREEN}✓${RESET} Installed ${BOLD}uv${RESET} successfully."
    fi
fi

# Step 3: Create Virtual Environment and Install
echo -e "${CYAN}==>${RESET} Preparing Python 3.12+ virtual environment..."
VENV_DIR="${TARGET_DIR}/.venv"

if [ "$USE_UV" = true ]; then
    if [ ! -d "${VENV_DIR}" ]; then
        uv venv "${VENV_DIR}" --python 3.12 >/dev/null 2>&1 || uv venv "${VENV_DIR}"
    else
        uv venv --allow-existing "${VENV_DIR}" >/dev/null 2>&1 || true
    fi
    echo -e "${CYAN}==>${RESET} Installing Chronos and polyglot AST dependencies via uv..."
    VIRTUAL_ENV="${VENV_DIR}" uv pip install -e .
else
    # Fallback to standard Python
    if ! command -v python3 >/dev/null 2>&1; then
        echo -e "${RED}Error: python3 is required. Please install Python 3.12 or later.${RESET}"
        exit 1
    fi
    if [ ! -d "${VENV_DIR}" ]; then
        python3 -m venv "${VENV_DIR}"
    fi
    echo -e "${CYAN}==>${RESET} Installing Chronos and dependencies via pip..."
    "${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
    "${VENV_DIR}/bin/pip" install -e .
fi

CHRONOS_BIN="${VENV_DIR}/bin/chronos"

# Step 4: Link binary to PATH (~/.local/bin)
LOCAL_BIN="${HOME}/.local/bin"
mkdir -p "${LOCAL_BIN}"
ln -sf "${CHRONOS_BIN}" "${LOCAL_BIN}/chronos"

if [[ ":$PATH:" != *":${LOCAL_BIN}:"* ]]; then
    export PATH="${LOCAL_BIN}:${PATH}"
    # Append to shell profile if not already present
    SHELL_RC=""
    if [ -n "${ZSH_VERSION:-}" ] || [ -f "${HOME}/.zshrc" ]; then
        SHELL_RC="${HOME}/.zshrc"
    elif [ -f "${HOME}/.bashrc" ]; then
        SHELL_RC="${HOME}/.bashrc"
    fi
    if [ -n "$SHELL_RC" ] && ! grep -q 'export PATH="$HOME/.local/bin:$PATH"' "$SHELL_RC" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
    fi
fi
echo -e "${GREEN}✓${RESET} Linked ${BOLD}chronos${RESET} command to ${BOLD}${LOCAL_BIN}/chronos${RESET}."

# Step 5: Interactive Customization Wizard (if not specified via CLI)
if [ -z "$OPT_SLM" ]; then
    if [ "$NON_INTERACTIVE" = true ]; then
        OPT_SLM=false
    else
        echo ""
        echo -e "${BOLD}🤖 Customization Option: OpenHands CodeScout-1.7B SLM Localization${RESET}"
        echo "Chronos features deterministic Tree-sitter AST + SQLite BM25 context resolution (<60ms)."
        echo "You can optionally enable OpenHands/CodeScout-1.7B for neural zero-path localization."
        echo ""
        echo "  [1] No, use Pure Deterministic AST + BM25 (Recommended: Instant, 0 memory)"
        echo "  [2] Yes, connect to local Ollama (localhost:11434)"
        echo "  [3] Yes, connect to remote OpenAI-compatible server (vLLM / LiteLLM)"
        echo ""
        read -r -p "Select option [1-3] (Default: 1): " slm_choice || slm_choice="1"
        case "$slm_choice" in
            2)
                OPT_SLM=true
                SLM_BACKEND="ollama"
                SLM_ENDPOINT="http://localhost:11434"
                ;;
            3)
                OPT_SLM=true
                SLM_BACKEND="openai_compatible"
                read -r -p "Enter server endpoint URL (Default: http://localhost:8000/v1): " custom_ep || custom_ep=""
                if [ -n "$custom_ep" ]; then
                    SLM_ENDPOINT="$custom_ep"
                fi
                ;;
            *)
                OPT_SLM=false
                ;;
        esac
    fi
fi

# Apply SLM Configuration
"${CHRONOS_BIN}" config set slm.enabled "$OPT_SLM" >/dev/null 2>&1 || true
if [ "$OPT_SLM" = true ]; then
    "${CHRONOS_BIN}" config set slm.backend "$SLM_BACKEND" >/dev/null 2>&1 || true
    "${CHRONOS_BIN}" config set slm.endpoint "$SLM_ENDPOINT" >/dev/null 2>&1 || true
    echo -e "${GREEN}✓${RESET} Enabled CodeScout-1.7B SLM (Backend: ${BOLD}${SLM_BACKEND}${RESET}, Endpoint: ${BOLD}${SLM_ENDPOINT}${RESET})"
else
    echo -e "${GREEN}✓${RESET} Set Pure AST + BM25 deterministic mode (Default, zero network)."
fi

# Step 6: IDE FastMCP Configuration (Cursor / Claude Desktop / Windsurf)
if [ "$CONFIGURE_MCP" = true ]; then
    # Cursor
    CURSOR_MCP="${HOME}/.cursor/mcp.json"
    if [ -d "${HOME}/.cursor" ] || [ -f "$CURSOR_MCP" ]; then
        mkdir -p "${HOME}/.cursor"
        cat <<EOF > "$CURSOR_MCP"
{
  "mcpServers": {
    "chronos": {
      "command": "${CHRONOS_BIN}",
      "args": ["serve"]
    }
  }
}
EOF
        echo -e "${GREEN}✓${RESET} Configured Cursor FastMCP integration: ${BOLD}${CURSOR_MCP}${RESET}"
    fi

    # Claude Desktop (macOS / Linux)
    CLAUDE_CONFIG=""
    if [ "$OS" = "Darwin" ]; then
        CLAUDE_CONFIG="${HOME}/Library/Application Support/Claude/claude_desktop_config.json"
    else
        CLAUDE_CONFIG="${HOME}/.config/Claude/claude_desktop_config.json"
    fi
    if [ -d "$(dirname "$CLAUDE_CONFIG")" ]; then
        cat <<EOF > "$CLAUDE_CONFIG"
{
  "mcpServers": {
    "chronos": {
      "command": "${CHRONOS_BIN}",
      "args": ["serve"]
    }
  }
}
EOF
        echo -e "${GREEN}✓${RESET} Configured Claude Desktop FastMCP integration: ${BOLD}${CLAUDE_CONFIG}${RESET}"
    fi
fi

# Step 7: Initialize Chronos & Verify System Health
echo ""
echo -e "${CYAN}==>${RESET} Initializing Chronos database and running diagnostic health check..."
"${CHRONOS_BIN}" init >/dev/null 2>&1 || true
"${CHRONOS_BIN}" doctor

echo ""
echo -e "${GREEN}${BOLD}🎉 Chronos successfully installed and verified in under 2 minutes!${RESET}"
echo ""
echo -e "Try these commands right now:"
echo -e "  ${BOLD}chronos sync${RESET}                                   # Index current repo AST (<2s)"
echo -e "  ${BOLD}chronos resolve \"Fix bug in lock renewal\"${RESET}        # Zero-path context resolution"
echo -e "  ${BOLD}chronos memory list${RESET}                            # Inspect epistemic living memories"
echo -e "  ${BOLD}chronos serve${RESET}                                  # Start FastMCP server for IDE / agents"
echo -e "  ${BOLD}chronos doctor${RESET}                                 # View toolchain & hardware status"
echo ""
echo -e "For full documentation and quick recipes, see: ${CYAN}${BOLD}GUIDE.md${RESET}"
