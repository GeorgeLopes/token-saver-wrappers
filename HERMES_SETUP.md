# Hermes + Token Saver — Guia de uso

Última atualização: 2026-07-12 (v1.0 — 7 módulos, 3 plataformas)

## Instalação (uma vez)

```sh
git clone https://github.com/GeorgeLopes/token-saver-wrappers.git ~/token-saver-wrappers
cd ~/token-saver-wrappers
./install.sh
```

O instalador detecta o OS e configura tudo. Para instruções detalhadas por plataforma, veja [PLATFORMS.md](PLATFORMS.md).

**Plataformas suportadas:**
- macOS (Apple Silicon / Intel)
- Linux Ubuntu 22.04+
- Windows 10/11 via WSL2

## Uso diário (atalhos)

```sh
hts         # hermes com pipeline completo (7 módulos)
hts-status  # status do pod
hts-dash    # abre dashboard no navegador
hts-stats   # economia real do headroom
```

Adicionado ao `~/.zshrc`:
```sh
alias hts="TOKEN_SAVER_FEATURE_SUMMARIZE=1 TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 TOKEN_SAVER_FEATURE_ROUTER=1 TOKEN_SAVER_SUMMARIZE_MODEL=deepseek-v4-flash-claude TOKEN_SAVER_ROUTER_CHEAP_MODEL=deepseek-v4-flash-claude hermes-token-saver"
alias hts-status="token-saver-ctl status"
alias hts-dash="open http://127.0.0.1:8786/dashboard"
alias hts-stats="token-saver-ctl stats"
```

## Ligar/desligar o pod

```sh
# Ligar com pipeline completo (7 módulos)
TOKEN_SAVER_TRANSLATE_ENABLED=1 \
TOKEN_SAVER_FEATURE_SUMMARIZE=1 \
TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 \
TOKEN_SAVER_FEATURE_ROUTER=1 \
TOKEN_SAVER_SUMMARIZE_MODEL=deepseek-v4-flash-claude \
TOKEN_SAVER_ROUTER_CHEAP_MODEL=deepseek-v4-flash-claude \
  token-saver-ctl start

# Apenas compressão headroom (sem pipeline)
token-saver-ctl start

# Parar / destruir
token-saver-ctl stop
token-saver-ctl destroy
```

## Pipeline completo (7 módulos)

```
REQUEST:
  1. prompt_cache  → SHA256 lookup, retorna instantâneo se hit
  2. summarize     → colapsa histórico > 15K chars em resumo
  3. strip_system  → dedup "You MUST"/"CRITICAL" no system prompt
  4. minify_tools  → remove descriptions/defaults do JSON Schema
  5. router        → queries simples → deepseek-v4-flash-claude
  6. translate     → pt-BR → EN (Google Translate)

RESPONSE:
  1. translate     → EN → pt-BR
  2. strip_response → remove logprobs/usage/system_fingerprint
  3. prompt_cache  → armazena resposta para reuso
```

## Módulos — liga/desliga individual

| Módulo | Variável | Default | Economia típica |
|---|---|---|---|
| translate | `TOKEN_SAVER_TRANSLATE_ENABLED` | ON* | pt↔EN reduz chars |
| strip_system | `TOKEN_SAVER_FEATURE_STRIP_SYSTEM` | ON | ~118 tokens/req |
| minify_tools | `TOKEN_SAVER_FEATURE_MINIFY_TOOLS` | ON | ~14.8K chars/req |
| strip_response | `TOKEN_SAVER_FEATURE_STRIP_RESPONSE` | ON | ~500 bytes/req |
| summarize | `TOKEN_SAVER_FEATURE_SUMMARIZE` | OFF** | colapsa histórico longo |
| prompt_cache | `TOKEN_SAVER_FEATURE_PROMPT_CACHE` | OFF** | hit = 0 tokens gastos |
| router | `TOKEN_SAVER_FEATURE_ROUTER` | OFF** | flash vs Opus (~80%) |

\* ON quando `TOKEN_SAVER_TRANSLATE_ENABLED=1` no start do pod
\** ON via alias `hts`

```sh
# Desligar tradução (manter compressão + demais módulos)
FEATURE_TRANSLATE=0 hts "pergunta em inglês"

# Desligar minificação de tools
FEATURE_MINIFY_TOOLS=0 hts "..."

# Apenas compressão pura (todos os módulos off)
FEATURE_TRANSLATE=0 FEATURE_STRIP_SYSTEM=0 \
FEATURE_MINIFY_TOOLS=0 FEATURE_STRIP_RESPONSE=0 \
  hermes-token-saver "..."
```

## Modelos configurados

| Uso | Modelo | Variável |
|---|---|---|
| Principal (hermes) | deepseek-v4-pro | (config do hermes) |
| Router (queries simples) | deepseek-v4-flash-claude | `TOKEN_SAVER_ROUTER_CHEAP_MODEL` |
| Summarize (resumo) | deepseek-v4-flash-claude | `TOKEN_SAVER_SUMMARIZE_MODEL` |

## Visibilidade

### 1. Header HTTP em toda resposta
```
X-Token-Saver-Modules: prompt_cache, summarize, strip_system, minify_tools, router, translate, strip_response
```

### 2. Dashboard (navegador)
```
http://127.0.0.1:8786/dashboard
```
Mostra em tempo real (auto-refresh 5s):
- 7 módulos (pills verde = ON, cinza = OFF)
- Total de requests
- Input chars reduzidos (pipeline)
- Cache hit rate + entries
- Ativações por módulo: strip_system, minify_tools, router, translate, strip_response (SSE separado)

### 3. Stats JSON
```sh
curl -s http://127.0.0.1:8786/stats | python3 -m json.tool
```

### 4. Economia real (headroom)
```sh
hts-stats
# Mostra tokens comprimidos e custo real economizado
```

### 5. Health check
```sh
curl -s http://127.0.0.1:8786/health | python3 -m json.tool
# Mostra quais módulos estão ativos, translator status, cache entries
```

## Métricas — o que cada número significa

| Métrica | Fonte | Significado |
|---|---|---|
| `chars_reduced` | `/stats` | Total de chars removidos do input (soma de todos os módulos). NÃO é custo. |
| `Tokens saved` | `hts-stats` | Tokens reais economizados pelo headroom (compressão). ESSE é o custo. |
| `Cost saved` | `hts-stats` | $ economizados (quando provider reporta pricing) |
| `cache hits/misses` | `/stats` | Requests idênticos (SHA256) que retornaram do cache |
| `sse_passthrough` | `/stats` | Responses streaming que pularam o pipeline |

## Resultados reais (3 rodadas de teste)

| Rodada | Requests | Headroom | strip_system | minify_tools | router |
|---|---|---|---|---|---|
| 1 | 8 | 12.98% (14K tokens) | 0 (bug) | 7/8 | off |
| 2 | 5 | 10.62% (5K tokens) | 4/5 ✅ | 4/5 | off |
| 3 | 3 | 0%* | 2/3 | 2/3 | 3/3 → flash ✅ |

\* Requests curtas, sem conteúdo compressível — esperado.

## Troubleshooting

**hts não encontra o comando:**
```sh
source ~/.zshrc  # ou abra nova aba
```

**Erro "Invalid model name":**
O router ou summarize está usando um modelo que não existe no GenPlat.
```sh
# Verificar modelo configurado:
echo $TOKEN_SAVER_ROUTER_CHEAP_MODEL
echo $TOKEN_SAVER_SUMMARIZE_MODEL

# Corrigir no ~/.zshrc (alias hts) ou desabilitar:
FEATURE_ROUTER=0 FEATURE_SUMMARIZE=0 hts "..."
```

**Tradução não funciona:**
```sh
hts-status
# Deve mostrar: translate: ready on 127.0.0.1:8786 (pt-BR ↔ EN)
# Se disabled, destruir e recriar com TOKEN_SAVER_TRANSLATE_ENABLED=1
```

**Portas em conflito:**
O script escolhe portas alternativas automaticamente. Verificar com `hts-status`.

**Pod não inicia:**
```sh
podman machine start
hts-status
```

**Cache não efetivo (>0 misses, 0 hits):**
Normal com poucas requests. Cache brilha após 10+ interações no mesmo contexto.
