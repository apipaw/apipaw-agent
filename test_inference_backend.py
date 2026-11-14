"""阶段六 P0 · 模块 A：Agent 侧统一推理后端抽象层自检。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_inference_backend.py

覆盖用例：
  1. Ollama 底座：连通性探测 / 模型清单（``/api/tags``）
  2. Ollama 底座：真流式生成（``/api/chat`` NDJSON）→ 拿到**真实** usage
  3. ``auto`` 自动探测：探得到 ⇒ 选中 Ollama；探不到 ⇒ 无后端（None）
  4. vLLM / llama.cpp 预留位：明确报 ``backend_unavailable``（不回落模拟）
  5. 隧道派单：有后端 ⇒ 回真分片 + 真实 usage；无后端 ⇒ 回
     ``backend_unavailable``（**绝不**把占位文本当真答案）
  6. 心跳帧上报 ``models`` / ``inference_backend``（A2）
  7. 本地 ``/v1/chat/completions``：有后端 ⇒ 真推理；无后端 ⇒ 占位实现并自报

测试完全离线：本地起一个假 Ollama 服务，不连生产云端。
"""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["APIPAW_DISABLE_TELEMETRY"] = "1"
os.environ["APIPAW_FORCE_IDLE"] = "1"
os.environ["APIPAW_NO_UI_BROWSER"] = "1"

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

RESULTS = []


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


# ──────────────────────────────────────────────────────────────
# 假 Ollama 服务
# ──────────────────────────────────────────────────────────────
class _FakeOllamaHandler(BaseHTTPRequestHandler):
    """最小 Ollama 兼容实现：``/api/tags`` + ``/api/chat``（NDJSON 流）。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send_json({"models": [
                {"name": "qwen2.5:7b", "size": 1},
                {"name": "llama3:8b", "size": 2},
            ]})
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except ValueError:
            req = {}
        if not self.path.startswith("/api/chat"):
            self.send_error(404)
            return
        if not req.get("stream"):
            self._send_json({"message": {"content": "非流式"}, "done": True})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in ("你", "好", "！"):
            self._write_chunk(
                {"message": {"role": "assistant", "content": piece}, "done": False})
        self._write_chunk({
            "message": {"role": "assistant", "content": ""},
            "done": True, "prompt_eval_count": 11, "eval_count": 7,
        })
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _write_chunk(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        self.wfile.write(("%x\r\n" % len(data)).encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()


class _FakeOllama(object):
    """假 Ollama 服务句柄。"""

    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


# ──────────────────────────────────────────────────────────────
# 隧道替身
# ──────────────────────────────────────────────────────────────
class _FakeMonitor(object):
    state = type("S", (object,), {"name": "IDLE"})()
    state_label = "空闲"
    is_idle = True


def _make_tunnel(ws_tunnel, cfg, frames):
    tunnel = ws_tunnel.WebSocketTunnel.__new__(ws_tunnel.WebSocketTunnel)
    tunnel._cfg = cfg
    tunnel._total_completed_tokens = 0
    tunnel._node_secret = "node-secret-for-test-0123456789"
    tunnel._models_cache = []
    tunnel._models_cache_ts = 0.0
    tunnel._loop = None

    async def _send_json(data):
        frames.append(data)

    tunnel._send_json = _send_json
    return tunnel


def _free_port():
    """返回一个**没人监听**的本地端口（用来制造"本地推理服务不存在"）。"""
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def _drive(tunnel, msg):
    """在事件循环里驱动隧道处理一条派单（补齐 ``_loop`` 引用）。"""
    tunnel._loop = asyncio.get_event_loop()
    await tunnel._handle_inference_request(msg)


def _cfg(**kwargs):
    base = {
        "node_id": "node-p6-test",
        "node_name": "P6 测试节点",
        "hardware_type": "cuda",
        "device": "cuda",
        "max_vram_gb": 24.0,
        "model_name": "apipaw-gpt2-small",
        "backend_model": "qwen2.5:7b",
        "backend_base_url": "http://127.0.0.1:11434",
        "backend_timeout_sec": 10.0,
        "inference_backend": "auto",
        "port": 52415,
    }
    base.update(kwargs)
    return type("C", (object,), base)()


async def _run(coro):
    return await coro


def main():
    import inference_backend
    import telemetry
    import ws_tunnel

    telemetry.collect_gpu_metrics_cached = lambda: {
        "gpu_temp_c": 50.0, "vram_used_gb": 3.0,
        "vram_total_gb": 24.0, "vram_free_gb": 21.0,
    }
    ws_tunnel.collect_gpu_metrics_cached = telemetry.collect_gpu_metrics_cached

    fake = _FakeOllama()
    try:
        # ---- 1. Ollama 底座：连通性 + 模型清单 ----
        inference_backend.reset_backend_cache()
        backend = inference_backend.OllamaBackend(fake.base_url, 10.0, "qwen2.5:7b")
        _record("Ollama 底座可连通", backend.is_available() is True, fake.base_url)
        models = backend.list_models()
        _record("Ollama 模型清单正确",
                models == ["qwen2.5:7b", "llama3:8b"], str(models))

        # ---- 2. 真流式生成 + 真实 usage ----
        async def _gen():
            chunks = []
            async for chunk in backend.generate(
                    "qwen2.5:7b", [{"role": "user", "content": "你好"}]):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_gen())
        text = "".join(c.text for c in chunks)
        last = chunks[-1]
        _record("Ollama 真流式返回分片", text == "你好！", repr(text))
        _record("Ollama 回报真实 usage（prompt=11 completion=7）",
                last.usage is not None and last.usage.prompt_tokens == 11
                and last.usage.completion_tokens == 7 and last.done is True,
                str(last.usage.to_dict() if last.usage else None))

        # ---- 3. auto 自动探测 ----
        inference_backend.reset_backend_cache()
        picked = inference_backend.get_backend(
            {"inference_backend": "auto", "backend_base_url": fake.base_url,
             "backend_timeout_sec": 10.0})
        _record("auto 模式探到 Ollama", picked is not None and picked.name == "ollama",
                getattr(picked, "name", "None"))
        inference_backend.reset_backend_cache()
        none_backend = inference_backend.get_backend(
            {"inference_backend": "auto",
             "backend_base_url": "http://127.0.0.1:59999",
             "backend_timeout_sec": 2.0})
        _record("auto 模式探不到后端 ⇒ None（上层据此报 backend_unavailable）",
                none_backend is None, str(none_backend))

        # ---- 4. vLLM / llama.cpp 预留位 ----
        reserved = inference_backend.build_backend({"inference_backend": "vllm"})
        _record("vllm 预留位已就绪", reserved is not None and reserved.name == "vllm")

        async def _gen_reserved():
            try:
                async for _ in reserved.generate("m", []):
                    pass
            except inference_backend.BackendUnavailable as exc:
                return str(exc)
            except Exception as exc:  # noqa: BLE001
                return "unexpected:%s" % exc
            return "no-error"

        msg = asyncio.run(_gen_reserved())
        _record("vllm 预留位明确报 backend_unavailable（不回落模拟）",
                "backend_unavailable" in msg or "尚未实现" in msg, msg[:80])

        # ---- 5. 隧道派单 ----
        # 5a. 有后端 ⇒ 真分片 + 真实 usage
        inference_backend.reset_backend_cache()
        frames = []
        cfg = _cfg(inference_backend="auto", backend_base_url=fake.base_url,
                   backend_timeout_sec=10.0)
        tunnel = _make_tunnel(ws_tunnel, cfg, frames)
        asyncio.run(_drive(tunnel, {
            "request_id": "req-real-1",
            "payload": {"model": "qwen2.5:7b",
                        "messages": [{"role": "user", "content": "你好"}],
                        "stream": True},
        }))
        chunk_frames = [f for f in frames if f.get("type") == "RESPONSE_CHUNK"]
        done_frames = [f for f in frames if f.get("type") == "RESPONSE_DONE"]
        err_frames = [f for f in frames if f.get("type") == "RESPONSE_ERROR"]
        content = "".join((f["chunk"]["choices"][0].get("delta") or {}).get("content", "")
                          for f in chunk_frames)
        _record("有后端时隧道回真分片", content == "你好！", repr(content))
        _record("有后端时隧道无错误帧", not err_frames, str(err_frames[:1]))
        _record("有后端时 DONE 带真实 usage",
                bool(done_frames) and done_frames[0].get("usage", {}).get("prompt_tokens") == 11
                and done_frames[0]["usage"].get("completion_tokens") == 7,
                str(done_frames[0].get("usage") if done_frames else None))

        # 5b. 无后端 ⇒ backend_unavailable（绝不回占位文本）
        inference_backend.reset_backend_cache()
        frames = []
        # port 指向一个**没人监听**的端口 ⇒ 模拟"本地也没有推理服务"
        cfg = _cfg(inference_backend="auto", backend_base_url="http://127.0.0.1:59999",
                   backend_timeout_sec=2.0, port=_free_port())
        tunnel = _make_tunnel(ws_tunnel, cfg, frames)
        asyncio.run(_drive(tunnel, {
            "request_id": "req-fake-1",
            "payload": {"model": "qwen2.5:7b",
                        "messages": [{"role": "user", "content": "你好"}],
                        "stream": True},
        }))
        err_frames = [f for f in frames if f.get("type") == "RESPONSE_ERROR"]
        chunk_frames = [f for f in frames if f.get("type") == "RESPONSE_CHUNK"]
        _record("无后端时隧道回 backend_unavailable",
                bool(err_frames) and err_frames[0].get("error") == "backend_unavailable",
                str(err_frames[:1]))
        _record("无后端时隧道**不**回任何分片（占位文本没被当真答案）",
                not chunk_frames, "chunks=%d" % len(chunk_frames))
        _record("无后端时用 502（不是 503：那属于让退语义，不该扣节点信用分）",
                bool(err_frames) and int(err_frames[0].get("status") or 0) == 502,
                str(err_frames[0].get("status") if err_frames else None))

        # ---- 6. A2：心跳帧上报模型清单 ----
        inference_backend.reset_backend_cache()
        cfg = _cfg(inference_backend="auto", backend_base_url=fake.base_url,
                   backend_timeout_sec=10.0)
        tunnel = _make_tunnel(ws_tunnel, cfg, [])
        frame = tunnel._build_frame("INIT", _FakeMonitor())
        _record("心跳帧上报 models（本地可用模型清单）",
                frame.get("models") == ["qwen2.5:7b", "llama3:8b"], str(frame.get("models")))
        _record("心跳帧上报 inference_backend",
                frame.get("inference_backend") == "ollama", str(frame.get("inference_backend")))
        _record("心跳帧保留既有字段（协议版本 / app_ping）",
                frame.get("protocol_version") == 2 and frame.get("app_ping") == 1)

        # ---- 7. 本地 /v1/chat/completions ----
        # ⚠️ 本仓 venv 没装 httpx ⇒ 不用 TestClient，直接调用路由端点函数。
        import main as agent_main

        real_get_config = agent_main.get_config

        def _patch_cfg(**kwargs):
            fake_cfg = _cfg(**kwargs)
            agent_main.get_config = lambda: fake_cfg
            inference_backend.reset_backend_cache()
            return fake_cfg

        def _chat_endpoint():
            app = agent_main.create_app()
            for route in app.routes:
                if getattr(route, "path", "") == "/v1/chat/completions":
                    return route.endpoint
            raise RuntimeError("找不到 /v1/chat/completions 路由")

        def _make_req(stream=False, model="qwen2.5:7b"):
            return agent_main.ChatCompletionRequest(
                model=model,
                messages=[agent_main.ChatMessage(role="user", content="你好")],
                stream=stream)

        # 7a. 有后端 ⇒ 真推理（真实 usage）
        try:
            _patch_cfg(inference_backend="auto", backend_base_url=fake.base_url,
                       backend_timeout_sec=10.0, backend_model="qwen2.5:7b")
            endpoint = _chat_endpoint()
            resp = asyncio.run(endpoint(_make_req()))
            body = json.loads(resp.body.decode("utf-8"))
            mode = resp.headers.get("X-APIPaw-Inference-Mode")
            _record("本地接口有后端 ⇒ 走真推理",
                    resp.status_code == 200
                    and body["choices"][0]["message"]["content"] == "你好！"
                    and mode == "backend",
                    "%s / %s / %s" % (resp.status_code, mode,
                                      str(body.get("choices"))[:60]))
            _record("本地接口真实 usage 回传",
                    body.get("usage", {}).get("prompt_tokens") == 11
                    and body.get("usage", {}).get("completion_tokens") == 7,
                    str(body.get("usage")))

            # 7a-2. 流式：真流式（分片逐个到，不是先算完再切片）
            resp = asyncio.run(endpoint(_make_req(stream=True)))
            chunks = []

            async def _drain():
                async for piece in resp.body_iterator:
                    chunks.append(piece)

            asyncio.run(_drain())
            joined = "".join(chunks)
            contents = ""
            for piece in chunks:
                for line in piece.split("\n"):
                    if not line.startswith("data: ") or line.endswith("[DONE]"):
                        continue
                    try:
                        obj = json.loads(line[6:].strip())
                    except ValueError:
                        continue
                    choices = obj.get("choices") or []
                    if choices:
                        contents += (choices[0].get("delta") or {}).get("content") or ""
            _record("本地接口流式回真流式分片",
                    joined.count("data: ") >= 4 and contents == "你好！"
                    and resp.headers.get("X-APIPaw-Inference-Mode") == "backend",
                    "frames=%d content=%r" % (joined.count("data: "), contents))
        finally:
            agent_main.get_config = real_get_config
            inference_backend.reset_backend_cache()

        # 7b. 无后端 ⇒ 占位实现并自报 placeholder
        try:
            _patch_cfg(inference_backend="auto",
                       backend_base_url="http://127.0.0.1:59999",
                       backend_timeout_sec=2.0)
            endpoint = _chat_endpoint()
            resp = asyncio.run(endpoint(_make_req(model=None)))
            mode = resp.headers.get("X-APIPaw-Inference-Mode")
            _record("无后端 ⇒ 占位实现且自报 placeholder",
                    resp.status_code == 200 and mode == "placeholder",
                    "%s / %s" % (resp.status_code, mode))
        finally:
            agent_main.get_config = real_get_config
            inference_backend.reset_backend_cache()
    finally:
        fake.stop()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n===== 阶段六 P0 · 模块 A（Agent 侧）沙箱：%d/%d 通过 =====" % (passed, total))
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print("  FAIL: %s | %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
