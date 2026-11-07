"""
web_ui.py — 本地配置面板（单文件 HTML）

设计目标：让完全不懂 JSON 的小白用户也能在浏览器里完成节点绑定，
不需要手动编辑 ``agent.config.json``。

约束：
- **单文件、零外部依赖**：不引 CDN、不引框架、不引构建流 —— 离线可用、
  安装包体积不增加（面板直接内联进本地 HTTP 响应）。
- 只与本机 127.0.0.1 的 Agent 通信，绝不外呼。

页面交互：
1. 载入时 ``GET /api/status`` 预填 Node ID、显示当前密钥是否已配置；
2. 提交时 ``POST /api/bind`` 写入配置并触发隧道平滑重连；
3. 提交后轮询 ``/api/status`` 刷新"云端连接"指示灯。
"""

from __future__ import annotations

from agent_config import AGENT_VERSION

UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>APIPaw 节点绑定</title>
<style>
  :root {
    --bg: #05070f;
    --bg-soft: #0a0f1d;
    --card: rgba(13, 20, 36, 0.92);
    --line: rgba(90, 180, 255, 0.16);
    --cyan: #35e0ff;
    --cyan-dim: rgba(53, 224, 255, 0.55);
    --violet: #8b7bff;
    --text: #e6f1ff;
    --muted: #8fa3c0;
    --ok: #35f0a0;
    --err: #ff5470;
  }

  * { box-sizing: border-box; }

  html, body { height: 100%; }

  body {
    margin: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 32px 20px;
    background:
      radial-gradient(900px 600px at 15% -10%, rgba(53, 224, 255, 0.14), transparent 60%),
      radial-gradient(800px 520px at 100% 110%, rgba(139, 123, 255, 0.16), transparent 60%),
      var(--bg);
    color: var(--text);
    font-family: "Segoe UI", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, sans-serif;
    -webkit-font-smoothing: antialiased;
  }

  /* 网格底纹 */
  body::before {
    content: "";
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(53, 224, 255, 0.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(53, 224, 255, 0.045) 1px, transparent 1px);
    background-size: 44px 44px;
    mask-image: radial-gradient(circle at 50% 40%, #000 0%, transparent 78%);
    pointer-events: none;
  }

  .wrap {
    position: relative;
    width: 100%;
    max-width: 460px;
    animation: rise 0.5s ease both;
  }

  @keyframes rise {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: none; }
  }

  .panel {
    position: relative;
    padding: 34px 32px 28px;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: var(--card);
    backdrop-filter: blur(14px);
    box-shadow:
      0 0 0 1px rgba(53, 224, 255, 0.05),
      0 24px 70px rgba(0, 0, 0, 0.55),
      inset 0 1px 0 rgba(255, 255, 255, 0.05);
  }

  .panel::after {
    content: "";
    position: absolute;
    inset: -1px;
    border-radius: 18px;
    padding: 1px;
    background: linear-gradient(140deg, var(--cyan), transparent 40%, transparent 60%, var(--violet));
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor;
    mask-composite: exclude;
    pointer-events: none;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 4px;
  }

  .logo {
    width: 34px; height: 34px;
    border-radius: 9px;
    display: grid; place-items: center;
    background: linear-gradient(140deg, var(--cyan), var(--violet));
    color: #04121a;
    font-weight: 800;
    font-size: 15px;
    letter-spacing: -0.5px;
    box-shadow: 0 0 18px rgba(53, 224, 255, 0.45);
  }

  h1 {
    margin: 0;
    font-size: 20px;
    letter-spacing: 0.5px;
    font-weight: 650;
  }

  .sub {
    margin: 12px 0 26px;
    font-size: 13px;
    line-height: 1.7;
    color: var(--muted);
  }

  label {
    display: block;
    margin-bottom: 8px;
    font-size: 12px;
    letter-spacing: 1.4px;
    text-transform: uppercase;
    color: var(--cyan-dim);
  }

  .field { margin-bottom: 18px; }

  .row { display: flex; gap: 10px; align-items: center; }

  input[type="text"], input[type="password"] {
    flex: 1;
    width: 100%;
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid var(--line);
    background: rgba(4, 9, 20, 0.85);
    color: var(--text);
    font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
    font-size: 14px;
    letter-spacing: 0.4px;
    outline: none;
    transition: border-color .2s, box-shadow .2s;
  }

  input:focus {
    border-color: var(--cyan-dim);
    box-shadow: 0 0 0 3px rgba(53, 224, 255, 0.12);
  }

  input::placeholder { color: rgba(143, 163, 192, 0.45); }

  .ghost {
    flex: none;
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid var(--line);
    background: rgba(53, 224, 255, 0.06);
    color: var(--cyan-dim);
    font-size: 12px;
    cursor: pointer;
    transition: background .2s, color .2s;
  }

  .ghost:hover { background: rgba(53, 224, 255, 0.14); color: var(--cyan); }

  button.primary {
    width: 100%;
    margin-top: 6px;
    padding: 13px;
    border: none;
    border-radius: 10px;
    background: linear-gradient(120deg, var(--cyan), #56b8ff 55%, var(--violet));
    color: #04121a;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 1.5px;
    cursor: pointer;
    transition: transform .12s, box-shadow .2s, opacity .2s;
    box-shadow: 0 8px 26px rgba(53, 224, 255, 0.22);
  }

  button.primary:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 12px 32px rgba(53, 224, 255, 0.3); }
  button.primary:active:not(:disabled) { transform: translateY(0); }
  button.primary:disabled { opacity: .55; cursor: progress; }

  .status {
    margin-top: 20px;
    padding: 13px 15px;
    border-radius: 10px;
    border: 1px solid var(--line);
    background: rgba(4, 9, 20, 0.6);
    font-size: 13px;
    line-height: 1.6;
    color: var(--muted);
    min-height: 20px;
    transition: border-color .2s, color .2s, background .2s;
  }

  .status.ok   { border-color: rgba(53, 240, 160, .45); color: var(--ok);   background: rgba(53, 240, 160, .07); }
  .status.err  { border-color: rgba(255, 84, 112, .45); color: var(--err);  background: rgba(255, 84, 112, .07); }
  .status.wait { border-color: rgba(53, 224, 255, .4);  color: var(--cyan); background: rgba(53, 224, 255, .06); }

  .meta {
    margin-top: 20px;
    padding-top: 16px;
    border-top: 1px solid var(--line);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    color: var(--muted);
  }

  .dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    margin-right: 7px;
    background: #4a5a72;
    box-shadow: 0 0 8px currentColor;
    vertical-align: middle;
  }

  .dot.on  { background: var(--ok);  color: var(--ok);  animation: pulse 1.8s infinite; }
  .dot.off { background: var(--err); color: var(--err); }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: .45; }
  }

  .hint {
    margin-top: 10px;
    font-size: 11.5px;
    line-height: 1.65;
    color: rgba(143, 163, 192, 0.75);
  }

  code {
    padding: 1px 5px;
    border-radius: 4px;
    background: rgba(53, 224, 255, 0.1);
    color: var(--cyan-dim);
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 11.5px;
  }
</style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <div class="brand">
        <div class="logo">AP</div>
        <h1>APIPaw 节点绑定</h1>
      </div>
      <p class="sub">
        把你从 APIPaw 控制台拿到的 <b>Node ID</b> 与 <b>Node Secret</b> 填到下面，
        保存后会自动写入本地配置并立即重新连接云端，<b>无需重启</b>。
      </p>

      <div class="field">
        <label for="nodeId">Node ID</label>
        <input id="nodeId" type="text" placeholder="node_default_001" autocomplete="off" spellcheck="false" />
      </div>

      <div class="field">
        <label for="nodeSecret">Node Secret</label>
        <div class="row">
          <input id="nodeSecret" type="password" placeholder="apipaw_secret_..." autocomplete="off" spellcheck="false" />
          <button type="button" class="ghost" id="toggle">显示</button>
        </div>
      </div>

      <button class="primary" id="submit">绑 定 节 点</button>

      <div class="status" id="status">等待绑定…</div>

      <div class="meta">
        <span><span class="dot" id="dot"></span><span id="connText">云端连接：检测中</span></span>
        <span id="ver">Agent v__AGENT_VERSION__</span>
      </div>

      <div class="hint">
        提示：Node Secret 在控制台「节点管理」页获取，形如 <code>xxxx_xxxx_xxxx</code>。
        密钥仅保存在本机 <code>agent.config.json</code>，不会上传到除云端网关外的任何位置。
      </div>
    </div>
  </div>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var nodeIdEl = $("nodeId"), secretEl = $("nodeSecret"), statusEl = $("status");
  var submitEl = $("submit"), toggleEl = $("toggle"), dotEl = $("dot"), connText = $("connText");

  function say(text, kind) {
    statusEl.textContent = text;
    statusEl.className = "status" + (kind ? " " + kind : "");
  }

  function setConn(connected) {
    dotEl.className = "dot " + (connected ? "on" : "off");
    connText.textContent = connected ? "云端连接：已连接" : "云端连接：未连接";
  }

  function refresh() {
    return fetch("/api/status", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        setConn(!!s.ws_connected);
        if (!nodeIdEl.value && s.node_id) { nodeIdEl.value = s.node_id; }
        if (s.agent_version) { $("ver").textContent = "Agent v" + s.agent_version; }
        return s;
      })
      .catch(function () { setConn(false); });
  }

  toggleEl.addEventListener("click", function () {
    var hidden = secretEl.type === "password";
    secretEl.type = hidden ? "text" : "password";
    toggleEl.textContent = hidden ? "隐藏" : "显示";
  });

  submitEl.addEventListener("click", function () {
    var nodeId = (nodeIdEl.value || "").trim();
    var secret = (secretEl.value || "").trim();
    if (!nodeId) { say("请填写 Node ID", "err"); nodeIdEl.focus(); return; }
    if (!secret) { say("请填写 Node Secret", "err"); secretEl.focus(); return; }

    submitEl.disabled = true;
    say("正在保存配置并重新连接云端…", "wait");

    fetch("/api/bind", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: nodeId, node_secret: secret })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.body || !res.body.ok) {
          say((res.body && res.body.message) || "绑定失败，请检查填写内容", "err");
          return;
        }
        if (res.body.connected) {
          say("绑定成功，节点已连上云端 (" + res.body.node_id + ")", "ok");
          secretEl.value = "";
        } else {
          say("配置已保存，正在用新凭证重连云端…（稍候即可生效）", "wait");
        }
        setTimeout(refresh, 1200);
      })
      .catch(function (e) {
        say("请求失败：" + e.message, "err");
      })
      .finally(function () { submitEl.disabled = false; });
  });

  refresh();
  setInterval(refresh, 5000);
})();
</script>
</body>
</html>
"""

# 版本号由 agent_config 单点维护，避免面板显示与 /health 上报不一致
UI_HTML = UI_HTML.replace("__AGENT_VERSION__", AGENT_VERSION)

__all__ = ["UI_HTML"]
