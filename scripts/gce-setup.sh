#!/bin/bash
# AS Grid Dragon — GCE 部署腳本
# 在全新的 Ubuntu 22.04 GCE VM 上執行
#
# 使用方式:
#   1. 建立 GCE VM (e2-small, Ubuntu 22.04, 固定外部 IP)
#   2. SSH 進入 VM
#   3. git clone https://github.com/RamonLiao/as-grid-dragon.git
#   4. cd as-grid-dragon && bash scripts/gce-setup.sh

set -euo pipefail

echo "=============================="
echo " AS Grid Dragon — GCE Setup"
echo "=============================="

# 1. 安裝 Docker
if ! command -v docker &> /dev/null; then
    echo "[1/4] 安裝 Docker..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # 讓當前使用者不需 sudo 就能用 docker
    sudo usermod -aG docker "$USER"
    echo "[✓] Docker 已安裝。注意：需要重新登入才能不加 sudo 使用 docker。"
else
    echo "[1/4] Docker 已安裝，跳過"
fi

# 2. Clone repo (如果不是從 repo 內執行)
REPO_DIR="$HOME/as-grid-dragon"
if [ ! -d "$REPO_DIR" ]; then
    echo "[2/4] Clone repo..."
    git clone https://github.com/RamonLiao/as-grid-dragon.git "$REPO_DIR"
else
    echo "[2/4] Repo 已存在，pull 最新版..."
    cd "$REPO_DIR" && git pull
fi

cd "$REPO_DIR"

# 3. 建立 config 目錄
echo "[3/4] 建立必要目錄..."
mkdir -p config data log

# 4. Build Docker image
echo "[4/4] Build Docker image..."
sudo docker compose -f docker-compose.terminal.yml build

echo ""
echo "=============================="
echo " 部署完成！"
echo "=============================="
echo ""
echo "下一步："
echo "  1. 重新登入 SSH (讓 docker group 生效)"
echo "  2. cd $REPO_DIR"
echo "  3. docker compose -f docker-compose.terminal.yml up"
echo "  4. 在 TUI 中設定 API Key (選項 7) 和 Telegram (選項 9)"
echo "  5. 到交易所綁定此 VM 的 IP 白名單"
echo ""
echo "VM 外部 IP:"
curl -s http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip -H "Metadata-Flavor: Google" 2>/dev/null || echo "(無法自動取得，請到 GCP Console 查看)"
echo ""
