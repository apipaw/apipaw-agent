# APIPaw Agent

> 挂机节点本地客户端 —— 在你的电脑空闲时把算力贡献给 APIPaw 网络，一碰键鼠就立刻让退。
>
> **APIPaw Agent** — A local idle-yield client for the APIPaw network. Contribute your
> compute while your machine is idle; yield within **50 ms** the moment you touch the keyboard.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![API](https://img.shields.io/badge/api-OpenAI%20compatible-brightgreen.svg)](https://platform.openai.com/docs/api-reference)
[![CI](https://github.com/apipaw/apipaw-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/apipaw/apipaw-agent/actions/workflows/ci.yml)

**语言 / Language**：[中文](#中文版--chinese) ｜ [English](#english-version)

---

<a id="中文版--chinese"></a>
# 中文版

## 这是什么

APIPaw Agent 是一个**常驻本地的挂机助手客户端**：

- 在你的电脑**空闲**时，以 OpenAI 兼容接口（`127.0.0.1:52415`）对外提供推理能力
- 一旦检测到**键鼠等外设活动**，节点在 **50ms 内**切换为「已让退」并释放显存，绝不抢占你正在使用的前台体验
- 每 10 秒向云端上报节点遥测（节点名、剩余显存、GPU 温度），让调度侧知道哪个节点还在线（WebSocket 长连接主通道）
- 系统托盘实时显示状态、显存与温度，支持一键暂停 / 恢复

**设计原则**：你的电脑优先。节点只在"你不用电脑"时工作。

---

## 特性

| 特性 | 说明 |
|---|---|
| 🚀 **50ms 极速让退** | `GetLastInputInfo` + `timeBeginPeriod(1)` 提升计时器精度到 1ms，5ms 轮询；**先切状态、后释放显存**，`empty_cache()` 不再击穿 SLA |
| 🖥️ **OpenAI 规范接口** | `/v1/chat/completions`（支持 `stream`）、`/v1/models`、`/health` |
| 📊 **托盘可视化** | 状态 / 显存占用 / 显卡温度 / 一键暂停恢复，图标颜色随状态变化 |
| 📡 **云端心跳** | 默认 10 秒一次，失败自动退避重试，绝不影响本地服务 |
| 🔌 **WebSocket 主通道** | 节点与云端统一走 `wss://cloud.apipaw.com/api/v1/node/stream` 长连接；云端关闭连接时**静默退避重连**，HTTP 心跳仅作兜底 |
| 🧩 **零重依赖** | torch 是**可选**依赖：没装 PyTorch 也能启动、能心跳、能当挂机节点 |
| 🛠️ **一键安装** | `install.ps1`（Windows）/ `install.sh`（macOS / Linux），自动建 venv、装依赖、生成配置 |
| 🧭 **跨平台** | Windows 走 Win32 原生实现；macOS / Linux 提供等价降级，模块始终可 import |
| 📜 **模型元数据标准** | [`PAWMODEL.md`](PAWMODEL.md) 定义社区模型接入与 **15% 创作者版税**绑定规范 |

---

## 系统要求

- **Python 3.10+**（开发验证环境：3.12.10）
- Windows 10 / 11（完整让退能力），macOS / Linux（可运行，Windows 独占特性降级）
- NVIDIA 显卡（可选，用于显存与温度指标；无 GPU 时指标上报为 `null`，不影响运行）

---

## 快速开始

### 一行命令安装（Windows / PowerShell）

```powershell
irm https://raw.githubusercontent.com/apipaw/apipaw-agent/main/install.ps1 | iex
```

### 一键脚本（Windows）

仓库根目录提供三个纯 UTF-8 的 Windows 脚本，双击即可用：

| 脚本 | 作用 |
|---|---|
| `install.bat` | 建 `.venv` 虚拟环境 + 安装 `requirements.txt` 依赖 |
| `start.bat` | 前台启动节点（控制台可见日志） |
| `start_silent.vbs` | 静默后台启动（无控制台窗口），日志见任务管理器 / 托盘 |

```bat
install.bat        :: 首次：初始化环境
start.bat          :: 启动节点
```

### 一行命令安装（macOS / Linux）

```bash
curl -fsSL https://raw.githubusercontent.com/apipaw/apipaw-agent/main/install.sh | bash
```

安装完成后启动：

```powershell
.\.venv\Scripts\python.exe main.py     # Windows
./.venv/bin/python main.py              # macOS / Linux
```

### 手动安装（Windows / PowerShell）

```powershell
git clone https://github.com/apipaw/apipaw-agent.git
cd apipaw-agent
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\.venv\Scripts\python.exe main.py
```

可选参数：

```powershell
.\install.ps1 -StartOnBoot   # 顺便注册开机自启快捷方式
.\install.ps1 -NoVenv        # 不建虚拟环境，直接装到当前解释器
```

### 手动安装（macOS / Linux）

```bash
git clone https://github.com/apipaw/apipaw-agent.git
cd apipaw-agent
bash install.sh
./.venv/bin/python main.py
```

可选参数：

```bash
bash install.sh --systemd        # 注册 systemd --user 服务并启动
bash install.sh --no-venv
```

### 验证

```bash
curl http://127.0.0.1:52415/health
```

```json
{
  "status": "ok",
  "agent_version": "1.0.3",
  "node_id": "node_default_001",
  "node_name": "APIPaw-Node",
  "state": "IDLE",
  "state_label": "挂机中",
  "available": true,
  "gpu": { "gpu_temp_c": 47.0, "vram_used_gb": 1.2, "vram_total_gb": 6.0, "vram_free_gb": 4.8 },
  "yield_metrics": { "yield_count": 3, "last_yield_latency_ms": 12.0, "sla_ms": 50, "sla_violations": 0 }
}
```

---

## 配置：`agent.config.json`

首次启动自动生成，可直接编辑后重启生效（`AgentConfig.reload()` 支持热加载）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `host` | str | `127.0.0.1` | 本地监听地址 |
| `port` | int | `52415` | 本地监听端口 |
| `api_base_url` | str | `http://127.0.0.1:52415/v1` | 对外基址（填给 OpenAI 客户端） |
| `model_name` | str | `apipaw-gpt2-small` | 节点提供的模型名 |
| `node_id` | str | `node_default_001` | 节点唯一标识 |
| `node_name` | str | `APIPaw-Node` | 节点名称（心跳上报） |
| `hardware_type` | str | `desktop` | 硬件类型：`desktop` / `laptop` / `server` |
| `wallet_address` | str | `""` | 钱包地址（收益结算） |
| `node_secret` | str | `""` | 节点密钥，作为 `Authorization: Bearer` 上送 |
| `device` | str | `cuda` | 推理设备 |
| `max_vram_gb` | float | `4.5` | 最大显存占用上限（GB） |
| `idle_threshold_seconds` | float | `15.0` | 连续空闲多久后回到「挂机中」 |
| `vram_yield_ms` | int | `50` | **让退 SLA（毫秒）**，超出即记一次违约 |
| `poll_interval_ms` | int | `5` | 输入轮询间隔（毫秒） |
| `cloud_heartbeat_url` | str | `https://cloud.apipaw.com/api/v1/nodes/heartbeat` | 心跳地址 |
| `heartbeat_interval_seconds` | float | `10.0` | 心跳周期 |
| `disable_telemetry` | bool | `false` | 关闭心跳上报 |
| `enable_tray` | bool | `true` | 是否启动系统托盘 |

也可以在 Python 中直接改：

```python
from agent_config import get_config
get_config().update(node_name="my-workstation", hardware_type="desktop",
                    wallet_address="0x...", max_vram_gb=6.0)
```

---

## 可视化配置面板（`/ui`）

**Windows 启动后会自动打开浏览器面板**：不用再手改 JSON，也不用重启进程。

```
http://127.0.0.1:52415/ui
```

面板里填 Node ID 与 Node Secret，点「绑定」即可完成三件事：

1. 原子写盘到 `agent.config.json`（`node_id` / `node_secret`）
2. 刷新内存中的配置单例
3. **主动断开**当前被拒（403/401）的无效连接，并唤醒退避中的连接循环，
   让节点用新凭证立刻重新握手 —— 实测 **0.1 秒**内完成重连

相关接口：

| 路由 | 方法 | 说明 |
|---|---|---|
| `/ui` | GET | 单文件 HTML 面板（零外部依赖，暗色科幻风） |
| `/api/status` | GET | 当前绑定状态与云端连接情况（**不返回明文密钥**） |
| `/api/bind` | POST | 绑定 Node ID / Secret 并触发平滑重连 |

> 环境变量 `APIPAW_NO_UI_BROWSER=1` 可关闭启动时自动打开浏览器（无头/服务器场景）。

---

## API（OpenAI 兼容）

### `GET /health`

返回节点状态、GPU 指标与让退指标。始终返回 `200`（进程活着就是健康）。

### `GET /v1/models`

标准 OpenAI 模型列表。

### `POST /v1/chat/completions`

标准 OpenAI 对话补全，支持 `stream: true` 的 SSE 输出。

```bash
curl http://127.0.0.1:52415/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "apipaw-gpt2-small",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": false
  }'
```

**节点已让退时返回 `503`**：

```json
{
  "error": {
    "message": "节点已让退，用户正在使用电脑",
    "type": "node_unavailable",
    "code": "node_yielded"
  }
}
```

> 这是**有意为之**的行为：调度侧据此把任务转移到其它节点，而不是让本机抢占用户前台。

**真实 token 用量**：

响应始终带 `usage`（`prompt_tokens` / `completion_tokens` / `total_tokens`），
流式时随**结束帧**一起下发。计数后端按优先级选择：本地 HF 分词器
（配置 `tokenizer_path`）→ `tiktoken` → CJK 感知启发式。

节点通过 WebSocket 主通道接单时，把这份真实用量随 `RESPONSE_DONE` 帧回传云端，
云端据此**精确计费**（而不是按字符数估算）；老版本节点不上报时云端自动回退到估算。

用 OpenAI SDK 调用：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:52415/v1", api_key="apipaw-local")
print(client.chat.completions.create(
    model="apipaw-gpt2-small",
    messages=[{"role": "user", "content": "hello"}],
).choices[0].message.content)
```

---

## 让退机制（50ms SLA）

```
                    用户触碰键鼠
                          │
                          ▼
    GetLastInputInfo ───┴─── 空闲时间骤降 (<1000ms)
                          │
                          ▼
       ┌──────────────────────────────┐
       │ 1. 立即切换状态 → YIELDED     │  ← 先切状态，几十微秒
       │ 2. 通知托盘 / 上报心跳        │
       │ 3. 异步释放显存（独立线程）    │  ← empty_cache() 不阻塞 SLA
       └──────────────────────────────┘
                          │
                          ▼
         连续空闲 ≥ idle_threshold_seconds → 回到 IDLE
```

**为什么这样设计**

| 细节 | 做法 | 原因 |
|---|---|---|
| 计时器精度 | `timeBeginPeriod(1)` | Windows 默认 15.6ms，单这一项就可能吃掉 50ms 预算的三分之一 |
| 轮询节奏 | 5ms 守护线程 | 兼顾 CPU 占用与检测延迟 |
| 状态与显存顺序 | **先切状态，后释放显存** | `torch.cuda.empty_cache()` 可能耗时上百毫秒，同步执行会出现"用户已回来但节点仍可接单"的危险窗口 |
| 延迟度量 | 记录每次让退的实际延迟 | `/health` 与心跳里可查 `last_yield_latency_ms` / `sla_violations` |
| 启动期残留活动 | 启动后首次探测到的历史输入记为 0 延迟 | 避免"用户刚敲完命令就启动节点"被误判成一次 SLA 违约 |

**状态机**

| 状态 | 含义 | 是否可接单 |
|---|---|---|
| `IDLE` | 挂机中（用户不在电脑前） | ✅ |
| `BUSY` | 推理进行中 | ❌ |
| `YIELDED` | 已让退（用户在用电脑） | ❌（接口返回 503） |
| `PAUSED` | 已暂停（用户手动暂停） | ❌ |

---

## 系统托盘

| 菜单项 | 说明 |
|---|---|
| `状态：挂机中 / 已让退 / 已暂停` | 实时状态 |
| `显存占用：3.2 GB / 6.0 GB` | 已用 / 总量 |
| `显卡温度：52 °C` | GPU 温度 |
| `暂停节点` / `恢复节点` | 一键切换 |
| `退出` | 结束进程 |

图标由 Pillow 运行时绘制（不依赖二进制资源），颜色随状态变化：绿=挂机、蓝=推理、黄=已让退、红=已暂停。

无桌面环境（SSH / 容器）下用 `--no-tray` 启动即可。

---

## 遥测与隐私

节点与云端之间统一走 **WebSocket 主通道** `wss://cloud.apipaw.com/api/v1/node/stream`：
连接建立后先发一帧注册（`INIT`），随后每 **10 秒**发一帧 `HEARTBEAT`。
云端关闭连接时客户端**静默退避重连**（1s → 2s → 4s … 上限 60s），直到再次显示
`[ws_tunnel] ✅ WebSocket 已连接`。

HTTP 心跳 `POST https://cloud.apipaw.com/api/v1/nodes/heartbeat` 退化为**兜底通道**：
只有在 WS 通道未连上时才会发送。载荷结构：

```json
{
  "node_id": "node_default_001",
  "node_name": "APIPaw-Node",
  "hardware_type": "desktop",
  "wallet_address": "0x...",
  "status": "IDLE",
  "gpu_temp_c": 47.0,
  "vram_used_gb": 1.2,
  "vram_total_gb": 6.0,
  "vram_free_gb": 4.8,
  "yield_count": 3,
  "last_yield_latency_ms": 12.0,
  "agent_version": "1.0.3",
  "platform": "Windows",
  "timestamp": 1767225600
}
```

- **不上报**任何文件内容、剪贴板、键盘记录或屏幕内容
- 关闭方式：`agent.config.json` 里 `disable_telemetry: true`，或环境变量 `APIPAW_DISABLE_TELEMETRY=1`
- 命令行 `--no-telemetry` 只对本次进程生效，**不会**改写配置文件
- 网络异常自动退避（10s → 20s → 40s → 60s 封顶），永远不影响本地服务

---

## 故障排查

<details>
<summary><b>节点一直是「已让退」，无法接单</b></summary>

某些环境（尤其是**前台运行的控制台 TUI 程序**，会持续注入输入事件）会让
`GetLastInputInfo` 的空闲时间恒为 0，节点因此永远判定为"用户正在操作"。

本地联调时可用环境变量绕过：

```powershell
$env:APIPAW_FORCE_IDLE = "1"
python main.py
```

> ⚠️ 该开关仅用于双端联调，**生产环境不要设置**——它会跳过整个让退状态机。
</details>

<details>
<summary><b>端口 52415 被占用</b></summary>

```powershell
netstat -ano | findstr :52415
python main.py --port 52416
```
</details>

<details>
<summary><b>显存 / 温度指标显示「未知」</b></summary>

指标依赖 `nvidia-smi`。请确认：

```powershell
nvidia-smi
```

若安装在非标准目录，可通过环境变量指定：

```powershell
$env:NVIDIA_SMI_PATH = "D:\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
```
</details>

<details>
<summary><b>托盘图标不出现</b></summary>

- 确认已安装 `pystray` 与 `Pillow`
- SSH / 无桌面会话的会话中托盘不可用，请加 `--no-tray`
</details>

<details>
<summary><b>想跑真实推理而不是占位回复</b></summary>

本仓库默认分发的是**挂机助手客户端**，不携带模型权重，`/v1/chat/completions`
返回结构合法的占位内容以便链路联调。安装 torch 后可在 `main.py` 的
`_simulate_answer()` 处接入本地模型推理。
</details>

---

## 项目结构

```
apipaw-agent/
├── main.py             # FastAPI 入口：/health、/v1/models、/v1/chat/completions
├── agent_config.py     # agent.config.json 的读写与校验（线程安全、原子写入）
├── console.py          # 控制台编码兜底（GBK 下 emoji 不会打断让退线程）
├── yield_monitor.py    # Win32 让退监视器：GetLastInputInfo + 50ms SLA
├── tray_icon.py        # pystray 系统托盘：状态 / 显存 / 温度 / 暂停恢复
├── telemetry.py        # GPU 指标采集 + 10s 心跳上报（WS 在线时退居兜底）
├── ws_tunnel.py        # WebSocket 主通道：WSS 长连接 + 静默重连 + 心跳帧
├── PAWMODEL.md         # 社区模型元数据标准 + 15% 创作者版税绑定规范
├── requirements.txt    # Python 依赖
├── install.ps1         # Windows 一键安装
├── install.sh          # macOS / Linux 一键安装
├── install.bat         # Windows 一键安装（UTF-8 批处理）
├── start.bat           # Windows 前台启动脚本
├── start_silent.vbs    # Windows 静默后台启动脚本
├── .github/
│   ├── workflows/ci.yml        # CI：编译检查 / 依赖矩阵 / 健康检查 / 让退冒烟
│   └── scripts/                # CI 脚本（可本地直接运行）
├── agent.config.json   # 运行时生成
├── CONTRIBUTING.md     # 贡献与 PR 指引
├── CODE_OF_CONDUCT.md  # 社区行为准则
└── README.md
```

---

## 开发

```bash
# 语法自检
python -m compileall -q -x '(\.venv|\\build|\\dist)' .

# 让退机制冒烟测试（不依赖 torch / pystray）
python .github/scripts/yield_smoke_test.py

# 端到端健康检查（启动服务并校验 OpenAI 规范接口）
python .github/scripts/health_check.py

# 启动（无托盘、无心跳，纯本地）
python main.py --no-tray --no-telemetry
```

---

## 贡献

欢迎提交 Issue 与 PR。请在 PR 描述中说明：改动动机、影响范围、验证方式
（至少贴出 `curl /health` 的输出）。改动让退机制前请先阅读
[CONTRIBUTING.md](CONTRIBUTING.md) 中的「50ms SLA 红线」。
社区行为准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

---

## License

[MIT](LICENSE) © 2026 APIPaw

---

<a id="english-version"></a>
# English Version

## What is this

APIPaw Agent is a **local, always-on idle-yield client**:

- While your machine is **idle**, it serves inference over an OpenAI-compatible
  API at `127.0.0.1:52415`
- The moment it detects **keyboard/mouse activity**, the node switches to
  `YIELDED` and releases VRAM **within 50 ms** — it never competes with you
- It reports node telemetry (name, free VRAM, GPU temperature) to the cloud
  every 10 seconds so the scheduler knows which nodes are alive
- A system tray icon shows live status, VRAM and temperature, with one-click
  pause / resume

**Design principle**: your computer comes first. The node only works while you
are away.

## Features

| Feature | Details |
|---|---|
| 🚀 **50 ms yield** | `GetLastInputInfo` + `timeBeginPeriod(1)` (1 ms timer resolution), 5 ms polling; **state flips before memory release** so `empty_cache()` never breaches the SLA |
| 🖥️ **OpenAI-compatible API** | `/v1/chat/completions` (with `stream`), `/v1/models`, `/health` |
| 📊 **System tray** | Status / VRAM / temperature / pause-resume; icon colour follows state |
| 📡 **Cloud heartbeat** | Every 10 s by default, exponential back-off on failure, never blocks local service |
| 🔌 **WebSocket primary channel** | The node talks to the cloud over `wss://cloud.apipaw.com/api/v1/node/stream`; on a cloud-side close it **silently reconnects with back-off**, and the HTTP heartbeat is only a fallback |
| 🧩 **Zero heavy dependencies** | `torch` is **optional** — the agent starts, reports heartbeats and serves traffic without it |
| 🛠️ **One-line install** | `install.ps1` (Windows) / `install.sh` (macOS / Linux) |
| 🧭 **Cross-platform** | Native Win32 implementation on Windows; graceful degradation elsewhere |
| 📜 **Model metadata standard** | [`PAWMODEL.md`](PAWMODEL.md) — community model onboarding + **15 % creator royalty** wallet binding |

## Requirements

- **Python 3.10+** (developed against 3.12.10)
- Windows 10 / 11 (full yield capability) · macOS / Linux (runs, with degraded
  Windows-only features)
- NVIDIA GPU optional (metrics report `null` when absent; the agent still runs)

## Quick start

### One-liner (Windows / PowerShell)

```powershell
irm https://raw.githubusercontent.com/apipaw/apipaw-agent/main/install.ps1 | iex
```

### One-click scripts (Windows)

Three UTF-8 Windows scripts live in the repository root:

| Script | Purpose |
|---|---|
| `install.bat` | Create the `.venv` virtual environment and install `requirements.txt` |
| `start.bat` | Run the node in the foreground (visible console logs) |
| `start_silent.vbs` | Run the node hidden in the background (no console window) |

```bat
install.bat        :: first run: prepare the environment
start.bat          :: start the node
```

### One-liner (macOS / Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/apipaw/apipaw-agent/main/install.sh | bash
```

Then start the agent:

```powershell
.\.venv\Scripts\python.exe main.py     # Windows
./.venv/bin/python main.py              # macOS / Linux
```

### From source

```bash
git clone https://github.com/apipaw/apipaw-agent.git
cd apipaw-agent
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### Verify

```bash
curl http://127.0.0.1:52415/health
```

```json
{
  "status": "ok",
  "agent_version": "1.0.3",
  "node_id": "node_default_001",
  "node_name": "APIPaw-Node",
  "state": "IDLE",
  "state_label": "Idle",
  "available": true,
  "gpu": { "gpu_temp_c": 47.0, "vram_used_gb": 1.2, "vram_total_gb": 6.0, "vram_free_gb": 4.8 },
  "yield_metrics": { "yield_count": 3, "last_yield_latency_ms": 12.0, "sla_ms": 50, "sla_violations": 0 }
}
```

## Configuration: `agent.config.json`

Generated on first start; edit and restart (or call `AgentConfig.reload()`).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `host` | str | `127.0.0.1` | Bind address |
| `port` | int | `52415` | Bind port |
| `api_base_url` | str | `http://127.0.0.1:52415/v1` | Base URL for OpenAI clients |
| `model_name` | str | `apipaw-gpt2-small` | Model advertised by the node |
| `node_id` | str | `node_default_001` | Unique node identifier |
| `node_name` | str | `APIPaw-Node` | Display name (reported in heartbeats) |
| `hardware_type` | str | `desktop` | `desktop` / `laptop` / `server` |
| `wallet_address` | str | `""` | Wallet for revenue settlement |
| `node_secret` | str | `""` | Node key, sent as `Authorization: Bearer` |
| `device` | str | `cuda` | Inference device |
| `max_vram_gb` | float | `4.5` | VRAM ceiling (GB) |
| `idle_threshold_seconds` | float | `15.0` | Idle time before returning to `IDLE` |
| `vram_yield_ms` | int | `50` | **Yield SLA (ms)** — exceeding it counts as a violation |
| `poll_interval_ms` | int | `5` | Input polling interval (ms) |
| `cloud_heartbeat_url` | str | `https://cloud.apipaw.com/api/v1/nodes/heartbeat` | Heartbeat endpoint |
| `heartbeat_interval_seconds` | float | `10.0` | Heartbeat period |
| `disable_telemetry` | bool | `false` | Disable heartbeats |
| `enable_tray` | bool | `true` | Enable the system tray |

## Visual configuration panel (`/ui`)

On **Windows the browser opens automatically** after startup — no more editing JSON
by hand, and no process restart.

```
http://127.0.0.1:52415/ui
```

Enter the Node ID and Node Secret, then hit **Bind**. The panel atomically writes
`agent.config.json`, refreshes the in-memory config, and **actively drops the
currently rejected (403/401) connection** so the tunnel re-handshakes with the
new credentials — measured reconnect time: **0.1 s**.

| Route | Method | Description |
|---|---|---|
| `/ui` | GET | Single-file HTML panel (no external assets) |
| `/api/status` | GET | Binding status and cloud connectivity (**never returns the plaintext secret**) |
| `/api/bind` | POST | Bind Node ID / Secret and trigger a smooth reconnect |

> Set `APIPAW_NO_UI_BROWSER=1` to suppress the automatic browser launch (headless/server setups).

## API (OpenAI-compatible)

- **`GET /health`** — always `200`; reports state, GPU metrics and yield metrics
- **`GET /v1/models`** — standard OpenAI model list
- **`POST /v1/chat/completions`** — standard chat completion, SSE when `stream: true`

When the node has yielded (user is active) the completion endpoint returns
**`503`** with:

```json
{
  "error": {
    "message": "Node yielded: the user is active on this machine",
    "type": "node_unavailable",
    "code": "node_yielded"
  }
}
```

This is intentional: the scheduler moves the request to another node instead of
preempting the human at the keyboard.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:52415/v1", api_key="apipaw-local")
print(client.chat.completions.create(
    model="apipaw-gpt2-small",
    messages=[{"role": "user", "content": "hello"}],
).choices[0].message.content)
```

## The 50 ms yield mechanism

```
            user touches keyboard / mouse
                        │
                        ▼
     GetLastInputInfo ─┴─ idle time drops (<1000 ms)
                        │
                        ▼
     ┌────────────────────────────────────┐
     │ 1. flip state → YIELDED immediately │  ← tens of microseconds
     │ 2. notify tray / report heartbeat   │
     │ 3. release VRAM (separate thread)   │  ← empty_cache() off the SLA
     └────────────────────────────────────┘
                        │
                        ▼
       idle ≥ idle_threshold_seconds → back to IDLE
```

| Detail | Approach | Why |
|---|---|---|
| Timer resolution | `timeBeginPeriod(1)` | Windows defaults to 15.6 ms — a third of the budget on its own |
| Polling | 5 ms daemon thread | Balances CPU usage against detection latency |
| Ordering | **state first, memory later** | `torch.cuda.empty_cache()` can take hundreds of ms; doing it inline opens a window where the node still accepts work while the user is back |
| Measurement | Every yield latency is recorded | `last_yield_latency_ms` / `sla_violations` are visible in `/health` and heartbeats |

| State | Meaning | Accepts work |
|---|---|---|
| `IDLE` | Idle (user is away) | ✅ |
| `BUSY` | Inference in progress | ❌ |
| `YIELDED` | Yielded (user is active) | ❌ (HTTP 503) |
| `PAUSED` | Paused by the user | ❌ |

## System tray

Live status, VRAM usage, GPU temperature, pause/resume and quit. The icon is
drawn at runtime with Pillow (no binary assets) and recoloured by state:
green = idle, blue = busy, yellow = yielded, red = paused. Use `--no-tray` on
headless machines.

## Telemetry & privacy

The node talks to the cloud over the **WebSocket primary channel**
`wss://cloud.apipaw.com/api/v1/node/stream`: it sends an `INIT` frame on
connect and a `HEARTBEAT` frame every **10 seconds**. When the cloud closes
the connection the client **silently reconnects with back-off** (1 s → 2 s →
4 s … capped at 60 s) until `[ws_tunnel] ✅ WebSocket 已连接` shows up again.

The HTTP heartbeat `POST https://cloud.apipaw.com/api/v1/nodes/heartbeat` is
now only a **fallback**: it is sent while the WebSocket channel is down. The
payload carries node id/name, hardware type, wallet address, state, GPU
temperature, VRAM figures and yield metrics.

- **Never** uploads file contents, clipboard, keystrokes or screen content
- Disable with `disable_telemetry: true` or `APIPAW_DISABLE_TELEMETRY=1`
  (the `--no-telemetry` CLI flag applies to the current process only and never
  rewrites the config file)
- Network errors back off 10 s → 20 s → 40 s → 60 s and never affect the local
  service

## Troubleshooting

**Node stays `YIELDED` and never accepts work** — some environments (notably a
foreground console TUI) keep `GetLastInputInfo` pinned at 0. For local
two-sided debugging set `APIPAW_FORCE_IDLE=1`; **never** in production, it skips
the yield state machine entirely.

**Port 52415 busy** — `netstat -ano | findstr :52415`, then
`python main.py --port 52416`.

**VRAM / temperature unknown** — metrics need `nvidia-smi`; set
`NVIDIA_SMI_PATH` if it lives in a non-standard directory.

**No tray icon** — install `pystray` and `Pillow`, or start with `--no-tray`.

**Placeholder answers** — this distribution ships the client without model
weights; `/v1/chat/completions` returns schema-valid placeholder content for
link testing. Hook a real local model into `_simulate_answer()` in `main.py`.

## Project layout

```
apipaw-agent/
├── main.py             # FastAPI entry: /health, /v1/models, /v1/chat/completions
├── agent_config.py     # agent.config.json I/O (thread-safe, atomic writes)
├── console.py          # Console encoding fallback (GBK-safe emoji printing)
├── yield_monitor.py    # Win32 yield monitor: GetLastInputInfo + 50 ms SLA
├── tray_icon.py        # pystray tray: status / VRAM / temperature / pause
├── telemetry.py        # GPU metrics + 10 s heartbeat (fallback while WS is up)
├── ws_tunnel.py        # WebSocket primary channel: WSS + silent reconnect
├── PAWMODEL.md         # Model metadata standard + 15 % royalty binding
├── requirements.txt
├── install.ps1 / install.sh
├── install.bat / start.bat / start_silent.vbs   # Windows one-click scripts
├── .github/workflows/ci.yml
└── README.md
```

## Development

```bash
python -m compileall -q -x '(\.venv|\\build|\\dist)' .
python .github/scripts/yield_smoke_test.py   # yield state machine (no torch/pystray needed)
python .github/scripts/health_check.py       # end-to-end OpenAI API check
python main.py --no-tray --no-telemetry
```

## Contributing

Issues and PRs are welcome. Describe the motivation, the blast radius and how
you verified the change (at minimum, paste the `curl /health` output). Read the
**50 ms SLA red lines** in [CONTRIBUTING.md](CONTRIBUTING.md) before touching
the yield path. Community rules: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © 2026 APIPaw
