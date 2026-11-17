"""Agent 侧真实 token 上报自检（阶段十六 P0 · **完全离线自包含**版）。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_usage_report.py

覆盖：
  1. ``token_counter``：CJK / 英文 / 混合文本的计数与 usage 结构
  2. 本地 ``/v1/chat/completions`` 非流式：响应带真实 usage
  3. 本地 ``/v1/chat/completions`` 流式：结束帧带真实 usage
     （云端据此覆盖预估、按真实 token 精确计费）

────────────────────────────────────────────────────────────────────
🔴 阶段十六 P0 · 为什么这个用例必须重写（历史"假红"根因）
────────────────────────────────────────────────────────────────────
阶段十五之前，本用例请求的模型名是 ``apipaw-gpt2-small`` —— 那是** transformers
时代的模型名**，如今任何机器的 Ollama 里都不可能有它。于是：

- 本机没装 Ollama ⇒ 后端不可用 ⇒ 503 ``backend_unavailable``；
- 本机装了 Ollama 但没有这个模型 ⇒ 同样 503（Ollama 的 404 会被
  ``OpenAICompatBackend`` 归类成 ``BackendUnavailable``）。

两种情况都让本用例变成**环境性失败**（假红）：它测的是"我机器上恰好有没有
那个模型"，而不是"Agent 的 usage 上报链路是否正确"。

现在改成**彻底离线、自包含、确定性**：

- 起一个**本地 Mock 推理服务**（OpenAI 兼容 SSE，标准库 ``http.server``），
  零外部依赖、零网络出网；
- 用**隔离配置**（临时 config 文件 + ``agent_config._config_instance``）
  把 Agent 指向 Mock ⇒ **真实 ``agent.config.json`` 一个字节都不动**；
- 遥测/WS 全部关闭 ⇒ 测试**绝不**连生产云端（老版本会真的去连
  ``wss://cloud.apipaw.com``，既污染生产又让用例结果依赖生产状态）。
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 必须在导入 main 之前设置：关闭遥测（不连生产云端）+ 强制 IDLE（避免让退检测拦截）
os.environ["APIPAW_DISABLE_TELEMETRY"] = "1"
os.environ["APIPAW_FORCE_IDLE"] = "1"
os.environ["APIPAW_NO_UI_BROWSER"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RESULTS = []

#: Mock 推理服务对外宣称的模型名（必须与隔离配置里的 ``backend_model`` 一致）
MOCK_MODEL = "mock-golden:latest"


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


# ─────────────────────────────────────────────────────────────────────
# 本地 Mock 推理服务（OpenAI 兼容 SSE）
# ─────────────────────────────────────────────────────────────────────
class _MockHandler(BaseHTTPRequestHandler):
    """只实现 Agent 用到的两个端点：``/v1/models`` 与 ``/v1/chat/completions``。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args, **kwargs):  # noqa: D401 - 静音默认 stderr 噪声
        """不打印访问日志（否则测试输出被刷屏）。"""

    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.startswith("/v1/models"):
            self._send(200, json.dumps({
                "object": "list",
                "data": [{"id": MOCK_MODEL, "object": "model", "owned_by": "mock"}],
            }))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        if not self.path.startswith("/v1/chat/completions"):
            self._send(404, json.dumps({"error": "not found"}))
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        messages = payload.get("messages") or []
        # 确定性 usage：prompt 按消息内容长度 + 每条消息的协议开销，
        # completion 固定 —— 两次请求（流式/非流式）必然得到**同一个** prompt_tokens。
        prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages)
        prompt_tokens += 4 * len(messages)
        completion_tokens = 8
        frames = [
            {"id": "mock-chat", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {"content": "本地 Mock "},
                          "finish_reason": None}]},
            {"id": "mock-chat", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {"content": "推理完成"},
                          "finish_reason": None}]},
            {"id": "mock-chat", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": prompt_tokens,
                       "completion_tokens": completion_tokens,
                       "total_tokens": prompt_tokens + completion_tokens}},
        ]
        body = "".join("data: %s\n\n" % json.dumps(f, ensure_ascii=False)
                       for f in frames) + "data: [DONE]\n\n"
        self._send(200, body, content_type="text/event-stream")


class MockInferenceServer(object):
    """本地 Mock 推理服务（后台线程）。"""

    def __init__(self):
        self._server = None
        self._thread = None

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="MockInference", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


# ─────────────────────────────────────────────────────────────────────
# 隔离配置（**不碰**真实 agent.config.json）
# ─────────────────────────────────────────────────────────────────────
def _build_isolated_config(config_path: Path, port: int, mock_url: str):
    """写一份**临时**配置并让 ``get_config()`` 返回它。

    ⚠️ 关键：``agent_config`` 有**两级缓存**（类属性 ``AgentConfig._instance``
    与模块级 ``_config_instance``）。``get_config()`` 读的是**模块级**那个，
    所以这里必须显式赋值 —— 否则 Agent 还是会去读真实配置文件。
    """
    import agent_config

    data = {
        "host": "127.0.0.1",
        "port": int(port),
        "api_base_url": "http://127.0.0.1:%d/v1" % int(port),
        "model_name": MOCK_MODEL,
        "agent_version": "1.0.14",
        "node_id": "unit_test_node",
        "node_name": "单元测试节点",
        "hardware_type": "desktop",
        "wallet_address": "",
        "node_secret": "unit_test_secret",
        "device": "cpu",
        "max_vram_gb": 1.0,
        "idle_threshold_seconds": 15.0,
        "vram_yield_ms": 50,
        "poll_interval_ms": 5,
        "force_idle": True,
        # ⚠️ 指向**本机废端口** ⇒ 万一有代码路径要连云端，也连不上生产
        "cloud_api_base": "http://127.0.0.1:1",
        "cloud_heartbeat_url": "http://127.0.0.1:1/api/v1/nodes/heartbeat",
        "cloud_ws_url": "ws://127.0.0.1:1/api/v1/node/stream",
        "heartbeat_interval_seconds": 10.0,
        "disable_telemetry": True,
        "enable_tray": False,
        "log_level": "WARNING",
        "tokenizer_path": "",
        "inference_backend": "llamacpp",
        "backend_base_url": mock_url,
        "backend_model": MOCK_MODEL,
        "backend_timeout_sec": 30.0,
        "backend_vllm_base_url": "",
        "backend_llamacpp_base_url": mock_url,
        "backend_api_key": "",
    }
    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    cfg = agent_config.AgentConfig(config_path=config_path)
    agent_config._config_instance = cfg  # 让 get_config() 返回隔离实例
    return cfg


def _start_server(port):
    import uvicorn

    import main as agent_main

    app = agent_main.create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        if getattr(srv, "started", False):
            return srv, thread
        time.sleep(0.1)
    raise RuntimeError("本地 Agent 服务启动超时")


def _check_counter():
    import token_counter

    _record("token_counter：CJK 文本约 1 字 1 token",
            token_counter.count_tokens("一二三四五") == 5,
            "count=%d" % token_counter.count_tokens("一二三四五"))
    _record("token_counter：英文文本明显少于字符数",
            0 < token_counter.count_tokens("hello world, how are you?") < 20,
            "count=%d" % token_counter.count_tokens("hello world, how are you?"))
    _record("token_counter：空文本计 0",
            token_counter.count_tokens("") == 0)
    usage = token_counter.build_usage(7, 11)
    _record("token_counter：usage 结构符合 OpenAI 规范",
            usage == {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
            json.dumps(usage))
    _record("token_counter：prompt 计数含消息协议开销",
            token_counter.count_prompt_tokens([{"role": "user", "content": "你好"}])
            > token_counter.count_tokens("你好"),
            "backend=%s" % token_counter.backend_name())


def _check_api(port):
    url = "http://127.0.0.1:%d/v1/chat/completions" % port
    prompt = "请用一句话解释什么是去中心化算力网络。"

    # ---- 非流式 ----
    body = json.loads(_post_json(url, {
        "model": MOCK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }))
    usage = body.get("usage") or {}
    _record("非流式响应带 usage",
            bool(usage), json.dumps(usage))
    _record("非流式 usage 的 prompt_tokens > 0",
            int(usage.get("prompt_tokens") or 0) > 0,
            str(usage.get("prompt_tokens")))
    _record("非流式 usage 的 completion_tokens > 0",
            int(usage.get("completion_tokens") or 0) > 0,
            str(usage.get("completion_tokens")))
    _record("非流式 usage 的 total_tokens 等于两项之和",
            int(usage.get("total_tokens") or 0)
            == int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0),
            json.dumps(usage))

    # ---- 流式 ----
    req = urllib.request.Request(
        url,
        data=json.dumps({
            "model": MOCK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    frames = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            frames.append(json.loads(payload))
        except ValueError:
            continue
    _record("流式响应有多个分片", len(frames) >= 2, "frames=%d" % len(frames))
    stream_usage = {}
    for frame in frames:
        if isinstance(frame.get("usage"), dict):
            stream_usage = frame["usage"]
    _record("流式结束帧带真实 usage", bool(stream_usage),
            json.dumps(stream_usage))
    _record("流式 usage 的 total_tokens 等于两项之和",
            int(stream_usage.get("total_tokens") or 0)
            == int(stream_usage.get("prompt_tokens") or 0) + int(stream_usage.get("completion_tokens") or 0),
            json.dumps(stream_usage))
    _record("流式与非流式的 prompt_tokens 一致（同一提示词）",
            int(stream_usage.get("prompt_tokens") or 0) == int(usage.get("prompt_tokens") or 0),
            "stream=%s non-stream=%s" % (stream_usage.get("prompt_tokens"),
                                         usage.get("prompt_tokens")))


def main():
    import agent_config

    _check_counter()

    real_config = Path(agent_config.__file__).parent / "agent.config.json"
    before = real_config.read_bytes() if real_config.exists() else b""

    mock = MockInferenceServer().start()
    port = _free_port()
    print("[test] 本地 Mock 推理服务: %s" % mock.base_url)
    print("[test] 本地 Agent 端口: %d" % port)

    tmp_dir = tempfile.mkdtemp(prefix="apipaw_usage_report_")
    tmp_cfg = Path(tmp_dir) / "agent.config.json"
    try:
        _build_isolated_config(tmp_cfg, port, mock.base_url)
        try:
            srv, thread = _start_server(port)
        except Exception as exc:
            print("[test] 本地服务启动失败: %s" % exc)
            return 1
        try:
            _check_api(port)
        finally:
            srv.should_exit = True
            thread.join(timeout=10)
    finally:
        mock.stop()

    after = real_config.read_bytes() if real_config.exists() else b""
    _record("真实 agent.config.json 未被污染（字节级一致）", before == after,
            "before=%d after=%d" % (len(before), len(after)))

    failed = [r for r in RESULTS if not r[1]]
    print("\n[test] 用例 %d 个，通过 %d 个，失败 %d 个"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
