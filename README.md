![napcat-docker-auto-restart](https://socialify.git.ci/VincentZyu233/napcat-docker-auto-restart/image?custom_language=Python&description=1&font=Inter&forks=1&issues=1&language=1&logo=https%3A%2F%2Fupload.wikimedia.org%2Fwikipedia%2Fcommons%2Fthumb%2Fc%2Fc3%2FPython-logo-notext.svg%2F120px-Python-logo-notext.svg.png&name=1&owner=1&pattern=Charlie+Brown&pulls=1&stargazers=1&theme=Auto)

# NapCat Docker Auto Restart 监控工具 捏 🐱

[![GitHub](https://img.shields.io/badge/GitHub-Repository-181717?logo=github&logoColor=white)](https://github.com/VincentZyu233/napcat-docker-auto-restart)
[![Gitee](https://img.shields.io/badge/Gitee-Repository-C71D23?logo=gitee&logoColor=white)](https://gitee.com/vincent-zyu/napcat-docker-auto-restart)

自动检测 NapCat Docker 容器中的账号在线状态，并在检测到离线时自动通过 SSH 重启容器的轻量级工具。

> **使用场景**：如果你跟我一样，在 Linux 机器上挂着 NapCat Docker，希望它在线状态更稳，那么你就是本项目的受众之一 (●'◡'●)

---

## ✨ 核心特性

- **易于定制**：使用 Python 实现，便于调试和按需修改。
- **智能策略**：
  - 支持配置多个容器，每个容器可独立开启/关闭。
  - **心跳错位**：支持设置多个容器间的检测错开时间，避免瞬间打满宿主机资源。
- **跨平台运行**：支持安装了 Python 3.8+ 的 Windows、Linux 和 macOS。
- **自动恢复**：通过 WebSocket 实时获取 Bot 运行状态，非在线即触发 SSH 执行 `docker restart`。
- **防重启风暴**：
  - **启动宽限期**：容器刚启动后的指定秒数内只观察不重启，给 NapCat 充足的登录与端口监听时间。
  - **重启冷却**：两次自动重启之间强制间隔，避免瞬时连续重启。
  - **连续熔断**：达到最大连续重启次数后自动暂停重启并告警，防止死循环。
  - **精准响应匹配**：按 `echo` 标识匹配 OneBot API 响应，忽略推送的心跳/事件，彻底避免误判。
  - **离线复检防抖**：首次探测失败后自动复检一次，过滤瞬时抖动，避免误重启。
  - **假在线深度探测**：调用需要服务端真实往返的只读接口（`get_cookies`）识别「连接还在但服务端已不响应」，连续失败 3 次才判死。
  - **人工介入识别**：识别「被顶下线 / 登录态失效 / 需要扫码 / 需要验证码」等**重启无法解决**的故障，直接跳过重启并告警（避免反复触发风控）。
  - **可选通知**：支持 webhook / Telegram 推送（掉线、需要人工介入、触发熔断、恢复在线）。

---

## 🛠️ 技术栈

### Python 版本依赖

| 依赖库 | 版本 | 说明 |
|:---|:---|:---|
| [![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org/) | 3.8+ | 编程语言 |
| [![WebSockets](https://img.shields.io/badge/WebSockets-16.0-FFD43B?style=flat-square&logo=python&logoColor=3776AB)](https://github.com/python-websockets/websockets) | 16.0 | WebSocket 通信 |
| [![PyYAML](https://img.shields.io/badge/PyYAML-6.0.3-FFD43B?style=flat-square&logo=python&logoColor=3776AB)](https://pyyaml.org/) | 6.0.3 | YAML 配置解析 |
| [![Requests](https://img.shields.io/badge/Requests-2.32.5-FFD43B?style=flat-square&logo=python&logoColor=3776AB)](https://requests.readthedocs.io/) | 2.32.5 | HTTP 请求库 |

---

## 🚀 快速开始

### 1. 下载

```bash
git clone https://github.com/VincentZyu233/napcat-docker-auto-restart.git
cd napcat-docker-auto-restart/py
```

### 2. SSH 免密登录配置（可选，推荐）

配置后，`scp` 和 `ssh` 不再需要输入密码，监控程序可以自动执行远程重启命令。完整说明请参考：[SSH CLI 使用与免密登录配置](https://vincentzyu-vitepress.pages.dev/notes/cli-tools/ssh-cli.html)。

**注意事项**：
- 确认**运行本程序的用户**（如 `root` 或普通用户）。
- 该用户需要能免密 SSH 到**远程服务器的目标用户**（配置文件中的 `ssh_user`）。
- 如果程序通过 systemd 或 MCSManager 等服务运行，需要为实际运行该服务的用户配置密钥。

#### Windows PowerShell

> ⚠️ PowerShell 管道会将公钥内容和 SSH 密码提示混淆，请勿使用 `type | ssh` 管道。下面是更稳妥的方式。

```powershell
# 1. 生成密钥（已有可跳过）
ssh-keygen -t ed25519 -C "YourName-Win"

# 2. 先读取公钥，再上传到服务器（需输入一次密码）
$key = Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
ssh root@<YOUR_SERVER> "mkdir -p ~/.ssh && echo '$key' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

# 3. 验证免密登录（不再要求输入密码即为成功）
ssh root@<YOUR_SERVER> "echo '✅ SSH 免密登录配置成功！'"
```

#### Windows CMD

```batch
REM 1. 生成密钥（已有可跳过）
ssh-keygen -t ed25519 -C "YourName-Win"

REM 2. 上传公钥到服务器（需输入一次密码）
type "%USERPROFILE%\.ssh\id_ed25519.pub" | ssh -p 22 root@<YOUR_SERVER> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

REM 3. 验证免密登录
ssh root@<YOUR_SERVER> "echo '✅ SSH 免密登录配置成功！'"
```

#### Linux / WSL

```bash
# 1. 生成密钥（已有可跳过）
ssh-keygen -t ed25519 -C "YourName-Linux"

# 2. 一键上传公钥（需输入一次密码）
ssh-copy-id -p 22 root@<YOUR_SERVER>

# 3. 验证免密登录
ssh root@<YOUR_SERVER> "echo '✅ SSH 免密登录配置成功！'"
```

> 建议使用 `ed25519` 算法，它比 RSA 更安全且密钥更短。私钥文件权限应设置为 `600`。

### 3. 配置 (`config.yaml`)
配置文件使用 YAML 格式，可以参考 `py/config.example.yaml`。

```yaml
check_interval_ms: 10000      # 总检测频率
stagger_interval_ms: 500      # 容器间错开时间
confirm_delay_ms: 3000        # 首次探测失败后的复检等待（毫秒）
offline_log_window_s: 600     # 判定「需要人工介入」时回溯容器日志的时间窗（秒）
notify_webhook: ""            # 通知 webhook（POST JSON），留空不通知
notify_telegram_bot_token: "" # Telegram Bot Token（可选）
notify_telegram_chat_id: ""   # Telegram Chat ID（可选）
notify_min_interval_s: 1800   # 同类通知最小间隔（秒）

containers:
  - enabled: true             # 是否启用
    name: napcat-dev          # Docker 容器名
    ssh_user: zyu             # SSH 登录用户名
    ssh_host: 192.168.31.233  # 服务器 IP
    ws_port: 3000             # NapCat 的 WS 端口
    token: your_token         # Access Token
    auto_restart: true        # 离线是否自动重启
    use_sudo: false           # 是否使用 sudo
    startup_grace_s: 120      # 容器启动后的宽限期（秒），期间只观察不重启
    restart_cooldown_s: 300   # 两次自动重启之间的最小间隔（秒）
    max_restart_attempts: 3   # 连续自动重启次数上限，超过后只告警
    deep_probe: true          # 深度探测（识别假在线），老版本 NapCat 不支持会自动跳过
```

**配置说明**：
- `use_sudo`: 如果 SSH 用户不在 docker 组，需设为 `true` 并配置 sudo 免密（见下方）
- `startup_grace_s` / `restart_cooldown_s` / `max_restart_attempts`: 防「重启风暴」的三道保险，都可省略，省略时使用默认值（`120` / `300` / `3`），详见下面「防重启风暴」一节


#### 配置 sudo 免密（可选）

如果 `use_sudo: true`，需在 Docker 宿主机上配置：

```bash
sudo visudo
# 添加（把 zyu 换成你的用户名）：
zyu ALL=(ALL) NOPASSWD: /usr/bin/docker
```

或者将用户加入 docker 组（推荐）：
```bash
sudo usermod -aG docker zyu
# 重新登录后生效，然后设置 use_sudo: false
```

### 4. 运行

```bash
cd py
pip install -r requirements.txt
python src/main.py
```

---

## 🛠️ 进阶技巧：配合 NapCat 自动登录

为了保证重启后能自动登录，**强烈建议**在 Docker 容器中同时配置 `ACCOUNT` 和密码回退环境变量 `NAPCAT_QUICK_PASSWORD`（或 `NAPCAT_QUICK_PASSWORD_MD5`）。

> **说明**：NapCat 官方在新版本中对自动登录进行了两级保护：
> 1. 首先尝试使用本地凭证快速登录；
> 2. 若凭证失效，则自动回退到 `NAPCAT_QUICK_PASSWORD` 环境变量配置的密码进行登录；
> 3. 若未配置密码，才会退化为需要扫码的二维码登录模式。

**方法一：命令行重启容器**
```bash
docker run -d \
  -e ACCOUNT=你的QQ号 \
  -e NAPCAT_QUICK_PASSWORD=你的QQ密码 \
  -p 3000:3000 \
  -v napcat-config:/app/napcat/config \
  -v napcat-qq:/app/.config/QQ \
  --name napcat-dev \
  mlikiowa/napcat-docker:latest
```

**方法二：1Panel 面板**
1. 进入容器详情 -> 编辑。
2. 在环境变量中添加：
   - `ACCOUNT=你的QQ号`
   - `NAPCAT_QUICK_PASSWORD=你的QQ密码`（可选 MD5：`NAPCAT_QUICK_PASSWORD_MD5=32位大写或小写MD5`）
3. 保存并由面板自动重建容器。

---

## 📂 项目结构
- `py/`: Python 实现的主程序。
- `js/`: 最初用于探索 WebSocket 接口逻辑的测试脚本。

---

## 💡 开发背景
1. 最开始使用 Python 快速验证并实现业务逻辑。
2. JS 的部分最初用于测试 WebSocket 接口，现保留为探索性测试脚本。

---

> ⚠️ **注意**：本项目目前暂无 License，仅供个人学习和技术交流使用捏。
