## 🚀 What's New | 本次重磅更新

**[EN]** The panel now **fills itself in from the link**: open `http://127.0.0.1:52415/ui?node_id=your-node&node_secret=your-secret` and the Node ID and Node Secret are already in the boxes — just click **"绑 定 节 点"**. That link is what the owner console hands out when it issues a credential in one click, so "get a credential → start the node" is now a copy-and-paste instead of a transcription job. The secret is wiped from the address bar as soon as it is read, so it does not linger in your browser history.

**[CN]** 面板现在**能从链接自动填好**：打开 `http://127.0.0.1:52415/ui?node_id=你的节点&node_secret=你的密钥`，Node ID 与 Node Secret 已经填在框里了，点「绑 定 节 点」即可。业主控制台一键签发凭证时给出的正是这条链接 —— 于是「拿到凭证 → 启动节点」从**手抄**变成了**复制粘贴**。密钥一读入即从地址栏抹掉，不会留在浏览器历史里。

**[EN]** A configuration file can no longer be silently overwritten by accident: the node's settings live in exactly one place (`agent.config.json`), and any tool that asks for a *different* settings file now gets its own private copy instead of quietly writing over your real one.

**[CN]** 配置文件不会再被**静默改写**：节点的配置只有一份（`agent.config.json`），任何工具想要**另一份**配置时，现在会拿到属于自己的独立副本，而不是悄悄把你真实的那份覆盖掉。

**[EN]** Your machine now **tells the cloud which models it can actually serve** — every heartbeat and every live connection carries the local model list plus the exact engine tag (an Ollama tag, or a vLLM / llama.cpp model name). The cloud merges that into the node registry **by itself**, so a freshly connected node shows up as *model-serving* right away. Until now you had to ask the platform to bind the models by hand for every single machine; that chore is gone. A node that reports an **empty** list is treated as "confirmed: no engine here" and is skipped for inference work, while a node that reports nothing at all keeps working as before.

**[CN]** 您的机器现在会**主动告诉云端自己到底能跑哪些模型**——每次心跳与每条长连接都会带上本地模型清单和引擎 tag（Ollama tag，或 vLLM / llama.cpp 的模型名）。云端据此**自动合并**进节点注册表，新接入的节点立刻就能被派发推理任务。此前每台新机器都要**手工**跑脚本绑模型，这件苦差事已经没有了。上报**空清单**的节点会被判定为「确认没有推理引擎」而不再参与派单；**完全不上报**的老节点行为不变，照常接单。

**[EN]** The local panel (**http://127.0.0.1:52415/ui**) gained two new controls, both saved to `agent.config.json` and **effective immediately** — no restart, no editing `start.bat`: a **「强制待命模式」(Force Idle)** checkbox that pins the node to *always available* regardless of mouse and keyboard activity (perfect for a dedicated mining box), and a **「本地推理模型」(Local Inference Model)** input where you type the engine tag your machine should use. If you still prefer the environment variable `APIPAW_FORCE_IDLE=1`, it keeps working exactly as before — the checkbox just makes it a one-click operation instead of a file-editing ritual.

**[CN]** 本地面板（**http://127.0.0.1:52415/ui**）新增两个开关，都写入 `agent.config.json` 且**立即生效**——不用重启、不用改 `start.bat`：**「强制待命模式」**勾选后无论键鼠是否有活动都把节点钉在「可接单」状态（适合专门挂机的机器）；**「本地推理模型」**输入框用来指定本机实际使用的引擎 tag。原先的环境变量 `APIPAW_FORCE_IDLE=1` 仍然完全可用——区别只是从「改脚本」变成「点一下」。

**[EN]** Binding a node no longer needs a server terminal: the owner console can now issue a **Node ID + Node Secret** for you in one click, and you paste those two values straight into the panel's bind box. Each machine gets its own credential, so revoking one machine does not disturb the others.

**[CN]** 绑定节点不必再开服务器终端：业主控制台现在可以**一键签发** `Node ID + Node Secret`，直接粘进面板的绑定框即可。每台机器一张独立凭证，吊销其中一台不会牵连其它机器。

**[EN]** The node can now talk to **OpenAI-compatible** inference servers — **vLLM** and **llama.cpp** are both supported, next to the Ollama it already knew. Point the node at your engine in `agent.config.json` (`backend_vllm_base_url` / `backend_llamacpp_base_url`, plus `backend_api_key` if your engine requires one). The auto-probe **only** tries an OpenAI-compatible engine when you write its address down explicitly — it will never mistake some unrelated HTTP service (a web app, or the APIPaw gateway itself, which also lives on port 8000) for an inference engine. Token counts are taken from the engine's own `usage` report, so what the cloud bills you matches what the engine really produced.

**[CN]** 节点现在支持对接 **OpenAI 兼容**推理服务——除原有的 Ollama 外，**vLLM** 与 **llama.cpp** 都能直接用。在 `agent.config.json` 里填好引擎地址（`backend_vllm_base_url` / `backend_llamacpp_base_url`，引擎要鉴权就再填 `backend_api_key`）即可。自动探测**只在您显式写了地址**时才会去试 OpenAI 兼容引擎——绝不会把某个不相干的 HTTP 服务（比如网页应用，或同样占用 8000 端口的 APIPaw 网关本身）误认成推理引擎。token 数取自引擎**自报**的 `usage`，云端计费与引擎真实产出对得上。

**[EN]** The node now measures its **own** speed and reports it: time-to-first-token and tokens-per-second travel with each answer, and the cloud uses those **real** numbers instead of guessing from network round-trips. Node owners can now see how fast their machine actually is, and the platform stops billing on a rough estimate.

**[CN]** 节点现在会**自测**并上报真实性能：首字延迟（TTFT）与吐字速度（tokens/s）随每次回答一并上报，云端据此采用**真实**性能口径，不再依赖网关观测到的网络往返时间去估算。节点业主能看到自己机器的真实速度，平台也不再拿粗略估算当依据。

**[EN]** If the inference engine **dies halfway through** an answer, the node now reports it as a **broken stream** instead of shipping half an answer as if it were complete — the cloud then **does not charge you** for that request. A failure that happens *before* the engine even starts is still reported separately as "engine unavailable", so the two stay distinguishable. The node never silently retries a failed request, and never sends a "done" marker for an answer that did not finish.

**[CN]** 推理引擎**答到一半挂掉**时，节点现在会明确上报「断流」，而不是把半截输出当成完整答案交上去——云端据此**不向您收费**。开跑前就失败仍单独报「引擎不可用」，两类故障**不混淆**。节点对失败的请求**不做静默重试**，没拿到完整答案时**绝不**回「完成」标记。

**[EN]** The node now answers with **exactly the model the cloud asked for**. If the cloud names a model this machine does not have, the node says so plainly and lists what it does have — it will never quietly answer with a different model and bill you for it. The node also reports its **model list** to the cloud, and the cloud only dispatches a task to a node that actually has that model loaded (older nodes that report nothing are still accepted, so existing nodes keep working). If this machine has exactly one model, that one may stand in.

**[CN]** 节点现在**严格按云端点名的模型**作答：云端点名而本机没有 ⇒ 明确报错并列出本机可用模型，**绝不**偷偷用别的模型顶替（否则您会为没点名的模型付费）；同时把本机**模型清单**上报云端，云端只把任务派给**确实装载了该模型**的节点（老版本节点未上报清单仍照常接单，存量节点不受影响）。本机只有一个模型时允许其顶替。

---

## 🛠️ Quick Start | 傻瓜式安装指南

**[EN] Windows Users:**
1. Download `{{ZIP_NAME}}` from the Assets below and extract it to a folder.
2. Double-click `install.bat` to automatically initialize the environment.
   - **In mainland China:** double-click `install_cn.bat` instead — it uses the Tsinghua TUNA pip mirror for much faster downloads.
   - **Overseas:** double-click `install.bat` — it uses the official PyPI index.
3. Double-click `start.bat` to run the agent. A Web UI will automatically pop up in your browser.
4. Enter your `Node ID` and `Node Secret` to bind the node. Done!

**[CN] Windows 用户：**
1. 下载下方 Assets 中的 `{{ZIP_NAME}}` 并将其解压到一个文件夹。
2. 双击运行安装脚本，等待环境自动初始化完成。
   - **如果您在中国大陆**，请双击运行 `install_cn.bat` 以开启国内网络加速（使用清华 TUNA 镜像源）；海外用户请双击 `install.bat`（使用官方 PyPI 源）。
3. 双击运行 `start.bat` 启动节点，浏览器将自动弹出 Web UI 绑定面板。
4. 填入系统分配给您的 `Node ID` 和 `Node Secret`，点击绑定即可亮起绿灯连接云端！

---

## 💻 Advanced Users | 进阶用户 (macOS / Linux)

**[EN]** You can still use the one-liner installation scripts.

**[CN]** 依然兼容历史版本的一键安装脚本，终端运行：

### Windows (PowerShell)
```powershell
irm https://raw.githubusercontent.com/apipaw/apipaw-agent/main/install.ps1 | iex
```

### macOS / Linux
```bash
curl -fsSL https://raw.githubusercontent.com/apipaw/apipaw-agent/main/install.sh | bash
```

---

## ✅ Verify | 验证安装

**[EN]** Check that the node is up and running:

**[CN]** 确认节点已启动并成功连接云端：

```bash
curl http://127.0.0.1:52415/health
```

---

## 🧠 推理引擎 | Real Inference Engine

**[EN]** The node probes a local **Ollama** server (`http://127.0.0.1:11434`) automatically. Configure it in `agent.config.json`:

```json
{
  "inference_backend": "auto",
  "backend_base_url": "http://127.0.0.1:11434",
  "backend_model": "",
  "backend_timeout_sec": 60
}
```

- `inference_backend`: `auto` (probe Ollama) / `ollama` / `vllm` / `llamacpp`
- `backend_base_url`: address of your inference engine
- `backend_model`: leave empty to use the model the cloud asks for
- `backend_timeout_sec`: engine timeout in seconds
- `backend_vllm_base_url` / `backend_llamacpp_base_url`: address of your **OpenAI-compatible** engine (vLLM / llama.cpp). Leave empty ⇒ **not** probed automatically, so an unrelated service on port 8000 can never be mistaken for an inference engine
- `backend_api_key`: optional API key for the OpenAI-compatible engine (`--api-key`)
- Status: `curl http://127.0.0.1:52415/health` → the `inference` field tells you which engine is live and which models it can serve

**[CN]** 节点会自动探测本机 **Ollama**（`http://127.0.0.1:11434`）。在 `agent.config.json` 里配置：

```json
{
  "inference_backend": "auto",
  "backend_base_url": "http://127.0.0.1:11434",
  "backend_model": "",
  "backend_timeout_sec": 60
}
```

- `inference_backend`：`auto`（自动探测）/ `ollama` / `vllm` / `llamacpp`
- `backend_base_url`：推理引擎地址
- `backend_model`：留空表示用云端派单指定的模型
- `backend_timeout_sec`：引擎超时秒数
- `backend_vllm_base_url` / `backend_llamacpp_base_url`：**OpenAI 兼容**引擎（vLLM / llama.cpp）地址；留空 ⇒ **不参与自动探测**，避免把 8000 端口上的无关服务误认成推理引擎
- `backend_api_key`：OpenAI 兼容引擎的可选 API Key（`--api-key`）
- 查看状态：`curl http://127.0.0.1:52415/health` 的 `inference` 字段会告诉你当前生效的引擎与可用模型

---

## 🆘 Troubleshooting | 常见问题

**[EN]**

| Symptom | Fix |
|---|---|
| `install.bat` flashes and closes | Python is missing, or `Add Python to PATH` was not ticked — reinstall Python 3.10+ with that option |
| `UnicodeDecodeError` during install | Your system locale is GBK — use `install_cn.bat` (it forces UTF-8 mode) or run `set PYTHONUTF8=1` first |
| The browser does not open | Open `http://127.0.0.1:52415/ui` manually |
| Panel keeps saying "not connected" | Check `Node ID` / `Node Secret` — they are case-sensitive and must not contain spaces |
| "节点不可用" (node unavailable) | The credential was revoked on the cloud side — issue a new one from the console |

**[CN]**

| 现象 | 处理办法 |
|---|---|
| 双击 `install.bat` 闪退 | 没装 Python，或安装时没勾 `Add Python to PATH`；重装 Python 3.10+ 并补勾 |
| 安装时报 `UnicodeDecodeError` | 系统默认 GBK 编码导致，请改用 `install_cn.bat`（已强制 UTF-8 模式），或先执行 `set PYTHONUTF8=1` |
| pip 下载很慢或超时 | 改用 `install_cn.bat`（清华 TUNA 镜像源） |
| 浏览器没有自动弹出面板 | 手动打开 `http://127.0.0.1:52415/ui` |
| 面板一直显示「未连接」 | 检查 `Node ID` / `Node Secret` 是否填错（区分大小写、不要有空格） |
| 提示「节点不可用」 | 凭证已被云端吊销，请到控制台重新签发 |

---

## 🔄 Run 24/7 | 开机自启（长期挂机）

**[EN]** Press `Win + R`, type `shell:startup` and press Enter, then drop a shortcut to `start_silent.vbs` into the folder that opens.

**[CN]** 按 `Win + R` → 输入 `shell:startup` 回车 → 把 `start_silent.vbs` 的快捷方式拖进打开的文件夹即可。

---

许可证：MIT License。模型元数据规范见 [`PAWMODEL.md`](https://github.com/apipaw/apipaw-agent/blob/v{{VERSION}}/PAWMODEL.md)。
