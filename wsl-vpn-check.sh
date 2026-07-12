#!/usr/bin/env bash
# wsl-vpn-check.sh — diagnostica conectividade WSL2 → GenPlat via VPN
set -euo pipefail

GENPLAT_HOST="${1:-generative-ai-platform-development.ifoodcorp.com.br}"
PROXY_HOST="${2:-}"  # opcional: proxy.ifoodcorp.com.br:8080

echo "============================================"
echo " Diagnóstico de conectividade WSL2"
echo "============================================"
echo ""

# --- 1. WSL version ---
echo "[1] Versão do WSL"
if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "    WSL2 detectado"
    WSL_VERSION=$(wsl.exe -l -v 2>/dev/null | grep -i ubuntu | awk '{print $NF}' || echo "desconhecido")
    echo "    WSL version: $WSL_VERSION"
else
    echo "    NÃO é WSL — pulando diagnóstico de rede WSL"
fi
echo ""

# --- 2. DNS resolution ---
echo "[2] Resolução DNS: $GENPLAT_HOST"
if nslookup "$GENPLAT_HOST" >/dev/null 2>&1; then
    IP=$(nslookup "$GENPLAT_HOST" 2>/dev/null | grep -A1 "Name:" | grep "Address:" | tail -1 | awk '{print $2}')
    echo "    ✓ Resolve: $IP"
else
    echo "    ✗ NÃO resolve"
    echo "    Possíveis causas:"
    echo "      - VPN não está conectada no Windows"
    echo "      - DNS da VPN não propaga para WSL2"
    echo ""
    echo "    Soluções:"
    echo "      A) WSL2 Mirror Mode (Win 11 23H2+):"
    echo "         Crie %USERPROFILE%\.wslconfig no Windows:"
    echo "         [wsl2]"
    echo "         networkingMode=mirrored"
    echo "         dnsTunneling=true"
    echo "         Depois: wsl --shutdown && wsl"
    echo ""
    echo "      B) Configurar DNS manualmente no WSL2:"
    echo "         sudo nano /etc/resolv.conf"
    echo "         nameserver <DNS_DA_VPN>"
    echo ""
    echo "      C) Usar proxy explícito:"
    echo "         export HTTP_PROXY=http://proxy.ifoodcorp.com.br:80"
    echo "         export HTTPS_PROXY=http://proxy.ifoodcorp.com.br:80"
fi
echo ""

# --- 3. TCP connectivity ---
echo "[3] Conectividade TCP: $GENPLAT_HOST:443"
if timeout 5 bash -c "echo > /dev/tcp/$GENPLAT_HOST/443" 2>/dev/null; then
    echo "    ✓ TCP ok"
else
    echo "    ✗ Sem conectividade TCP"
    echo "    A VPN pode estar com split tunneling — só rotas específicas passam."
fi
echo ""

# --- 4. HTTPS request ---
echo "[4] Requisição HTTPS"
if curl -sk -o /dev/null -w "%{http_code}" --connect-timeout 5 "https://$GENPLAT_HOST/api/v2/models" 2>/dev/null; then
    echo ""
    echo "    ✓ HTTPS respondeu"
else
    echo "    ✗ HTTPS falhou"
fi
echo ""

# --- 5. Proxy check ---
if [ -n "$PROXY_HOST" ]; then
    echo "[5] Teste via proxy: $PROXY_HOST"
    if curl -sk -o /dev/null -w "%{http_code}" --connect-timeout 5 --proxy "$PROXY_HOST" "https://$GENPLAT_HOST/api/v2/models" 2>/dev/null; then
        echo ""
        echo "    ✓ Proxy funciona"
    else
        echo "    ✗ Proxy falhou"
    fi
    echo ""
fi

# --- 6. Token GenPlat ---
echo "[6] Token GenPlat (GENPLAT_API_KEY)"
HERMES_ENV="$HOME/.hermes/.env"
if [ -f "$HERMES_ENV" ] && grep -q "GENPLAT_API_KEY" "$HERMES_ENV" 2>/dev/null; then
    KEY_LEN=$(grep "GENPLAT_API_KEY" "$HERMES_ENV" | cut -d= -f2 | wc -c)
    echo "    ✓ Token configurado (~${KEY_LEN} chars)"
else
    echo "    ✗ Token NÃO configurado"
    echo "    Configure: echo 'GENPLAT_API_KEY=seu-token' >> $HERMES_ENV"
fi
echo ""

# --- 7. Recomendação ---
echo "============================================"
echo " Recomendação"
echo "============================================"
echo ""
echo "Para WSL2 + VPN iFood, a opção mais estável é o Mirror Mode"
echo "(Windows 11 23H2 ou superior):"
echo ""
echo "  1. No Windows, crie/edit %USERPROFILE%\\.wslconfig:"
echo "     [wsl2]"
echo "     networkingMode=mirrored"
echo "     dnsTunneling=true"
echo ""
echo "  2. Reinicie o WSL:"
echo "     wsl --shutdown"
echo "     wsl"
echo ""
echo "  3. Teste novamente:"
echo "     bash wsl-vpn-check.sh"
