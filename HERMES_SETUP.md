# Hermes + Token Saver — Guia de uso

Última atualização: 2026-07-12

## Pré-requisitos

```sh
# Verificar que está tudo instalado
which hermes-token-saver token-saver-ctl
podman image ls | grep -E "headroom|translate|mitmproxy"

# Se não tiver, rodar uma vez:
cd ~/token-saver-wrappers && ./build-and-install
```

## Ligar o pod

```sh
# Com pipeline completo (tradução + strip modules)
TOKEN_SAVER_TRANSLATE_ENABLED=1 token-saver-ctl start

# Sem tradução (só compressão headroom)
token-saver-ctl start
```

Verificar:
```sh
token-saver-ctl status
# Deve mostrar:
#   headroom: ready on 127.0.0.1:8787
#   translate: ready on 127.0.0.1:8786 (pt-BR ↔ EN)
```

## Usar com hermes

```sh
# No lugar de "hermes", use "hermes-token-saver"
hermes-token-saver "faça code review do arquivo src/main.py"

# Com módulos pesados ativados:
TOKEN_SAVER_FEATURE_SUMMARIZE=1 \
TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 \
TOKEN_SAVER_FEATURE_ROUTER=1 \
  hermes-token-saver "refatore o módulo de auth"
```

O wrapper hermes-token-saver:
1. Garante que o pod está rodando
2. Configura HTTP_PROXY/HTTPS_PROXY → mitm sidecar
3. Configura CA trust → mitmproxy CA
4. Executa `hermes` real com os overrides de ambiente
5. Todo tráfego LLM passa pelo pipeline automaticamente

## Fluxo completo

```
hermes --env--> HTTP_PROXY=mitm:8790 --> mitm intercepta hosts LLM
                                               │
                                               ▼
                                     proxy pipeline :8786
                                     ├── strip_system (dedup)
                                     ├── minify_tools (strip JSON Schema)
                                     ├── router (opcional)
                                     ├── translate (pt→EN)
                                     │
                                     ▼
                                     headroom :8787 (compressão)
                                     │
                                     ▼
                                     GenPlat / DeepSeek / OpenRouter
```

## Módulos disponíveis

| Módulo | Variável | Default | O que faz |
|---|---|---|---|
| translate | `TOKEN_SAVER_TRANSLATE_ENABLED` | ON* | pt-BR ↔ EN via Google Translate |
| strip_system | `TOKEN_SAVER_FEATURE_STRIP_SYSTEM` | ON | Remove instruções repetidas do system prompt |
| minify_tools | `TOKEN_SAVER_FEATURE_MINIFY_TOOLS` | ON | Remove descriptions/defaults do JSON Schema das tools |
| strip_response | `TOKEN_SAVER_FEATURE_STRIP_RESPONSE` | ON | Remove logprobs/usage/system_fingerprint das respostas |
| summarize | `TOKEN_SAVER_FEATURE_SUMMARIZE` | OFF | Resume histórico antigo quando >15K tokens |
| prompt_cache | `TOKEN_SAVER_FEATURE_PROMPT_CACHE` | OFF | Cache SHA256 — requests idênticos retornam instantâneo |
| router | `TOKEN_SAVER_FEATURE_ROUTER` | OFF | Roteia queries simples pra modelo mais barato |

\* ON quando `TOKEN_SAVER_TRANSLATE_ENABLED=1` é passado no start.

## Desabilitar módulos específicos

```sh
# Desabilitar tradução (mantém compressão + demais módulos)
FEATURE_TRANSLATE=0 hermes-token-saver "pergunta em inglês"

# Desabilitar minificação de tools (se o modelo precisar das descriptions)
FEATURE_MINIFY_TOOLS=0 hermes-token-saver "..."

# Desabilitar tudo, só compressão pura
FEATURE_TRANSLATE=0 FEATURE_STRIP_SYSTEM=0 \
FEATURE_MINIFY_TOOLS=0 FEATURE_STRIP_RESPONSE=0 \
  hermes-token-saver "..."
```

## Como saber se está funcionando

### 1. Header nas respostas

Toda resposta do proxy inclui:
```
X-Token-Saver-Modules: strip_system, minify_tools, translate, strip_response
```

Para ver no hermes, habilite verbose logging ou inspecione com mitmproxy:
```sh
# Ver os headers das requisições que passam pelo mitm
token-saver-ctl logs mitm
```

### 2. Dashboard

```sh
open http://127.0.0.1:8786/dashboard
```

Mostra em tempo real:
- Módulos ativos (pills verde = ON, cinza = OFF)
- Total de requests processados
- Tokens saved (estimado)
- Cache hit rate
- Ativações por módulo

### 3. Stats JSON

```sh
curl -s http://127.0.0.1:8786/stats | python3 -m json.tool
```

Exemplo de output:
```json
{
  "modules": ["strip_system", "minify_tools", "translate", "strip_response"],
  "total_requests": 42,
  "tokens_saved_est": 12500,
  "cache": {"hits": 3, "misses": 39, "entries": 12, "size_bytes": 45000},
  "activations": {
    "strip_system": 42,
    "minify_tools": 38,
    "translate_in": 35,
    "translate_out": 40,
    "strip_response": 42
  }
}
```

### 4. Health check

```sh
curl -s http://127.0.0.1:8786/health | python3 -m json.tool
```

### 5. Headroom stats (compressão)

```sh
token-saver-ctl stats
# Mostra resumo de tokens comprimidos pelo headroom
```

## Desligar

```sh
# Parar (containers mantidos, restart rápido)
token-saver-ctl stop

# Destruir completamente
token-saver-ctl destroy
```

## Troubleshooting

**hermes-token-saver não encontra o binário:**
```sh
export TOKEN_SAVER_HERMES_BIN=/caminho/do/hermes
hermes-token-saver "..."
```

**Tradução não funciona (só compressão):**
```sh
# Verificar se translate está no pod
token-saver-ctl status
# Deve aparecer "translate: ready on 127.0.0.1:8786"

# Se aparecer "disabled", recriar com a flag:
token-saver-ctl destroy
TOKEN_SAVER_TRANSLATE_ENABLED=1 token-saver-ctl start
```

**Erro de CA certificate:**
```sh
# O hermes-token-saver gerencia o CA automaticamente.
# Se persistir, destruir e recriar o pod:
token-saver-ctl destroy
TOKEN_SAVER_TRANSLATE_ENABLED=1 token-saver-ctl start
```

**Portas em conflito (8787/8786/8790 em uso):**
```sh
# O script automaticamente escolhe portas alternativas.
# Verificar quais foram escolhidas:
token-saver-ctl status
```

**Pod não inicia (podman machine parada):**
```sh
podman machine start
token-saver-ctl start
```

**Dashboard não abre:**
```sh
# Verificar porta correta
token-saver-ctl status | grep translate
# Ex: translate: ready on 127.0.0.1:8792
# Abrir http://127.0.0.1:<porta>/dashboard
```

## Atalhos

```sh
# Alias para o dia a dia
alias hts='hermes-token-saver'
alias hts-full='TOKEN_SAVER_TRANSLATE_ENABLED=1 hermes-token-saver'
alias hts-status='TOKEN_SAVER_TRANSLATE_ENABLED=1 token-saver-ctl status'
alias hts-dash='open http://127.0.0.1:8786/dashboard'
```

Adicione ao `~/.zshrc` ou `~/.bashrc`.
