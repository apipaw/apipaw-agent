## 🚀 What's New | 本次重磅更新

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
