## 🚀 What's New | 本次重磅更新

**[EN]** Added a sleek Local Web UI Console! Say goodbye to manual config file editing. The cloud gateway has also been upgraded to strict "One-Machine-One-Code" database authentication.

**[CN]** 新增本地可视化 Web 控制台！彻底告别手动修改配置文件的痛苦。云端鉴权全面升级，支持“一机一码”独立凭证校验。

---

## 🛠️ Quick Start | 傻瓜式安装指南

**[EN] Windows Users:**
1. Download `{{ZIP_NAME}}` from the Assets below and extract it to a folder.
2. Double-click `install.bat` to automatically initialize the environment.
3. Double-click `start.bat` to run the agent. A Web UI will automatically pop up in your browser.
4. Enter your `Node ID` and `Node Secret` to bind the node. Done!

**[CN] Windows 用户：**
1. 下载下方 Assets 中的 `{{ZIP_NAME}}` 并将其解压到一个文件夹。
2. 双击运行 `install.bat`，等待环境自动初始化完成。
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

## 🆘 Troubleshooting | 常见问题

**[EN]**

| Symptom | Fix |
|---|---|
| `install.bat` flashes and closes | Python is missing, or `Add Python to PATH` was not ticked — reinstall Python 3.10+ with that option |
| The browser does not open | Open `http://127.0.0.1:52415/ui` manually |
| Panel keeps saying "not connected" | Check `Node ID` / `Node Secret` — they are case-sensitive and must not contain spaces |
| "节点不可用" (node unavailable) | The credential was revoked on the cloud side — issue a new one from the console |

**[CN]**

| 现象 | 处理办法 |
|---|---|
| 双击 `install.bat` 闪退 | 没装 Python，或安装时没勾 `Add Python to PATH`；重装 Python 3.10+ 并补勾 |
| 浏览器没有自动弹出面板 | 手动打开 `http://127.0.0.1:52415/ui` |
| 面板一直显示「未连接」 | 检查 `Node ID` / `Node Secret` 是否填错（区分大小写、不要有空格） |
| 提示「节点不可用」 | 凭证已被云端吊销，请到控制台重新签发 |

---

## 🔄 Run 24/7 | 开机自启（长期挂机）

**[EN]** Press `Win + R`, type `shell:startup` and press Enter, then drop a shortcut to `start_silent.vbs` into the folder that opens.

**[CN]** 按 `Win + R` → 输入 `shell:startup` 回车 → 把 `start_silent.vbs` 的快捷方式拖进打开的文件夹即可。

---

许可证：MIT License。模型元数据规范见 [`PAWMODEL.md`](https://github.com/apipaw/apipaw-agent/blob/v{{VERSION}}/PAWMODEL.md)。
