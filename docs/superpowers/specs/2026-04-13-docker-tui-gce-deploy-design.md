# Docker TUI + GCE 部署設計

## 概述

將 `as_terminal_max.py` 網格交易 bot 打包為 Docker container，在 GCE VM 上以互動式 TUI 模式運行（類似 Hummingbot），並加入 Telegram 通知機制。

## 架構

```
GCE VM (e2-small, 固定外部 IP)
└── Docker container (interactive TUI)
    ├── as_terminal_max.py (主程式 + Rich TUI)
    ├── config/ (volume mount, API keys 明文 JSON)
    ├── SIGTERM handler (graceful shutdown)
    └── Telegram notifier
        ├── crash / restart 通知
        ├── 每日損益摘要 (每天 20:00 Asia/Taipei)
        └── 風控警報 (止損、保證金不足)
```

### 操作方式

```bash
# 啟動
docker compose -f docker-compose.terminal.yml run -it as-grid

# 斷開但保持運行
Ctrl+P, Ctrl+Q

# 重新接回
docker attach <container_name>
```

## 改動清單

### 新增檔案

| 檔案 | 用途 |
|------|------|
| `Dockerfile.terminal` | Terminal 版專用 image，CMD 指向 `as_terminal_max.py` |
| `docker-compose.terminal.yml` | TUI 模式 compose，stdin_open + tty 開啟 |
| `grid_engine/notifier.py` | Telegram 通知模組 |
| `.dockerignore` | 排除 config/、.claude/、.git/ 等敏感目錄 |
| `scripts/gce-setup.sh` | GCE 一鍵部署腳本（安裝 Docker、clone repo） |

### 修改檔案

| 檔案 | 改動 |
|------|------|
| `grid_engine/bot.py` | 加 SIGTERM handler、接入 notifier、每日摘要定時任務 |
| `as_terminal_max.py` | 加 signal handling、Telegram 設定選單（選項 9） |
| `grid_engine/__init__.py` | export TelegramNotifier |
| `grid_engine/config.py` | GlobalConfig 加 telegram_bot_token、telegram_chat_id 欄位 |

## 模組設計

### TelegramNotifier (`grid_engine/notifier.py`)

```python
class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str)
    async def send(self, message: str) -> bool
    async def notify_crash(self, error: str)
    async def notify_restart(self)
    async def notify_daily_pnl(self, pnl_data: dict)
    async def notify_risk_alert(self, alert: str)
```

- 使用 Telegram Bot API（HTTP POST via `aiohttp`，不需額外套件）
- `bot_token` + `chat_id` 存在 `config/trading_config_max.json`
- TUI 選單內設定（新增選項 9: Telegram 設定）
- 未設定時靜默跳過，不影響交易功能

### SIGTERM Handler

```python
# 在 bot.run() 開始時註冊
loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(bot.stop()))
loop.add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(bot.stop()))
```

### Graceful Shutdown 流程

```
Docker stop → SIGTERM
  → bot.stop()
    → cancel all async tasks
    → 等待清理完成
    → Telegram 通知 "Bot 已停止"
  → Container exit (code 0)

Docker restart policy (unless-stopped)
  → Container 重起
  → 停在主選單
  → Telegram 通知 "Bot 已重啟，等待手動操作"
```

### 每日損益摘要

- Bot 運行中，每天 20:00 (Asia/Taipei) 觸發
- 使用 `asyncio` 定時任務
- 內容：當日 PnL、持倉、運行時間

### 風控警報觸發條件

- 觸及最大虧損閾值
- 保證金率過低
- 連續錯誤超過閾值

## Docker 配置

### Dockerfile.terminal

- 基於 `python:3.11-slim`
- 安裝 `requirements-terminal.txt`
- 不 COPY config/（由 volume mount 提供）
- CMD: `python as_terminal_max.py`
- Non-root user 運行

### docker-compose.terminal.yml

```yaml
services:
  as-grid:
    build:
      context: .
      dockerfile: Dockerfile.terminal
    stdin_open: true  # docker run -i
    tty: true         # docker run -t
    volumes:
      - ./config:/app/config
      - ./data:/app/data
    environment:
      - TZ=Asia/Taipei
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 1G
```

### .dockerignore

```
config/
.claude/
.git/
__pycache__/
*.pyc
.DS_Store
log/
data/
asBack/
docs/
```

## GCE 部署

### VM 規格

- 機型：e2-small（2 vCPU, 2GB RAM）
- OS：Ubuntu 22.04 LTS
- 固定外部 IP
- Firewall：只開 SSH (port 22)

### 部署腳本 (`scripts/gce-setup.sh`)

1. 安裝 Docker + Docker Compose
2. 設定 SSH key only（關閉密碼登入）
3. Clone repo
4. 提示設定 config

### 安全措施

- `.dockerignore` 排除 config/，image 不含 API key
- GCE firewall 只開 SSH
- 交易所 API 只開交易權限，綁 GCE 固定 IP 白名單
- Container 以 non-root user 運行
- SSH 關閉密碼登入，只用 key
