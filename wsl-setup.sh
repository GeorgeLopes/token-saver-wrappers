#!/usr/bin/env bash
# wsl-setup.sh — configura ambiente token-saver + hermes dentro do WSL2
# Rode uma vez após clonar o repositório no WSL2.
set -euo pipefail

echo "============================================"
echo " Token Saver · iFood SRE"
echo " Setup WSL2"
echo "============================================"
echo ""

# --- Verifica se está no WSL ---
if ! grep -qi microsoft /proc/version 2>/dev/null && ! grep -qi wsl /proc/version 2>/dev/null; then
    echo "AVISO: Não parece ser WSL2. Continuando mesmo assim..."
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Instala dependências ---
echo "[1/5] Instalando dependências..."
sudo apt-get update -qq
sudo apt-get install -y -qq podman uidmap curl >/dev/null 2>&1
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER 2>/dev/null || true

# --- Token GenPlat ---
echo ""
echo "[2/5] Configurando token GenPlat..."
HERMES_ENV="$HOME/.hermes/.env"
mkdir -p "$HOME/.hermes"

if [ -f "$HERMES_ENV" ] && grep -q "GENPLAT_API_KEY" "$HERMES_ENV" 2>/dev/null; then
    echo "       Token GenPlat já configurado em $HERMES_ENV"
else
    echo "       Cole seu token GenPlat (gerado via tompero):"
    read -r -s TOKEN
    echo ""
    if [ -n "$TOKEN" ]; then
        echo "GENPLAT_API_KEY=$TOKEN" >> "$HERMES_ENV"
        echo "       Token salvo em $HERMES_ENV"
    else
        echo "       Token vazio — configure depois em $HERMES_ENV"
    fi
fi

# --- CA cert (se existir) ---
if [ -f "$HOME/.hermes/genplat-ca.pem" ]; then
    echo "       CA cert GenPlat encontrado"
else
    echo "       AVISO: genplat-ca.pem não encontrado em ~/.hermes/"
    echo "       Copie o certificado se necessário."
fi

# --- Build e instalação ---
echo ""
echo "[3/5] Construindo imagens (10-30 min na primeira vez)..."
cd "$REPO_DIR"
./build-and-install

# --- Iniciar pod ---
echo ""
echo "[4/5] Iniciando pod..."
CHEAP_MODEL="${TOKEN_SAVER_ROUTER_CHEAP_MODEL:-deepseek-v4-flash-claude}"
"$HOME/.local/bin/token-saver-ctl" destroy 2>/dev/null || true
TOKEN_SAVER_TRANSLATE_ENABLED=1 \
TOKEN_SAVER_FEATURE_SUMMARIZE=1 \
TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 \
TOKEN_SAVER_FEATURE_ROUTER=1 \
TOKEN_SAVER_SUMMARIZE_MODEL="$CHEAP_MODEL" \
TOKEN_SAVER_ROUTER_CHEAP_MODEL="$CHEAP_MODEL" \
"$HOME/.local/bin/token-saver-ctl" start

sleep 5

# --- Criar atalho no Desktop do Windows ---
echo ""
echo "[5/5] Criando atalho no Desktop do Windows..."
DESKTOP="/mnt/c/Users/$(/mnt/c/Windows/System32/cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r\n' || echo 'Public')/Desktop"
BAT_FILE="$DESKTOP/Token Saver.bat"

if [ -d "$DESKTOP" ]; then
    cat > "$BAT_FILE" << 'BATEOF'
@echo off
echo ===========================================
echo  Token Saver · iFood SRE
echo ===========================================
echo.
echo Iniciando hermes com pipeline de reducao de tokens...
echo.
wsl -d Ubuntu -- bash -c "cd ~/token-saver-wrappers && TOKEN_SAVER_FEATURE_SUMMARIZE=1 TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 TOKEN_SAVER_FEATURE_ROUTER=1 TOKEN_SAVER_SUMMARIZE_MODEL=deepseek-v4-flash-claude TOKEN_SAVER_ROUTER_CHEAP_MODEL=deepseek-v4-flash-claude hermes-token-saver"
pause
BATEOF
    echo "       Atalho criado: $BAT_FILE"
else
    echo "       Não foi possível acessar o Desktop do Windows"
    echo "       Você pode criar um atalho manualmente:"
    echo "       Alvo: wsl -d Ubuntu -- bash -c 'cd ~/token-saver-wrappers && TOKEN_SAVER_FEATURE_SUMMARIZE=1 TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 TOKEN_SAVER_FEATURE_ROUTER=1 TOKEN_SAVER_SUMMARIZE_MODEL=deepseek-v4-flash-claude TOKEN_SAVER_ROUTER_CHEAP_MODEL=deepseek-v4-flash-claude hermes-token-saver'"
fi

echo ""
echo "============================================"
echo " Setup concluído!"
echo "============================================"
echo ""
echo "Atalho no Desktop: Token Saver.bat"
echo "Dashboard: http://localhost:8786/dashboard"
echo ""
echo "Comandos dentro do WSL2:"
echo "  hts 'pergunta'    (se aliases configurados)"
echo "  hts-dash           (abre dashboard)"
echo ""
echo "Para adicionar os aliases, cole no ~/.bashrc:"
echo "  alias hts='TOKEN_SAVER_FEATURE_SUMMARIZE=1 TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 TOKEN_SAVER_FEATURE_ROUTER=1 TOKEN_SAVER_SUMMARIZE_MODEL=deepseek-v4-flash-claude TOKEN_SAVER_ROUTER_CHEAP_MODEL=deepseek-v4-flash-claude hermes-token-saver'"
echo "  alias hts-dash='powershell.exe -Command Start-Process http://localhost:8786/dashboard'"
echo "  alias hts-status='token-saver-ctl status'"
echo "  alias hts-stats='token-saver-ctl stats'"
