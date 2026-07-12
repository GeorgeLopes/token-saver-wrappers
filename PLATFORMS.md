# Token Saver — Guia de instalação por plataforma

## macOS (Apple Silicon / Intel)

```sh
# 1. Instalar podman
brew install podman

# 2. Clonar e instalar
git clone https://github.com/GeorgeLopes/token-saver-wrappers.git ~/token-saver-wrappers
cd ~/token-saver-wrappers
./install.sh
```

O instalador cria a VM Linux automaticamente.

## Linux Ubuntu 22.04+

```sh
# 1. Instalar dependências
sudo apt-get update
sudo apt-get install -y podman uidmap git
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER

# 2. Clonar e instalar
git clone https://github.com/GeorgeLopes/token-saver-wrappers.git ~/token-saver-wrappers
cd ~/token-saver-wrappers
./install.sh
```

## Windows 10/11 (via WSL2)

### Passo 1: Instalar WSL2 (PowerShell como Admin)

```powershell
wsl --install -d Ubuntu
# Reiniciar o computador se solicitado
```

### Passo 2: Setup completo dentro do Ubuntu (WSL2)

```sh
# Clonar repositório
git clone https://github.com/GeorgeLopes/token-saver-wrappers.git ~/token-saver-wrappers
cd ~/token-saver-wrappers

# Rodar setup (instala podman, pede token GenPlat, builda imagens, cria atalho)
bash wsl-setup.sh
```

O `wsl-setup.sh` faz **tudo**:
1. Instala podman + dependências
2. Pede o token GenPlat interativamente e salva em `~/.hermes/.env`
3. Builda as imagens (10-30 min na primeira vez)
4. Inicia o pod com 7 módulos
5. Cria atalho `Token Saver.bat` no Desktop do Windows

### Uso no Windows

**Opção A: Atalho no Desktop**
Clique duas vezes em `Token Saver.bat` — abre terminal, inicia hermes com pipeline completo.

**Opção B: Terminal WSL2**
```sh
# Dentro do Ubuntu (WSL2):
cd ~/token-saver-wrappers
TOKEN_SAVER_FEATURE_SUMMARIZE=1 \
TOKEN_SAVER_FEATURE_PROMPT_CACHE=1 \
TOKEN_SAVER_FEATURE_ROUTER=1 \
TOKEN_SAVER_SUMMARIZE_MODEL=deepseek-v4-flash-claude \
TOKEN_SAVER_ROUTER_CHEAP_MODEL=deepseek-v4-flash-claude \
  hermes-token-saver
```

**Dashboard:** abra `http://localhost:8786/dashboard` no navegador Windows.
(WSL2 faz port forwarding automático)

### Arquitetura no Windows

```
┌─────────────────────────────────────────────┐
│ Windows 10/11                               │
│  ┌───────────────────────────────────────┐  │
│  │ WSL2 (Ubuntu)                         │  │
│  │  ┌─────────────────────────────────┐  │  │
│  │  │ podman pod "token-saver"        │  │  │
│  │  │ ├── headroom    :8787           │  │  │
│  │  │ ├── proxy       :8786           │  │  │
│  │  │ └── mitm        :8790           │  │  │
│  │  └─────────────────────────────────┘  │  │
│  │  hermes-token-saver (wrapper bash)    │  │
│  │  hermes (binário Linux)               │  │
│  └───────────────────────────────────────┘  │
│                                              │
│  Navegador → http://localhost:8786/dashboard │
│  Atalho Desktop → Token Saver.bat            │
└─────────────────────────────────────────────┘
```

### VPN Corporativa + WSL2

A GenPlat está atrás da VPN do iFood. O WSL2 tem rede isolada — a VPN do Windows
**não** propaga automaticamente.

**Diagnóstico:**
```sh
bash wsl-vpn-check.sh
```

**Solução recomendada: WSL2 Mirror Mode (Windows 11 23H2+)**

Crie `%USERPROFILE%\.wslconfig` no Windows:
```ini
[wsl2]
networkingMode=mirrored
dnsTunneling=true
```

Depois reinicie o WSL:
```powershell
wsl --shutdown
wsl
```

Isso faz o WSL2 compartilhar a rede do Windows — VPN, DNS, tudo funciona.

**Alternativa: Proxy explícito**
```sh
export HTTP_PROXY=http://proxy.ifoodcorp.com.br:80
export HTTPS_PROXY=http://proxy.ifoodcorp.com.br:80
```

### Token GenPlat

**Setup inicial** (interativo, feito pelo `wsl-setup.sh`):
```sh
# O script pede o token e salva em ~/.hermes/.env
```

**Renovar token:**
```sh
# Opção 1: Editar manualmente
nano ~/.hermes/.env
# Atualizar: GENPLAT_API_KEY=seu-novo-token

# Opção 2: Via tompero (se instalado no WSL2)
tompero token
# O token vai para ~/.config/tompero/requester_token
# O hermes-token-saver usa GENPLAT_API_KEY do ~/.hermes/.env
```

**Verificar:**
```sh
cat ~/.hermes/.env | grep GENPLAT_API_KEY
```

## Resumo

| Plataforma | Setup | Tempo 1ª vez | Complexidade |
|---|---|---|---|
| macOS | `brew install podman && ./install.sh` | 15-30 min | Baixa |
| Linux | `apt install podman && ./install.sh` | 15-30 min | Muito baixa |
| Windows | WSL2 + `bash wsl-setup.sh` | 20-40 min | Média |
