# Token Saver — Guia de instalação por plataforma

## macOS (Apple Silicon / Intel)

```sh
# 1. Instalar podman
brew install podman

# 2. Rodar instalador
cd ~/token-saver-wrappers
./install.sh
```

O instalador cria a VM Linux automaticamente (`podman machine init`).

## Linux Ubuntu 22.04+

```sh
# 1. Instalar podman
sudo apt-get update
sudo apt-get install -y podman uidmap

# 2. Configurar rootless podman
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER
podman system migrate

# 3. Rodar instalador
cd ~/token-saver-wrappers
./install.sh
```

Se estiver em ZFS:
```sh
sudo apt-get install -y fuse-overlayfs
```

O `install.sh` detecta Linux automaticamente e pula a etapa de VM.

## Windows 10/11 (via WSL2)

Token Saver usa bash + podman. No Windows, o caminho é rodar dentro do WSL2.

```powershell
# 1. Instalar WSL2 (PowerShell como Admin)
wsl --install -d Ubuntu

# 2. Dentro do Ubuntu (WSL2):
sudo apt-get update
sudo apt-get install -y podman uidmap
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER

# 3. Clonar e instalar
git clone https://github.com/GeorgeLopes/token-saver-wrappers.git ~/token-saver-wrappers
cd ~/token-saver-wrappers
./install.sh
```

**Limitações no Windows:**
- O pod roda dentro do WSL2, não nativamente no Windows
- `open http://...` não funciona — acesse o dashboard pelo navegador Windows em `http://localhost:8786`
- Os wrappers (`hermes-token-saver`) precisam ser executados de dentro do WSL2
- O binário `hermes` precisa estar instalado dentro do WSL2

**Para usar o dashboard do Windows:**
```
http://localhost:8786/dashboard
```
(O WSL2 faz port forwarding automático para o host Windows)

## Resumo

| Plataforma | Runtime | Instalação | Complexidade |
|---|---|---|---|
| macOS | podman machine (VM) | `brew install podman && ./install.sh` | Baixa |
| Linux Ubuntu | podman rootless (nativo) | `apt install podman && ./install.sh` | Muito baixa |
| Windows | WSL2 + Ubuntu | WSL2 + seguir passos Linux | Média |
