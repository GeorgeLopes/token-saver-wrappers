#!/usr/bin/env bash
# install.sh — one-shot setup for token-saver + hermes pipeline
# Usage: ./install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================"
echo " Token Saver · iFood SRE"
echo " Pipeline modular de redução de tokens"
echo "============================================"
echo ""

# --- Prerequisites ---
command -v podman >/dev/null || { echo "ERRO: podman não encontrado. Instale com: brew install podman"; exit 1; }
command -v python3 >/dev/null || { echo "ERRO: python3 não encontrado."; exit 1; }

# Ensure podman machine is running (macOS)
if [ "$(uname -s)" = "Darwin" ]; then
    if ! podman machine inspect >/dev/null 2>&1; then
        echo "[1/4] Criando podman machine (VM Linux)..."
        podman machine init
    fi
    if [ "$(podman machine inspect --format '{{.State}}' 2>/dev/null)" != "running" ]; then
        echo "[1/4] Iniciando podman machine..."
        podman machine start
    else
        echo "[1/4] podman machine: OK"
    fi
else
    echo "[1/4] Linux detectado: podman rootless"
fi

# --- Build images ---
echo "[2/4] Construindo imagens..."
cd "$REPO_DIR"
./build-and-install

# --- Model config ---
CHEAP_MODEL="${TOKEN_SAVER_ROUTER_CHEAP_MODEL:-deepseek-v4-flash-claude}"
echo "[3/4] Modelo barato: $CHEAP_MODEL"

# --- Destroy existing pod ---
"$HOME/.local/bin/token-saver-ctl" destroy 2>/dev/null || true

# --- Start pod with all modules ---
echo "[4/4] Iniciando pod com 7 módulos..."
TOKEN_SAVER_TRANSLATE_ENABLED=1 \
TOKEN_SAVER_FEATURE_SUMMARIZE=1 \
TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 \
TOKEN_SAVER_FEATURE_ROUTER=1 \
TOKEN_SAVER_SUMMARIZE_MODEL="$CHEAP_MODEL" \
TOKEN_SAVER_ROUTER_CHEAP_MODEL="$CHEAP_MODEL" \
"$HOME/.local/bin/token-saver-ctl" start

sleep 6

echo ""
echo "============================================"
echo " Instalação concluída"
echo "============================================"
"$HOME/.local/bin/token-saver-ctl" status
echo ""
echo "Atalhos (adicione ao ~/.zshrc):"
echo "  alias hts='TOKEN_SAVER_FEATURE_SUMMARIZE=1 TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 TOKEN_SAVER_FEATURE_ROUTER=1 TOKEN_SAVER_SUMMARIZE_MODEL=${CHEAP_MODEL} TOKEN_SAVER_ROUTER_CHEAP_MODEL=${CHEAP_MODEL} hermes-token-saver'"
echo "  alias hts-status='token-saver-ctl status'"
echo "  alias hts-dash='open http://127.0.0.1:8786/dashboard'"
echo "  alias hts-stats='token-saver-ctl stats'"
echo ""
echo "Dashboard: http://127.0.0.1:8786/dashboard"
echo "Uso:       hts 'sua pergunta'"
